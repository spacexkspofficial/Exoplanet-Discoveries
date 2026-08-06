"""Bounded storage retention for reproducible survey artifacts.

The permanent survey record is the metrics ledger plus compact JSON/CSV
diagnostics. Downloaded FITS products can be fetched again from MAST, and plots
for automatically rejected targets can be regenerated from the source data.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterable


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validated_root(path: str | Path, *, label: str) -> Path:
    root = Path(path).resolve()
    anchor = Path(root.anchor).resolve()
    forbidden = {anchor, Path.home().resolve(), Path.cwd().resolve()}
    if root in forbidden:
        raise ValueError(f"Refusing to prune unsafe {label} root: {root}")
    return root


def prune_fits_cache(
    cache_dir: str | Path,
    *,
    max_bytes: int,
    dry_run: bool = False,
    min_age_seconds: float = 0.0,
) -> dict[str, object]:
    """Delete the oldest re-downloadable astronomy files under ``max_bytes``.

    TESScut retains the downloaded ZIP as well as the extracted FITS product,
    so both formats must count toward the same rolling cache ceiling.

    ``min_age_seconds`` protects files modified more recently than that from
    deletion. It exists so a campaign can prune *while downloads are still in
    flight* instead of draining its pipeline first: an in-flight download is
    writing a file whose mtime is seconds old, and deleting it underneath the
    writer would fail the target. Protected bytes still count toward
    ``bytes_before``, so the caller can see when the ceiling could not be met
    because too much of the cache was too new.
    """

    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    if min_age_seconds < 0:
        raise ValueError("min_age_seconds must be non-negative")
    root = _validated_root(cache_dir, label="cache")
    if not root.exists():
        return {
            "root": str(root),
            "dry_run": dry_run,
            "max_bytes": max_bytes,
            "bytes_before": 0,
            "bytes_after": 0,
            "files_considered": 0,
            "files_deleted": 0,
            "bytes_deleted": 0,
            "files_protected": 0,
            "bytes_protected": 0,
        }

    protect_after = time.time() - min_age_seconds if min_age_seconds > 0 else None
    files: list[tuple[float, str, Path, int]] = []
    protected_files = 0
    protected_bytes = 0
    for candidate in root.rglob("*"):
        if not candidate.is_file() or candidate.suffix.casefold() not in {
            ".fits",
            ".fit",
            ".zip",
        }:
            continue
        resolved = candidate.resolve()
        if not _within(resolved, root):
            continue
        stat = resolved.stat()
        if protect_after is not None and stat.st_mtime > protect_after:
            protected_files += 1
            protected_bytes += stat.st_size
            continue
        files.append((stat.st_mtime, str(resolved).casefold(), resolved, stat.st_size))

    bytes_before = sum(item[3] for item in files) + protected_bytes
    bytes_to_remove = max(0, bytes_before - max_bytes)
    selected: list[tuple[Path, int]] = []
    selected_bytes = 0
    for _, _, candidate, size in sorted(files):
        if selected_bytes >= bytes_to_remove:
            break
        selected.append((candidate, size))
        selected_bytes += size

    deleted_files = 0
    deleted_bytes = 0
    if not dry_run:
        for candidate, size in selected:
            try:
                candidate.unlink()
            except FileNotFoundError:
                continue
            deleted_files += 1
            deleted_bytes += size

        # Remove only directories proven empty, deepest first. When the cache
        # is already below budget ``selected`` is empty; a second recursive
        # walk then cannot remove anything and cost several minutes on the
        # production 90 GB cache.
        if selected:
            directories = sorted(
                (item for item in root.rglob("*") if item.is_dir()),
                key=lambda item: len(item.parts),
                reverse=True,
            )
            for directory in directories:
                try:
                    directory.rmdir()
                except OSError:
                    pass
    else:
        deleted_files = len(selected)
        deleted_bytes = selected_bytes

    return {
        "root": str(root),
        "dry_run": dry_run,
        "max_bytes": max_bytes,
        "bytes_before": bytes_before,
        "bytes_after": max(0, bytes_before - deleted_bytes),
        "files_considered": len(files),
        "files_deleted": deleted_files,
        "bytes_deleted": deleted_bytes,
        "files_protected": protected_files,
        "bytes_protected": protected_bytes,
        "extensions": [".fit", ".fits", ".zip"],
    }


def directory_size_bytes(root: str | Path) -> int:
    """Return the size of regular files below a project directory.

    This helper is read-only, so measuring the current working directory is
    both expected and safe.  Destructive retention functions still use
    ``_validated_root`` and refuse broad workspace roots.
    """

    resolved = Path(root).resolve()
    if not resolved.exists():
        return 0
    total = 0
    for directory, _, filenames in os.walk(resolved):
        base = Path(directory)
        for name in filenames:
            candidate = base / name
            try:
                if candidate.is_symlink():
                    continue
                total += candidate.stat().st_size
            except OSError:
                continue
    return total


def prune_rejected_plots(
    rows: Iterable[dict[str, object]],
    *,
    results_root: str | Path,
    workspace_root: str | Path = ".",
    dry_run: bool = False,
) -> dict[str, object]:
    """Delete only PNGs explicitly referenced by rejected campaign rows."""

    root = _validated_root(results_root, label="results")
    workspace = Path(workspace_root).resolve()
    # Inventory the actual files beneath the already-validated root once.
    # Resolving and stat'ing the path named by every one of 64,000 rows took
    # more than ten minutes even when rolling retention had already removed
    # every rejected plot. A row can select only a path present in this
    # inventory, which also preserves the containment guarantee without a
    # filesystem lookup per row.
    existing: dict[str, Path] = {}
    if root.exists():
        for directory, _, filenames in os.walk(root):
            for name in filenames:
                if not name.casefold().endswith(".png"):
                    continue
                candidate = Path(directory) / name
                existing[os.path.normcase(os.path.abspath(candidate))] = candidate

    selected: dict[Path, int] = {}
    for row in rows:
        if row.get("status") != "rejected" or not row.get("plot"):
            continue
        raw = Path(str(row["plot"]))
        requested = raw if raw.is_absolute() else workspace / raw
        candidate = existing.get(os.path.normcase(os.path.abspath(requested)))
        if candidate is not None:
            selected[candidate] = candidate.stat().st_size

    deleted_files = 0
    deleted_bytes = 0
    for candidate, size in sorted(selected.items(), key=lambda item: str(item[0]).casefold()):
        if not dry_run:
            try:
                candidate.unlink()
            except FileNotFoundError:
                continue
        deleted_files += 1
        deleted_bytes += size

    return {
        "root": str(root),
        "dry_run": dry_run,
        "files_deleted": deleted_files,
        "bytes_deleted": deleted_bytes,
        "deleted_paths": [str(path) for path in sorted(selected, key=lambda item: str(item).casefold())],
    }


def prune_historical_rejected_plots(
    results_root: str | Path,
    *,
    workspace_root: str | Path = ".",
    dry_run: bool = False,
) -> dict[str, object]:
    """Apply rejected-plot retention to every readable batch summary."""

    root = _validated_root(results_root, label="results")
    rows: list[dict[str, object]] = []
    summaries_read = 0
    if root.exists():
        for summary_path in root.rglob("batch_summary.json"):
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            summaries_read += 1
            rows.extend(
                row for row in summary.get("results", []) if isinstance(row, dict)
            )
    report = prune_rejected_plots(
        rows,
        results_root=root,
        workspace_root=workspace_root,
        dry_run=dry_run,
    )
    report["summaries_read"] = summaries_read
    return report
