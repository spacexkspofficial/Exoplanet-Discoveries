"""Tests for the non-shipping P2 harmonic-matching diagnostic."""

from __future__ import annotations

from scripts.measure_p2_catalog_matching import PERIOD_ONLY_REJECTION
from scripts.measure_p2_harmonic_matching import (
    diagnose_harmonic_relation,
)


def _historical_report(
    *,
    period_days: float,
    transit_time: float = 0.0,
    duration_hours: float = 0.1,
) -> dict[str, object]:
    return {
        "data": {"tic_id": 42, "author": "SPOC"},
        "observation_window": {
            "start_btjd": 0.0,
            "end_btjd": 8.0,
        },
        "strongest_residual_signal": {
            "period_days": period_days,
            "transit_time": transit_time,
            "duration_hours": duration_hours,
            "depth_snr": 10.0,
            "observed_transits": 3,
        },
        "automated_triage": {
            "passes": False,
            "rejection_reasons": [PERIOD_ONLY_REJECTION],
        },
    }


def _relation(name: str) -> dict[str, object]:
    return {
        "known_signal": "TOI-42.01",
        "relation": name,
        "fractional_error_to_relation": 0.0,
    }


def _mask_report(
    *,
    end_btjd: float = 8.0,
    mask_width_hours: float = 0.1,
) -> dict[str, object]:
    return {
        "observation_window": {
            "start_btjd": 0.0,
            "end_btjd": end_btjd,
        },
        "known_signal_masks": [
            {
                "label": "TOI-42.01",
                "mask_status": "masked",
                "period_days": 2.0,
                "propagated_epoch_in_light_curve_time": 0.0,
                "mask_width_hours": mask_width_hours,
            }
        ],
    }


def test_longer_period_alias_requires_every_recovered_event_to_align() -> None:
    diagnosis = diagnose_harmonic_relation(
        _historical_report(period_days=4.0),
        _relation("double-period alias"),
        _mask_report(),
    )

    assert diagnosis["epoch_verdict"] == (
        "consistent_with_catalog_harmonic"
    )
    assert diagnosis["overlapping_event_windows"] == 3


def test_shorter_period_alias_aligns_one_repeating_event_class() -> None:
    diagnosis = diagnose_harmonic_relation(
        _historical_report(period_days=1.0),
        _relation("half-period alias"),
        _mask_report(),
    )

    assert diagnosis["epoch_verdict"] == (
        "consistent_with_catalog_harmonic"
    )
    assert [
        row["overlapping_event_windows"]
        for row in diagnosis["event_number_classes"]
    ] == [5, 0]


def test_zero_harmonic_event_overlap_is_phase_distinct() -> None:
    diagnosis = diagnose_harmonic_relation(
        _historical_report(period_days=4.0, transit_time=0.5),
        _relation("double-period alias"),
        _mask_report(),
    )

    assert diagnosis["epoch_verdict"] == (
        "phase_distinct_from_catalog_harmonic"
    )
    assert diagnosis["would_pass_without_period_only_rejection"] is True


def test_partial_longer_period_overlap_remains_ambiguous() -> None:
    report = _historical_report(period_days=4.05)
    report["observation_window"]["end_btjd"] = 8.2
    diagnosis = diagnose_harmonic_relation(
        report,
        _relation("double-period alias"),
        _mask_report(mask_width_hours=4.0),
    )

    assert diagnosis["overlapping_event_windows"] == 2
    assert diagnosis["predicted_recovered_events"] == 3
    assert diagnosis["epoch_verdict"] == (
        "ambiguous_partial_harmonic_overlap"
    )


def test_single_overlap_cannot_establish_harmonic_identity() -> None:
    report = _historical_report(period_days=6.0)
    report["observation_window"]["end_btjd"] = 1.0
    diagnosis = diagnose_harmonic_relation(
        report,
        _relation("triple-period alias"),
        _mask_report(),
    )

    assert diagnosis["overlapping_event_windows"] == 1
    assert diagnosis["epoch_verdict"] == (
        "insufficient_event_number_support"
    )
