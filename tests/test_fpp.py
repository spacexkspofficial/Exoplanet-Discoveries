"""The FPP bridge must fail as absence, never as a fabricated probability.

These tests deliberately do not require ``.venv-triceratops`` to exist. The
contract being pinned is that every failure path returns ``not_run`` with a
reason -- because :func:`exohunt.packet._is_measured` treats ``not_run`` as
absence, and absence must keep a packet blocked. Correction 57 is the shape
this guards against: a check that cannot run reporting as *not blocking*
rather than as failing.
"""

from __future__ import annotations

import json

from exohunt import fpp, packet as pk


def _blocking(section: object) -> bool:
    """True when the packet contract treats the section as absent."""

    return not pk._is_measured(section)


def test_a_missing_interpreter_is_absence_not_a_probability(monkeypatch) -> None:
    monkeypatch.setattr(fpp, "isolated_interpreter", lambda: None)
    result = fpp.probe()
    assert result["state"] == "not_run"
    assert "venv-triceratops" in result["reason"]
    assert "fpp" not in result
    assert _blocking(result)


def test_a_timeout_is_absence(monkeypatch, tmp_path) -> None:
    import subprocess

    fake = tmp_path / "python.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(fpp, "isolated_interpreter", lambda: fake)

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="triceratops", timeout=1)

    monkeypatch.setattr(subprocess, "run", _timeout)
    result = fpp.false_positive_probability(
        tic_id=1,
        sectors=[14],
        period_days=3.0,
        depth_fraction=0.001,
        time=[0.0, 1.0],
        flux=[1.0, 1.0],
        flux_err=0.001,
        timeout=1,
    )
    assert result["state"] == "not_run"
    assert _blocking(result)


def test_unreadable_output_is_absence(monkeypatch, tmp_path) -> None:
    import subprocess

    fake = tmp_path / "python.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(fpp, "isolated_interpreter", lambda: fake)

    class _Completed:
        stdout = "not json at all"
        stderr = ""
        returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed())
    result = fpp.probe()
    assert result["state"] == "not_run"
    assert _blocking(result)


def test_a_measured_fpp_satisfies_the_packet_section(monkeypatch, tmp_path) -> None:
    import subprocess

    fake = tmp_path / "python.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(fpp, "isolated_interpreter", lambda: fake)

    class _Completed:
        stdout = json.dumps(
            {"state": "measured", "fpp": 0.004, "nfpp": 0.001, "scenario_probabilities": {}}
        )
        stderr = ""
        returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed())
    result = fpp.false_positive_probability(
        tic_id=298732908,
        sectors=[14, 15, 16],
        period_days=14.705,
        depth_fraction=0.005742,
        time=[0.0, 1.0],
        flux=[1.0, 1.0],
        flux_err=0.001,
        aperture_pixels=[[[0, 0]]],
    )
    assert result["state"] == "measured"
    assert result["fpp"] == 0.004
    assert not _blocking(result)


def test_the_runner_is_never_imported_into_the_kernel_environment() -> None:
    """The whole point of the split: exohunt must not import triceratops.

    If this ever fails, TRICERATOPS has been pulled into the main environment
    and its numba pin is free to move numpy underneath the frozen modules.
    """

    import sys

    assert "triceratops" not in sys.modules
    assert "pytransit" not in sys.modules
