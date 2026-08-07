"""T8: fit the transit, then ask whether the fit describes a possible star.

MASTER_PLAN.md section 4.6. The fit itself is the easy half -- batman plus
emcee, both lightweight and Windows-friendly. The half that catches false
positives is what the posteriors *imply*:

**Stellar density is the discriminator.** A transit fit returns a scaled
semi-major axis, and (a/R*) with the period gives the mean density of the star
being transited, independent of any catalogue. The same light curve can be fit
by a small planet crossing a dwarf or by a grazing binary crossing a giant --
those solutions differ by orders of magnitude in density, and nothing else in
the light curve separates them. Comparing the fitted density against the one
implied by the catalogued mass and radius is therefore not a nicety; it is the
check that says which of the two fits you actually have.

**A posterior is only reported when the chain mixed.** An unconverged fit
produces a number with an error bar attached to nothing, and it is
indistinguishable on inspection from a converged one. This module returns
``not_run`` in that case rather than a plausible-looking interval, which is
what lets ``packet.py`` refuse to assemble around it.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Sequence

import numpy as np

from .config import CURRENT_TRANSIT_FIT, TransitFitConfig

# Newton's constant in solar densities: rho_star / rho_sun =
# (3 * pi / (G * P^2)) * (a/R*)^3, with P in days. The numeric factor collects
# the unit conversion so no call site carries a bare constant.
_DENSITY_FACTOR_SOLAR_DAYS = 3.0 * math.pi / (
    6.674_30e-11 * (86_400.0**2) * 1_408.0
)


@dataclass(frozen=True, slots=True)
class StellarParameters:
    """What the catalogue says about the star being transited."""

    radius_solar: float | None = None
    mass_solar: float | None = None
    teff_k: float | None = None

    def mean_density_solar(self) -> float | None:
        """Mean density in solar units, from catalogued mass and radius."""

        if not self.radius_solar or not self.mass_solar or self.radius_solar <= 0:
            return None
        return float(self.mass_solar) / float(self.radius_solar) ** 3


def stellar_density_from_transit(
    a_over_rs: float, period_days: float
) -> float | None:
    """Mean stellar density implied by the transit geometry alone.

    Seager & Mallen-Ornelas: the transit shape constrains a/R*, and Kepler's
    third law turns that plus the period into the density of the star, with no
    reference to any catalogue. That independence is the entire point -- it is
    what lets the fit contradict the catalogue and mean something.
    """

    if not a_over_rs or not period_days or a_over_rs <= 0 or period_days <= 0:
        return None
    return _DENSITY_FACTOR_SOLAR_DAYS * (float(a_over_rs) ** 3) / (
        float(period_days) ** 2
    )


def density_consistency(
    fitted_density_solar: float | None,
    stellar: StellarParameters,
    *,
    fitted_uncertainty_solar: float | None = None,
    config: TransitFitConfig | None = None,
) -> dict[str, Any]:
    """Does the fit require a star the catalogue does not describe?"""

    settings = config or CURRENT_TRANSIT_FIT
    catalogued = stellar.mean_density_solar()
    if fitted_density_solar is None:
        return {"state": "not_run", "reason": "no fitted density available"}
    if (
        fitted_density_solar < settings.minimum_physical_density_solar
        or fitted_density_solar > settings.maximum_physical_density_solar
    ):
        return {
            "state": "measured",
            "fitted_density_solar": fitted_density_solar,
            "catalogued_density_solar": catalogued,
            "verdict": "unphysical_fitted_density",
            "note": (
                "the transit geometry requires a star outside any plausible "
                "main-sequence density, which is a blend or a bad fit rather "
                "than a planet"
            ),
        }
    if catalogued is None:
        return {
            "state": "measured",
            "fitted_density_solar": fitted_density_solar,
            "catalogued_density_solar": None,
            "verdict": "no_catalogued_density_to_compare",
        }
    # Without a fitted uncertainty the comparison is a ratio, not a
    # significance, and it is reported as such rather than dressed up.
    if not fitted_uncertainty_solar or fitted_uncertainty_solar <= 0:
        ratio = fitted_density_solar / catalogued
        return {
            "state": "measured",
            "fitted_density_solar": fitted_density_solar,
            "catalogued_density_solar": catalogued,
            "ratio": ratio,
            "significance": None,
            "verdict": (
                "consistent_without_uncertainty"
                if 1 / 3 <= ratio <= 3
                else "density_mismatch_without_uncertainty"
            ),
        }
    significance = abs(fitted_density_solar - catalogued) / fitted_uncertainty_solar
    return {
        "state": "measured",
        "fitted_density_solar": fitted_density_solar,
        "catalogued_density_solar": catalogued,
        "ratio": fitted_density_solar / catalogued,
        "significance": significance,
        "tolerance_sigma": settings.density_agreement_sigma,
        "verdict": (
            "density_mismatch"
            if significance > settings.density_agreement_sigma
            else "consistent_with_catalogued_star"
        ),
        "policy_version": settings.policy_version,
    }


def transit_model(
    time: Sequence[float],
    *,
    period_days: float,
    epoch: float,
    rp_over_rs: float,
    a_over_rs: float,
    inclination_deg: float,
    u1: float,
    u2: float,
) -> np.ndarray:
    """Quadratic-limb-darkened transit model (batman)."""

    import batman

    params = batman.TransitParams()
    params.t0 = float(epoch)
    params.per = float(period_days)
    params.rp = float(rp_over_rs)
    params.a = float(a_over_rs)
    params.inc = float(inclination_deg)
    params.ecc = 0.0
    params.w = 90.0
    params.u = [float(u1), float(u2)]
    params.limb_dark = "quadratic"
    model = batman.TransitModel(params, np.asarray(time, dtype=float))
    return np.asarray(model.light_curve(params), dtype=float)


def _log_probability(
    theta: np.ndarray,
    time: np.ndarray,
    flux: np.ndarray,
    flux_err: np.ndarray,
    period_days: float,
    settings: TransitFitConfig,
) -> float:
    epoch, rp, a_over_rs, inclination, u1, u2 = theta
    if not (0.0 < rp < 0.5):
        return -np.inf
    if not (1.5 < a_over_rs < 200.0):
        return -np.inf
    if not (60.0 <= inclination <= 90.0):
        return -np.inf
    low1, high1 = settings.limb_darkening_u1_range
    low2, high2 = settings.limb_darkening_u2_range
    if not (low1 <= u1 <= high1) or not (low2 <= u2 <= high2):
        return -np.inf
    # A grazing geometry that never actually occults is not a transit fit.
    if a_over_rs * math.cos(math.radians(inclination)) > 1.0 + rp:
        return -np.inf
    try:
        model = transit_model(
            time,
            period_days=period_days,
            epoch=epoch,
            rp_over_rs=rp,
            a_over_rs=a_over_rs,
            inclination_deg=inclination,
            u1=u1,
            u2=u2,
        )
    except Exception:  # noqa: BLE001 - a sampler must not die on one proposal
        return -np.inf
    residual = (flux - model) / flux_err
    return float(-0.5 * np.sum(residual**2))


def fit_transit(
    time: Sequence[float],
    flux: Sequence[float],
    flux_err: Sequence[float],
    *,
    period_days: float,
    epoch: float,
    rp_over_rs: float = 0.05,
    a_over_rs: float = 15.0,
    stellar: StellarParameters | None = None,
    config: TransitFitConfig | None = None,
    seed: int = 0,
) -> dict[str, Any]:
    """Sample the transit posterior, and say so when it did not converge."""

    settings = config or CURRENT_TRANSIT_FIT
    try:
        import emcee
    except ModuleNotFoundError:
        return {
            "state": "not_run",
            "reason": "emcee is not installed; posteriors cannot be sampled",
        }

    time_array = np.asarray(time, dtype=float)
    flux_array = np.asarray(flux, dtype=float)
    error_array = np.asarray(flux_err, dtype=float)
    if time_array.size < 20:
        return {"state": "not_run", "reason": "too few cadences to fit"}

    start = np.array(
        [
            epoch,
            rp_over_rs,
            a_over_rs,
            89.0,
            float(np.mean(settings.limb_darkening_u1_range)),
            float(np.mean(settings.limb_darkening_u2_range)),
        ]
    )
    # emcee's stretch move needs at least two walkers per dimension, and it
    # raises rather than degrading. A pipeline stage must not die on a
    # configuration mistake, so this is reported the same way every other
    # unproducible posterior is.
    if settings.walkers < 2 * start.size:
        return {
            "state": "not_run",
            "reason": (
                f"{settings.walkers} walkers is fewer than the {2 * start.size} "
                f"required for {start.size} fitted parameters"
            ),
        }

    generator = np.random.default_rng(seed)
    scatter = np.array([0.002, 0.002, 0.20, 0.10, 0.01, 0.01])
    initial = start + scatter * generator.normal(size=(settings.walkers, start.size))
    initial[:, 1] = np.abs(initial[:, 1])
    initial[:, 3] = np.clip(initial[:, 3], 60.0, 90.0)

    sampler = emcee.EnsembleSampler(
        settings.walkers,
        start.size,
        _log_probability,
        args=(time_array, flux_array, error_array, period_days, settings),
    )
    state = sampler.run_mcmc(initial, settings.burn_in_steps, progress=False)
    sampler.reset()
    sampler.run_mcmc(state, settings.production_steps, progress=False)

    chain = sampler.get_chain(flat=True)
    if chain.size == 0:
        return {"state": "not_run", "reason": "sampler produced no samples"}

    # Convergence is checked, not assumed. An unconverged chain yields an
    # interval that looks exactly like a converged one on inspection.
    try:
        autocorr = float(np.nanmax(sampler.get_autocorr_time(quiet=True)))
    except Exception:  # noqa: BLE001
        autocorr = float("nan")
    converged = bool(
        np.isfinite(autocorr)
        and settings.production_steps
        >= settings.max_autocorrelation_ratio * autocorr
    )

    names = ("epoch", "rp_over_rs", "a_over_rs", "inclination_deg", "u1", "u2")
    posteriors: dict[str, Any] = {}
    for index, name in enumerate(names):
        low, median, high = np.percentile(chain[:, index], [16, 50, 84])
        posteriors[name] = {
            "median": float(median),
            "minus": float(median - low),
            "plus": float(high - median),
        }

    density_samples = np.array(
        [
            stellar_density_from_transit(value, period_days) or np.nan
            for value in chain[:, 2]
        ]
    )
    density_median = float(np.nanmedian(density_samples))
    density_error = float(np.nanstd(density_samples))

    if not converged:
        return {
            "state": "not_run",
            "reason": (
                "the chain did not mix: production steps are fewer than "
                f"{settings.max_autocorrelation_ratio}x the autocorrelation "
                f"time ({autocorr:.1f})"
            ),
            "autocorrelation_time": autocorr,
            "diagnostic_posteriors": posteriors,
        }

    result: dict[str, Any] = {
        "state": "measured",
        "posteriors": posteriors,
        "fitted_density_solar": density_median,
        "fitted_density_uncertainty_solar": density_error,
        "autocorrelation_time": autocorr,
        "samples": int(chain.shape[0]),
        "acceptance_fraction": float(np.mean(sampler.acceptance_fraction)),
        "policy_version": settings.policy_version,
    }
    if stellar is not None:
        result["density_consistency"] = density_consistency(
            density_median,
            stellar,
            fitted_uncertainty_solar=density_error,
            config=settings,
        )
    return result


def sed_dwarf_check(
    *,
    gaia_g: float | None,
    bp_rp: float | None,
    parallax_mas: float | None,
    two_mass_k: float | None = None,
) -> dict[str, Any]:
    """Giant impostor check from the colour-magnitude position.

    A giant and a dwarf of the same colour differ by many magnitudes in
    absolute brightness, so the parallax settles which one is being observed.
    This matters because a transit depth is a *ratio*: the same fractional dip
    on a giant is a stellar companion, not a planet.
    """

    if gaia_g is None or bp_rp is None or not parallax_mas or parallax_mas <= 0:
        return {
            "state": "not_run",
            "reason": "needs Gaia G, BP-RP and a positive parallax",
        }
    distance_pc = 1000.0 / float(parallax_mas)
    absolute_g = float(gaia_g) - 5.0 * math.log10(distance_pc / 10.0)
    # Main-sequence ridge for TESS-relevant colours, as a linear guide. A star
    # far brighter than the ridge at its colour is evolved.
    expected_main_sequence = 2.1 + 4.4 * float(bp_rp)
    excess = expected_main_sequence - absolute_g
    if excess > 2.5:
        verdict = "likely_evolved_star"
    elif excess > 1.0:
        verdict = "possibly_evolved_star"
    else:
        verdict = "consistent_with_main_sequence"
    return {
        "state": "measured",
        "distance_pc": distance_pc,
        "absolute_g": absolute_g,
        "expected_main_sequence_g": expected_main_sequence,
        "magnitudes_above_main_sequence": excess,
        "two_mass_k": two_mass_k,
        "verdict": verdict,
    }


@dataclass(frozen=True, slots=True)
class FitSummary:
    """The compact block `packet.py` stores as its `fit_posteriors` section."""

    state: str
    posteriors: dict[str, Any] | None = None
    density_consistency: dict[str, Any] | None = None
    sed: dict[str, Any] | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}
