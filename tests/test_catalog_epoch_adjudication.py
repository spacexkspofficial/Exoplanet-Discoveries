"""Production exact-period catalog identity tests."""

from __future__ import annotations

from exohunt.screening import _adjudicate_catalog_relation


def _relation(name: str = "exact") -> dict[str, object]:
    return {
        "status": "exact" if name == "exact" else "harmonic_alias",
        "relation": name,
        "fractional_error_to_relation": 0.0,
    }


def _mask(**updates: object) -> dict[str, object]:
    record = {
        "label": "TOI-42.01",
        "mask_status": "masked",
        "period_days": 2.0,
        "propagated_epoch_in_light_curve_time": 101.0,
        # Complete mask width = 3.6 h; half-width = 1.8 h.
        "mask_width_hours": 3.6,
    }
    record.update(updates)
    return record


def _adjudicate(
    *,
    relation: str = "exact",
    recovered_period_days: float = 2.0,
    recovered_transit_time_btjd: float = 101.0,
    mask: dict[str, object] | None = None,
) -> dict[str, object]:
    return _adjudicate_catalog_relation(
        _relation(relation),
        mask or _mask(),
        recovered_period_days=recovered_period_days,
        recovered_transit_time_btjd=recovered_transit_time_btjd,
        recovered_duration_hours=2.4,
        start_btjd=100.0,
        end_btjd=110.0,
    )


def test_exact_events_overlapping_the_mask_remain_rejected() -> None:
    result = _adjudicate()

    assert result["epoch_verdict"] == (
        "consistent_with_masked_known_signal"
    )
    assert result["overlapping_event_windows"] == 5
    assert result["catalog_match_rejects"] is True


def test_exact_events_at_a_distinct_phase_lose_catalog_rejection() -> None:
    result = _adjudicate(recovered_transit_time_btjd=101.5)

    assert result["epoch_verdict"] == (
        "phase_distinct_from_masked_known_signal"
    )
    assert result["overlapping_event_windows"] == 0
    assert result["catalog_match_rejects"] is False


def test_partial_exact_overlap_remains_rejected_conservatively() -> None:
    result = _adjudicate(recovered_period_days=2.05)

    assert 0 < result["overlapping_event_windows"] < result[
        "predicted_recovered_events"
    ]
    assert result["epoch_verdict"] == "ambiguous_partial_epoch_overlap"
    assert result["catalog_match_rejects"] is True


def test_harmonic_behavior_is_deliberately_unchanged() -> None:
    result = _adjudicate(
        relation="half-period alias",
        recovered_period_days=1.0,
    )

    assert result["epoch_verdict"] == "not_evaluated_non_exact_relation"
    assert result["catalog_match_rejects"] is True


def test_untrustworthy_exact_mask_remains_rejected() -> None:
    result = _adjudicate(
        mask=_mask(
            mask_status="unmasked_ephemeris_uncertainty",
            mask_reason="test uncertainty",
        )
    )

    assert result["epoch_verdict"] == (
        "not_evaluable_untrustworthy_mask"
    )
    assert result["catalog_match_rejects"] is True
