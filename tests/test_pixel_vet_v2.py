"""Pixel vetting v2 (MASTER_PLAN.md section 4.4).

Every scene here is synthetic, so the truth is known by construction: a
transit is placed on a chosen source and the vetting is asked which source it
belongs to. That is the only way to test a localizer -- on real data nobody
knows the answer, which is precisely why the stage exists.
"""

from __future__ import annotations

import numpy as np
import pytest

from exohunt import pixel
from exohunt.config import PixelVetConfig

PERIOD = 3.0
EPOCH = 1.0
DURATION_HOURS = 4.0
SHAPE = (11, 11)


def _scene(
    *,
    host_row: float,
    host_column: float,
    depth: float,
    host_flux: float = 1000.0,
    other: tuple[float, float, float] | None = None,
    noise: float = 0.0,
    seed: int = 7,
    cadences: int = 900,
    psf_sigma: float = 0.7,
) -> tuple[np.ndarray, np.ndarray]:
    """Build a pixel cube with a Gaussian PSF per source and one dimming host."""

    time = np.linspace(0.0, 9.0, cadences)
    in_mask, _ = pixel.transit_cadence_masks(time, PERIOD, EPOCH, DURATION_HOURS)
    rows, columns = np.indices(SHAPE)

    def psf(row: float, column: float) -> np.ndarray:
        return np.exp(
            -(((rows - row) ** 2 + (columns - column) ** 2) / (2 * psf_sigma**2))
        )

    host_psf = psf(host_row, host_column)
    cube = np.repeat((host_flux * host_psf)[None, :, :], cadences, axis=0)
    cube[in_mask] -= (depth * host_flux) * host_psf

    if other is not None:
        other_row, other_column, other_flux = other
        cube = cube + (other_flux * psf(other_row, other_column))[None, :, :]

    if noise > 0:
        generator = np.random.default_rng(seed)
        cube = cube + generator.normal(0.0, noise, size=cube.shape)
    return time, cube


TARGET = (5.0, 5.0)
NEIGHBOUR = (5.0, 8.0)


def test_on_target_transit_does_not_grow_with_aperture() -> None:
    """A real on-target signal dilutes as the mask widens; it never deepens."""

    time, cube = _scene(host_row=TARGET[0], host_column=TARGET[1], depth=0.02)
    result = pixel.aperture_depth_curve(
        time,
        cube,
        period_days=PERIOD,
        transit_time=EPOCH,
        duration_hours=DURATION_HOURS,
        center_row=TARGET[0],
        center_column=TARGET[1],
    )
    assert result["verdict"] == "consistent_with_on_target"
    assert result["growth_fraction"] <= 0.0
    assert len(result["depths"]) == 3


def test_a_neighbours_eclipse_deepens_as_the_aperture_reaches_it() -> None:
    """The discriminator the plan names, on a scene where truth is known.

    The dimming source sits three pixels away. The small aperture sees the
    steady target and almost none of it; the wide aperture reaches the
    neighbour, and the measured depth grows.
    """

    time, cube = _scene(
        host_row=NEIGHBOUR[0],
        host_column=NEIGHBOUR[1],
        depth=0.30,
        host_flux=900.0,
        other=(TARGET[0], TARGET[1], 1000.0),
    )
    result = pixel.aperture_depth_curve(
        time,
        cube,
        period_days=PERIOD,
        transit_time=EPOCH,
        duration_hours=DURATION_HOURS,
        center_row=TARGET[0],
        center_column=TARGET[1],
    )
    assert result["verdict"] == "contaminating_neighbour"
    # Bounded statistic: approaches +1 as the signal leaves the target's own
    # aperture entirely, rather than exploding on a near-zero denominator.
    assert 0.5 <= result["growth_fraction"] <= 1.0


def test_growth_thresholds_are_configurable() -> None:
    time, cube = _scene(
        host_row=NEIGHBOUR[0],
        host_column=NEIGHBOUR[1],
        depth=0.30,
        host_flux=900.0,
        other=(TARGET[0], TARGET[1], 1000.0),
    )
    # Growth is bounded in [-1, 1], so a threshold above 1 can never fire.
    lenient = PixelVetConfig(
        aperture_growth_kill_fraction=1.5, aperture_growth_flag_fraction=1.2
    )
    result = pixel.aperture_depth_curve(
        time,
        cube,
        period_days=PERIOD,
        transit_time=EPOCH,
        duration_hours=DURATION_HOURS,
        center_row=TARGET[0],
        center_column=TARGET[1],
        config=lenient,
    )
    assert result["verdict"] == "consistent_with_on_target"


