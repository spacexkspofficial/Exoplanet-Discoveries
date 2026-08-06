from __future__ import annotations

import numpy as np

from exohunt.calibration import (
    catalog_without_expected_signal,
    inject_limb_darkened_transit,
    injection_trials,
    invert_prepared_flux,
    segment_shift_scramble,
    select_calibration_sample,
    summarize_calibration,
    three_hour_noise_ppm,
)
from exohunt.config import CalibrationConfig


def test_limb_darkened_injection_has_requested_depth_and_is_deterministic() -> None:
    time = np.linspace(0.0, 10.0, 10_001)
    flux = np.ones_like(time, dtype=np.float32)
    first, metadata = inject_limb_darkened_transit(
        time,
        flux,
        period_days=2.0,
        transit_time_btjd=1.0,
        depth_ppm=2_000.0,
        impact_parameter=0.5,
        stellar_radius_solar=0.5,
        stellar_mass_solar=0.5,
        teff_k=3500.0,
    )
    second, _ = inject_limb_darkened_transit(
        time,
        flux,
        period_days=2.0,
        transit_time_btjd=1.0,
        depth_ppm=2_000.0,
        impact_parameter=0.5,
        stellar_radius_solar=0.5,
        stellar_mass_solar=0.5,
        teff_k=3500.0,
    )
    assert first.dtype == flux.dtype
    assert np.array_equal(first, second)
    assert abs(float(metadata["realized_depth_ppm"]) - 2_000.0) < 1.0
    assert metadata["model"] == "batman_quadratic_limb_darkening"


def test_null_transforms_flip_and_break_global_coherence() -> None:
    time = np.r_[np.arange(20) / 48.0, 1.0 + np.arange(20) / 48.0]
    flux = np.linspace(0.9, 1.1, time.size)
    inverted = invert_prepared_flux(flux)
    assert np.allclose(inverted - np.median(inverted), -(flux - np.median(flux)))
    shifted, metadata = segment_shift_scramble(time, flux, seed=7, gap_days=0.1)
    assert not np.array_equal(shifted, flux)
    assert sorted(shifted.tolist()) == sorted(flux.tolist())
    assert len(metadata["segments"]) == 2
    assert all(row["shift_cadences"] != 0 for row in metadata["segments"])


def test_sampling_and_trial_plan_are_reproducible() -> None:
    rows = [
        {
            "target": f"TIC {index}",
            "tic_id": str(index),
            "sectors": "100",
            "tmag": str(8 + index / 100),
            "teff_k": str(3000 + index),
            "stellar_radius_solar": str(0.1 + index / 1000),
            "distance_pc": str(index),
            "camera": str(index % 4 + 1),
            "ccd": str(index % 4 + 1),
        }
        for index in range(1, 101)
    ]
    config = CalibrationConfig(archetype_count=10)
    first = select_calibration_sample(rows, seed=42, config=config)
    second = select_calibration_sample(rows, seed=42, config=config)
    assert first == second
    assert first["random_count"] == 5
    assert 10 <= first["sample_count"] <= 15

    time = np.linspace(100.0, 127.0, 1_000)
    trials = injection_trials(
        time,
        tic_id=1,
        noise_ppm=100.0,
        max_period_days=20.0,
        seed=42,
        config=config,
    )
    assert len(trials) == 40
    assert sum(trial.phase_class == "random" for trial in trials) == 20
    assert sum(trial.phase_class == "segment_edge" for trial in trials) == 20
    assert trials == injection_trials(
        time,
        tic_id=1,
        noise_ppm=100.0,
        max_period_days=20.0,
        seed=42,
        config=config,
    )


def test_three_hour_noise_scales_white_cadence_noise() -> None:
    rng = np.random.default_rng(4)
    time = np.arange(20_000) / (24 * 30)
    flux = 1.0 + rng.normal(0.0, 1_000e-6, time.size)
    noise = three_hour_noise_ppm(time, flux)
    assert 90 < noise < 130


def test_release_summary_fails_an_intentionally_broken_null() -> None:
    injection = {
        "recovered": True,
        "phase_class": "random",
        "period_grid_value_days": 1.0,
        "period_bin": 0,
        "depth_multiplier": 2.0,
        "realized_depth_ppm": 1_000.0,
        "recovered_depth_ppm": 1_010.0,
    }
    baseline = [{"survivor": index == 0} for index in range(500)]
    clean_null = [{"survivor": False} for _ in range(500)]
    summary = summarize_calibration(
        [injection],
        baseline_rows=baseline,
        inverted_rows=clean_null,
        scrambled_rows=clean_null,
        maximum_epoch_enrichment=1.1,
    )
    # Edge evidence is absent, so the release cannot pass accidentally.
    assert not summary["release_gate_passes"]
    broken = summarize_calibration(
        [injection],
        baseline_rows=baseline,
        inverted_rows=[{"survivor": True}, *clean_null[1:]],
        scrambled_rows=clean_null,
        maximum_epoch_enrichment=1.1,
    )
    assert not broken["gates"]["inverted_survivor_rate"]["passes"]


def test_known_recovery_exposes_only_expected_period() -> None:
    catalog = {
        "tic_id": 1,
        "tois": [
            {"pl_orbper": "3.0", "toi": "1.01"},
            {"pl_orbper": "7.0", "toi": "1.02"},
        ],
        "confirmed_planets": [
            {"pl_orbper": "3.001", "pl_name": "target b"},
            {"pl_orbper": "12.0", "pl_name": "target d"},
        ],
    }
    filtered = catalog_without_expected_signal(
        catalog, expected_period_days=3.0
    )
    assert [row["pl_orbper"] for row in filtered["tois"]] == ["7.0"]
    assert [row["pl_orbper"] for row in filtered["confirmed_planets"]] == ["12.0"]
