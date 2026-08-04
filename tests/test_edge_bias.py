"""Tests for the segment-edge trend-bias instrument.

The instrument's whole purpose is to say whether an edge error is estimator
bias or noise, so its own tests have to prove it cannot manufacture either
answer: a flat-trend control must report no bias, and a strongly curved trend
must report bias well above its null.
"""

from __future__ import annotations

import numpy as np
import pytest

from exohunt.edge_bias import (
    EdgeBiasSamples,
    biweight_trend_estimator,
    concatenate_samples,
    full_support_floor_ppm,
    measure_segment_edge_bias,
    profile_by_offset,
    savgol_trend_estimator,
    sufficient_guard_cadences,
)


def _moving_average_estimator(half: int):
    """A local mean with the same edge behaviour a real smoother has.

    Deep inside a series the window is symmetric; near an edge it is truncated
    to what exists. That is the property under measurement, and using a trivial
    estimator keeps these tests independent of scipy's and wotan's internals.
    """

    def estimate(time: np.ndarray, flux: np.ndarray) -> np.ndarray:
        values = np.asarray(flux, dtype=float)
        trend = np.empty(values.size, dtype=float)
        for index in range(values.size):
            low = max(0, index - half)
            high = min(values.size, index + half + 1)
            trend[index] = float(np.mean(values[low:high]))
        return trend

    return estimate


def _series(size: int = 600, cadence: float = 0.002) -> np.ndarray:
    return np.arange(size, dtype=float) * cadence


def test_bias_vanishes_once_the_window_regains_support() -> None:
    """A local estimator cannot see a truncation a half-window away."""

    time = _series()
    rng = np.random.default_rng(7)
    flux = 1.0 + 0.004 * np.sin(2 * np.pi * time / 0.35) + rng.normal(
        0.0, 2.0e-4, time.size
    )
    half = 20
    samples = measure_segment_edge_bias(
        time,
        flux,
        estimator=_moving_average_estimator(half),
        half_window_cadences=half,
        truncation_points=8,
    )

    deepest = samples.offset == samples.offset.max()
    assert samples.offset.max() == half
    assert np.allclose(samples.observed_ppm[deepest], 0.0, atol=1e-6)
    assert np.allclose(samples.detrended_null_ppm[deepest], 0.0, atol=1e-6)


def test_savgol_edge_bias_dominates_its_full_support_floor() -> None:
    """Regression against a degenerate trend, and a floor that must stay small.

    An earlier version passed ``break_tolerance = series length`` to lightkurve
    to stop it re-splitting an already contiguous segment. lightkurve also uses
    that parameter as a minimum segment length, so it silently replaced every
    trend with the segment median. The resulting error was identical at every
    offset -- including offsets with full support, where the true error is
    exactly zero.

    The floor is asserted to be small rather than zero because the shipping
    estimator genuinely is not local: its 3-sigma clip is global, so truncation
    can shift the trend even where the window is fully supported. That is a
    property to bound and report, not one to assume away.
    """

    pytest.importorskip("lightkurve")
    time = _series(size=600)
    rng = np.random.default_rng(23)
    flux = (
        1.0
        + 0.004 * np.sin(2 * np.pi * time / 0.35)
        + rng.normal(0.0, 2.0e-4, time.size)
    )
    window = 51
    samples = measure_segment_edge_bias(
        time,
        flux,
        estimator=savgol_trend_estimator(window),
        half_window_cadences=window // 2,
        truncation_points=24,
    )

    at_edge = samples.offset == 0
    edge_rms = float(np.sqrt(np.mean(samples.observed_ppm[at_edge] ** 2)))
    floor = full_support_floor_ppm(samples)
    assert edge_rms > 2 * floor
    # Around three quarters of truncations reach full support exactly; the leak
    # is occasional, and a degenerate median trend would leave none at all.
    deepest = samples.observed_ppm[samples.offset == samples.offset.max()]
    assert np.count_nonzero(deepest == 0.0) >= deepest.size // 2