def test_centroid_carries_an_uncertainty_that_grows_with_noise() -> None:
    """A distance without an error bar is not a localization."""

    quiet_time, quiet = _scene(
        host_row=TARGET[0], host_column=TARGET[1], depth=0.05, noise=1.0
    )
    noisy_time, noisy = _scene(
        host_row=TARGET[0], host_column=TARGET[1], depth=0.05, noise=40.0
    )
    small = PixelVetConfig(bootstrap_samples=48)
    quiet_result = pixel.bootstrap_centroid(
        quiet_time,
        quiet,
        period_days=PERIOD,
        transit_time=EPOCH,
        duration_hours=DURATION_HOURS,
        config=small,
    )
    noisy_result = pixel.bootstrap_centroid(
        noisy_time,
        noisy,
        period_days=PERIOD,
        transit_time=EPOCH,
        duration_hours=DURATION_HOURS,
        config=small,
    )
    assert quiet_result["verdict"] == "measured"
    assert quiet_result["centroid_row"] == pytest.approx(TARGET[0], abs=0.3)
    assert quiet_result["centroid_column"] == pytest.approx(TARGET[1], abs=0.3)
    assert noisy_result["row_uncertainty"] > quiet_result["row_uncertainty"]


def test_bootstrap_is_deterministic_in_its_seed() -> None:
    time, cube = _scene(
        host_row=TARGET[0], host_column=TARGET[1], depth=0.05, noise=5.0
    )
    kwargs = dict(
        period_days=PERIOD,
        transit_time=EPOCH,
        duration_hours=DURATION_HOURS,
        config=PixelVetConfig(bootstrap_samples=32),
    )
    first = pixel.bootstrap_centroid(time, cube, seed=11, **kwargs)
    again = pixel.bootstrap_centroid(time, cube, seed=11, **kwargs)
    different = pixel.bootstrap_centroid(time, cube, seed=12, **kwargs)
    assert first["centroid_row"] == again["centroid_row"]
    assert first["row_uncertainty"] == again["row_uncertainty"]
    assert different["centroid_row"] != first["centroid_row"]


def test_offset_significance_uses_the_error_not_a_fixed_distance() -> None:
    """The same distance is decisive or meaningless depending on its error."""

    tight = {
        "centroid_row": 5.0,
        "centroid_column": 5.6,
        "row_uncertainty": 0.05,
        "column_uncertainty": 0.05,
    }
    loose = {
        "centroid_row": 5.0,
        "centroid_column": 5.6,
        "row_uncertainty": 0.8,
        "column_uncertainty": 0.8,
    }
    tight_result = pixel.localization_offset(tight, 5.0, 5.0)
    loose_result = pixel.localization_offset(loose, 5.0, 5.0)
    assert tight_result["offset_pixels"] == pytest.approx(
        loose_result["offset_pixels"]
    )
    assert tight_result["verdict"] == "off_target"
    assert loose_result["verdict"] == "consistent_with_on_target"
    assert tight_result["significance"] > loose_result["significance"]


def test_offset_without_an_uncertainty_says_so() -> None:
    result = pixel.localization_offset(
        {"centroid_row": 5.0, "centroid_column": 6.0}, 5.0, 5.0
    )
    assert result["verdict"] == "measured_without_uncertainty"
    assert result["offset_pixels"] == pytest.approx(1.0)
    assert pixel.localization_offset(
        {"centroid_row": float("nan"), "centroid_column": float("nan")}, 5.0, 5.0
    )["verdict"] == "not_evaluable"


