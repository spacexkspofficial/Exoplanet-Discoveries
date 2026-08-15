"""Direct measurement of trend-model bias at segment edges.

`docs/p2/P2_EDGE_DIAGNOSTIC.md` closed two edge-recovery mechanisms and named what the
next one must do: *measure or avoid trend-model bias itself*. Both rejected arms
assumed the edge problem was a **support** problem -- too few samples -- and
tried to price it with a sample count (a support floor, an uncertainty inflation
`1/f**alpha`, a narrower guard). Neither worked, and the recorded diagnosis was
that the residual error is estimator *bias*, which does not shrink like
variance. That diagnosis was inferred from downstream survivor counts. It has
never been measured on the quantity itself.

This module measures it, with the estimator's own full-support answer as the
reference:

* Take a real, contiguous segment and fit the trend once over all of it. For a
  cadence deep in the interior that fit has symmetric support -- it is exactly
  the estimate the pipeline already trusts and ships.
* Truncate the segment so that same cadence now sits `k` samples from a
  synthetic segment start, and fit again with identical settings.
* ``bias(k) = trend_truncated - trend_full`` at that cadence, in ppm of relative
  flux, so it is directly comparable to a transit depth.

The comparison is **paired** at the level of a single cadence of a single star:
the flux, the noise realization and the stellar variability are common to both
fits and cancel exactly. Correction 24 showed that pairing is what gives an
edge comparison any resolving power, and this is the same idea applied one
stage earlier, to the estimator rather than to the survivors.

**Separating bias from variance.** Two nulls do the work an analytic variance
law cannot do for a sigma-clipped polynomial smoother, because both are driven
through the identical code path as the observed arm:

* the **detrended null** divides the segment by its own fitted trend, so its
  true trend is a constant. Every local smoother fits a constant without bias
  at any offset, so whatever edge error remains is produced by the noise --
  including its real correlation structure.
* the **white null** additionally permutes those residuals, destroying
  correlation while preserving the noise amplitude exactly.

**The white null is the one that decides the question.** The rejected
mechanisms priced the edge with an uncertainty inflation ``1/f**alpha``, and an
uncertainty is a *variance*. The white null is exactly the variance term: same
noise amplitude, no structure for a truncated window to mis-extrapolate. Excess
of observed over it is therefore precisely the part of the edge error that no
uncertainty inflation can price, which is the claim `docs/p2/P2_EDGE_DIAGNOSTIC.md`
made from downstream survivor counts and never measured directly.

The detrended null is reported alongside it as a deliberately conservative
variant, not as the primary control. It divides by the *fitted* trend rather
than the true one, so everything the estimator failed to track stays in the
null and carries real structure with it. Measured on cached Sector 100
photometry, that leftover is large enough that the detrended null's edge error
can exceed the observed arm's outright -- it absorbs the very bias it was meant
to isolate. It bounds the tracked-trend component from below; it cannot bound
the total.

A null that kept the trend would cancel the bias entirely, which is the trap
this construction exists to avoid.

Nothing here changes production behaviour: this is an instrument.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Protocol

import numpy as np

from .detrending import DetrendingPlan, edge_safe_mask

# Deterministic permutation seed, in the style of the other P2 gates.
DEFAULT_SEED = 20260804


class TrendEstimator(Protocol):
    """Fit a smooth trend to one contiguous, strictly increasing series."""

    def __call__(
        self, time: np.ndarray, flux: np.ndarray
    ) -> np.ndarray: ...  # pragma: no cover - structural


def savgol_trend_estimator(
    window_cadences: int,
    *,
    polyorder: int = 2,
    niters: int = 3,
    sigma: float = 3.0,
    break_tolerance: int = 5,
) -> TrendEstimator:
    """The shipping estimator: lightkurve's iteratively clipped Savitzky-Golay.

    Defaults mirror ``LightCurve.flatten``, which `photometry.py` calls through
    :func:`exohunt.detrending.flatten_edge_safe`. The iterative clipping is kept
    because it is part of the estimator -- which samples get clipped can itself
    change when a window loses support, and excluding that would measure a
    smoother we do not ship.

    ``break_tolerance`` must be a real production value and never something
    large enough to "disable" splitting. lightkurve overloads the parameter:
    besides splitting where ``dt > break_tolerance * median(dt)``, it replaces a
    segment's entire trend with that segment's **median** whenever the segment
    is shorter than ``break_tolerance`` samples. Passing the series length to
    suppress splitting therefore silently degrades every trend to a constant,
    which reads downstream as a huge offset-independent error. Callers should
    pass ``DetrendingPlan.break_tolerance_cadences``; the default is the
    production floor, :attr:`DetrendingConfig.minimum_break_tolerance_cadences`.
    """

    if window_cadences < 3:
        raise ValueError("Window must span at least three cadences.")
    if window_cadences % 2 == 0:
        raise ValueError("Savitzky-Golay window must be odd.")
    if break_tolerance < 1:
        raise ValueError("Break tolerance must be at least one cadence.")

    def estimate(time: np.ndarray, flux: np.ndarray) -> np.ndarray:
        import lightkurve as lk

        curve = lk.LightCurve(time=np.asarray(time, dtype=float),
                              flux=np.asarray(flux, dtype=float))
        _, trend = curve.flatten(
            window_length=window_cadences,
            polyorder=polyorder,
            niters=niters,
            sigma=sigma,
            break_tolerance=break_tolerance,
            return_trend=True,
        )
        return np.asarray(trend.flux.value, dtype=float)

    return estimate


def biweight_trend_estimator(
    window_days: float, *, break_tolerance_days: float = 0.10
) -> TrendEstimator:
    """The `detrend.py` candidate estimator: time-windowed biweight (wotan).

    Built but unwired -- correction 10 reverted its wiring. Measuring it here
    costs nothing extra and answers whether its edge behaviour is any better
    than the shipping estimator's, which the survivor-count comparison could
    not isolate.

    ``break_tolerance_days`` mirrors what :func:`exohunt.detrend.prepare_flux`
    passes, so the estimator segments exactly as the candidate path would. A
    caller handing in a single contiguous segment sees no split either way.
    """

    if window_days <= 0:
        raise ValueError("Window must be positive.")
    if break_tolerance_days <= 0:
        raise ValueError("Break tolerance must be positive.")

    def estimate(time: np.ndarray, flux: np.ndarray) -> np.ndarray:
        from wotan import flatten as wotan_flatten

        _, trend = wotan_flatten(
            np.asarray(time, dtype=float),
            np.asarray(flux, dtype=float),
            method="biweight",
            window_length=window_days,
            break_tolerance=break_tolerance_days,
            edge_cutoff=0.0,
            return_trend=True,
        )
        return np.asarray(trend, dtype=float)

    return estimate


@dataclass(frozen=True, slots=True)
class EdgeBiasSamples:
    """Paired per-cadence edge errors, one row per (truncation point, offset).

    ``offset`` counts cadences between a synthetic segment edge and the cadence
    whose trend is read: 0 is the first cadence after the edge. All three arms
    are in ppm of relative flux and share their construction exactly.
    """

    offset: np.ndarray
    observed_ppm: np.ndarray
    detrended_null_ppm: np.ndarray
    white_null_ppm: np.ndarray
    half_window_cadences: int
    truncation_points: int

    def __post_init__(self) -> None:
        sizes = {
            self.offset.size,
            self.observed_ppm.size,
            self.detrended_null_ppm.size,
            self.white_null_ppm.size,
        }
        if len(sizes) != 1:
            raise ValueError("Sample arrays must be the same length.")


def _detrended_residual(trend: np.ndarray, flux: np.ndarray) -> np.ndarray:
    """Relative residual about the fitted trend, with a constant true trend.

    Dividing by the trend is what makes the null honest: the series that
    results has no trend structure left for an edge window to mis-extrapolate,
    so its edge error is attributable to the noise alone.
    """

    safe = np.where(np.abs(trend) > 0, trend, np.nan)
    residual = flux / safe
    # A non-finite residual carries no information about edge behaviour; a flat
    # value contributes nothing to either null.
    return np.where(np.isfinite(residual), residual, 1.0)


def _whitened(residual: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Same residual distribution, correlation structure destroyed."""

    return rng.permutation(residual)


