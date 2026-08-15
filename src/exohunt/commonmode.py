"""Reject signals produced by the spacecraft rather than by a star.

A transit is a property of one star. A scattered-light ramp, a momentum dump, or
a detrending artifact at a downlink gap is a property of the *observatory*, and
it therefore dims many unrelated stars at the same absolute time.

This module measures that directly. For every searched target it asks how many
*other* targets in the same campaign carry the same ephemeris: the same period
to within a tolerance, and transits falling in the same phase. Real planets
around unrelated stars do not share ephemerides; an instrumental event makes
hundreds of them share one.

Requiring period agreement as well as phase agreement is what makes the test
sharp. In a five-thousand-target campaign a target's transits will overlap
*some* other target's by chance almost always, so "shares an instant" is nearly
uninformative. Sharing a whole ephemeris is not.

The count is compared against a uniform-phase expectation computed from the
targets that already share the period. That baseline deliberately keeps any
clustering in the period distribution -- including instrumental clustering --
so the enrichment isolates coincidence in phase, which is the part no
population of independent planets can produce.

Sharing alone does not say *which* instrument effect is responsible, so the
screen also records detector and sky evidence: an event seen across all four
cameras is observatory-wide, while one confined to a few arcminutes is more
likely a single bright contaminating source bleeding into its neighbours.
"""

from __future__ import annotations

import csv
import json
import math
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


# Two targets are treated as carrying the same ephemeris when their periods
# agree to this fraction and their transits fall in phase. Requiring period
# agreement as well as phase agreement is what makes the test sharp: in a
# five-thousand-target campaign, sharing merely *some* instant is common by
# chance, while sharing a full ephemeris is not.
PERIOD_TOLERANCE = 0.02
MIN_PHASE_TOLERANCE_DAYS = 0.02

# A signal is called instrumental only when it is strongly enriched over the
# uniform-phase expectation and shared with a non-trivial number of targets.
# The absolute floors keep a small campaign from flagging a chance pair.
MIN_ENRICHMENT = 10.0
MIN_SHARED_FRACTION = 0.005
MIN_SHARED_TARGETS = 10

# Above this angular spread the sharers cannot be one contaminating source.
OBSERVATORY_SPREAD_DEG = 1.0

# The shared-*instant* screen (correction 85). Separate constants because it is
# a separate test: the screen above asks who shares a whole ephemeris, this one
# asks who dims at the same absolute time regardless of period. A single
# instrumental event is fitted by different stars at different periods, so it is
# invisible to the first test and obvious to the second.
SHARED_INSTANT_BIN_MINUTES = 30.0
SHARED_INSTANT_ALPHA = 0.05
# One coincidence is not a verdict -- a real planet with five transits will
# occasionally place one on a momentum dump. A fit whose events are mostly
# shared is a signal assembled from the observatory's timeline.
MIN_INSTRUMENTAL_EVENT_FRACTION = 0.5

# TESS occupies a 13.7-day lunar-resonant orbit. Perigee downlink gaps and the
# scattered-light ramps around them recur on that period, so instrumental power
# concentrates there and at its simple ratios. A candidate sitting on one of
# those ratios is not thereby disproved -- planets do exist at 6.85 days -- but
# it carries a prior that the rest of the population makes very unfavourable.
TESS_ORBIT_DAYS = 13.70
HARMONIC_RATIOS = (
    (1, 4),
    (1, 3),
    (1, 2),
    (2, 3),
    (3, 4),
    (1, 1),
    (3, 2),
    (2, 1),
)
HARMONIC_TOLERANCE = 0.03

# The search grids the campaigns use. A fit pinned to the first or last value of
# a grid is a fit that wanted to leave the grid, which is characteristic of a
# trend rather than a transit.
DURATION_GRID_HOURS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0)
SEARCH_CEILING_TOLERANCE = 0.02


