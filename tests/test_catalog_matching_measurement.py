"""Tests for the non-shipping P2 catalog-matching diagnostic."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from scripts.measure_p2_catalog_matching import (
    PERIOD_ONLY_REJECTION,
    diagnose_relation,
    measure,
)


def _report() -> dict[str, object]:
    return {
        "data": {"tic_id": 42},
        "observation_window": {"start_btjd": 100.0, "end_btjd": 110.0},
        "strongest_residual_signal": {
            "period_days": 2.0,
            "transit_time": 100.0,
            "duration_hours": 2.0,
            "depth_snr": 20.0,
            "observed_transits": 5,
        },
        "automated_triage": {
            "passes": False,
            "rejection_reasons": [PERIOD_ONLY_REJECTION],
        },
        "known_signal_masks": [
            {
                "label": "TOI-42.01",
                "period_days": 2.0,
                "propagated_epoch_in_light_curve_time": 100.0,
                # Complete width = 4 h, so the half-width is 2 h.
                "mask_width_hours": 4.0,
                "mask_status": "masked",
                "mask_reason": None,
            }
        ],
    }


def _relation(**updates: object) -> dict[str, object]:
    relation = {
        "known_signal": "TOI-42.01",
        "mask_status": "masked",
        "status": "exact",
        "relation": "exact",
        "fractional_error_to_relation": 0.0,
    }
    relation.update(updates)
    return relation


def test_exact_relation_with_overlapping_events_is_consistent() -> None:
    diagnosis = diagnose_relation(_report(), _relation())

    assert diagnosis["epoch_verdict"] == "consistent_with_masked_known_signal"
    assert diagnosis["overlapping_event_windows"] == 6
    assert diagnosis["would_pass_without_period_only_rejection"] is True


def test_exact_relation_at_a_distinct_phase_is_not_the_masked_signal() -> None:
    report = _report()
    report["strongest_residual_signal"]["transit_time"] = 100.5

    diagnosis = diagnose_relation(report, _relation())

    assert diagnosis["epoch_verdict"] == (
        "phase_distinct_from_masked_known_signal"
    )
    assert diagnosis["overlapping_event_windows"] == 0


def test_transit_window_overlap_conservatively_counts_mask_edge_leakage() -> None:
    report = _report()
    # The recovered center is outside the known mask's 2 h half-width, but its
    # 1 h half-duration still overlaps that removed window.
    report["strongest_residual_signal"]["transit_time"] = 100.12

    diagnosis = diagnose_relation(report, _relation())

    assert diagnosis["minimum_center_offset_hours"] > 2.0
    assert diagnosis["epoch_verdict"] == "consistent_with_masked_known_signal"


def test_untrustworthy_and_harmonic_relations_are_not_overinterpreted() -> None:
    untrustworthy = _report()
    untrustworthy["known_signal_masks"][0]["mask_status"] = (
        "unmaskable_ephemeris_drift"
    )
    untrustworthy_diagnosis = diagnose_relation(
        untrustworthy,
        _relation(mask_status="unmaskable_ephemeris_drift"),
    )
    assert untrustworthy_diagnosis["epoch_verdict"] == (
        "not_evaluable_untrustworthy_mask"
    )

    harmonic = deepcopy(_report())
    harmonic_diagnosis = diagnose_relation(
        harmonic,
        _relation(status="harmonic_alias", relation="half-period alias"),
    )
    assert harmonic_diagnosis["epoch_verdict"] == (
        "not_evaluated_non_exact_relation"
    )


def test_replay_projects_only_phase_distinct_exact_report_to_pass(
    tmp_path: Path,
) -> None:
    distinct = _report()
    distinct["strongest_residual_signal"]["transit_time"] = 100.5
    distinct["relations_to_known_periods"] = [_relation()]
    overlapping = deepcopy(_report())
    overlapping["data"]["tic_id"] = 43
    overlapping["relations_to_known_periods"] = [_relation()]
    (tmp_path / "TIC_42_s100_residual.json").write_text(
        json.dumps(distinct),
        encoding="utf-8",
    )
    (tmp_path / "TIC_43_s100_residual.json").write_text(
        json.dumps(overlapping),
        encoding="utf-8",
    )

    result = measure([tmp_path])

    assert result["original_triage_passes"] == 0
    assert result["projected_triage_passes"] == 1
    assert result["new_projected_triage_passes"] == 1
    assert result["reports_losing_period_only_rejection"] == 1
