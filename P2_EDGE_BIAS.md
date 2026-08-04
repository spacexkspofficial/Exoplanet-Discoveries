# P2 segment-edge trend-model bias: direct measurement

Measured 2026-08-04 PDT. `P2_EDGE_DIAGNOSTIC.md` closed two edge-recovery
mechanisms and set the condition for a third: any next design must "measure or
avoid trend-model bias itself". This is the measurement. It changes no
production behaviour.

## What was missing

Corrections 10 and 13 rejected support-weighted biweight and an
owner-selected quarter-window guard. Both mechanisms shared an assumption: that
the edge problem is a **support** problem, and can therefore be priced with a
sample count — a support floor, an uncertainty inflation `1/f**alpha`, a
narrower guard. Both failed, and the recorded diagnosis was that the residual
error is estimator *bias*, which neither shrinks with more data nor follows the
variance law.

That diagnosis was inferred from downstream survivor counts. Correction 24 then
showed the survivor criterion is the only §2.3 artifact number with resolving
power, and correction 25 showed the historical epochs that criterion was
measured at do nothing on a neutral cohort. So the diagnosis rested on a
statistic that has since been substantially qualified, and the quantity it
named — trend-model bias — had never been measured directly.

## Construction

The estimator's own full-support answer is the reference:

1. Fit the trend once over a full contiguous segment. A cadence deep in the
   interior has symmetric support; that is the estimate the pipeline ships.
2. Truncate the segment so the same cadence sits `k` samples from a synthetic
   segment start, and refit with identical settings.
3. `bias(k) = trend_truncated - trend_full` at that cadence, in ppm of relative
   flux, directly comparable with a transit depth.

The comparison is **paired per cadence of a single star**, so flux, noise
realization and stellar variability are common to both fits and cancel exactly.
This is correction 24's lesson — pairing is what gives an edge comparison
resolving power — applied one stage earlier, to the estimator rather than to
the survivors.

Truncations refit the entire suffix rather than a slice, because that is
literally what a real segment truncation is, and it needs no argument about
which estimators happen to be local.

### Separating bias from variance

Two nulls, both driven through the identical code path as the observed arm:

- **white null** — the segment's residuals about its own fitted trend,
  permuted. Its true trend is a constant and its noise is white, so its edge
  error is *exactly* the variance term. This is the null that decides the
  question: the rejected mechanisms priced the edge with an uncertainty
  inflation, and an uncertainty is a variance. Excess of observed over the
  white null is the part no inflation can price.
- **detrended null** — the same residuals unpermuted. Reported as a
  deliberately conservative variant only. It divides by the *fitted* trend
  rather than the true one, so everything the estimator failed to track stays
  in the null and carries real structure with it; on real photometry its edge
  error can exceed the observed arm's outright.

A null that kept the trend would cancel the bias rather than expose it.

### The floor, and why every number is read against it

`full_support_floor_ppm` reports what the instrument returns at the offset whose
true answer is zero — the first cadence whose truncated window is fully
supported again. A strictly local estimator must read exactly zero there.

This is not a formality. It is what makes the measurement falsifiable, and on
the shipping estimator it does not come out zero.

## Instrument validation

- **Flat-trend control.** Pure noise about a constant reports no excess bias:
  the observed and white arms agree to within their sampling scatter, and the
  sufficient guard is 0 cadences. The construction cannot manufacture bias.
- **Curved-trend control.** A strongly curved synthetic series reports edge
  bias well above its null, decaying with support, with the profile ordered
  correctly in both bias and support fraction.
- **Locality invariant.** For a local estimator the bias is exactly zero at and
  beyond the half-window, to machine precision.
- **Shipping-path fidelity.** The Savitzky-Golay arm reproduces
  `LightCurve.flatten`'s trend to 1e-12, the same call `photometry.py` makes
  through `detrending.flatten_edge_safe`.

Thirteen tests cover these plus the argument-validation branches. Full suite:
**292 passed** (279 before).

## Result

120 cached SPOC Sector 100 stars, deterministic stride over the cache, 24
truncation points each, **0 failures, 1,127 s offline**. Point-to-point scatter
runs from 245 ppm to absurd values (median 38,243 ppm; the cache holds some
targets whose normalized flux is essentially noise). **37 of 120 stars have
point-to-point scatter at or below 10,000 ppm**, and only those are summarized
below — for the rest every edge error is variance and an absolute ppm bias
means nothing. Both subsets are in the report; the noisy one is not
interpretable and should not be quoted.

Quiet subset, 37 stars, median across stars:

**Savitzky-Golay, the shipping estimator** (2.0 d window, half-window 720
cadences). Floor **30.4 ppm**; exact-zero fraction 0.083.

| offset (cadences) | support | observed | white null | excess bias | obs/white |
|---|---:|---:|---:|---:|---:|
| 0–1 | 0.50 | 637.3 | 194.6 | **458.3** | 3.11 |
| 104–138 | 0.57 | 380.2 | 121.8 | 281.7 | 3.26 |
| 315–415 | 0.72 | 202.6 | 80.0 | 199.0 | 4.12 |
| 547–720 | 0.88 | 114.4 | 48.3 | 101.7 | 3.33 |

**Biweight, the candidate** (1.0 d window, half-window 360). Floor **0.0 ppm**;
exact-zero fraction **1.000**.

| offset (cadences) | support | observed | white null | excess bias | obs/white |
|---|---:|---:|---:|---:|---:|
| 0–1 | 0.50 | 568.5 | 143.5 | **549.1** | 2.99 |
| 104–134 | 0.64 | 403.4 | 102.9 | 365.5 | 3.94 |
| 281–360 | 0.89 | 81.8 | 37.7 | 71.5 | 3.16 |

