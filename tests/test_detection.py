import numpy as np

from exohunt.detection import (
    binned_phase_curve,
    harmonic_diagnostics,
    inject_box_transit,
    mask_periodic_events,
    search_transits,
)


def test_binned_phase_curve_preserves_actual_transit_shape_compactly():
    time = np.linspace(0.0, 20.0, 10_000, endpoint=False)
    period = 2.0
    transit_time = 0.5
    phase = ((time - transit_time + period / 2) % period) / period - 0.5
    flux = np.ones_like(time)
    flux[np.abs(phase) < 0.015] -= 0.004

    curve = binned_phase_curve(time, flux, period, transit_time, bin_count=80)

    assert curve["source"] == "actual normalized residual TESS photometry"
    assert len(curve["phase"]) <= 80
    assert len(curve["phase"]) == len(curve["median_residual_flux_ppm"])
    assert len(curve["phase"]) == len(curve["scatter_ppm"])
    assert sum(curve["count"]) == curve["measurements_in_range"]
    center_index = int(np.argmin(np.abs(curve["phase"])))
    assert curve["median_residual_flux_ppm"][center_index] < -3_500


def test_recovers_synthetic_transit():
    rng = np.random.default_rng(42)
    time = np.arange(0.0, 27.0, 20.0 / (24 * 60))
    period = 3.25
    transit_time = 0.7
    duration_days = 3.0 / 24.0
    phase_time = ((time - transit_time + period / 2) % period) - period / 2
    flux = 1.0 + rng.normal(0.0, 8e-4, time.size)
    flux[np.abs(phase_time) < duration_days / 2] -= 0.01

    result, _ = search_transits(
        time,
        flux,
        min_period_days=1.0,
        max_period_days=6.0,
        durations_hours=np.array([2.5, 3.0, 3.5]),
    )

    assert abs(result.period_days - period) < 0.03
    assert abs(result.depth_ppm - 10_000) < 2_000
    assert result.observed_transits >= 7
    assert result.depth_snr > 20


def test_masking_known_planet_reveals_second_planet():
    rng = np.random.default_rng(7)
    time = np.arange(0.0, 80.0, 20.0 / (24 * 60))
    flux = 1.0 + rng.normal(0.0, 5e-4, time.size)

    known_period = 3.0
    known_epoch_btjd = 0.5
    known_phase = ((time - known_epoch_btjd + known_period / 2) % known_period) - known_period / 2
    flux[np.abs(known_phase) < (3.0 / 24.0) / 2] -= 0.012

    second_period = 7.0
    second_epoch = 1.2
    second_phase = ((time - second_epoch + second_period / 2) % second_period) - second_period / 2
    flux[np.abs(second_phase) < (2.0 / 24.0) / 2] -= 0.006

    first, _ = search_transits(time, flux, min_period_days=1.0, max_period_days=10.0)
    assert abs(first.period_days - known_period) < 0.03

    cleaned_time, cleaned_flux, records = mask_periodic_events(
        time,
        flux,
        [
            {
                "label": "known",
                "period_days": known_period,
                # Full BJD checks conversion to the BTJD time base.
                "epoch_bjd": known_epoch_btjd + 2_457_000.0,
                "duration_hours": 3.0,
            }
        ],
        width_factor=1.5,
    )
    second, _ = search_transits(
        cleaned_time, cleaned_flux, min_period_days=1.0, max_period_days=10.0
    )
    assert records[0]["removed_measurements"] > 0
    assert abs(second.period_days - second_period) < 0.05


def test_harmonic_diagnostics_finds_double_period_peak():
    periods = np.linspace(1.0, 20.0, 10_000)
    power = np.exp(-0.5 * ((periods - 8.0) / 0.03) ** 2)
    power += 0.8 * np.exp(-0.5 * ((periods - 16.0) / 0.04) ** 2)
    checks = harmonic_diagnostics(periods, power, 8.0)
    double = next(row for row in checks if row["relation_to_strongest"] == "double")
    assert double["plausible_alias"] is True
    assert abs(double["nearby_peak_period_days"] - 16.0) < 0.02
    assert 0.75 < double["relative_power"] < 0.85


def test_injected_transit_is_recovered():
    rng = np.random.default_rng(23)
    time = np.arange(0.0, 30.0, 20.0 / (24 * 60))
    flux = 1.0 + rng.normal(0.0, 4e-4, time.size)
    injected_flux, mask, event_count = inject_box_transit(
        time,
        flux,
        period_days=4.2,
        transit_time=0.9,
        duration_hours=2.0,
        depth_ppm=4_000,
    )
    result, _ = search_transits(
        time, injected_flux, min_period_days=1.0, max_period_days=8.0
    )
    assert np.count_nonzero(mask) > 20
    assert event_count >= 7
    assert abs(result.period_days - 4.2) < 0.04
