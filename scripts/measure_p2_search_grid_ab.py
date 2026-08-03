"""Compare frozen, fallback-grid, and density-grid shipping-path reports."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from pathlib import Path
from typing import Iterable

from exohunt.benchmarks import compare_period


SCIENCE_FIELDS = (
    "observation_window",
    "strongest_residual_signal",
    "search_grid",
    "top_period_peaks",
    "harmonic_checks",
    "screening_flags",
    "sensitivity_probe",
    "deeper_vetting",
    "automated_triage",
)


def _optional_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _load_reports(path: Path) -> dict[int, dict[str, object]]:
    reports: dict[int, dict[str, object]] = {}
    for report_path in sorted(path.glob("TIC_*_residual.json")):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        tic_id = int(report["data"]["tic_id"])
        if tic_id in reports:
            raise ValueError(f"Duplicate TIC {tic_id} in {path}")
        reports[tic_id] = report
    return reports


def _load_cohort(path: Path) -> tuple[list[int], set[int], set[int]]:
    ordered: list[int] = []
    density: set[int] = set()
    fallback: set[int] = set()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            tic_id = int(row["tic_id"])
            ordered.append(tic_id)
            radius = _optional_float(row.get("stellar_radius_solar"))
            mass = _optional_float(row.get("stellar_mass_solar"))
            (density if radius is not None and mass is not None else fallback).add(
                tic_id
            )
    if len(ordered) != len(set(ordered)):
        raise ValueError("Cohort CSV contains duplicate TIC IDs.")
    return ordered, density, fallback


def _passes(report: dict[str, object]) -> bool:
    triage = report.get("automated_triage")
    return bool(triage["passes"]) if isinstance(triage, dict) else False


def _reasons(report: dict[str, object]) -> list[str]:
    triage = report.get("automated_triage")
    if not isinstance(triage, dict):
        return []
    reasons = triage.get("rejection_reasons")
    return [str(reason) for reason in reasons] if isinstance(reasons, list) else []


def _normalized_data(report: dict[str, object]) -> dict[str, object]:
    data = dict(report.get("data") or {})
    data.pop("stellar_mass_solar", None)
    data.pop("stellar_radius_solar", None)
    return data


def _science_payload(report: dict[str, object]) -> dict[str, object]:
    return {field: report.get(field) for field in SCIENCE_FIELDS}


def _transition(left: dict[str, object], right: dict[str, object]) -> str:
    return (
        ("survivor" if _passes(left) else "rejected")
        + "_to_"
        + ("survivor" if _passes(right) else "rejected")
    )


def _pair_summary(
    left: dict[int, dict[str, object]],
    right: dict[int, dict[str, object]],
    *,
    ids: Iterable[int],
) -> dict[str, object]:
    requested = list(ids)
    common = [tic_id for tic_id in requested if tic_id in left and tic_id in right]
    transitions = Counter(_transition(left[tic_id], right[tic_id]) for tic_id in common)
    lost = [
        tic_id
        for tic_id in common
        if _passes(left[tic_id]) and not _passes(right[tic_id])
    ]
    gained = [
        tic_id
        for tic_id in common
        if not _passes(left[tic_id]) and _passes(right[tic_id])
    ]
    period_relations = Counter(
        str(
            compare_period(
                float(right[tic_id]["strongest_residual_signal"]["period_days"]),
                float(left[tic_id]["strongest_residual_signal"]["period_days"]),
            )["status"]
        )
        for tic_id in common
    )
    return {
        "requested_targets": len(requested),
        "common_targets": len(common),
        "missing_from_left": sorted(set(requested) - set(left)),
        "missing_from_right": sorted(set(requested) - set(right)),
        "left_passes": sum(_passes(left[tic_id]) for tic_id in common),
        "right_passes": sum(_passes(right[tic_id]) for tic_id in common),
        "transition_counts": dict(sorted(transitions.items())),
        "lost_survivor_tic_ids": lost,
        "gained_survivor_tic_ids": gained,
        "lost_survivor_new_reasons": dict(
            sorted(
                Counter(
                    reason
                    for tic_id in lost
                    for reason in _reasons(right[tic_id])
                ).items()
            )
        ),
        "gained_survivor_previous_reasons": dict(
            sorted(
                Counter(
                    reason
                    for tic_id in gained
                    for reason in _reasons(left[tic_id])
                ).items()
            )
        ),
        "period_relation_counts": dict(sorted(period_relations.items())),
        "observation_window_exact": sum(
            left[tic_id].get("observation_window")
            == right[tic_id].get("observation_window")
            for tic_id in common
        ),
        "normalized_data_exact": sum(
            _normalized_data(left[tic_id]) == _normalized_data(right[tic_id])
            for tic_id in common
        ),
        "strongest_signal_exact": sum(
            left[tic_id].get("strongest_residual_signal")
            == right[tic_id].get("strongest_residual_signal")
            for tic_id in common
        ),
        "triage_exact": sum(
            left[tic_id].get("automated_triage")
            == right[tic_id].get("automated_triage")
            for tic_id in common
        ),
        "science_payload_exact": sum(
            _science_payload(left[tic_id]) == _science_payload(right[tic_id])
            for tic_id in common
        ),
    }


def _arm_summary(reports: dict[int, dict[str, object]]) -> dict[str, object]:
    density_sources: Counter[str] = Counter()
    rejection_reasons: Counter[str] = Counter()
    overscan = 0
    overscan_passes = 0
    grid_rail = 0
    grid_rail_passes = 0
    period_rail = 0
    duration_rail = 0
    for report in reports.values():
        grid = report.get("search_grid")
        grid = grid if isinstance(grid, dict) else {}
        source = grid.get("density_source")
        if source:
            density_sources[str(source)] += 1
        in_overscan = bool(grid.get("best_period_in_overscan"))
        at_grid_rail = bool(grid.get("grid_rail"))
        overscan += in_overscan
        overscan_passes += in_overscan and _passes(report)
        grid_rail += at_grid_rail
        grid_rail_passes += at_grid_rail and _passes(report)
        period_rail += bool(grid.get("period_at_grid_rail"))
        duration_rail += bool(grid.get("duration_at_grid_rail"))
        rejection_reasons.update(_reasons(report))
    return {
        "reports": len(reports),
        "passes": sum(_passes(report) for report in reports.values()),
        "rejected": sum(not _passes(report) for report in reports.values()),
        "density_source_counts": dict(sorted(density_sources.items())),
        "best_period_in_overscan": overscan,
        "overscan_passes": overscan_passes,
        "grid_rail": grid_rail,
        "grid_rail_passes": grid_rail_passes,
        "period_at_grid_rail": period_rail,
        "duration_at_grid_rail": duration_rail,
        "rejection_reason_counts": dict(sorted(rejection_reasons.items())),
    }


def measure(
    golden_dir: Path,
    *,
    fallback_dir: Path,
    density_dir: Path,
    cohort_csv: Path,
) -> dict[str, object]:
    golden = _load_reports(golden_dir)
    fallback = _load_reports(fallback_dir)
    density = _load_reports(density_dir)
    ordered, density_ids, fallback_ids = _load_cohort(cohort_csv)
    cohort_set = set(ordered)
    identity = {
        "cohort_targets": len(ordered),
        "golden_reports": len(golden),
        "fallback_reports": len(fallback),
        "density_reports": len(density),
        "golden_exact_cohort": set(golden) == cohort_set,
        "fallback_exact_cohort": set(fallback) == cohort_set,
        "density_exact_cohort": set(density) == cohort_set,
    }
    arms = {
        "golden": _arm_summary(golden),
        "fallback_grid": _arm_summary(fallback),
        "density_grid": _arm_summary(density),
    }
    comparisons = {
        "golden_to_fallback_grid": _pair_summary(
            golden, fallback, ids=ordered
        ),
        "fallback_to_density_grid": _pair_summary(
            fallback, density, ids=ordered
        ),
        "fallback_to_density_for_density_backed_targets": _pair_summary(
            fallback,
            density,
            ids=[tic_id for tic_id in ordered if tic_id in density_ids],
        ),
        "fallback_invariance_for_solar_fallback_targets": _pair_summary(
            fallback,
            density,
            ids=[tic_id for tic_id in ordered if tic_id in fallback_ids],
        ),
        "golden_to_density_grid": _pair_summary(
            golden, density, ids=ordered
        ),
    }
    all_density = comparisons["fallback_to_density_grid"]
    fallback_invariance = comparisons[
        "fallback_invariance_for_solar_fallback_targets"
    ]
    return {
        "schema_version": 1,
        "scope": (
            "shipping-path A/B over the same frozen photometry cohort; "
            "fallback versus density isolates stellar-density duration grids"
        ),
        "identity": identity,
        "cohort": {
            "density_backed_targets": len(density_ids),
            "solar_fallback_targets": len(fallback_ids),
        },
        "arms": arms,
        "comparisons": comparisons,
        "acceptance_checks": {
            "all_arms_exact_cohort": all(
                bool(identity[key])
                for key in (
                    "golden_exact_cohort",
                    "fallback_exact_cohort",
                    "density_exact_cohort",
                )
            ),
            "all_density_arm_inputs_match_fallback": (
                all_density["normalized_data_exact"]
                == len(ordered)
                and all_density["observation_window_exact"] == len(ordered)
            ),
            "solar_fallback_science_is_invariant": (
                fallback_invariance["science_payload_exact"] == len(fallback_ids)
            ),
            "no_density_arm_boundary_fit_passes": (
                arms["density_grid"]["overscan_passes"] == 0
                and arms["density_grid"]["grid_rail_passes"] == 0
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden-dir", required=True, type=Path)
    parser.add_argument("--fallback-dir", required=True, type=Path)
    parser.add_argument("--density-dir", required=True, type=Path)
    parser.add_argument("--cohort-csv", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = measure(
        args.golden_dir,
        fallback_dir=args.fallback_dir,
        density_dir=args.density_dir,
        cohort_csv=args.cohort_csv,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2))
    print(f"Saved {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
