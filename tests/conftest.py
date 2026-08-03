"""Test-runner hygiene for synced Windows workspaces."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Keep temporary trees outside OneDrive and locked pytest roots."""

    if config.option.basetemp is None:
        config.option.basetemp = Path(tempfile.gettempdir()) / (
            f"exohunt-pytest-{os.getpid()}"
        )


@pytest.fixture(autouse=True)
def isolate_control_plane(tmp_path_factory, monkeypatch):
    """No test may touch the developer's real ledger.

    Campaign code publishes a coordinator lease heartbeat, so running the
    suite against the default state root wrote live-looking lease rows into
    the operator's own database -- which the dashboard then reported as a
    running campaign. Redirecting the state root per test keeps the control
    plane hermetic. Tests that need a specific database still override these
    variables themselves; a test body's ``monkeypatch.setenv`` runs after
    this fixture and wins.
    """

    root = tmp_path_factory.mktemp("exohunt-state")
    monkeypatch.setenv("EXOHUNT_STATE_DIR", str(root))
    monkeypatch.setenv("EXOHUNT_DB_PATH", str(root / "exohunt.db"))
