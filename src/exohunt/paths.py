"""Where mutable state lives: outside the OneDrive-synced project tree.

The project directory is synced by OneDrive, which locks files mid-write and
has repeatedly broken high-churn writers (pytest temp trees, the 9.4 GB
light-curve cache). Policy, per MASTER_PLAN.md section 7.6:

* durable, write-once evidence (``results/``) stays in the project tree, where
  OneDrive is a backup rather than a hazard;
* everything high-churn or lock-sensitive -- the FITS download cache, the
  control-plane database, coordinator lock files -- lives under a local,
  unsynced state root, ``%LOCALAPPDATA%\\exohunt`` on Windows.

Environment overrides (``EXOHUNT_STATE_DIR``, ``EXOHUNT_CACHE_DIR``,
``EXOHUNT_DB_PATH``) take precedence so tests and unusual setups can redirect
each location without editing code.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def state_root() -> Path:
    """Return the unsynced local root for caches, locks, and the ledger."""

    override = os.environ.get("EXOHUNT_STATE_DIR")
    if override:
        return Path(override).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "exohunt"
    home = Path.home()
    if str(home) not in {"", "/"}:
        return home / ".local" / "state" / "exohunt"
    return Path(tempfile.gettempdir()) / "exohunt-state"


def default_cache_dir() -> Path:
    """Return the rolling FITS-cache directory (re-downloadable data only)."""

    override = os.environ.get("EXOHUNT_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    return state_root() / "cache" / "lightkurve"


def default_db_path() -> Path:
    """Return the SQLite control-plane database path."""

    override = os.environ.get("EXOHUNT_DB_PATH")
    if override:
        return Path(override).expanduser()
    return state_root() / "exohunt.db"


def lock_dir() -> Path:
    """Return the directory for coordinator lock files (non-Windows fallback)."""

    return state_root() / "locks"


def workspace_cache_dir(
    cache_dir: str | Path,
    *,
    workspace_root: str | Path = ".",
) -> Path:
    """Validate a cache placed inside the workspace: a child of ``data/`` only.

    This is the historical containment rule, kept so retention pruning can
    never be pointed at project evidence by a mistyped path.
    """

    workspace = Path(workspace_root).resolve()
    data_root = (workspace / "data").resolve()
    raw = Path(cache_dir)
    resolved = (raw if raw.is_absolute() else workspace / raw).resolve()
    try:
        relative = resolved.relative_to(data_root)
    except ValueError as exc:
        raise ValueError(
            f"Cache directory must be inside the project data directory: {data_root}"
        ) from exc
    if not relative.parts:
        raise ValueError(
            "Cache directory must be a dedicated child of the project data directory."
        )
    return resolved


def resolve_cache_dir(
    setting: str | Path | None,
    *,
    workspace_root: str | Path = ".",
) -> Path:
    """Resolve the FITS cache location, allowing homes outside the workspace.

    A cache placed *inside* the workspace must still be a dedicated child of
    ``<workspace>/data`` (the historical rule, enforced so retention can never
    prune project evidence). A cache outside the workspace is now the default
    and is accepted as-is: it is bounded by its own ceiling, not the workspace
    ceiling, and it keeps OneDrive out of the download path.
    """

    workspace = Path(workspace_root).resolve()
    if setting in (None, ""):
        return default_cache_dir().resolve()
    raw = Path(setting).expanduser()
    resolved = (raw if raw.is_absolute() else workspace / raw).resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError:
        return resolved
    return workspace_cache_dir(resolved, workspace_root=workspace)


def path_is_within(path: str | Path, root: str | Path) -> bool:
    """Return whether ``path`` is ``root`` or one of its descendants."""

    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return False
    return True