def test_flat_trend_reports_no_excess_bias() -> None:
    """The control: pure noise about a constant must not look like bias.

    If the construction manufactured bias, this is where it would show, because
    there is no trend structure for a truncated window to mis-extrapolate.
    """

    time = _series()
    rng = np.random.default_rng(11)
    flux = 1.0 + rng.normal(0.0, 3.0e-4, time.size)
    half = 25
    samples = measure_segment_edge_bias(
        time,
        flux,
        estimator=_moving_average_estimator(half),
        half_window_cadences=half,
        truncation_points=16,
    )

    at_edge = samples.offset == 0
    observed = float(np.sqrt(np.mean(samples.observed_ppm[at_edge] ** 2)))
    null = float(np.sqrt(np.mean(samples.detrended_null_ppm[at_edge] ** 2)))
    # Same noise, same construction: the arms must agree to within their own
    # sampling scatter rather than by construction, so this is a loose bound.
    assert observed == pytest.approx(null, rel=0.35)
    assert sufficient_guard_cadences(samples, tolerance_ppm=observed) == 0


def test_curved_trend_produces_bias_above_its_null() -> None:
    """A truncated window mis-extrapolates real curvature; the null cannot."""

    time = _series()
    rng = np.random.default_rng(13)
    curvature = 0.01 * np.sin(2 * np.pi * time / 0.30)
    flux = 1.0 + curvature + rng.normal(0.0, 1.0e-4, time.size)
    half = 25
    samples = measure_segment_edge_bias(
        time,
        flux,
        estimator=_moving_average_estimator(half),
        half_window_cadences=half,
        truncation_points=16,
    )

    at_edge = samples.offset == 0
    observed = float(np.sqrt(np.mean(samples.observed_ppm[at_edge] ** 2)))
    null = float(np.sqrt(np.mean(samples.detrended_null_ppm[at_edge] ** 2)))
    white = float(np.sqrt(np.mean(samples.white_null_ppm[at_edge] ** 2)))
    # The null sits well above the white-noise floor because it divides by the
    # *fitted* trend: a moving average lags a sinusoid, so its own smoothing
    # residual keeps some curvature and the null absorbs part of the bias. The
    # separation is therefore a lower bound, and the bound still holds clearly.
    assert observed > 3 * null
    assert null > 3 * white

    rows = profile_by_offset(samples, offset_bins=6)
    assert rows
    assert rows[0]["excess_bias_ppm"] > rows[-1]["excess_bias_ppm"]
    assert rows[0]["support_fraction"] < rows[-1]["support_fraction"]


def test_guard_width_tightens_as_tolerance_falls() -> None:
    time = _series()
    rng = np.random.default_rng(17)
    flux = (
        1.0
        + 0.01 * np.sin(2 * np.pi * time / 0.30)
        + rng.normal(0.0, 1.0e-4, time.size)
    )
    half = 25
    samples = measure_segment_edge_bias(
        time,
        flux,
        estimator=_moving_average_estimator(half),
        half_window_cadences=half,
        truncation_points=16,
    )

    generous = sufficient_guard_cadences(samples, tolerance_ppm=1.0e6)
    strict = sufficient_guard_cadences(samples, tolerance_ppm=1.0)
    assert generous == 0
    assert strict is not None
    assert strict > generous


def test_unreachable_tolerance_reports_no_sufficient_guard() -> None:
    """No width qualifies when even full support carries more bias than asked.

    Returning the reach plus one says "not within what was measured" rather
    than silently offering the widest measured guard as if it passed.
    """

    samples = EdgeBiasSamples(
        offset=np.array([0, 1, 2]),
        observed_ppm=np.array([500.0, 500.0, 500.0]),
        detrended_null_ppm=np.array([1.0, 1.0, 1.0]),
        white_null_ppm=np.array([1.0, 1.0, 1.0]),
        half_window_cadences=2,
        truncation_points=1,
    )
    assert sufficient_guard_cadences(samples, tolerance_ppm=10.0) == 3


def test_savgol_estimator_matches_the_shipping_flatten() -> None:
    """The measured estimator must be the one production actually runs."""

    lk = pytest.importorskip("lightkurve")
    time = _series(size=400)
    rng = np.random.default_rng(19)
    flux = 1.0 + 0.003 * np.sin(2 * np.pi * time / 0.4) + rng.normal(
        0.0, 2.0e-4, time.size
    )
    window = 51

    curve = lk.LightCurve(time=time, flux=flux)
    _, expected = curve.flatten(
        window_length=window,
        break_tolerance=5,
        return_trend=True,
    )
    produced = savgol_trend_estimator(window)(time, flux)
    assert np.allclose(produced, np.asarray(expected.flux.value), rtol=0, atol=1e-12)
    # A degenerate break tolerance makes lightkurve return the segment median
    # for everything; the estimator must not be able to do that by accident.
    assert float(np.std(produced)) > 0.0


