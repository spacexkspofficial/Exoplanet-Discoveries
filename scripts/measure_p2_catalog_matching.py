"""Measure exact-period catalog matching after uncertainty-aware masking.

This is a diagnostic, not the shipping matcher. It deliberately limits the
phase verdict to exact-period relations. Harmonic aliases require a separate
event-number model and remain explicitly unevaluated here.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from exohunt.screening import (
    CATALOG_PERIOD_REJECTION_REASON as PERIOD_ONLY_REJECTION,
    _adjudicate_catalog_relation,
    _folded_center_offset_days as _folded_offset_days,
    _predicted_transit_centers as _predicted_centers,
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
            "catalog_match_rejects": True,
        }
    adjudication = _adjudicate_catalog_relation(
        relation,
        mask,
        recovered_period_days=float(signal["period_days"]),
        recovered_transit_time_btjd=float(signal["transit_time"]),
        recovered_duration_hours=float(signal["duration_hours"]),
        start_btjd=float(observation["start_btjd"]),
        end_btjd=float(observation["end_btjd"]),
    )
    return {
        **base,
        **adjudication,
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
    projected_reports: list[dict[str, object]] = []
    for report in reports:
        report_diagnoses: list[dict[str, object]] = []
        for relation in report.get("relations_to_known_periods", []):
            if isinstance(relation, dict):
                diagnosis = diagnose_relation(report, relation)
                diagnoses.append(diagnosis)
                report_diagnoses.append(diagnosis)
        original_reasons = list(
            report["automated_triage"]["rejection_reasons"]
        )
        projected_reasons = list(original_reasons)
        if (
            PERIOD_ONLY_REJECTION in projected_reasons
            and report_diagnoses
            and not any(
                bool(row["catalog_match_rejects"])
                for row in report_diagnoses
            )
        ):
            projected_reasons.remove(PERIOD_ONLY_REJECTION)
        projected_reports.append(
            {
                "tic_id": int(report["data"]["tic_id"]),
                "author": report["data"].get("author"),
                "original_triage_passes": bool(
                    report["automated_triage"]["passes"]
                ),
                "projected_triage_passes": not projected_reasons,
                "original_rejection_reasons": original_reasons,
                "projected_rejection_reasons": projected_reasons,
                "period_relations": len(report_diagnoses),
            }
        )

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
            "production-helper replay of exact-period adjudication after "
            "uncertainty-aware catalog masking"
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
        "original_triage_passes": sum(
            bool(row["original_triage_passes"])
            for row in projected_reports
        ),
        "projected_triage_passes": sum(
            bool(row["projected_triage_passes"])
            for row in projected_reports
        ),
        "new_projected_triage_passes": sum(
            bool(row["projected_triage_passes"])
            and not bool(row["original_triage_passes"])
            for row in projected_reports
        ),
        "reports_losing_period_only_rejection": sum(
            PERIOD_ONLY_REJECTION in row["original_rejection_reasons"]
            and PERIOD_ONLY_REJECTION
            not in row["projected_rejection_reasons"]
            for row in projected_reports
        ),
        "decision": (
            "The production exact-period helper reproduces the locked "
            "event-window verdicts. Only safely masked, zero-overlap exact "
            "relations lose the catalog rejection; harmonic, ambiguous, and "
            "untrustworthy relations remain rejected."
        ),
        "relations": diagnoses,
        "projected_reports": projected_reports,
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
                if key not in {
                    "relations",
                    "projected_reports",
                    "source_reports",
                }
            },
            indent=2,
        )
    )
    print(f"Saved {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
