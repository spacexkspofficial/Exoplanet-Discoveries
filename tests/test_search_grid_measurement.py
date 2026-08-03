"""Tests for the frozen-report search-grid projection."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from scripts.measure_p2_search_grids import measure


def _write_report(
    path: Path,
    *,
    tic_id: int,
    duration_hours: float,
    period_days: float = 2.0,
) -> None:
    path.write_text(
        json.dumps(
            {
                "data": {
                    "tic_id": tic_id,
                    "downloaded_sectors": [100],
                },
                "observation_window": {
                    "start_btjd": 100.0,
                    "end_btjd": 110.0,
                },
                "strongest_residual_signal": {
                    "period_days": period_days,
                    "duration_hours": duration_hours,
                },
                "search_configuration": {
                    "period_range_days": [0.5, 20.0],
                },
                "automated_triage": {"passes": False},
            }
        ),
        encoding="utf-8",
    )


def test_measurement_keeps_projection_separate_from_rerun(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    _write_report(
        reports / "TIC_42_s100_residual.json",
        tic_id=42,
        duration_hours=6.0,
    )
    _write_report(
        reports / "TIC_43_s100_residual.json",
        tic_id=43,
        duration_hours=1.0,
        period_days=5.2,
    )
    targets = tmp_path / "targets.csv"
    with targets.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "target",
                "tic_id",
                "sectors",
                "stellar_radius_solar",
                "stellar_mass_solar",
            ],
        )
        writer.writeheader()
        writer.writerows(
            [
                {
                    "target": "TIC 42",
                    "tic_id": 42,
                    "sectors": 100,
                    "stellar_radius_solar": 0.3,
                    "stellar_mass_solar": 0.3,
                },
                {
                    "target": "TIC 43",
                    "tic_id": 43,
                    "sectors": 100,
                    "stellar_radius_solar": 1.0,
                    "stellar_mass_solar": "",
                },
            ]
        )

    result = measure(reports, target_csv=targets)

    assert result["reports"] == 2
    assert result["legacy_duration_rail_fits"] == 1
    assert result["historical_fits_above_planned_duration_grid"] == 1
    assert result["historical_fits_in_planned_period_overscan"] == 1
    assert result["density_source_counts"] == {
        "catalog_stellar_mass_and_radius": 1,
        "solar_density_fallback_missing_stellar_mass_or_radius": 1,
    }
    assert result["stellar_parameter_source_counts"] == {
        "incomplete": 1,
        "target_csv": 1,
    }
    assert "were not rerun" in result["scope"]