def test_a_centroid_that_wanders_between_sectors_is_caught() -> None:
    """Every sector inside tolerance, yet mutually inconsistent.

    This is the case a per-sector distance check passes and a blend fails: no
    single sector is more than a pixel out, but they do not agree with each
    other anywhere near their own errors.
    """

    wandering = [
        {
            "centroid_row": 5.0,
            "centroid_column": column,
            "row_uncertainty": 0.03,
            "column_uncertainty": 0.03,
        }
        for column in (5.0, 5.5, 4.6)
    ]
    result = pixel.sector_centroid_consistency(wandering)
    assert result["verdict"] == "centroid_wanders_between_sectors"
    assert result["worst_reduced_chi2"] > 3.0
    assert result["sectors"] == 3

    steady = [
        {
            "centroid_row": 5.0,
            "centroid_column": column,
            "row_uncertainty": 0.05,
            "column_uncertainty": 0.05,
        }
        for column in (5.00, 5.02, 4.98)
    ]
    assert (
        pixel.sector_centroid_consistency(steady)["verdict"]
        == "consistent_across_sectors"
    )


def test_consistency_refuses_to_answer_from_one_sector() -> None:
    single = [
        {
            "centroid_row": 5.0,
            "centroid_column": 5.0,
            "row_uncertainty": 0.05,
            "column_uncertainty": 0.05,
        }
    ]
    result = pixel.sector_centroid_consistency(single)
    assert result["verdict"] == "not_evaluable"
    assert "at least 2" in result["reason"]
    # Sectors with no measured uncertainty cannot vote either.
    assert (
        pixel.sector_centroid_consistency(
            [{"centroid_row": 5.0, "centroid_column": 5.0}] * 3
        )["verdict"]
        == "not_evaluable"
    )


def test_neighbour_extraction_reassigns_the_host() -> None:
    """The decisive test: which object in the pixel is actually dimming."""

    time, cube = _scene(
        host_row=NEIGHBOUR[0],
        host_column=NEIGHBOUR[1],
        depth=0.30,
        host_flux=900.0,
        other=(TARGET[0], TARGET[1], 1000.0),
    )
    result = pixel.neighbour_transit_extraction(
        time,
        cube,
        period_days=PERIOD,
        transit_time=EPOCH,
        duration_hours=DURATION_HOURS,
        candidates=[
            {"identifier": "target", "row": TARGET[0], "column": TARGET[1], "is_target": True},
            {"identifier": "neighbour", "row": NEIGHBOUR[0], "column": NEIGHBOUR[1]},
        ],
    )
    assert result["verdict"] == "signal_belongs_to_neighbour"
    assert result["best_host"] == "neighbour"
    assert result["candidates"][0]["identifier"] == "neighbour"
    assert result["candidates"][0]["depth"] > result["target_depth"]


def test_neighbour_extraction_keeps_an_on_target_signal() -> None:
    time, cube = _scene(
        host_row=TARGET[0],
        host_column=TARGET[1],
        depth=0.10,
        other=(NEIGHBOUR[0], NEIGHBOUR[1], 900.0),
    )
    result = pixel.neighbour_transit_extraction(
        time,
        cube,
        period_days=PERIOD,
        transit_time=EPOCH,
        duration_hours=DURATION_HOURS,
        candidates=[
            {"identifier": "target", "row": TARGET[0], "column": TARGET[1], "is_target": True},
            {"identifier": "neighbour", "row": NEIGHBOUR[0], "column": NEIGHBOUR[1]},
        ],
    )
    assert result["verdict"] == "target_is_best_host"
    assert result["best_host"] == "target"


def test_short_series_are_refused_rather_than_guessed() -> None:
    time = np.linspace(0.0, 0.05, 12)
    cube = np.ones((12, *SHAPE))
    for call in (
        lambda: pixel.aperture_depth_curve(
            time,
            cube,
            period_days=PERIOD,
            transit_time=EPOCH,
            duration_hours=DURATION_HOURS,
            center_row=5.0,
            center_column=5.0,
        ),
        lambda: pixel.bootstrap_centroid(
            time,
            cube,
            period_days=PERIOD,
            transit_time=EPOCH,
            duration_hours=DURATION_HOURS,
        ),
    ):
        with pytest.raises(ValueError, match="Too few"):
            call()


def test_v1_difference_image_still_works() -> None:
    """v2 is additive; the existing localizer is untouched."""

    time, cube = _scene(host_row=TARGET[0], host_column=TARGET[1], depth=0.05)
    result = pixel.difference_image(time, cube, PERIOD, EPOCH, DURATION_HOURS)
    assert result["centroid_row"] == pytest.approx(TARGET[0], abs=0.2)
    assert result["centroid_column"] == pytest.approx(TARGET[1], abs=0.2)