def test_biweight_estimator_returns_a_finite_trend() -> None:
    pytest.importorskip("wotan")
    time = _series(size=400)
    flux = 1.0 + 0.003 * np.sin(2 * np.pi * time / 0.4)
    trend = biweight_trend_estimator(0.2)(time, flux)
    assert trend.shape == flux.shape
    assert np.all(np.isfinite(trend))


def test_pooling_requires_a_common_half_window() -> None:
    def build(half: int) -> EdgeBiasSamples:
        return EdgeBiasSamples(
            offset=np.array([0]),
            observed_ppm=np.array([1.0]),
            detrended_null_ppm=np.array([1.0]),
            white_null_ppm=np.array([1.0]),
            half_window_cadences=half,
            truncation_points=1,
        )

    pooled = concatenate_samples([build(4), build(4)])
    assert pooled.offset.size == 2
    assert pooled.truncation_points == 2

    with pytest.raises(ValueError, match="share a half-window"):
        concatenate_samples([build(4), build(5)])
    with pytest.raises(ValueError, match="least one sample set"):
        concatenate_samples([])


def test_mismatched_sample_arrays_are_rejected() -> None:
    with pytest.raises(ValueError, match="same length"):
        EdgeBiasSamples(
            offset=np.array([0, 1]),
            observed_ppm=np.array([1.0]),
            detrended_null_ppm=np.array([1.0]),
            white_null_ppm=np.array([1.0]),
            half_window_cadences=2,
            truncation_points=1,
        )


def test_invalid_measurement_arguments_are_rejected() -> None:
    time = _series(size=200)
    flux = np.ones_like(time)
    estimator = _moving_average_estimator(5)

    with pytest.raises(ValueError, match="equal length"):
        measure_segment_edge_bias(
            time, flux[:-1], estimator=estimator, half_window_cadences=5
        )
    with pytest.raises(ValueError, match="Half window"):
        measure_segment_edge_bias(
            time, flux, estimator=estimator, half_window_cadences=0
        )
    with pytest.raises(ValueError, match="truncation point"):
        measure_segment_edge_bias(
            time,
            flux,
            estimator=estimator,
            half_window_cadences=5,
            truncation_points=0,
        )
    with pytest.raises(ValueError, match="Maximum offset"):
        measure_segment_edge_bias(
            time,
            flux,
            estimator=estimator,
            half_window_cadences=5,
            max_offset=0,
        )
    with pytest.raises(ValueError, match="too short"):
        measure_segment_edge_bias(
            time[:30],
            flux[:30],
            estimator=estimator,
            half_window_cadences=12,
        )


def test_invalid_estimator_and_summary_arguments_are_rejected() -> None:
    with pytest.raises(ValueError, match="three cadences"):
        savgol_trend_estimator(1)
    with pytest.raises(ValueError, match="odd"):
        savgol_trend_estimator(50)
    with pytest.raises(ValueError, match="Break tolerance"):
        savgol_trend_estimator(51, break_tolerance=0)
    with pytest.raises(ValueError, match="positive"):
        biweight_trend_estimator(0.0)
    with pytest.raises(ValueError, match="Break tolerance"):
        biweight_trend_estimator(1.0, break_tolerance_days=0.0)

    samples = EdgeBiasSamples(
        offset=np.array([0]),
        observed_ppm=np.array([1.0]),
        detrended_null_ppm=np.array([1.0]),
        white_null_ppm=np.array([1.0]),
        half_window_cadences=2,
        truncation_points=1,
    )
    with pytest.raises(ValueError, match="offset bin"):
        profile_by_offset(samples, offset_bins=0)
    with pytest.raises(ValueError, match="Tolerance"):
        sufficient_guard_cadences(samples, tolerance_ppm=0.0)


def test_summaries_handle_an_empty_sample_set() -> None:
    empty = EdgeBiasSamples(
        offset=np.array([], dtype=int),
        observed_ppm=np.array([]),
        detrended_null_ppm=np.array([]),
        white_null_ppm=np.array([]),
        half_window_cadences=2,
        truncation_points=0,
    )
    assert profile_by_offset(empty) == []
    assert sufficient_guard_cadences(empty, tolerance_ppm=1.0) is None
    assert np.isnan(full_support_floor_ppm(empty))