def measure_segment_edge_bias(
    time: np.ndarray,
    flux: np.ndarray,
    *,
    estimator: TrendEstimator,
    half_window_cadences: int,
    truncation_points: int = 32,
    max_offset: int | None = None,
    seed: int = DEFAULT_SEED,
) -> EdgeBiasSamples:
    """Measure edge bias on one contiguous segment.

    ``max_offset`` defaults to ``half_window_cadences + 1``: one cadence past
    the point where a truncated window regains full support. A strictly local
    estimator must read exactly zero there, so the last offset doubles as a
    correctness check rather than being a free parameter -- see
    :func:`full_support_floor_ppm`, which reports it and explains why the
    shipping estimator does not quite reach zero.

    Each truncation refits the **entire suffix** of the segment rather than a
    slice around the cadences being read. Slicing would be sound in principle --
    ``scipy.signal.savgol_filter`` is exactly local, agreeing with a full fit
    from the half-window onward to machine precision -- but refitting the suffix
    is literally what a real segment truncation is, so it needs no argument
    about which estimators happen to be local. wotan's biweight and any future
    candidate get the same treatment for free.
    """

    t = np.asarray(time, dtype=float)
    y = np.asarray(flux, dtype=float)
    if t.ndim != 1 or y.ndim != 1 or t.size != y.size:
        raise ValueError("Time and flux must be one-dimensional and equal length.")
    if half_window_cadences < 1:
        raise ValueError("Half window must be at least one cadence.")
    if truncation_points < 1:
        raise ValueError("At least one truncation point is required.")

    half = int(half_window_cadences)
    reach = int(max_offset) if max_offset is not None else half + 1
    if reach < 1:
        raise ValueError("Maximum offset must be at least one cadence.")

    # A truncation point must itself sit in the full fit's supported interior,
    # and the cadences read from it must stay a half-window clear of the
    # segment's own far end, so the reference they are compared against is
    # itself fully supported.
    span = reach + half
    first = half
    last = t.size - span
    if last <= first:
        raise ValueError(
            "Segment is too short for the requested window and offset reach."
        )

    reference = np.asarray(estimator(t, y), dtype=float)
    rng = np.random.default_rng(seed)
    detrended = _detrended_residual(reference, y)
    whitened = _whitened(detrended, rng)

    arms = {
        "observed": y,
        "detrended_null": detrended,
        "white_null": whitened,
    }
    references = {
        name: np.asarray(estimator(t, series), dtype=float)
        for name, series in arms.items()
    }

    starts = np.unique(
        np.linspace(first, last, num=truncation_points).astype(int)
    )
    offsets: list[np.ndarray] = []
    collected: dict[str, list[np.ndarray]] = {name: [] for name in arms}
    for start in starts:
        read = slice(int(start), int(start) + reach)
        offsets.append(np.arange(reach, dtype=int))
        for name, series in arms.items():
            truncated = np.asarray(
                estimator(t[int(start):], series[int(start):]), dtype=float
            )
            collected[name].append(
                (truncated[:reach] - references[name][read]) * 1.0e6
            )

    return EdgeBiasSamples(
        offset=np.concatenate(offsets),
        observed_ppm=np.concatenate(collected["observed"]),
        detrended_null_ppm=np.concatenate(collected["detrended_null"]),
        white_null_ppm=np.concatenate(collected["white_null"]),
        half_window_cadences=half,
        truncation_points=int(starts.size),
    )


