"""Measure exact-period catalog matching after uncertainty-aware masking.

This is a diagnostic, not the shipping matcher. It deliberately limits the
phase verdict to exact-period relations. Harmonic aliases require a separate
event-number model and remain explicitly unevaluated here.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


PERIOD_ONLY_REJECTION = (
    "the strongest period is within 5% of a catalogued transit period or "
    "simple harmonic"
)


def _predicted_centers(
    *,
    period_days: float,
    transit_time: float,
    start_btjd: float,
    end_btjd: float,
) -> list[float]:
    first = math.ceil((start_btjd - transit_time) / period_days)
    last = math.floor((end_btjd - transit_time) / period_days)
    return [
        transit_time + number * period_days
        for number in range(first, last + 1)
    ]


def _folded_offset_days(
    epoch: float,
    *,
    period_days: float,
    transit_time: float,
) -> float:
    return abs(
        (epoch - transit_time + period_days / 2) % period_days
        - period_days / 2
    )


def _matching_mask(
    report: dict[str, object],
    relation: dict[str, object],
) -> dict[str, object] | None:
    label = relation.get("known_signal")
    return next(
        (
            mask
            for mask in report.get("known_signal_masks", [])
            if isinstance(mask, dict) and mask.get("label") == label
        ),
        None,
    )


def diagnose_relation(
    report: dict[str, object],
    relation: dict[str, object],
) -> dict[str, object]:
    signal = report["strongest_residual_signal"]
    observation = report["observation_window"]
    mask = _matching_mask(report, relation)
    base = {
        "tic_id": int(report["data"]["tic_id"]),
        "known_signal": relation.get("known_signal"),
        "mask_status": relation.get("mask_status"),
        "period_relation": relation.get("relation"),
        "period_status": relation.get("status"),
        "fractional_error_to_relation": float(
            relation["fractional_error_to_relation"]
        ),
        "recovered_period_days": float(signal["period_days"]),
        "recovered_transit_time_btjd": float(signal["transit_time"]),
        "recovered_duration_hours": float(signal["duration_hours"]),
        "recovered_depth_snr": float(signal["depth_snr"]),
        "recovered_observed_transits": int(signal["observed_transits"]),
        "original_triage_passes": bool(report["automated_triage"]["passes"]),
        "original_rejection_reasons": list(
            report["automated_triage"]["rejection_reasons"]
        ),
    }
    remaining_reasons = [
        reason
        for reason in base["original_rejection_reasons"]
        if reason != PERIOD_ONLY_REJECTION
    ]
    base["remaining_reasons_without_period_only_rejection"] = remaining_reasons
    base["would_pass_without_period_only_rejection"] = not remaining_reasons

    if not isinstance(mask, dict):
        return {
            **base,
            "epoch_verdict": "not_evaluable_missing_mask_record",
        }
    if relation.get("mask_status") != "masked":
        return {
            **base,
            "epoch_verdict": "not_evaluable_untrustworthy_mask",
            "mask_reason": mask.get("mask_reason"),
        }
    if relation.get("relation") != "exact":
        return {
            **base,
            "epoch_verdict": "not_evaluated_non_exact_relation",
            "note": (
                "harmonic identity needs an event-number model; period ratio "
                "alone is not a phase comparison"
            ),
        }

    recovered_centers = _predicted_centers(
        period_days=float(signal["period_days"]),
        transit_time=float(signal["transit_time"]),
        start_btjd=float(observation["start_btjd"]),
        end_btjd=float(observation["end_btjd"]),
    )
    known_period = float(mask["period_days"])
    known_epoch = float(mask["propagated_epoch_in_light_curve_time"])
    # `mask_width_hours` stores the complete uncertainty-expanded width.
    known_mask_half_width_days = float(mask["mask_width_hours"]) / 48.0
    recovered_half_duration_days = float(signal["duration_hours"]) / 48.0
    overlap_tolerance_days = (
        known_mask_half_width_days + recovered_half_duration_days
    )
    offsets = [
        _folded_offset_days(
            center,
            period_days=known_period,
            transit_time=known_epoch,
        )
        for center in recovered_centers
    ]
    overlaps = [offset <= overlap_tolerance_days for offset in offsets]
    overlap_count = sum(overlaps)
    if recovered_centers and overlap_count == len(recovered_centers):
        verdict = "consistent_with_masked_known_signal"
    elif overlap_count == 0:
        verdict = "phase_distinct_from_masked_known_signal"
    else:
        verdict = "ambiguous_partial_epoch_overlap"

    return {
        **base,
        "epoch_verdict": verdict,
        "known_period_days": known_period,
        "known_propagated_epoch_btjd": known_epoch,
        "known_mask_half_width_hours": known_mask_half_width_days * 24.0,
        "recovered_half_duration_hours": recovered_half_duration_days * 24.0,
        "overlap_tolerance_hours": overlap_tolerance_days * 24.0,
        "predicted_recovered_events": len(recovered_centers),
        "overlapping_event_windows": overlap_count,
        "overlap_fraction": (
            overlap_count / len(recovered_centers)
            if recovered_centers
            else None
        ),
        "minimum_center_offset_hours": (
            min(offsets) * 24.0 if offsets else None
        ),
        "maximum_center_offset_hours": (
            max(offsets) * 24.0 if offsets else None
        ),
        "event_center_offsets_hours": [
            round(offset * 24.0, 5) for offset in offsets
        ],
    }


def measure(results_dirs: list[Path]) -> dict[str, object]:
    reports: list[dict[str, object]] = []
    source_reports: list[str] = []
    for results_dir in results_dirs:
        for report_path in sorted(results_dir.glob("TIC_*_residual.json")):
            reports.append(json.loads(report_path.read_text(encoding="utf-8")))
            source_reports.append(str(report_path.resolve()))
    if not reports:
        raise RuntimeError("No per-target reports found.")

    diagnoses: list[dict[str, object]] = []
    for report in reports:
        for relation in report.get("relations_to_known_periods", []):
            if isinstance(relation, dict):
                diagnoses.append(diagnose_relation(report, relation))

    exact_masked = [
        row
        for row in diagnoses
        if row["mask_status"] == "masked"
        and row["period_relation"] == "exact"
    ]
    verdict_counts: dict[str, int] = {}
    for row in diagnoses:
        verdict = str(row["epoch_verdict"])
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
    phase_distinct = [
        row
        for row in exact_masked
        if row["epoch_verdict"]
        == "phase_distinct_from_masked_known_signal"
    ]

    return {
        "schema_version": 1,
        "scope": (
            "diagnostic exact-period adjudication after uncertainty-aware "
            "catalog masking; no production behavior change"
        ),
        "input_directories": [str(path.resolve()) for path in results_dirs],
        "product_reports": len(reports),
        "catalog_signals_in_reports": sum(
            len(report.get("known_signal_masks", [])) for report in reports
        ),
        "period_only_relations": len(diagnoses),
        "safely_masked_exact_relations": len(exact_masked),
        "verdict_counts": verdict_counts,
        "phase_distinct_exact_relations": len(phase_distinct),
        "phase_distinct_would_pass_without_period_rejection": sum(
            bool(row["would_pass_without_period_only_rejection"])
            for row in phase_distinct
        ),
        "decision": (
            "Exact-period matching should include epoch-window overlap. "
            "The current cohort justifies that narrow change, but harmonic "
            "relations remain uncalibrated and must not inherit this verdict."
        ),
        "relations": diagnoses,
        "source_reports": source_reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results_dirs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    measured = measure(args.results_dirs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(measured, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                key: value
                for key, value in measured.items()
                if key not in {"relations", "source_reports"}
            },
            indent=2,
        )
    )
    print(f"Saved {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
