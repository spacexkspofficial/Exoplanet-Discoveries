"""Tests for the shipping-path T3 A/B measurement."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.measure_p2_t3_ab import measure


def _write_report(
    directory: Path,
    *,
    tic_id: int,
    passes: bool,
    t3: dict[str, object] | None = None,
) -> None:
    directory.mkdir(exist_ok=True)
    report = {
        "data": {"tic_id": tic_id},
        "observation_window": {"start_btjd": 100.0, "end_btjd": 110.0},
        "strongest_residual_signal": {"period_days": 2.0},
        "search_grid": {"density_source": "catalog"},
        "automated_triage": {
            "passes": passes,
            "rejection_reasons": [] if passes else ["test rejection"],
        },
    }
    if t3 is not None:
        report["t3_vetoes"] = t3
        report["automated_triage"]["rejection_reasons"] = list(
            t3["rejection_reasons"]
        )
    (directory / f"TIC_{tic_id}_s100_residual.json").write_text(
        json.dumps(report),
        encoding="utf-8",
    )


def _t3(*, passes: bool, supported: int) -> dict[str, object]:
    reason = "insufficient support"
    return {
        "passes": passes,
        "routes_to_eb_lane": False,
        "minimum_supported_events": 2,
        "rejection_reasons": [] if passes else [reason],
        "review_flags": [],
        "checks": {
            "duration_density": {"verdict": "pass"},
            "depth_physicality": {"verdict": "pass"},
            "odd_even": {"verdict": "pass"},
            "full_phase_secondary": {"verdict": "pass"},
            "event_support": {"supported_events": supported},
        },
    }


def test_measurement_keeps_search_outputs_separate_from_t3_effect(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    t3_dir = tmp_path / "t3"
    _write_report(baseline, tic_id=42, passes=True)
    _write_report(baseline, tic_id=43, passes=False)
    _write_report(t3_dir, tic_id=42, passes=False, t3=_t3(passes=False, supported=1))
    _write_report(t3_dir, tic_id=43, passes=False, t3=_t3(passes=True, supported=3))

    result = measure(baseline, t3_dir=t3_dir)

    assert result["identity"]["exact_tic_set"] is True
    assert result["comparison"]["transition_counts"] == {
        "rejected_to_rejected": 1,
        "survivor_to_rejected": 1,
    }
    assert result["arms"]["t3"]["insufficient_event_support"] == 1
    assert result["comparison"]["exact_fields"] == {
        "observation_window": 2,
        "strongest_residual_signal": 2,
        "search_grid": 2,
        "complete_pre_t3_science_payload": 2,
    }
    assert all(result["acceptance_checks"].values())