### The edge error is not variance, at any offset

`observed / white null` is **~3 across the entire half-window** for both
estimators — 3.11 → 3.33 for Savitzky-Golay, 2.99 → 3.16 for biweight, rising
to ~4 in between. A ratio of 3 in RMS is a factor **9 in error power**, so the
variance term is roughly **11%** of the edge error and about **89%** is
structure the estimator mis-extrapolates.

This is the first direct measurement of the claim `P2_EDGE_DIAGNOSTIC.md` made
from survivor counts, and it explains the shape of both failures. A support
weighting `1/f**alpha` prices the edge as a *variance*, and the variance is a
ninth of the problem. Worse, the ratio is roughly **flat in support fraction**:
the bias does not become variance-like as support improves, so no exponent can
make a variance model track it. Correction 10's finding that no `(window,
floor, alpha)` combination works is what a flat ratio of 3 predicts.

There is a second, physical reason inflation cannot work, now that the size is
known. An uncertainty tells a search "this point is noisy"; it does not remove a
coherent displacement. Neighbouring edge cadences share most of their trend
window, so their biases are correlated and survive the averaging that BLS
performs, while the inflated error bars claim they should not.

### The guard width is now a choice with a number attached

Smallest guard whose remaining excess bias stays within a stated tolerance
(median across stars):

| bias tolerance | Savitzky-Golay guard | biweight guard |
|---|---:|---:|
| 100 ppm | **626** cadences | 297 |
| 500 ppm | **0** | 32 |
| 1000 ppm | **0** | 0 |

The production guard is 720 cadences, and this is the first evidence that says
whether that is the right number rather than a safe-sounding one.

It depends entirely on the shallowest depth being claimed, which is why no
single guard width was ever defensible:

- The small-star campaign's survivors ran **2,320–49,657 ppm** deep. Holding
  edge trend bias to §2.3's 5% depth-bias budget for a 2,000 ppm transit means
  a 100 ppm tolerance — which needs **626 of the current 720 cadences**. For
  shallow transits the existing guard is approximately correct, and the 33%
  cadence cost is largely *earned* rather than wasteful.
- Tolerating 500 ppm — defensible for transits deeper than ~10,000 ppm — makes
  the guard unnecessary altogether.

That reframes MASTER_PLAN §2.2, which treats the 33% as a cost to be recovered.
The measurement says most of it cannot be recovered without accepting trend
bias comparable to a shallow transit. What can be recovered is depth-dependent,
which is a different mechanism from either rejected arm: neither a support
weight nor a fixed narrower guard, but a guard sized by the shallowest depth a
given lane intends to claim. **Nothing here ships that** — it would be a
behaviour change needing its own commit and the §2.3 gates.

### The shipping estimator is not local

Biweight reads a **0.0 ppm floor with 100% exact zeros**: wotan's trend at a
cadence depends only on nearby data, exactly as a local smoother should.

Savitzky-Golay does not. Only **8.3%** of its truncations return exactly zero
where the window is fully supported. The cause is lightkurve's 3-sigma clip,
which is computed over the whole series: shortening the series can reclassify a
distant sample and shift the interpolated trend near the cadence of interest.
`scipy.signal.savgol_filter` itself is exactly local — the coupling is entirely
in the wrapper.

The magnitude on quiet stars is small, **30.4 ppm median**, roughly a fifteenth
of the edge bias, so it does not threaten the numbers above. But it is not
zero, and it means removing a segment's edge half-window does not fully remove
that boundary's influence on the trend. Any claim that a guard "removes" edge
contamination is approximate for the shipping estimator and exact for the
candidate.

## Reading the two estimators together

Both are measured, but they are **not** a like-for-like comparison and no
ranking should be read from their absolute ppm. The shipping Savitzky-Golay
runs a 2.0 d window (half-window 720 cadences at 120 s); the candidate biweight
runs `DetrendConfig.short_window_days` = 1.0 d (half-window 360). Their edge
regions differ by a factor of two by construction, and the scale-free
`observed / white null` ratio is the only column that compares directly.

## Two defects found while building it

Both would have produced confident, wrong numbers, and both are now pinned by
regression tests.

**A null that preserved the trend.** The first null permuted residuals but
multiplied them back onto the fitted trend, so both arms mis-extrapolated the
same structure and the difference cancelled the bias it was meant to isolate.
Caught by reasoning, before it produced a result.

**`break_tolerance` is overloaded in lightkurve.** Passing the series length to
stop it re-splitting an already contiguous segment also trips its
*minimum segment length* rule, which replaces the segment's entire trend with
that segment's median. On real photometry this showed as an identical
10,711 ppm error at *every* offset, including offsets with full support where
the true error is exactly zero. The locality invariant is what caught it.
Callers must pass a real production break tolerance.

## Reproducibility

```powershell
$env:EXOHUNT_CACHE_DIR = "<local-cache-root>"
python scripts\measure_edge_trend_bias.py `
  --cache-dir $env:EXOHUNT_CACHE_DIR `
  --output results\p2_gates\edge_trend_bias_120.json `
  --max-stars 120 --truncation-points 24
```

Strictly offline: light curves are read from cached FITS by path and no MAST
search or download is issued. Deterministic under seed 20260804. Raw evidence:
`results/p2_gates/edge_trend_bias_120.json` (and `_40.json`, an earlier
40-star run whose pooled floor was noise-dominated and is superseded).

Note that `EXOHUNT_CACHE_DIR` is frequently absent from a non-login shell even
when it is set as a user variable, and `paths.default_cache_dir()` then falls
back to a `%LOCALAPPDATA%` path that may not exist — a run described as
"offline from cache" can silently become a MAST fetch. The script requires the
directory explicitly for that reason.
