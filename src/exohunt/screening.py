"""Historical screening helpers extracted from the command-line interface.

This module preserves the legacy campaign behavior byte-for-byte while the P2
kernel is wired in.  Its historical inline thresholds are intentionally not
changed in this structural slice; they migrate to named science configuration
only in a separately measured behavior commit.
"""

from __future__ import annotations

import numpy as np


def _optional_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _asymmetric_uncertainty(
    row: dict[str, object],
    positive_key: str,
    negative_key: str,
) -> float | None:
    """Conservatively collapse asymmetric catalog errors to one magnitude."""

    values = [
        abs(value)
        for value in (
            _optional_float(row.get(positive_key)),
            _optional_float(row.get(negative_key)),
        )
        if value is not None
    ]
    return max(values) if values else None


def _catalog_ephemerides(catalog: dict[str, object]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for row in catalog["tois"]:
        period = _optional_float(row.get("pl_orbper"))
        epoch = _optional_float(row.get("pl_tranmid"))
        duration = _optional_float(row.get("pl_trandurh"))
        if period and epoch and duration:
            events.append(
                {
                    "label": f"TOI-{row.get('toi')}",
                    "source": "NASA Exoplanet Archive TOI table",
                    "disposition": row.get("tfopwg_disp"),
                    "period_days": period,
                    "epoch_bjd": epoch,
                    "duration_hours": duration,
                    "period_uncertainty_days": _asymmetric_uncertainty(
                        row, "pl_orbpererr1", "pl_orbpererr2"
                    ),
                    "epoch_uncertainty_days": _asymmetric_uncertainty(
                        row, "pl_tranmiderr1", "pl_tranmiderr2"
                    ),
                    "duration_uncertainty_hours": _asymmetric_uncertainty(
                        row, "pl_trandurherr1", "pl_trandurherr2"
                    ),
                }
            )
    for row in catalog["confirmed_planets"]:
        period = _optional_float(row.get("pl_orbper"))
        epoch = _optional_float(row.get("pl_tranmid"))
        duration = _optional_float(row.get("pl_trandur"))
        duplicate = period is not None and any(
            abs(float(event["period_days"]) - period) / period < 0.001
            for event in events
        )
        if (
            row.get("tran_flag") == "1"
            and period
            and epoch
            and duration
            and not duplicate
        ):
            events.append(
                {
                    "label": row.get("pl_name"),
                    "source": "NASA Exoplanet Archive confirmed planets table",
                    "disposition": "confirmed",
                    "period_days": period,
                    "epoch_bjd": epoch,
                    "duration_hours": duration,
                    "period_uncertainty_days": _asymmetric_uncertainty(
                        row, "pl_orbpererr1", "pl_orbpererr2"
                    ),
                    "epoch_uncertainty_days": _asymmetric_uncertainty(
                        row, "pl_tranmiderr1", "pl_tranmiderr2"
                    ),
                    "duration_uncertainty_hours": _asymmetric_uncertainty(
                        row, "pl_trandurerr1", "pl_trandurerr2"
                    ),
                }
            )
    return events


def _known_transiting_periods(catalog: dict[str, object]) -> list[float]:
    """Count known transit periods even when a row lacks mask parameters.

    A missing duration must not make a multi-planet system appear to be a
    single-planet system. TOI rows explicitly marked false positive are not
    counted; all transiting confirmed-planet rows are counted.
    """

    periods: list[float] = []
    for row in catalog["tois"]:
        disposition = str(row.get("tfopwg_disp") or "").upper()
        period = _optional_float(row.get("pl_orbper"))
        if period and disposition not in {"FP", "FA"}:
            periods.append(period)
    for row in catalog["confirmed_planets"]:
        period = _optional_float(row.get("pl_orbper"))
        if period and str(row.get("tran_flag")) == "1":
            periods.append(period)
    unique: list[float] = []
    for period in sorted(periods):
        if not any(abs(period - known) / known < 0.01 for known in unique):
            unique.append(period)
    return unique


def _screening_flags(result) -> dict[str, bool]:
    duty_cycle = result.duration_hours / (result.period_days * 24.0)
    return {
        "white_noise_depth_snr_below_7_1": result.depth_snr < 7.1,
        "fewer_than_two_observed_transits": result.observed_transits < 2,
        "odd_even_mismatch_over_3_sigma": (
            result.odd_even_depth_difference_sigma is not None
            and result.odd_even_depth_difference_sigma > 3
        ),
        "secondary_eclipse_over_3_sigma": (
            result.secondary_snr is not None and result.secondary_snr > 3
        ),
        "transit_duty_cycle_over_15_percent": duty_cycle > 0.15,
        "transit_depth_over_5_percent": result.depth_ppm > 50_000,
    }


def _classify_screening_result(
    result,
    rejection_reasons: list[str],
    deeper_vetting: dict[str, object] | None = None,
) -> dict[str, object]:
    """Assign a follow-up class without claiming that any star is planet-free."""

    reasons = set(rejection_reasons)
    deeper_flags = [
        str(value)
        for value in (
            deeper_vetting.get("flags", [])
            if isinstance(deeper_vetting, dict)
            else []
        )
    ]
    recommended_sources = [
        "alternate TESS reduction (SPOC, QLP, or TGLC)",
        "additional TESS sectors",
        "Gaia DR3 neighbor and astrometry context",
        "Kepler or K2 light curves when sky coverage overlaps",
        "ZTF or ASAS-SN variability context when available",
    ]
    if not rejection_reasons:
        screening_class = "automated_survivor"
        if deeper_flags:
            vetting_tier = "needs_manual_review"
            priority = min(79, 55 + int(max(0.0, result.depth_snr - 7.1) / 4.0))
        else:
            vetting_tier = (
                "legacy_unmeasured"
                if deeper_vetting is None
                else "high_priority_followup"
            )
            priority = min(99, 75 + int(max(0.0, result.depth_snr - 7.1) / 2.0))
        followup = [
            "localize the signal in target pixels",
            "check nearby stars and official TCE records",
            "test independent TESS sectors when available",
            "compare an independently reduced TESS light curve",
        ]
    elif (
        "fewer than two transit events are represented" in reasons
        and result.depth_snr >= 7.1
        and not reasons.intersection(
            {
                "odd and even transit depths differ by more than 3 sigma",
                "a secondary eclipse is detected above 3 sigma",
                "the fitted transit duty cycle exceeds 15 percent",
                "the fitted transit depth exceeds 5 percent",
            }
        )
    ):
        screening_class = "single_event_lead"
        if deeper_flags:
            vetting_tier = "fragile_single_event"
            priority = min(69, 50 + int(max(0.0, result.depth_snr - 7.1) / 5.0))
        else:
            vetting_tier = (
                "legacy_unmeasured"
                if deeper_vetting is None
                else "supported_single_event"
            )
            priority = min(94, 65 + int(max(0.0, result.depth_snr - 7.1) / 3.0))
        followup = [
            "search earlier or later TESS sectors for another event",
            "inspect target-pixel localization and nearby sources",
            "fit a single-transit model before assigning an orbital period",
            "check cross-mission coverage for a longer time baseline",
        ]
    elif reasons == {"white-noise BLS depth S/N is below 7.1"}:
        screening_class = "no_transit_detected"
        vetting_tier = "deprioritized_for_this_window"
        priority = 5
        followup = [
            "deprioritize for this exact TESS window",
            "retain for longer-baseline or non-transit surveys",
        ]
    else:
        screening_class = "screened_rejected"
        vetting_tier = "strongest_signal_rejected"
        priority = 15
        followup = [
            "do not promote the strongest signal",
            "retain the star for possible weaker-signal or other-method searches",
        ]
    return {
        "screening_class": screening_class,
        "followup_priority": priority,
        "followup_reasons": followup,
        "vetting_tier": vetting_tier,
        "deeper_vetting_flags": deeper_flags,
        "recommended_data_sources": recommended_sources,
        "planet_free": False,
        "scope_warning": (
            "Classification applies only to detectable transits in the searched "
            "TESS sectors and period range."
        ),
    }


def _sensitivity_depth_at_period(
    sensitivity: dict[str, object] | None,
    period_days: float,
) -> float | None:
    if not isinstance(sensitivity, dict):
        return None
    rows = sensitivity.get("periods")
    if not isinstance(rows, list):
        return None
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and abs(float(row.get("period_days", -1)) - period_days) < 1e-6
    ]
    if not matches:
        return None
    value = matches[0].get("minimum_recovered_depth_ppm")
    return None if value is None else float(value)
