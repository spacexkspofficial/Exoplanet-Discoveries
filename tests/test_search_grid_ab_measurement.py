"""Tests for the search-grid shipping-path A/B measurement."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from scripts.measure_p2_search_grid_ab import measure


def _write_report(
    directory: Path,
    *,
    tic_id: int,
    passes: bool,
    period_days: float,
    density_source: str | None = None,
    overscan: bool = False,
    grid_rail: bool = False,
) -> None:
    directory.mkdir(exist_ok=True)
    report = {
        "data": {
            "tic_id": tic_id,
            "downloaded_sectors": [100],
            "cadence_minutes": 2.0,
        },
        "observation_window": {
            "start_btjd": 100.0,
            "end_btjd": 110.0,
            "measurements": 100,
        },
        "strongest_residual_signal": {
            "period_days": period_days,
            "duration_hours": 1.0,
        },
        "search_grid": {
            "density_source": density_source,
            "best_period_in_overscan": overscan,
            "grid_rail": grid_rail,
            "period_at_grid_rail": grid_rail,
            "duration_at_grid_rail": False,
        },
        "screening_flags": {},
        "automated_triage": {
            "passes": passes,
            "rejection_reasons": [] if passes else ["test rejection"],
        },
    }
    (directory / f"TIC_{tic_id}_s100_residual.json").write_text(
        json.dumps(report),
        encoding="utf-8",
    )


def test_measurement_separates_density_effect_and_fallback_invariance(
    tmp_path: Path,
) -> None:
    golden = tmp_path / "golden"
    fallback = tmp_path / "fallback"
    density = tmp_path / "density"
    for directory in (golden, fallback):
        _write_report(
            directory,
            tic_id=42,
            passes=True,
            period_days=2.0,
            density_source="solar",
        )
        _write_report(
            directory,
            tic_id=43,
            passes=True,
            period_days=3.0,
            density_source="solar",
        )
    _write_report(
        density,
        tic_id=42,
        passes=False,
        period_days=2.0,
        density_source="catalog",
        grid_rail=True,
    )
    _write_report(
        density,
        tic_id=43,
        passes=True,
        period_days=3.0,
        density_source="solar",
    )
    cohort = tmp_path / "cohort.csv"
    with cohort.open("w", newline="", encoding="utf-8") as handle:
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
                    "stellar_radius_solar": 0.4,
                    "stellar_mass_solar": 0.3,
                },
                {
                    "target": "TIC 43",
                    "tic_id": 43,
                    "sectors": 100,
                    "stellar_radius_solar": 0.5,
                    "stellar_mass_solar": "",
                },
            ]
        )

    result = measure(
        golden,
        fallback_dir=fallback,
        density_dir=density,
        cohort_csv=cohort,
    )

    assert result["identity"]["golden_exact_cohort"] is True
    assert result["cohort"] == {
        "density_backed_targets": 1,
        "solar_fallback_targets": 1,
    }
    density_effect = result["comparisons"][
        "fallback_to_density_for_density_backed_targets"
    ]
    assert density_effect["transition_counts"] == {
        "survivor_to_rejected": 1
    }
    invariant = result["comparisons"][
        "fallback_invariance_for_solar_fallback_targets"
    ]
    assert invariant["science_payload_exact"] == 1
    assert result["acceptance_checks"] == {
        "all_arms_exact_cohort": True,
        "all_density_arm_inputs_match_fallback": True,
        "solar_fallback_science_is_invariant": True,
        "no_density_arm_boundary_fit_passes": True,
    }
