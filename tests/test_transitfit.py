"""T8: the transit fit, and whether its posteriors describe a possible star."""

from __future__ import annotations

import numpy as np
import pytest

from exohunt import transitfit as t8
from exohunt.config import TransitFitConfig

SUN_EARTH_A_OVER_RS = 1.495978707e11 / 6.957e8


def test_the_density_relation_reproduces_the_sun() -> None:
    """The physics anchor. If this drifts, every density verdict is wrong.

    Earth's orbit around the Sun is the one transit geometry whose answer is
    known exactly: a/R* = 1 AU / R_sun at P = 365.25 d must return one solar
    density.
    """

    density = t8.stellar_density_from_transit(SUN_EARTH_A_OVER_RS, 365.25)
    assert density == pytest.approx(1.0, rel=2e-3)
    # And it scales as (a/R*)^3 / P^2, not some other power.
    assert t8.stellar_density_from_transit(20.0, 3.0) == pytest.approx(
        8 * t8.stellar_density_from_transit(10.0, 3.0)
    )
    assert t8.stellar_density_from_transit(10.0, 6.0) == pytest.approx(
        t8.stellar_density_from_transit(10.0, 3.0) / 4
    )
    assert t8.stellar_density_from_transit(0.0, 3.0) is None
    assert t8.stellar_density_from_transit(10.0, 0.0) is None


def test_catalogued_density_is_mass_over_radius_cubed() -> None:
    sun = t8.StellarParameters(radius_solar=1.0, mass_solar=1.0)
    assert sun.mean_density_solar() == pytest.approx(1.0)
    m_dwarf = t8.StellarParameters(radius_solar=0.2, mass_solar=0.2)
    assert m_dwarf.mean_density_solar() == pytest.approx(25.0)
    assert t8.StellarParameters().mean_density_solar() is None


def test_a_fit_requiring_the_wrong_star_is_caught() -> None:
    """The check that separates a small planet on a dwarf from a binary.

    The same light curve admits both; only the density the geometry demands
    tells them apart.
    """

    m_dwarf = t8.StellarParameters(radius_solar=0.3, mass_solar=0.3, teff_k=3400)
    # A fit implying a Sun-like density around a catalogued M dwarf.
    result = t8.density_consistency(1.0, m_dwarf, fitted_uncertainty_solar=0.3)
    assert result["verdict"] == "density_mismatch"
    assert result["significance"] > 3.0

    consistent = t8.density_consistency(
        m_dwarf.mean_density_solar(), m_dwarf, fitted_uncertainty_solar=1.5
    )
    assert consistent["verdict"] == "consistent_with_catalogued_star"


def test_an_unphysical_density_is_rejected_whatever_the_catalogue_says() -> None:
    result = t8.density_consistency(
        5000.0, t8.StellarParameters(radius_solar=1.0, mass_solar=1.0)
    )
    assert result["verdict"] == "unphysical_fitted_density"
    tiny = t8.density_consistency(
        1e-4, t8.StellarParameters(radius_solar=1.0, mass_solar=1.0)
    )
    assert tiny["verdict"] == "unphysical_fitted_density"


def test_a_comparison_without_an_uncertainty_is_reported_as_a_ratio() -> None:
    """Not dressed up as a significance it cannot support."""

    star = t8.StellarParameters(radius_solar=1.0, mass_solar=1.0)
    close = t8.density_consistency(1.5, star)
    far = t8.density_consistency(30.0, star)
    assert close["significance"] is None
    assert close["verdict"] == "consistent_without_uncertainty"
    assert far["verdict"] == "density_mismatch_without_uncertainty"


def test_missing_inputs_say_not_run() -> None:
    assert t8.density_consistency(None, t8.StellarParameters())["state"] == "not_run"
    no_catalogue = t8.density_consistency(1.0, t8.StellarParameters())
    assert no_catalogue["verdict"] == "no_catalogued_density_to_compare"


def _synthetic(depth_rp: float = 0.08, noise: float = 2e-4, seed: int = 3):
    period, epoch = 3.0, 1.5
    time = np.linspace(0.0, 6.0, 900)
    truth = t8.transit_model(
        time,
        period_days=period,
        epoch=epoch,
        rp_over_rs=depth_rp,
        a_over_rs=12.0,
        inclination_deg=89.0,
        u1=0.35,
        u2=0.2,
    )
    flux = truth + np.random.default_rng(seed).normal(0.0, noise, time.size)
    return time, flux, np.full(time.size, noise), period, epoch


