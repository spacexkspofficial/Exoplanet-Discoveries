"""Monotransit detector: matched filter over single-transit templates (S3.4).

MASTER_PLAN section 3.4 and lane section 6.2. SPOC and QLP both require two
transits, so single long-period events are a *structural* gap in the official
products rather than a sensitivity one -- nobody has searched for them in this
data. The lane rides section 6.1's sample, so it costs no new downloads.

What this module is not
-----------------------
It does not produce a period, ever. A single event constrains duration and
depth; a period requires a repeat or an external ephemeris. The claim ceiling
is ``single_event_lead`` (section 3.4), and nothing here may promote past it.

The threshold is NOT calibrated yet
-----------------------------------
Section 3.4 specifies detection at peak significance >= 8 sigma, "calibrate on
inverted data, target <= 0.3 false events/star at first pass". That
calibration has not been run: ``DEFAULT_SIGNIFICANCE_THRESHOLD`` is the plan's
*written* value, not a measured one. Section 6.2 is blunt that "false-alarm
control is the whole game" here, and correction 68 measured the P5 cohort's
inverted survivor rate at 4.2x over budget for the *periodic* search -- a
monotransit has no repeat to confirm it, so it is strictly more exposed.

Until the re-calibration reports an inverted false-event rate for this
detector, treat its output as diagnostic. :func:`search_monotransits` records
``threshold_calibrated: False`` in every result so that a downstream reader
cannot mistake the state.

Deliberately outside ScienceConfig
----------------------------------
None of these parameters live in :class:`exohunt.config.ScienceConfig`. Adding
them would move the detection identity digest, which would invalidate the
re-calibration currently in flight and every trusted release keyed to it. This
detector is additive: it reads a prepared flux and reports events, and changes
nothing about how the periodic search behaves.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

METHOD_TAG = "monotransit-matched-filter-v1"

# Section 3.4: "durations 1.5-24 h". Geometric spacing keeps the fractional
# duration step roughly constant, which is what matters for matched-filter
# loss between adjacent templates.
DEFAULT_MIN_DURATION_HOURS = 1.5
DEFAULT_MAX_DURATION_HOURS = 24.0
DEFAULT_DURATION_COUNT = 12

# Section 3.4's written value. Not measured -- see the module docstring.
DEFAULT_SIGNIFICANCE_THRESHOLD = 8.0

# Quadratic limb darkening. These are ordinary cool-dwarf values in the TESS
# band; the template shape is only weakly sensitive to them, and a matched
# filter loses very little to a modest template mismatch.
DEFAULT_U1 = 0.35
DEFAULT_U2 = 0.23

# Local scatter is estimated in a window this many durations wide on each side
# of the trial centre. Wide enough to be stable, narrow enough to track a
# changing noise level across a sector.
LOCAL_SCATTER_DURATIONS = 6.0


@dataclass(frozen=True, slots=True)
class MonotransitEvent:
    """One single-transit detection. Never carries a period."""

    time_btjd: float
    duration_hours: float
    depth: float
    depth_uncertainty: float
    significance: float
    # Cadences inside the event window; a "detection" resting on one or two
    # points is an outlier, not a transit.
    cadences_in_event: int
    # Fraction of the local baseline present on each side. A one-sided event
    # is usually a segment edge or a gap wall.
    baseline_before: int
    baseline_after: int
    vetoes: tuple[str, ...]

    @property
    def passes(self) -> bool:
        return not self.vetoes

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["vetoes"] = list(self.vetoes)
        payload["passes"] = self.passes
        return payload


def limb_darkened_template(
    offsets: np.ndarray,
    *,
    impact_parameter: float = 0.0,
    u1: float = DEFAULT_U1,
    u2: float = DEFAULT_U2,
) -> np.ndarray:
    """Normalized single-transit shape on ``offsets`` in units of duration.

    ``offsets`` is (t - t0) / duration, so the event spans [-0.5, 0.5]. The
    profile is the small-planet limb-darkening law evaluated along the chord:
    intensity ``1 - u1(1-mu) - u2(1-mu)^2`` with ``mu = sqrt(1 - r^2)``. That
    gives the rounded bottom of a real transit rather than a box, which is what
    separates a transit from a step or a cosmic ray in a matched filter.

    Returned peak-normalized to 1.0 so the fitted amplitude is a depth.
    """

    x = np.asarray(offsets, dtype=float)
    shape = np.zeros_like(x)
    inside = np.abs(x) <= 0.5
    if not np.any(inside):
        return shape

    # Chord half-length in stellar radii, so that |x| = 0.5 lands on the limb.
    half_chord = np.sqrt(max(1.0 - impact_parameter**2, 1e-12))
    r = np.sqrt(impact_parameter**2 + (2.0 * x[inside] * half_chord) ** 2)
    r = np.clip(r, 0.0, 1.0)
    mu = np.sqrt(np.clip(1.0 - r**2, 0.0, 1.0))
    intensity = 1.0 - u1 * (1.0 - mu) - u2 * (1.0 - mu) ** 2
    peak = 1.0 - u1 * (1.0 - np.sqrt(max(1.0 - impact_parameter**2, 0.0))) - u2 * (
        1.0 - np.sqrt(max(1.0 - impact_parameter**2, 0.0))
    ) ** 2
    shape[inside] = intensity / peak if peak > 0 else intensity
    return shape


def duration_grid_hours(
    *,
    minimum_hours: float = DEFAULT_MIN_DURATION_HOURS,
    maximum_hours: float = DEFAULT_MAX_DURATION_HOURS,
    count: int = DEFAULT_DURATION_COUNT,
) -> np.ndarray:
    if minimum_hours <= 0 or maximum_hours <= minimum_hours:
        raise ValueError("Duration bounds must satisfy 0 < minimum < maximum.")
    if count < 2:
        raise ValueError("A template bank needs at least two durations.")
    return np.geomspace(minimum_hours, maximum_hours, count)


def _local_scatter(
    flux: np.ndarray, index: int, half_width: int
) -> float:
    """Robust sigma from the neighbourhood, excluding the event itself."""

    low = max(0, index - half_width)
    high = min(flux.size, index + half_width + 1)
    window = flux[low:high]
    if window.size < 8:
        window = flux
    median = float(np.nanmedian(window))
    mad = float(np.nanmedian(np.abs(window - median)))
    sigma = 1.4826 * mad
    if not np.isfinite(sigma) or sigma <= 0:
        sigma = float(np.nanstd(window))
    return sigma if np.isfinite(sigma) and sigma > 0 else float("nan")


def search_monotransits(
    time: np.ndarray,
    flux: np.ndarray,
    *,
    uncertainty_scale: np.ndarray | None = None,
    durations_hours: np.ndarray | None = None,
    significance_threshold: float = DEFAULT_SIGNIFICANCE_THRESHOLD,
    impact_parameter: float = 0.0,
    minimum_cadences: int = 3,
    segment_gap_days: float = 0.1,
    max_events: int = 20,
) -> dict[str, Any]:
    """Run the template bank over one prepared long-window flux.

    ``flux`` should be the *long-window* detrended flux
    (``DetrendConfig.long_window_days``, 3.0 d): a short window flattens away
    the very events this looks for.

    Returns a diagnostic block, never a candidate. See the module docstring on
    why ``threshold_calibrated`` is False.
    """

    t = np.asarray(time, dtype=float)
    y = np.asarray(flux, dtype=float)
    keep = np.isfinite(t) & np.isfinite(y)
    t, y = t[keep], y[keep]
    if t.size < 50:
        raise ValueError("At least 50 finite measurements are required.")
    order = np.argsort(t)
    t, y = t[order], y[order]

    scale = (
        np.ones_like(y)
        if uncertainty_scale is None
        else np.asarray(uncertainty_scale, dtype=float)[keep][order]
    )
    scale = np.where(np.isfinite(scale) & (scale > 0), scale, 1.0)

    if durations_hours is None:
        durations_hours = duration_grid_hours()
    durations_days = np.asarray(durations_hours, dtype=float) / 24.0

    median_cadence = float(np.median(np.diff(t))) if t.size > 1 else 0.0
    if median_cadence <= 0:
        raise ValueError("Could not determine a positive median cadence.")

    # Segment edges: an event abutting a real interruption is the gap wall, not
    # a transit, and section 3.4 lists that veto explicitly.
    gap_starts = set()
    gap_ends = set()
    for index in np.flatnonzero(np.diff(t) > segment_gap_days):
        gap_ends.add(int(index))
        gap_starts.add(int(index) + 1)
    gap_ends.add(t.size - 1)
    gap_starts.add(0)

    residual = 1.0 - y
    events: list[MonotransitEvent] = []

    for duration_days, duration_hours in zip(durations_days, durations_hours):
        span = int(round(duration_days / median_cadence))
        if span < minimum_cadences:
            continue
        half = max(span // 2, 1)
        scatter_half = int(LOCAL_SCATTER_DURATIONS * span)

        # Step by a third of a duration: finer buys almost nothing for a
        # matched filter, coarser starts losing peak significance.
        step = max(span // 3, 1)
        for centre in range(half, t.size - half, step):
            low, high = centre - half, centre + half + 1
            offsets = (t[low:high] - t[centre]) / duration_days
            template = limb_darkened_template(
                offsets, impact_parameter=impact_parameter
            )
            if not np.any(template > 0):
                continue
            sigma = _local_scatter(y, centre, scatter_half)
            if not np.isfinite(sigma):
                continue
            weights = 1.0 / (sigma * scale[low:high]) ** 2
            denominator = float(np.sum(weights * template**2))
            if denominator <= 0:
                continue
            depth = float(np.sum(weights * template * residual[low:high]) / denominator)
            depth_uncertainty = float(1.0 / np.sqrt(denominator))
            significance = depth / depth_uncertainty if depth_uncertainty > 0 else 0.0
            if significance < significance_threshold or depth <= 0:
                continue

            in_event = int(np.count_nonzero(template > 0.5))
            before = int(np.count_nonzero((t < t[low]) & (t > t[low] - duration_days)))
            after = int(np.count_nonzero((t > t[high - 1]) & (t < t[high - 1] + duration_days)))

            vetoes: list[str] = []
            if in_event < minimum_cadences:
                vetoes.append("fewer_than_minimum_cadences_in_event")
            if before == 0 or after == 0:
                vetoes.append("no_two_sided_local_baseline")
            if any(abs(centre - edge) <= span for edge in gap_starts | gap_ends):
                vetoes.append("adjacent_to_segment_edge_or_gap")
            if depth > 0.05:
                vetoes.append("depth_over_5_percent")

            events.append(
                MonotransitEvent(
                    time_btjd=float(t[centre]),
                    duration_hours=float(duration_hours),
                    depth=depth,
                    depth_uncertainty=depth_uncertainty,
                    significance=float(significance),
                    cadences_in_event=in_event,
                    baseline_before=before,
                    baseline_after=after,
                    vetoes=tuple(vetoes),
                )
            )

    events.sort(key=lambda event: event.significance, reverse=True)
    deduped: list[MonotransitEvent] = []
    for event in events:
        if any(
            abs(event.time_btjd - kept.time_btjd)
            < max(event.duration_hours, kept.duration_hours) / 24.0
            for kept in deduped
        ):
            continue
        deduped.append(event)
        if len(deduped) >= max_events:
            break

    survivors = [event for event in deduped if event.passes]
    return {
        "method": METHOD_TAG,
        "warning": (
            "Single-event detections only. A monotransit constrains duration "
            "and depth, never a period, and cannot be confirmed without a "
            "repeat or an external ephemeris."
        ),
        "threshold_calibrated": False,
        "threshold_note": (
            "significance_threshold is MASTER_PLAN section 3.4's written value, "
            "not a measured one. No inverted-data false-event rate has been "
            "established for this detector; section 6.2 requires <= 0.3 false "
            "events/star before its output means anything."
        ),
        "significance_threshold": float(significance_threshold),
        "durations_hours": [float(value) for value in durations_hours],
        "events": [event.to_dict() for event in deduped],
        "event_count": len(deduped),
        "survivor_count": len(survivors),
        "claim_ceiling": "single_event_lead",
    }