def spacecraft_harmonic(period_days: float) -> dict[str, Any] | None:
    """Return the TESS-orbit ratio a period sits on, if any."""

    best: dict[str, Any] | None = None
    for numerator, denominator in HARMONIC_RATIOS:
        expected = TESS_ORBIT_DAYS * numerator / denominator
        offset = abs(period_days - expected) / expected
        if offset <= HARMONIC_TOLERANCE and (
            best is None or offset < best["offset_fraction"]
        ):
            best = {
                "ratio": f"{numerator}/{denominator}",
                "period_days": round(expected, 4),
                "offset_fraction": round(offset, 5),
            }
    return best


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _transit_times(
    epoch: float, period: float, start: float, end: float, limit: int = 512
) -> list[float]:
    """Project a fitted ephemeris across the observed baseline."""

    if period <= 0 or end < start:
        return []
    first = math.ceil((start - epoch) / period)
    last = math.floor((end - epoch) / period)
    if last < first:
        return []
    if last - first + 1 > limit:
        last = first + limit - 1
    return [epoch + index * period for index in range(first, last + 1)]


def _angular_separation_deg(
    ra_a: float, dec_a: float, ra_b: float, dec_b: float
) -> float:
    """Great-circle separation, stable for the small angles that matter here."""

    phi_a = math.radians(dec_a)
    phi_b = math.radians(dec_b)
    delta_phi = phi_b - phi_a
    delta_lambda = math.radians(ra_b - ra_a)
    haversine = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta_lambda / 2) ** 2
    )
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(max(0.0, haversine)))))


class _Ephemeris:
    __slots__ = (
        "tic_id",
        "period",
        "epoch",
        "duration_hours",
        "camera",
        "ccd",
        "ra",
        "dec",
    )

    def __init__(
        self,
        tic_id: int,
        period: float,
        epoch: float,
        duration_hours: float | None,
        camera: int | None,
        ccd: int | None,
        ra: float | None,
        dec: float | None,
    ) -> None:
        self.tic_id = tic_id
        self.period = period
        self.epoch = epoch
        self.duration_hours = duration_hours
        self.camera = camera
        self.ccd = ccd
        self.ra = ra
        self.dec = dec

    def phase_tolerance_days(self) -> float:
        """Half a transit duration, floored so a grid-minimum box still counts."""

        if self.duration_hours is None or self.duration_hours <= 0:
            return MIN_PHASE_TOLERANCE_DAYS
        return max(MIN_PHASE_TOLERANCE_DAYS, 0.5 * self.duration_hours / 24.0)


def _collect(
    rows: Iterable[dict[str, Any]],
    metadata: dict[int, dict[str, Any]] | None,
) -> list[_Ephemeris]:
    metadata = metadata or {}
    collected: list[_Ephemeris] = []
    for row in rows:
        try:
            tic_id = int(row["tic_id"])
        except (KeyError, TypeError, ValueError):
            continue
        period = _finite(row.get("period_days"))
        epoch = _finite(row.get("transit_time"))
        if period is None or epoch is None or period <= 0:
            continue
        extra = metadata.get(tic_id, {})

        def _int(value: Any) -> int | None:
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None

        collected.append(
            _Ephemeris(
                tic_id=tic_id,
                period=period,
                epoch=epoch,
                duration_hours=_finite(row.get("duration_hours")),
                camera=_int(row.get("camera") or extra.get("camera")),
                ccd=_int(row.get("ccd") or extra.get("ccd")),
                ra=_finite(row.get("ra_deg") or extra.get("ra_deg")),
                dec=_finite(row.get("dec_deg") or extra.get("dec_deg")),
            )
        )
    return collected


def _phase_offset(reference: _Ephemeris, other: _Ephemeris) -> float:
    """Signed separation between two epochs, folded on the reference period."""

    offset = (other.epoch - reference.epoch) % reference.period
    if offset > reference.period / 2:
        offset -= reference.period
    return offset


def _period_neighbours(
    ordered: Sequence[_Ephemeris], periods: Sequence[float], index: int
) -> tuple[int, int]:
    """Half-open index range of targets whose period matches within tolerance."""

    period = ordered[index].period
    low = bisect_left(periods, period * (1.0 - PERIOD_TOLERANCE))
    high = bisect_right(periods, period * (1.0 + PERIOD_TOLERANCE))
    return low, high


