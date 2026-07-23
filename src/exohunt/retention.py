"""Bounded storage retention for reproducible survey artifacts.

The permanent survey record is the metrics ledger plus compact JSON/CSV
diagnostics. Downloaded FITS products can be fetched again from MAST, and plots
for automatically rejected targets can be regenerated from the source data.
"""

from __future__ import annotations

import json
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
) -> dict[str, object]:
    """Delete the oldest FITS cache files until the cache fits under max_bytes."""

    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
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
        }

    files: list[tuple[float, str, Path, int]] = []
    for candidate in root.rglob("*"):
        if not candidate.is_file() or candidate.suffix.casefold() not in {".fits", ".fit"}:
            continue
        resolved = candidate.resolve()
        if not _within(resolved, root):
            continue
        stat = resolved.stat()
        files.append((stat.st_mtime, str(resolved).casefold(), resolved, stat.st_size))

    bytes_before = sum(item[3] for item in files)
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

        # Remove only directories proven empty, deepest first.
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
    }


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
    selected: dict[Path, int] = {}
    for row in rows:
        if row.get("status") != "rejected" or not row.get("plot"):
            continue
        raw = Path(str(row["plot"]))
        candidate = (raw if raw.is_absolute() else workspace / raw).resolve()
        if candidate.suffix.casefold() != ".png" or not _within(candidate, root):
            continue
        if candidate.exists() and candidate.is_file():
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
