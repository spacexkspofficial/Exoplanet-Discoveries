"""Measure event-number-aware harmonic matching on frozen reports.

This diagnostic combines historical shipping-path detections with the
uncertainty-aware mask records for the same product-targets. It does not run a
campaign or alter the production matcher.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

try:
    from scripts.measure_p2_catalog_matching import (
        PERIOD_ONLY_REJECTION,
        _folded_offset_days,
        _predicted_centers,
    )
except ModuleNotFoundError:  # Direct ``python scripts/...py`` execution.
    from measure_p2_catalog_matching import (  # type: ignore[no-redef]
        PERIOD_ONLY_REJECTION,
        _folded_offset_days,
        _predicted_centers,
    )


SHORTER_PERIOD_DIVISORS = {
    "half-period alias": 2,
    "one-third-period alias": 3,
}
LONGER_PERIOD_MULTIPLIERS = {
    "double-period alias": 2,
    "triple-period alias": 3,
}


def _matching_mask(
    report: dict[str, object],
    known_signal: object,
) -> dict[str, object] | None:
    return next(
        (
            record
            for record in report.get("known_signal_masks", [])
            if isinstance(record, dict)
            and record.get("label") == known_signal
        ),
        None,
    )


def diagnose_harmonic_relation(
    historical_report: dict[str, object],
    relation: dict[str, object],
    mask_report: dict[str, object],
) -> dict[str, object]:
    relation_name = str(relation["relation"])
    if (
        relation_name not in SHORTER_PERIOD_DIVISORS
        and relation_name not in LONGER_PERIOD_MULTIPLIERS
    ):
        raise ValueError(f"Unsupported harmonic relation: {relation_name}")

    signal = historical_report["strongest_residual_signal"]
    triage = historical_report["automated_triage"]
    known_signal = relation.get("known_signal")
    mask = _matching_mask(mask_report, known_signal)
    base = {
        "tic_id": int(historical_report["data"]["tic_id"]),
        "author": historical_report["data"].get("author"),
        "known_signal": known_signal,
        "period_relation": relation_name,
        "fractional_error_to_relation": relation.get(
            "fractional_error_to_relation"
        ),
        "recovered_period_days": float(signal["period_days"]),
        "recovered_transit_time_btjd": float(signal["transit_time"]),
        "recovered_duration_hours": float(signal["duration_hours"]),
        "recovered_depth_snr": float(signal["depth_snr"]),
        "recovered_observed_transits": int(signal["observed_transits"]),
        "original_triage_passes": bool(triage["passes"]),
        "original_rejection_reasons": list(triage["rejection_reasons"]),
    }
    remaining_reasons = [
        reason
        for reason in base["original_rejection_reasons"]
        if reason != PERIOD_ONLY_REJECTION
    ]
    base["remaining_reasons_without_period_only_rejection"] = (
        remaining_reasons
    )
    base["would_pass_without_period_only_rejection"] = not remaining_reasons

    if mask is None:
        return {
            **base,
            "epoch_verdict": "not_evaluable_missing_mask_record",
        }
    if mask.get("mask_status") != "masked":
        return {
            **base,
            "epoch_verdict": "not_evaluable_untrustworthy_mask",
            "current_mask_status": mask.get("mask_status"),
            "mask_reason": mask.get("mask_reason"),
        }

    # Evaluate the recovered ephemeris over the data span that produced it.
    # The current masking report may have a narrower edge-safe span.
    observation = historical_report["observation_window"]
    recovered_centers = _predicted_centers(
        period_days=float(signal["period_days"]),
        transit_time=float(signal["transit_time"]),
        start_btjd=float(observation["start_btjd"]),
        end_btjd=float(observation["end_btjd"]),
    )
    known_period = float(mask["period_days"])
    known_epoch = float(mask["propagated_epoch_in_light_curve_time"])
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
    event_number_classes: list[dict[str, object]] = []

    if not recovered_centers:
        verdict = "insufficient_event_number_support"
    elif overlap_count == 0:
        verdict = "phase_distinct_from_catalog_harmonic"
    elif relation_name in LONGER_PERIOD_MULTIPLIERS:
        if len(recovered_centers) >= 2 and overlap_count == len(
            recovered_centers
        ):
            verdict = "consistent_with_catalog_harmonic"
        elif len(recovered_centers) < 2:
            verdict = "insufficient_event_number_support"
        else:
            verdict = "ambiguous_partial_harmonic_overlap"
    else:
        divisor = SHORTER_PERIOD_DIVISORS[relation_name]
        for residue in range(divisor):
            indices = [
                index
                for index in range(len(recovered_centers))
                if index % divisor == residue
            ]
            class_overlaps = sum(overlaps[index] for index in indices)
            event_number_classes.append(
                {
                    "residue": residue,
                    "predicted_events": len(indices),
                    "overlapping_event_windows": class_overlaps,
                    "all_events_overlap": bool(indices)
                    and class_overlaps == len(indices),
                }
            )
        qualifying_classes = [
            row
            for row in event_number_classes
            if row["predicted_events"] >= 2 and row["all_events_overlap"]
        ]
        overlaps_outside_best_class = (
            overlap_count
            - max(
                (
                    int(row["overlapping_event_windows"])
                    for row in event_number_classes
                ),
                default=0,
            )
        )
        if qualifying_classes and overlaps_outside_best_class == 0:
            verdict = "consistent_with_catalog_harmonic"
        else:
            verdict = "ambiguous_partial_harmonic_overlap"

    return {
        **base,
        "epoch_verdict": verdict,
        "known_period_days": known_period,
        "known_propagated_epoch_btjd": known_epoch,
        "known_mask_half_width_hours": known_mask_half_width_days * 24.0,
        "recovered_half_duration_hours": (
            recovered_half_duration_days * 24.0
        ),
        "overlap_tolerance_hours": overlap_tolerance_days * 24.0,
        "predicted_recovered_events": len(recovered_centers),
        "overlapping_event_windows": overlap_count,
        "overlap_fraction": (
            overlap_count / len(recovered_centers)
            if recovered_centers
            else None
        ),
        "event_number_classes": event_number_classes,
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


def measure(
    manifest_path: Path,
    *,
    results_root: Path,
) -> dict[str, object]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    diagnoses: list[dict[str, object]] = []
    source_reports: list[str] = []
    mask_reports: list[str] = []
    for product in manifest["selected_products"]:
        sources = {
            source["report"]: source
            for source in product["historical_sources"]
        }
        mask_path = results_root / product["current_mask_report"]
        mask_report = json.loads(mask_path.read_text(encoding="utf-8"))
        mask_reports.append(str(mask_path.resolve()))
        for relation in product["safely_masked_relations"]:
            for source_path in relation["source_reports"]:
                if source_path not in sources:
                    raise ValueError(
                        f"Relation source is absent from manifest: "
                        f"{source_path}"
                    )
                historical_path = results_root / source_path
                historical_report = json.loads(
                    historical_path.read_text(encoding="utf-8")
                )
                diagnosis = diagnose_harmonic_relation(
                    historical_report,
                    relation,
                    mask_report,
                )
                diagnosis["historical_report"] = str(
                    historical_path.resolve()
                )
                diagnosis["mask_report"] = str(mask_path.resolve())
                diagnoses.append(diagnosis)
                source_reports.append(str(historical_path.resolve()))

    verdict_counts = Counter(
        str(row["epoch_verdict"]) for row in diagnoses
    )
    relation_counts = Counter(
        str(row["period_relation"]) for row in diagnoses
    )
    verdicts_by_relation: dict[str, dict[str, int]] = {}
    for relation_name in sorted(relation_counts):
        verdicts_by_relation[relation_name] = dict(
            sorted(
                Counter(
                    str(row["epoch_verdict"])
                    for row in diagnoses
                    if row["period_relation"] == relation_name
                ).items()
            )
        )
    distinct = [
        row
        for row in diagnoses
        if row["epoch_verdict"]
        == "phase_distinct_from_catalog_harmonic"
    ]
    return {
        "schema_version": 1,
        "scope": (
            "non-shipping event-number-aware harmonic diagnostic over frozen "
            "historical detections and uncertainty-aware mask records"
        ),
        "cohort_manifest": str(manifest_path.resolve()),
        "product_targets": len(manifest["selected_products"]),
        "harmonic_relations": len(diagnoses),
        "relation_counts": dict(sorted(relation_counts.items())),
        "verdict_counts": dict(sorted(verdict_counts.items())),
        "verdicts_by_relation": verdicts_by_relation,
        "controlled_relation_classes": [
            "half-period alias",
            "double-period alias",
            "triple-period alias",
        ],
        "undercontrolled_relation_classes": [
            "one-third-period alias",
        ],
        "phase_distinct_relations": len(distinct),
        "phase_distinct_would_pass_without_period_rejection": sum(
            bool(row["would_pass_without_period_only_rejection"])
            for row in distinct
        ),
        "decision": (
            "Zero event-window overlap is evidence that a harmonic-period "
            "relation is phase-distinct. Partial overlap remains ambiguous. "
            "A shorter-period alias is consistent only when one complete "
            "event-number class aligns at least twice; a longer-period alias "
            "is consistent only when every recovered event aligns at least "
            "twice. This frozen regression cohort supports the adjudication "
            "rule for half, double, and triple periods, but is not a "
            "population calibration. One-third period remains under-"
            "controlled and must not change in production."
        ),
        "relations": diagnoses,
        "source_reports": sorted(set(source_reports)),
        "mask_reports": sorted(set(mask_reports)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--results-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    measured = measure(args.manifest, results_root=args.results_root)
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
                if key not in {
                    "relations",
                    "source_reports",
                    "mask_reports",
                }
            },
            indent=2,
        )
    )
    print(f"Saved {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