def test_the_model_actually_produces_a_transit() -> None:
    time, flux, _, _, _ = _synthetic()
    assert flux.min() < 0.995
    assert np.median(flux) == pytest.approx(1.0, abs=1e-3)


def test_the_fit_recovers_an_injected_transit() -> None:
    time, flux, error, period, epoch = _synthetic()
    settings = TransitFitConfig(
        walkers=16, burn_in_steps=300, production_steps=1500
    )
    result = t8.fit_transit(
        time,
        flux,
        error,
        period_days=period,
        epoch=epoch,
        rp_over_rs=0.08,
        a_over_rs=12.0,
        stellar=t8.StellarParameters(radius_solar=1.0, mass_solar=1.0),
        config=settings,
        seed=5,
    )
    if result["state"] == "not_run":
        # Convergence is genuinely not guaranteed at this budget; when it is
        # not reached the module must say so rather than emit an interval.
        assert "did not mix" in result["reason"]
        assert "diagnostic_posteriors" in result
        return
    assert result["posteriors"]["rp_over_rs"]["median"] == pytest.approx(
        0.08, abs=0.02
    )
    assert result["fitted_density_solar"] > 0
    assert "density_consistency" in result


def test_an_unconverged_chain_is_refused_not_reported() -> None:
    """An unconverged interval looks exactly like a converged one."""

    time, flux, error, period, epoch = _synthetic()
    starved = TransitFitConfig(
        walkers=12, burn_in_steps=1, production_steps=6, max_autocorrelation_ratio=50.0
    )
    result = t8.fit_transit(
        time, flux, error, period_days=period, epoch=epoch, config=starved, seed=1
    )
    assert result["state"] == "not_run"
    assert "mix" in result["reason"] or "autocorrelation" in result["reason"]


def test_too_few_walkers_is_refused_rather_than_raised() -> None:
    """emcee raises on this; a pipeline stage must report it instead."""

    time, flux, error, period, epoch = _synthetic()
    result = t8.fit_transit(
        time,
        flux,
        error,
        period_days=period,
        epoch=epoch,
        config=TransitFitConfig(walkers=8, burn_in_steps=1, production_steps=4),
    )
    assert result["state"] == "not_run"
    assert "walkers" in result["reason"]


def test_too_few_cadences_is_refused() -> None:
    result = t8.fit_transit(
        [0.0, 1.0], [1.0, 1.0], [1e-3, 1e-3], period_days=3.0, epoch=0.5
    )
    assert result["state"] == "not_run"
    assert "too few" in result["reason"].lower()


def test_the_sed_check_separates_a_giant_from_a_dwarf() -> None:
    """Depth is a ratio, so the same dip on a giant is a stellar companion."""

    dwarf = t8.sed_dwarf_check(gaia_g=11.0, bp_rp=1.2, parallax_mas=20.0)
    assert dwarf["verdict"] == "consistent_with_main_sequence"

    # Same colour, same apparent magnitude, ten times further away: the star
    # must be far more luminous, so it is not on the main sequence.
    giant = t8.sed_dwarf_check(gaia_g=11.0, bp_rp=1.2, parallax_mas=0.6)
    assert giant["verdict"] == "likely_evolved_star"
    assert giant["magnitudes_above_main_sequence"] > 2.5


def test_the_sed_check_refuses_without_a_parallax() -> None:
    for kwargs in (
        {"gaia_g": None, "bp_rp": 1.0, "parallax_mas": 10.0},
        {"gaia_g": 11.0, "bp_rp": None, "parallax_mas": 10.0},
        {"gaia_g": 11.0, "bp_rp": 1.0, "parallax_mas": 0.0},
    ):
        assert t8.sed_dwarf_check(**kwargs)["state"] == "not_run"


def test_the_summary_block_drops_empty_fields() -> None:
    summary = t8.FitSummary(state="not_run", reason="emcee missing").to_dict()
    assert summary == {"state": "not_run", "reason": "emcee missing"}

    full = t8.FitSummary(
        state="measured",
        posteriors={"rp_over_rs": {"median": 0.08}},
        density_consistency={"verdict": "consistent_with_catalogued_star"},
    ).to_dict()
    assert set(full) == {"state", "posteriors", "density_consistency"}


def test_the_summary_satisfies_the_packet_when_measured() -> None:
    from exohunt import packet as pk

    measured = t8.FitSummary(
        state="measured", posteriors={"rp_over_rs": {"median": 0.08}}
    ).to_dict()
    assert pk._is_measured(measured) is True
    assert pk._is_measured(t8.FitSummary(state="not_run").to_dict()) is False
