"""Holding the machine awake for unattended runs."""

from __future__ import annotations

import sys

import pytest

from exohunt.keepawake import (
    ES_CONTINUOUS,
    ES_DISPLAY_REQUIRED,
    ES_SYSTEM_REQUIRED,
    KeepAwake,
)

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="Win32 execution-state API"
)


@windows_only
def test_start_holds_and_stop_releases() -> None:
    guard = KeepAwake()
    assert guard.active is False
    assert guard.start() is True
    try:
        assert guard.active is True
        assert "awake" in guard.reason
    finally:
        guard.stop()
    assert guard.active is False
    assert guard.reason == "released"


@windows_only
def test_context_manager_releases_even_on_error() -> None:
    guard = KeepAwake()
    with pytest.raises(RuntimeError):
        with guard:
            assert guard.active is True
            raise RuntimeError("boom")
    assert guard.active is False


@windows_only
def test_starting_twice_is_idempotent() -> None:
    guard = KeepAwake()
    assert guard.start() is True
    assert guard.start() is True
    guard.stop()
    assert guard.active is False


@windows_only
def test_display_flag_is_reflected_in_the_reported_reason() -> None:
    # The caller prints this, so it must not claim the screen is held on
    # when only the system is.
    system_only = KeepAwake()
    system_only.start()
    try:
        assert "display may still sleep" in system_only.reason
    finally:
        system_only.stop()

    with KeepAwake(keep_display_on=True) as both:
        assert "display" in both.reason
        assert "may still sleep" not in both.reason


def test_stop_without_start_is_safe() -> None:
    guard = KeepAwake()
    guard.stop()
    assert guard.active is False


def test_unsupported_platform_reports_failure_rather_than_pretending(
    monkeypatch,
) -> None:
    # An unsupported platform must return False so the caller can say the
    # machine may still sleep, instead of promising something untrue.
    monkeypatch.setattr(
        "exohunt.keepawake.sys", type("S", (), {"platform": "linux"})
    )
    guard = KeepAwake()
    assert guard.supported is False
    assert guard.start() is False
    assert guard.active is False
    assert "may sleep" in guard.reason


def test_flags_match_the_win32_constants() -> None:
    # Wrong values here would silently fail to hold the machine awake.
    assert ES_CONTINUOUS == 0x80000000
    assert ES_SYSTEM_REQUIRED == 0x00000001
    assert ES_DISPLAY_REQUIRED == 0x00000002