def concatenate_samples(parts: list[EdgeBiasSamples]) -> EdgeBiasSamples:
    """Pool samples measured on different segments or stars."""

    if not parts:
        raise ValueError("At least one sample set is required.")
    halves = {part.half_window_cadences for part in parts}
    if len(halves) != 1:
        raise ValueError("Pooled samples must share a half-window.")
    return EdgeBiasSamples(
        offset=np.concatenate([part.offset for part in parts]),
        observed_ppm=np.concatenate([part.observed_ppm for part in parts]),
        detrended_null_ppm=np.concatenate(
            [part.detrended_null_ppm for part in parts]
        ),
        white_null_ppm=np.concatenate(
            [part.white_null_ppm for part in parts]
        ),
        half_window_cadences=halves.pop(),
        truncation_points=sum(part.truncation_points for part in parts),
    )


def _robust_rms(values: np.ndarray) -> float:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return float("nan")
    return float(np.sqrt(np.mean(np.square(finite))))


def _excess_bias_ppm(observed: np.ndarray, null: np.ndarray) -> float:
    """Trend-bias power remaining once the noise null is removed.

    Independent error terms add in quadrature, so the bias term is the
    quadrature difference. A negative difference means the observed arm carries
    no excess over its null; it is reported as zero rather than as a negative
    bias, which has no meaning.
    """

    excess = _robust_rms(observed) ** 2 - _robust_rms(null) ** 2
    if not np.isfinite(excess) or excess <= 0:
        return 0.0
    return float(np.sqrt(excess))


