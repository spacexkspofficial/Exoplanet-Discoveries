"""Project the shipping search-grid policy over frozen reports.

This is an offline policy replay. It does not rerun BLS and therefore does not
claim that a historical best fit would remain the best fit on the new grid.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

import numpy as np

from exohunt.search import build_search_grid


LEGACY_DURATION_GRID_HOURS = (
    0.25,
    0.5,
    0.75,
    1.0,
    1.5,
    2.0,
    3.0,
    4.0,
    6.0,
)


def _optional_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _target_metadata(path: Path) -> dict[int, dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return {
            int(row["tic_id"]): row
            for row in csv.DictReader(handle)
            if row.get("tic_id")
        }


def measure(
    report_dir: Path,
    *,
    target_csv: Path,
    context_dir: Path | None = None,
) -> dict[str, object]:
    targets = _target_metadata(target_csv)
    rows: list[dict[str, object]] = []
    for report_path in sorted(report_dir.glob("*_residual.json")):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        data = report["data"]
        signal = report["strongest_residual_signal"]
        observation = report["observation_window"]
        settings = report["search_configuration"]
        tic_id = int(data["tic_id"])
        target = targets.get(tic_id, {})
        context_path = (
            context_dir / f"TIC_{tic_id}_cross_mission_context.json"
            if context_dir is not None
            else None
        )
        context_tic: dict[str, object] = {}
        if context_path is not None and context_path.exists():
            context = json.loads(context_path.read_text(encoding="utf-8"))
            if isinstance(context.get("tic"), dict):
                context_tic = context["tic"]
        requested_minimum, requested_maximum = (
            float(value) for value in settings["period_range_days"]
        )
        sectors = data.get("downloaded_sectors") or data.get(
            "requested_sectors"
        )
        sectors = sectors if isinstance(sectors, list) else [sectors]
        stellar_pairs = [
            (
                "report_metadata",
                _optional_float(data.get("stellar_radius_solar")),
                _optional_float(data.get("stellar_mass_solar")),
            ),
            (
                "target_csv",
                _optional_float(target.get("stellar_radius_solar")),
                _optional_float(target.get("stellar_mass_solar")),
            ),
            (
                "saved_cross_mission_context",
                _optional_float(context_tic.get("stellar_radius_solar")),
                _optional_float(context_tic.get("stellar_mass_solar")),
            ),
        ]
        complete_pair = next(
            (
                (source, radius, mass)
                for source, radius, mass in stellar_pairs
                if radius is not None and mass is not None
            ),
            None,
        )
        if complete_pair is None:
            stellar_parameter_source = "incomplete"
            radius = next(
                (
                    radius
                    for _, radius, _ in stellar_pairs
                    if radius is not None
                ),
                None,
            )
            mass = None
        else:
            stellar_parameter_source, radius, mass = complete_pair
        plan = build_search_grid(
            baseline_days=(
                float(observation["end_btjd"])
                - float(observation["start_btjd"])
            ),
            single_sector=len({value for value in sectors if value}) <= 1,
            requested_min_period_days=requested_minimum,
            requested_max_period_days=requested_maximum,
            stellar_radius_solar=radius,
            stellar_mass_solar=mass,
        )
        period = float(signal["period_days"])
        duration = float(signal["duration_hours"])
        duration_minimum = float(plan.duration_hours[0])
        duration_maximum = float(plan.duration_hours[-1])
        rows.append(
            {
                "tic_id": tic_id,
                "report": str(report_path.resolve()),
                "historical_period_days": period,
                "historical_duration_hours": duration,
                "historical_triage_passes": bool(
                    report["automated_triage"]["passes"]
                ),
                "legacy_duration_at_grid_rail": any(
                    np.isclose(duration, boundary)
                    for boundary in (
                        LEGACY_DURATION_GRID_HOURS[0],
                        LEGACY_DURATION_GRID_HOURS[-1],
                    )
                ),
                "stellar_parameter_source": stellar_parameter_source,
                "planned_grid": plan.to_dict(),
                "historical_fit_below_planned_duration_grid": (
                    duration < duration_minimum
                    and not np.isclose(duration, duration_minimum)
                ),
                "historical_fit_above_planned_duration_grid": (
                    duration > duration_maximum
                    and not np.isclose(duration, duration_maximum)
                ),
                "historical_fit_on_planned_duration_boundary": bool(
                    np.isclose(duration, duration_minimum)
                    or np.isclose(duration, duration_maximum)
                ),
                "historical_fit_in_planned_period_overscan": (
                    plan.period.in_overscan(period)
                    and period <= plan.period.max_search_days
                ),
                "historical_fit_beyond_planned_period_search": (
                    period > plan.period.max_search_days
                ),
                "historical_fit_on_planned_period_boundary": bool(
                    np.isclose(period, plan.period.min_period_days)
                    or np.isclose(period, plan.period.max_search_days)
                ),
            }
        )

    density_sources = Counter(
        str(row["planned_grid"]["density_source"]) for row in rows
    )
    stellar_parameter_sources = Counter(
        str(row["stellar_parameter_source"]) for row in rows
    )
    return {
        "schema_version": 1,
        "scope": (
            "offline search-grid policy projection over frozen reports; "
            "historical fits were not rerun"
        ),
        "report_dir": str(report_dir.resolve()),
        "target_csv": str(target_csv.resolve()),
        "context_dir": (
            str(context_dir.resolve()) if context_dir is not None else None
        ),
        "reports": len(rows),
        "historical_triage_passes": sum(
            bool(row["historical_triage_passes"]) for row in rows
        ),
        "density_source_counts": dict(sorted(density_sources.items())),
        "stellar_parameter_source_counts": dict(
            sorted(stellar_parameter_sources.items())
        ),
        "legacy_duration_rail_fits": sum(
            bool(row["legacy_duration_at_grid_rail"]) for row in rows
        ),
        "historical_passes_at_legacy_duration_rail": sum(
            bool(row["historical_triage_passes"])
            and bool(row["legacy_duration_at_grid_rail"])
            for row in rows
        ),
        "historical_fits_below_planned_duration_grid": sum(
            bool(row["historical_fit_below_planned_duration_grid"])
            for row in rows
        ),
        "historical_passes_below_planned_duration_grid": sum(
            bool(row["historical_triage_passes"])
            and bool(row["historical_fit_below_planned_duration_grid"])
            for row in rows
        ),
        "historical_fits_above_planned_duration_grid": sum(
            bool(row["historical_fit_above_planned_duration_grid"])
            for row in rows
        ),
        "historical_passes_above_planned_duration_grid": sum(
            bool(row["historical_triage_passes"])
            and bool(row["historical_fit_above_planned_duration_grid"])
            for row in rows
        ),
        "historical_fits_on_planned_duration_boundary": sum(
            bool(row["historical_fit_on_planned_duration_boundary"])
            for row in rows
        ),
        "historical_fits_in_planned_period_overscan": sum(
            bool(row["historical_fit_in_planned_period_overscan"])
            for row in rows
        ),
        "historical_passes_in_planned_period_overscan": sum(
            bool(row["historical_triage_passes"])
            and bool(row["historical_fit_in_planned_period_overscan"])
            for row in rows
        ),
        "historical_fits_beyond_planned_period_search": sum(
            bool(row["historical_fit_beyond_planned_period_search"])
            for row in rows
        ),
        "historical_passes_beyond_planned_period_search": sum(
            bool(row["historical_triage_passes"])
            and bool(row["historical_fit_beyond_planned_period_search"])
            for row in rows
        ),
        "historical_fits_on_planned_period_boundary": sum(
            bool(row["historical_fit_on_planned_period_boundary"])
            for row in rows
        ),
        "historical_passes_touched_by_planned_grid": sum(
            bool(row["historical_triage_passes"])
            and (
                bool(row["historical_fit_below_planned_duration_grid"])
                or bool(row["historical_fit_above_planned_duration_grid"])
                or bool(row["historical_fit_in_planned_period_overscan"])
                or bool(row["historical_fit_beyond_planned_period_search"])
            )
            for row in rows
        ),
        "interpretation": (
            "Counts describe where historical best fits fall relative to the "
            "new policy. They do not predict the best fit or triage result of "
            "a BLS rerun. Shipping-path tests separately prove that density "
            "grids are passed to BLS and that overscan/grid-rail fits reject."
        ),
        "relations": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--target-csv", required=True, type=Path)
    parser.add_argument("--context-dir", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    result = measure(
        args.report_dir,
        target_csv=args.target_csv,
        context_dir=args.context_dir,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "relations"},
            indent=2,
        )
    )
    print(f"Saved {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
