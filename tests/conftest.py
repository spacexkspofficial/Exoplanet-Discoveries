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