def profile_by_offset(
    samples: EdgeBiasSamples, *, offset_bins: int = 24
) -> list[dict[str, Any]]:
    """Summarize bias against distance from the edge.

    Offsets are pooled into logarithmically spaced bins because the interesting
    structure is concentrated within the first few percent of the half-window,
    where a linear binning would spend most of its resolution on the flat tail.
    """

    if offset_bins < 1:
        raise ValueError("At least one offset bin is required.")
    if samples.offset.size == 0:
        return []
    highest = int(samples.offset.max())
    edges = np.unique(
        np.geomspace(1, max(highest + 1, 2), num=offset_bins + 1).astype(int)
    )
    rows: list[dict[str, Any]] = []
    for low, high in zip(edges, edges[1:]):
        selected = (samples.offset >= low - 1) & (samples.offset < high)
        count = int(np.count_nonzero(selected))
        if count == 0:
            continue
        observed = samples.observed_ppm[selected]
        detrended_null = samples.detrended_null_ppm[selected]
        white_null = samples.white_null_ppm[selected]
        observed_rms = _robust_rms(observed)
        detrended_rms = _robust_rms(detrended_null)
        white_rms = _robust_rms(white_null)
        rows.append(
            {
                "offset_low": int(low - 1),
                "offset_high": int(high - 1),
                "support_fraction": round(
                    min(
                        1.0,
                        (samples.half_window_cadences + (low - 1) + 1)
                        / (2 * samples.half_window_cadences + 1),
                    ),
                    4,
                ),
                "samples": count,
                "observed_rms_ppm": round(observed_rms, 3),
                "detrended_null_rms_ppm": round(detrended_rms, 3),
                "white_null_rms_ppm": round(white_rms, 3),
                # Primary: the term an uncertainty inflation cannot price.
                "excess_bias_ppm": round(
                    _excess_bias_ppm(observed, white_null), 3
                ),
                # Conservative variant; see the module docstring for why this
                # bounds only the tracked-trend component.
                "tracked_trend_excess_ppm": round(
                    _excess_bias_ppm(observed, detrended_null), 3
                ),
                "residual_structure_ppm": round(
                    _excess_bias_ppm(detrended_null, white_null), 3
                ),
                "observed_p95_abs_ppm": round(
                    float(np.nanpercentile(np.abs(observed), 95)), 3
                ),
                "white_null_p95_abs_ppm": round(
                    float(np.nanpercentile(np.abs(white_null), 95)), 3
                ),
                "ratio_observed_to_white_null": (
                    round(observed_rms / white_rms, 4)
                    if white_rms > 0
                    else None
                ),
            }
        )
    return rows


def full_support_floor_ppm(samples: EdgeBiasSamples) -> float:
    """What the instrument reads at the offset whose true answer is zero.

    Every measured bias must be read against this floor. For a strictly local
    estimator it is zero by construction, and for the moving-average stand-in
    in the tests it is. The shipping estimator does **not** reach zero: its
    3-sigma clip is computed over the whole series, so shortening the series
    can reclassify a distant sample and shift the interpolated trend near the
    cadence of interest. Measured on synthetic series, that leaks tens to a few
    hundred ppm in a minority of truncations, and it does not fall off with
    series length -- it is occasional rather than asymptotic.

    The practical consequence is worth stating plainly: removing a segment's
    edge half-window does not fully remove that segment boundary's influence on
    the trend, because the estimator is not local.
    """

    if samples.offset.size == 0:
        return float("nan")
    deepest = samples.offset == samples.offset.max()
    return _robust_rms(samples.observed_ppm[deepest])


def guard_retention(
    time: np.ndarray, plan: DetrendingPlan, guard_cadences: int
) -> float:
    """Fraction of cadences a guard of ``guard_cadences`` would keep.

    This is the other half of the guard trade-off: :func:`sufficient_guard_cadences`
    says how wide a guard has to be to hold trend bias within a tolerance, and
    this says what that width costs against §2.3's retention criterion.

    It defers to the shipping :func:`exohunt.detrending.edge_safe_mask` with
    only ``edge_guard_days`` substituted, so the segmentation, the strict
    monotonicity check and the "segment shorter than twice the guard drops
    entirely" behaviour are production's rather than a reimplementation's. A
    guard of 0 is not the same as no guard at all: non-finite samples are still
    excluded.
    """

    if guard_cadences < 0:
        raise ValueError("Guard width cannot be negative.")
    adjusted = replace(
        plan, edge_guard_days=guard_cadences * plan.cadence_days
    )
    keep, _ = edge_safe_mask(np.asarray(time, dtype=float), adjusted)
    if keep.size == 0:
        return float("nan")
    return float(np.count_nonzero(keep) / keep.size)


def sufficient_guard_cadences(
    samples: EdgeBiasSamples, *, tolerance_ppm: float
) -> int | None:
    """Smallest guard width whose remaining bias stays under a depth tolerance.

    This is the number the two rejected mechanisms lacked. A guard of ``g``
    cadences discards offsets below ``g``, so the guard is sufficient when
    every offset it *keeps* has excess bias within tolerance. Returns ``None``
    when no width up to the measured reach qualifies, which would say the
    estimator's edge error never becomes negligible on this data.
    """

    if tolerance_ppm <= 0:
        raise ValueError("Tolerance must be positive.")
    if samples.offset.size == 0:
        return None
    highest = int(samples.offset.max())
    # Walk inward from the widest measured guard: the answer is the smallest
    # width from which every deeper offset also passes.
    sufficient = highest + 1
    for offset in range(highest, -1, -1):
        selected = samples.offset == offset
        if not np.any(selected):
            continue
        bias = _excess_bias_ppm(
            samples.observed_ppm[selected],
            samples.white_null_ppm[selected],
        )
        if bias > tolerance_ppm:
            return sufficient
        sufficient = offset
    return sufficient