def _detector_evidence(
    ephemeris: _Ephemeris,
    sharers: set[int],
    by_tic: dict[int, _Ephemeris],
) -> dict[str, Any]:
    cameras = {
        other.camera
        for tic in sharers
        if (other := by_tic.get(tic)) is not None and other.camera is not None
    }
    ccds = {
        (other.camera, other.ccd)
        for tic in sharers
        if (other := by_tic.get(tic)) is not None and other.ccd is not None
    }
    separations = [
        _angular_separation_deg(ephemeris.ra, ephemeris.dec, other.ra, other.dec)
        for tic in sharers
        if (other := by_tic.get(tic)) is not None
        and other.ra is not None
        and other.dec is not None
    ] if ephemeris.ra is not None and ephemeris.dec is not None else []
    return {
        "cameras_spanned": len(cameras),
        "detectors_spanned": len(ccds),
        "sky_spread_deg": round(max(separations), 4) if separations else None,
        "median_separation_deg": (
            round(sorted(separations)[len(separations) // 2], 4)
            if separations
            else None
        ),
    }


def _poisson_upper_tail(observed: int, expected: float) -> float:
    """P(X >= observed) for a Poisson mean, in stdlib arithmetic.

    `commonmode` is deliberately free of numpy and scipy -- it is imported by
    `screening`, which is on the detection-kernel module list, and a screen that
    drags a numeric stack into the kernel is a bigger change than the one being
    made here. Summed in log space because the counts reach ~90 against means
    near 3, where the direct terms underflow.
    """

    if observed <= 0:
        return 1.0
    if expected <= 0:
        return 0.0

    def _term(index: int) -> float:
        return math.exp(
            index * math.log(expected) - expected - math.lgamma(index + 1)
        )

    if observed <= expected:
        # The tail is not small here, so one minus the lower sum is accurate and
        # terminates immediately.
        return max(0.0, 1.0 - sum(_term(index) for index in range(observed)))

    # Summing the upper tail directly. `1 - lower` cancels catastrophically once
    # the tail drops below machine epsilon: at 32 observed against 3.32 expected
    # it returned 4.4e-16 for a true 7.2e-21, and at 90 against 3.57 it returned
    # 6.7e-16 for 1.1e-90. The flagging decision survived that -- everything
    # floors well under any Bonferroni threshold -- but the reported probability
    # is a number people quote, and it was wrong by seventy orders of magnitude.
    total = 0.0
    index = observed
    while True:
        term = _term(index)
        total += term
        index += 1
        if term <= total * 1e-17 or index > observed + 10_000:
            break
    return total


def instrumental_instants(
    ephemerides: Sequence[_Ephemeris],
    *,
    window: tuple[float, float] | None = None,
    bin_minutes: float = SHARED_INSTANT_BIN_MINUTES,
    alpha: float = SHARED_INSTANT_ALPHA,
) -> dict[str, Any]:
    """Absolute times at which far more targets dim than chance allows.

    This is the check the shared-ephemeris screen cannot make. That screen
    requires period agreement *and* phase agreement, deliberately, because
    sharing merely some instant is common by chance. But a single instrumental
    event -- a momentum dump, a scattered-light ramp -- is fitted by different
    stars at *different* periods, each placing one transit on the event. Every
    such target then shares an instant with the others and a full ephemeris with
    none of them, so the screen sees nothing while the population is visibly
    clustered in absolute time.

    Measured on the v5 calibration: 32 targets with a transit inside one
    30-minute bin against an expectation of 3.3, p = 7e-21, sharing no common
    period (correction 85).

    The null is phase-uniform: a target of period P places a transit in a bin of
    width `step` with probability step/P, so the expectation is the summed
    occupancy over the targets that could contribute. Bins are then scored by
    their Poisson upper tail and kept only if they survive a Bonferroni
    correction for the number of bins searched -- correction 82's lesson, that a
    raw ratio stops meaning the same thing when the population changes size.
    """

    step = bin_minutes / (24.0 * 60.0)
    usable = [item for item in ephemerides if item.period > 0]
    if not usable or step <= 0:
        return {
            "instants": [],
            "bins_searched": 0,
            "bin_days": step,
            "window_btjd": [0.0, 0.0],
            "_transits_by_tic": {},
        }

    if window is None:
        # Every reported epoch lies inside the data that produced it, so their
        # span is a floor on the observed window. Stated rather than assumed:
        # a caller with the real window should pass it, because a short span
        # counts fewer transits per target and weakens the test.
        low = min(item.epoch for item in usable)
        high = max(item.epoch for item in usable)
    else:
        low, high = float(window[0]), float(window[1])
    if not (high > low):
        return {
            "instants": [],
            "bins_searched": 0,
            "bin_days": step,
            "window_btjd": [0.0, 0.0],
            "_transits_by_tic": {},
        }

    # Bin the transit centres themselves rather than walking a grid of epochs
    # and re-testing every target at each one: the grid walk is bins x targets,
    # which does not survive a 64,000-target campaign, while this is linear in
    # the number of transits.
    occupancy: dict[int, set[int]] = {}
    transits_by_tic: dict[int, list[float]] = {}
    expected_per_bin = 0.0
    for item in usable:
        expected_per_bin += min(1.0, step / item.period)
        first = math.ceil((low - item.epoch) / item.period)
        last = math.floor((high - item.epoch) / item.period)
        if last < first:
            continue
        times: list[float] = []
        for number in range(first, last + 1):
            moment = item.epoch + number * item.period
            times.append(moment)
            occupancy.setdefault(int((moment - low) // step), set()).add(item.tic_id)
        transits_by_tic[item.tic_id] = times

    bins_searched = max(1, int((high - low) // step) + 1)
    threshold = alpha / bins_searched
    instants: list[dict[str, Any]] = []
    for index, holders in occupancy.items():
        observed = len(holders)
        probability = _poisson_upper_tail(observed, expected_per_bin)
        if probability >= threshold:
            continue
        instants.append(
            {
                "bin_index": index,
                "start_btjd": round(low + index * step, 6),
                "end_btjd": round(low + (index + 1) * step, 6),
                "targets": observed,
                "expected_targets": round(expected_per_bin, 6),
                "enrichment": round(observed / expected_per_bin, 6)
                if expected_per_bin > 0
                else None,
                "probability": probability,
                "tic_ids": sorted(holders),
            }
        )
    instants.sort(key=lambda entry: -int(entry["targets"]))
    return {
        "instants": instants,
        "bins_searched": bins_searched,
        "bin_days": step,
        "expected_targets_per_bin": round(expected_per_bin, 6),
        "trials_correction": f"Bonferroni, alpha={alpha} over {bins_searched} bins",
        "window_btjd": [round(low, 6), round(high, 6)],
        "window_source": "supplied" if window is not None else "epoch span (floor)",
        "_transits_by_tic": transits_by_tic,
    }


def screen_shared_instants(
    rows: Iterable[dict[str, Any]],
    *,
    metadata: dict[int, dict[str, Any]] | None = None,
    window: tuple[float, float] | None = None,
    bin_minutes: float = SHARED_INSTANT_BIN_MINUTES,
    alpha: float = SHARED_INSTANT_ALPHA,
    minimum_event_fraction: float = MIN_INSTRUMENTAL_EVENT_FRACTION,
) -> dict[int, dict[str, Any]]:
    """Flag targets whose events are mostly instrumental instants.

    A verdict per target, never a claim about the star. Coinciding with one
    instrumental instant is not disqualifying -- a real planet with five
    transits will occasionally put one on a momentum dump. What is disqualifying
    is a fit whose events are *mostly* shared: that is a signal assembled from
    the observatory's timeline rather than the star's.
    """

    collected = _collect(rows, metadata)
    report = instrumental_instants(
        collected, window=window, bin_minutes=bin_minutes, alpha=alpha
    )
    flagged_bins = {int(entry["bin_index"]): entry for entry in report["instants"]}
    transits_by_tic: dict[int, list[float]] = report.get("_transits_by_tic") or {}
    low = (report.get("window_btjd") or [0.0, 0.0])[0]
    step = report["bin_days"] or 1.0

    verdicts: dict[int, dict[str, Any]] = {}
    for item in collected:
        times = transits_by_tic.get(item.tic_id, [])
        if not times:
            continue
        on_instant = [
            moment
            for moment in times
            if int((moment - low) // step) in flagged_bins
        ]
        fraction = len(on_instant) / len(times)
        flagged = bool(on_instant) and fraction >= minimum_event_fraction
        verdicts[item.tic_id] = {
            "verdict": (
                "shared_instant_systematic" if flagged else "independent_instants"
            ),
            "observed_events": len(times),
            "events_on_shared_instants": len(on_instant),
            "shared_event_fraction": round(fraction, 6),
            "strongest_instant_targets": max(
                (int(flagged_bins[int((m - low) // step)]["targets"]) for m in on_instant),
                default=0,
            ),
            "instants_searched": report["bins_searched"],
        }
    return verdicts


def screen_campaign(
    rows: Iterable[dict[str, Any]],
    *,
    metadata: dict[int, dict[str, Any]] | None = None,
) -> dict[int, dict[str, Any]]:
    """Score every searched target for shared-time (instrumental) dimming.

    Returns one verdict per TIC. A target with no fitted ephemeris receives no
    entry at all, because absence of a measurement is not evidence.
    """

    ephemerides = _collect(rows, metadata)
    if len(ephemerides) < 2:
        return {}

    ordered = sorted(ephemerides, key=lambda item: item.period)
    periods = [item.period for item in ordered]
    by_tic = {item.tic_id: item for item in ordered}
    population = len(ordered) - 1
    # The longest period any target reached approximates the search ceiling,
    # which is where a truncated instrumental peak accumulates.
    search_ceiling = periods[-1] if periods else 0.0

    verdicts: dict[int, dict[str, Any]] = {}
    for index, ephemeris in enumerate(ordered):
        low, high = _period_neighbours(ordered, periods, index)
        tolerance = ephemeris.phase_tolerance_days()
        sharers: set[int] = set()
        for other_index in range(low, high):
            other = ordered[other_index]
            if other.tic_id == ephemeris.tic_id:
                continue
            if abs(_phase_offset(ephemeris, other)) <= tolerance:
                sharers.add(other.tic_id)

        # Uniform-phase expectation: of the targets that already share this
        # period, the fraction whose epoch would land in the window by chance.
        period_group = max(0, (high - low) - 1)
        expected = period_group * min(1.0, 2.0 * tolerance / ephemeris.period)
        count = len(sharers)
        enrichment = (
            count / expected if expected > 0 else (float(count) if count else 0.0)
        )
        shared_fraction = count / population if population else 0.0
        evidence = _detector_evidence(ephemeris, sharers, by_tic)

        flagged = (
            count >= MIN_SHARED_TARGETS
            and shared_fraction >= MIN_SHARED_FRACTION
            and enrichment >= MIN_ENRICHMENT
        )
        if not flagged:
            verdict = "independent_timing"
        elif (
            evidence["cameras_spanned"] >= 2
            or (evidence["sky_spread_deg"] or 0.0) >= OBSERVATORY_SPREAD_DEG
        ):
            verdict = "common_mode_systematic"
        else:
            verdict = "localized_coincidence"

        harmonic = spacecraft_harmonic(ephemeris.period)
        duration = ephemeris.duration_hours
        verdicts[ephemeris.tic_id] = {
            "verdict": verdict,
            "spacecraft_harmonic": harmonic["ratio"] if harmonic else None,
            "spacecraft_harmonic_period_days": (
                harmonic["period_days"] if harmonic else None
            ),
            "spacecraft_harmonic_offset_fraction": (
                harmonic["offset_fraction"] if harmonic else None
            ),
            "duration_at_grid_rail": (
                duration is not None
                and duration in (DURATION_GRID_HOURS[0], DURATION_GRID_HOURS[-1])
            ),
            "period_at_search_ceiling": (
                search_ceiling > 0
                and abs(ephemeris.period - search_ceiling) / search_ceiling
                <= SEARCH_CEILING_TOLERANCE
            ),
            "shared_targets": count,
            "shared_fraction": round(shared_fraction, 6),
            "period_group_targets": period_group,
            "expected_shared_targets": round(expected, 3),
            "enrichment": round(enrichment, 2) if math.isfinite(enrichment) else None,
            "phase_tolerance_days": round(tolerance, 5),
            "shared_epoch_btjd": round(ephemeris.epoch, 4),
            "campaign_targets": population + 1,
            **evidence,
        }
    return verdicts


COMMON_MODE_LABELS = {
    "common_mode_systematic": "Observatory-wide shared dimming",
    "localized_coincidence": "Shared dimming with close neighbours",
    "independent_timing": "Timing not shared with other targets",
}


def _load_target_metadata(path: Path) -> dict[int, dict[str, Any]]:
    metadata: dict[int, dict[str, Any]] = {}
    if not path.exists():
        return metadata
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                try:
                    metadata[int(float(str(row.get("tic_id"))))] = row
                except (TypeError, ValueError):
                    continue
    except (OSError, csv.Error, UnicodeDecodeError):
        return {}
    return metadata


def _backfill_ephemeris_fields(
    rows: list[dict[str, Any]], summary_path: Path, root: Path
) -> int:
    """Fill missing epochs from the per-target reports beside the summary.

    `_collect` needs a period *and* an epoch; a row with only a period is
    dropped. Older campaign summaries never wrote `transit_time`, so those
    campaigns were skipped in full and the screen reported "no campaign carried
    a fitted ephemeris" -- indistinguishable from "nothing to screen". Measured
    on this workspace: **8 of 25** campaigns with a summary carry no epoch in
    any row, which is the likeliest mechanism behind correction 71's finding
    that 85% of the survey was never common-mode screened.

    The epoch was never actually missing. It is in each per-target residual
    report, which the summary row already points at. Returns the number of rows
    repaired so callers can report it rather than silently benefiting.
    """

    repaired = 0
    for row in rows:
        if _finite(row.get("transit_time")) is not None:
            continue
        report = row.get("report")
        if not report:
            continue
        candidates = [Path(str(report))]
        if not candidates[0].is_absolute():
            candidates.append(root / str(report))
        # A campaign directory that has been moved or copied still has its
        # reports beside the summary, so try there before giving up.
        candidates.append(summary_path.parent / Path(str(report)).name)
        for candidate in candidates:
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            signal = payload.get("strongest_residual_signal")
            if not isinstance(signal, dict):
                continue
            epoch = _finite(signal.get("transit_time"))
            if epoch is None:
                continue
            row["transit_time"] = epoch
            if _finite(row.get("duration_hours")) is None:
                duration = _finite(signal.get("duration_hours"))
                if duration is not None:
                    row["duration_hours"] = duration
            repaired += 1
            break
    return repaired


def _unsummarised_campaigns(
    campaign_root: str | Path, summaries: Sequence[Path]
) -> list[Path]:
    """Campaign directories this screen cannot see, newest-looking first.

    ``batch_summary.json`` is written once, at the end of a run. A campaign that
    is still going -- or that was interrupted -- has only ``batch_progress.json``,
    so ``rglob`` on summaries does not find it and it is screened as though it did
    not exist. `full_remaining_pool` spent four days in exactly that state.
    """

    screened_directories = {path.parent.resolve() for path in summaries}
    unsummarised: list[Path] = []
    for progress_path in sorted(Path(campaign_root).rglob("batch_progress.json")):
        directory = progress_path.parent
        if directory.resolve() in screened_directories:
            continue
        unsummarised.append(directory)
    return unsummarised


def screen_campaign_root(
    campaign_root: str | Path, workspace: str | Path = "."
) -> dict[str, Any]:
    """Screen every saved campaign under a root, one campaign at a time.

    Campaigns are screened independently because the systematic being measured
    is shared by targets observed *together*. Pooling separate sectors would
    dilute a real common-mode event and could manufacture coincidences between
    stars that were never observed at the same time.
    """

    root = Path(workspace).resolve()
    campaigns: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    verdicts: dict[int, dict[str, Any]] = {}

    summaries = sorted(Path(campaign_root).rglob("batch_summary.json"))
    for directory in _unsummarised_campaigns(campaign_root, summaries):
        skipped.append(
            {
                "campaign": directory.name,
                "summary": None,
                "reason": "no_batch_summary",
                "detail": (
                    "The campaign has a progress file but no batch_summary.json, so "
                    "it is still running or was interrupted. The summary is written "
                    "once at the end of a run, and this screen reads only summaries."
                ),
            }
        )
    for summary_path in summaries:
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            skipped.append(
                {
                    "campaign": summary_path.parent.name,
                    "summary": str(summary_path),
                    "reason": "unreadable_summary",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        rows = summary.get("results")
        if not isinstance(rows, list) or not rows:
            skipped.append(
                {
                    "campaign": summary_path.parent.name,
                    "summary": str(summary_path),
                    "reason": "no_result_rows",
                    "detail": "The summary carries no `results` list to screen.",
                }
            )
            continue
        target_list = str(summary.get("target_list") or "")
        metadata_path = Path(target_list)
        if target_list and not metadata_path.is_absolute():
            metadata_path = root / metadata_path
        metadata = _load_target_metadata(metadata_path)
        repaired = _backfill_ephemeris_fields(rows, summary_path, root)

        screened = screen_campaign(rows, metadata=metadata)
        if not screened:
            skipped.append(
                {
                    "campaign": summary_path.parent.name,
                    "summary": str(summary_path),
                    "reason": "no_ephemeris",
                    "detail": (
                        f"None of {len(rows)} row(s) carried both a period and an "
                        "epoch, even after backfilling from the per-target reports."
                    ),
                }
            )
            continue
        name = summary_path.parent.name

        # Correction 86: the ephemeris screen above requires period *and* phase
        # agreement, so a single instrumental event fitted by different stars at
        # different periods is invisible to it -- each such target shares an
        # instant with the others and an ephemeris with none. Measured on the v5
        # calibration: 32 targets in one 30-minute bin against an expectation of
        # 3.3, p = 7e-21, every one called `independent_timing`.
        #
        # Folded into `common_mode_systematic` rather than given a status of its
        # own: both verdicts mean the observatory produced the dimming, and a new
        # status would need registry and dashboard changes to say the same thing.
        # `flagged_by` keeps the two distinguishable, and the reclassification is
        # counted below rather than applied silently -- correction 71 is the
        # record of what a quiet screen change can destroy.
        instants = screen_shared_instants(rows, metadata=metadata)
        reclassified = 0
        for tic_id, verdict in screened.items():
            instant = instants.get(tic_id)
            already = verdict["verdict"] == "common_mode_systematic"
            hit = bool(
                instant and instant["verdict"] == "shared_instant_systematic"
            )
            verdict["shared_instant"] = instant
            if already and hit:
                verdict["flagged_by"] = "ephemeris_and_instant"
            elif already:
                verdict["flagged_by"] = "ephemeris"
            elif hit:
                verdict["flagged_by"] = "instant"
                verdict["verdict_before_instant_screen"] = verdict["verdict"]
                verdict["verdict"] = "common_mode_systematic"
                reclassified += 1
            else:
                verdict["flagged_by"] = None

        counts: dict[str, int] = {}
        for tic_id, verdict in screened.items():
            verdict["campaign"] = name
            counts[verdict["verdict"]] = counts.get(verdict["verdict"], 0) + 1
            previous = verdicts.get(tic_id)
            # Keep the strongest evidence when a target appears twice.
            if previous is None or verdict["shared_targets"] > previous["shared_targets"]:
                verdicts[tic_id] = verdict
        campaigns.append(
            {
                "campaign": name,
                "summary": str(summary_path),
                "target_list": target_list or None,
                "screened_targets": len(screened),
                "detector_metadata_available": bool(metadata),
                "counts": counts,
                # The transition, stated per campaign. Correction 71's method
                # note: check what moved between the old screen and the new one,
                # not the headline totals, which improve either way.
                "reclassified_by_instant_screen": reclassified,
            }
        )

    totals: dict[str, int] = {}
    for verdict in verdicts.values():
        totals[verdict["verdict"]] = totals.get(verdict["verdict"], 0) + 1
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "warning": (
            "A shared ephemeris is evidence that the observatory, not the star, "
            "produced the dimming. This screen does not certify the remaining "
            "signals as planets."
        ),
        "settings": {
            "period_tolerance": PERIOD_TOLERANCE,
            "minimum_phase_tolerance_days": MIN_PHASE_TOLERANCE_DAYS,
            "minimum_enrichment": MIN_ENRICHMENT,
            "minimum_shared_fraction": MIN_SHARED_FRACTION,
            "minimum_shared_targets": MIN_SHARED_TARGETS,
            "observatory_spread_deg": OBSERVATORY_SPREAD_DEG,
        },
        "campaigns": campaigns,
        "skipped_campaigns": sorted(skipped, key=lambda item: str(item["campaign"])),
        "counts": totals,
        "screened_targets": len(verdicts),
        "verdicts": {str(tic): value for tic, value in sorted(verdicts.items())},
    }
