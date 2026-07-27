"""State-root and cache-location policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from exohunt.paths import (
    default_cache_dir,
    default_db_path,
    path_is_within,
    resolve_cache_dir,
    state_root,
)


def test_env_overrides_take_precedence(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXOHUNT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("EXOHUNT_CACHE_DIR", raising=False)
    monkeypatch.delenv("EXOHUNT_DB_PATH", raising=False)
    assert state_root() == tmp_path / "state"
    assert default_cache_dir() == tmp_path / "state" / "cache" / "lightkurve"
    assert default_db_path() == tmp_path / "state" / "exohunt.db"

    monkeypatch.setenv("EXOHUNT_CACHE_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("EXOHUNT_DB_PATH", str(tmp_path / "db" / "l.db"))
    assert default_cache_dir() == tmp_path / "elsewhere"
    assert default_db_path() == tmp_path / "db" / "l.db"


def test_default_state_root_is_outside_a_synced_workspace(monkeypatch) -> None:
    monkeypatch.delenv("EXOHUNT_STATE_DIR", raising=False)
    root = state_root()
    # The policy this encodes: mutable state never defaults into the project
    # tree, where OneDrive locks files mid-write.
    assert not path_is_within(root, Path.cwd())


def test_resolve_cache_dir_defaults_to_state_root(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EXOHUNT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("EXOHUNT_CACHE_DIR", raising=False)
    resolved = resolve_cache_dir(None, workspace_root=tmp_path / "ws")
    assert resolved == (tmp_path / "state" / "cache" / "lightkurve").resolve()


def test_resolve_cache_dir_accepts_paths_outside_the_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "fast-disk" / "cache"
    assert resolve_cache_dir(outside, workspace_root=workspace) == outside.resolve()


def test_resolve_cache_dir_keeps_data_child_rule_inside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    (workspace / "data").mkdir(parents=True)
    resolved = resolve_cache_dir("data/lightkurve", workspace_root=workspace)
    assert resolved == (workspace / "data" / "lightkurve").resolve()
    with pytest.raises(ValueError):
        resolve_cache_dir(workspace / "data", workspace_root=workspace)
    with pytest.raises(ValueError):
        resolve_cache_dir("results/cache", workspace_root=workspace)
