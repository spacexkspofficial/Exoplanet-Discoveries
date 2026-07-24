"""Transit search and lightweight screening metrics.

This module deliberately calls its output a *signal*, not a planet candidate.
Instrumental systematics, stellar variability, and eclipsing binaries can all
produce impressive Box Least Squares (BLS) peaks.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
from astropy.timeseries import BoxLeastSquares


@dataclass(frozen=True)
class DetectionResult:
    period_days: float
    transit_time: float
    duration_hours: float
    depth_ppm: float
    depth_snr: float
    radius_ratio: float
    observed_transits: int
    odd_even_depth_difference_sigma: float | None
    secondary_depth_ppm: float | None
    secondary_snr: float | None

    def to_dict(self) -> dict[str, float | int | None]:
        return asdict(self)


def _clean_arrays(time: np.ndarray, flux: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    t = np.asarray(time, dtype=float)
    y = np.asarray(flux, dtype=float)
    keep = np.isfinite(t) & np.isfinite(y)
    t = t[keep]
    y = y[keep]
    if t.size < 100:
        raise ValueError("At least 100 finite measurements are required.")
    order = np.argsort(t)
    t = t[order]
    y = y[order]
    median = np.nanmedian(y)
    if not np.isfinite(median) or median == 0:
        raise ValueError("Flux cannot be normalized because its median is invalid.")
    return t, y / median


def _robust_scatter(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=float)
    if values.size < 2:
        return float("nan")
    center = np.nanmedian(values)
    return float(1.4826 * np.nanmedian(np.abs(values - center)))


def _point_noise(flux: np.ndarray) -> float:
    """Estimate per-cadence white noise from successive flux differences."""

    differences = np.diff(np.asarray(flux, dtype=float))
    sigma = _robust_scatter(differences) / np.sqrt(2.0)
    if not np.isfinite(sigma) or sigma <= 0:
        sigma = _robust_scatter(flux)
    if not np.isfinite(sigma) or sigma <= 0:
        raise ValueError("Could not estimate a positive photometric uncertainty.")
    return float(sigma)


def _event_depths(
    time: np.ndarray,
    flux: np.ndarray,
    period: float,
    transit_time: float,
    duration: float,
) -> tuple[np.ndarray, np.ndarray]:
    event_number = np.rint((time - transit_time) / period).astype(int)
    centers = transit_time + event_number * period
    in_event = np.abs(time - centers) <= duration / 2
    baseline = np.nanmedian(flux[~in_event])
    numbers: list[int] = []
    depths: list[float] = []
    for number in np.unique(event_number[in_event]):
        mask = in_event & (event_number == number)
        if np.count_nonzero(mask) >= 2:
            numbers.append(int(number))
            depths.append(float(baseline - np.nanmedian(flux[mask])))
    return np.asarray(numbers, dtype=int), np.asarray(depths, dtype=float)


def _odd_even_sigma(numbers: np.ndarray, depths: np.ndarray) -> float | None:
    odd = depths[numbers % 2 != 0]
    even = depths[numbers % 2 == 0]
    if odd.size < 2 or even.size < 2:
        return None
    difference = abs(float(np.nanmedian(odd) - np.nanmedian(even)))
    odd_error = _robust_scatter(odd) / np.sqrt(odd.size)
    even_error = _robust_scatter(even) / np.sqrt(even.size)
    error = np.hypot(odd_error, even_error)
    if not np.isfinite(error) or error == 0:
        return None
    return float(difference / error)


def _secondary_screen(
    time: np.ndarray,
    flux: np.ndarray,
    period: float,
    transit_time: float,
    duration: float,
) -> tuple[float | None, float | None]:
    phase = ((time - transit_time + period / 2) % period) - period / 2
    primary = np.abs(phase) <= duration / 2
    secondary_distance = np.abs(np.abs(phase) - period / 2)
    secondary = secondary_distance <= duration / 2
    baseline = ~(primary | secondary)
    n_secondary = int(np.count_nonzero(secondary))
    if n_secondary < 3 or np.count_nonzero(baseline) < 20:
        return None, None
    baseline_level = float(np.nanmedian(flux[baseline]))
    depth = baseline_level - float(np.nanmedian(flux[secondary]))
    point_scatter = _robust_scatter(flux[baseline])
    if not np.isfinite(point_scatter) or point_scatter == 0:
        return float(depth * 1e6), None
    snr = depth / (point_scatter / np.sqrt(n_secondary))
    return float(depth * 1e6), float(snr)


def signal_vetting_diagnostics(
    time: np.ndarray,
    flux: np.ndarray,
    result: DetectionResult,
) -> dict[str, object]:
    """Run inexpensive second-stage checks on the already-loaded light curve.

    These diagnostics rank follow-up work; they do not confirm or exclude a
    planet. In particular, a single event cannot establish an orbital period.
    """

    t, y = _clean_arrays(time, flux)
    period = float(result.period_days)
    transit_time = float(result.transit_time)
    duration = float(result.duration_hours) / 24.0
    phase_time = ((t - transit_time + period / 2) % period) - period / 2
    in_transit = np.abs(phase_time) <= duration / 2
    baseline_mask = np.abs(phase_time) >= duration
    numbers, depths = _event_depths(t, y, period, transit_time, duration)
    depths_ppm = depths * 1_000_000
    positive_fraction = (
        float(np.count_nonzero(depths > 0) / depths.size)
        if depths.size
        else 0.0
    )
    median_depth_ppm = (
        float(np.nanmedian(depths_ppm)) if depths_ppm.size else None
    )
    depth_scatter_ppm = (
        _robust_scatter(depths_ppm) if depths_ppm.size >= 2 else None
    )

    first_event = int(np.ceil((t[0] - transit_time) / period))
    last_event = int(np.floor((t[-1] - transit_time) / period))
    predicted_centers = (
        transit_time + np.arange(first_event, last_event + 1) * period
        if last_event >= first_event
        else np.asarray([], dtype=float)
    )
    sampled_predicted = sum(
        np.count_nonzero(np.abs(t - center) <= duration / 2) >= 2
        for center in predicted_centers
    )
    event_coverage = (
        float(sampled_predicted / predicted_centers.size)
        if predicted_centers.size
        else 0.0
    )

    cadence = float(np.nanmedian(np.diff(t)))
    points_per_duration = max(2, int(round(duration / cadence)))
    baseline_values = y[baseline_mask]
    complete_bins = baseline_values.size // points_per_duration
    red_noise_factor = 1.0
    if complete_bins >= 4:
        binned = baseline_values[: complete_bins * points_per_duration].reshape(
            complete_bins, points_per_duration
        )
        binned_means = np.nanmean(binned, axis=1)
        measured_binned_noise = _robust_scatter(binned_means)
        try:
            expected_binned_noise = _point_noise(baseline_values) / np.sqrt(
                points_per_duration
            )
        except ValueError:
            expected_binned_noise = float("nan")
        if (
            np.isfinite(measured_binned_noise)
            and np.isfinite(expected_binned_noise)
            and expected_binned_noise > 0
        ):
            red_noise_factor = max(
                1.0, min(25.0, measured_binned_noise / expected_binned_noise)
            )
    red_noise_adjusted_snr = float(result.depth_snr / red_noise_factor)

    single_event_edge_margin_durations: float | None = None
    single_event_two_sided_baseline: bool | None = None
    single_event_adjacent_gap: bool | None = None
    if numbers.size == 1:
        center = transit_time + int(numbers[0]) * period
        single_event_edge_margin_durations = float(
            min(center - t[0], t[-1] - center) / duration
        )
        before = (t >= center - 3 * duration) & (t <= center - duration)
        after = (t >= center + duration) & (t <= center + 3 * duration)
        single_event_two_sided_baseline = bool(
            np.count_nonzero(before) >= 3 and np.count_nonzero(after) >= 3
        )
        local_times = t[np.abs(t - center) <= 3 * duration]
        single_event_adjacent_gap = bool(
            local_times.size < 6
            or np.nanmax(np.diff(local_times)) > max(5 * cadence, duration)
        )

    flags: list[str] = []
    if red_noise_adjusted_snr < 7.1:
        flags.append("red-noise-adjusted depth S/N is below 7.1")
    if depths.size >= 3 and positive_fraction < 0.75:
        flags.append("fewer than 75% of sampled events have positive depth")
    if (
        depths.size >= 3
        and median_depth_ppm is not None
        and depth_scatter_ppm is not None
        and depth_scatter_ppm > abs(median_depth_ppm)
    ):
        flags.append("event-to-event depth scatter exceeds the median depth")
    if predicted_centers.size >= 2 and event_coverage < 0.6:
        flags.append("fewer than 60% of predicted events are sampled")
    if numbers.size == 1:
        if (
            single_event_edge_margin_durations is not None
            and single_event_edge_margin_durations < 3
        ):
            flags.append("single event is close to the light-curve boundary")
        if single_event_two_sided_baseline is False:
            flags.append("single event lacks a two-sided local baseline")
        if single_event_adjacent_gap:
            flags.append("single event is adjacent to a data gap")

    if numbers.size == 1:
        outcome = "supported_single_event" if not flags else "fragile_single_event"
    else:
        outcome = "passes_additional_checks" if not flags else "needs_manual_review"
    return {
        "schema_version": 1,
        "outcome": outcome,
        "warning": (
            "Additional automated vetting ranks follow-up only; it does not "
            "confirm a planet or establish that another planet is absent."
        ),
        "red_noise_factor": round(float(red_noise_factor), 3),
        "red_noise_adjusted_snr": round(red_noise_adjusted_snr, 3),
        "sampled_event_count": int(numbers.size),
        "predicted_event_count": int(predicted_centers.size),
        "event_coverage_fraction": round(event_coverage, 3),
        "positive_depth_event_fraction": round(positive_fraction, 3),
        "median_event_depth_ppm": (
            round(median_depth_ppm, 2) if median_depth_ppm is not None else None
        ),
        "event_depth_scatter_ppm": (
            round(float(depth_scatter_ppm), 2)
            if depth_scatter_ppm is not None and np.isfinite(depth_scatter_ppm)
            else None
        ),
        "single_event_edge_margin_durations": (
            round(single_event_edge_margin_durations, 2)
            if single_event_edge_margin_durations is not None
            else None
        ),
        "single_event_two_sided_baseline": single_event_two_sided_baseline,
        "single_event_adjacent_to_gap": single_event_adjacent_gap,
        "flags": flags,
    }


def search_transits(
    time: np.ndarray,
    flux: np.ndarray,
    *,
    min_period_days: float = 0.5,
    max_period_days: float = 13.5,
    durations_hours: np.ndarray | None = None,
    frequency_factor: float = 8.0,
    max_period_grid_size: int = 100_000,
) -> tuple[DetectionResult, dict[str, np.ndarray]]:
    """Return the strongest BLS signal and arrays useful for plotting.

    The returned S/N is the white-noise BLS depth statistic. It is a screening
    metric only and is usually optimistic in real TESS data with red noise.
    """

    t, y = _clean_arrays(time, flux)
    if min_period_days <= 0 or max_period_days <= min_period_days:
        raise ValueError("Period bounds must satisfy 0 < min < max.")
    span = float(t[-1] - t[0])
    if max_period_days > span:
        max_period_days = span
    if durations_hours is None:
        # Short transits around small stars can last well under an hour. A
        # mixed grid also retains sensitivity to ordinary hot-Jupiter events.
        durations_hours = np.array([0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0])
    durations_days = np.asarray(durations_hours, dtype=float) / 24.0
    durations_days = durations_days[
        (durations_days > 0) & (durations_days < min_period_days)
    ]
    if durations_days.size == 0:
        raise ValueError("No transit duration is shorter than the minimum period.")

    # Astropy's default grid density scales with baseline squared. Sparse
    # sectors separated by years can otherwise request hundreds of millions
    # of trial frequencies. Coarsen only as much as needed to stay bounded.
    if max_period_grid_size < 1_000:
        raise ValueError("Maximum period-grid size must be at least 1,000.")
    frequency_range = 1.0 / min_period_days - 1.0 / max_period_days
    minimum_duration = float(np.min(durations_days))
    required_factor = (
        frequency_range * span**2 / (max_period_grid_size * minimum_duration)
    )
    effective_frequency_factor = max(float(frequency_factor), required_factor)

    # Astropy otherwise assumes dy=1, which makes depth_err and depth S/N
    # numerically meaningless for normalized light curves. This robust estimate
    # is still a white-noise approximation, so the report labels it screening.
    point_noise = _point_noise(y)
    model = BoxLeastSquares(t, y, dy=np.full_like(y, point_noise))
    power = model.autopower(
        durations_days,
        minimum_period=min_period_days,
        maximum_period=max_period_days,
        frequency_factor=effective_frequency_factor,
    )
    best = int(np.nanargmax(power.power))
    period = float(power.period[best])
    duration = float(power.duration[best])
    transit_time = float(power.transit_time[best])
    depth = float(power.depth[best])
    depth_error = float(power.depth_err[best])
    depth_snr = depth / depth_error if depth_error > 0 else float("nan")

    numbers, event_depths = _event_depths(t, y, period, transit_time, duration)
    secondary_depth, secondary_snr = _secondary_screen(
        t, y, period, transit_time, duration
    )
    result = DetectionResult(
        period_days=period,
        transit_time=transit_time,
        duration_hours=duration * 24.0,
        depth_ppm=depth * 1e6,
        depth_snr=float(depth_snr),
        radius_ratio=float(np.sqrt(max(depth, 0.0))),
        observed_transits=int(numbers.size),
        odd_even_depth_difference_sigma=_odd_even_sigma(numbers, event_depths),
        secondary_depth_ppm=secondary_depth,
        secondary_snr=secondary_snr,
    )
    arrays = {
        "time": t,
        "flux": y,
        "period_grid": np.asarray(power.period, dtype=float),
        "power": np.asarray(power.power, dtype=float),
        "effective_frequency_factor": np.asarray(effective_frequency_factor),
        "period_grid_was_capped": np.asarray(required_factor > frequency_factor),
    }
    return result, arrays


def phase_fold(
    time: np.ndarray, flux: np.ndarray, period: float, transit_time: float
) -> tuple[np.ndarray, np.ndarray]:
    phase = ((time - transit_time + period / 2) % period) / period - 0.5
    order = np.argsort(phase)
    return phase[order], flux[order]


def binned_phase_curve(
    time: np.ndarray,
    flux: np.ndarray,
    period: float,
    transit_time: float,
    *,
    bin_count: int = 160,
    phase_min: float = -0.12,
    phase_max: float = 0.12,
) -> dict[str, object]:
    """Return a compact, display-ready phase curve from actual photometry.

    Only robust per-bin summaries are retained. This keeps the durable report
    small while preserving the detected event's shape and local scatter.
    """

    if bin_count < 1:
        raise ValueError("bin_count must be positive")
    if not phase_min < phase_max:
        raise ValueError("phase_min must be smaller than phase_max")

    phase, folded_flux = phase_fold(time, flux, period, transit_time)
    finite = np.isfinite(phase) & np.isfinite(folded_flux)
    phase = phase[finite]
    folded_flux = folded_flux[finite]
    if phase.size == 0:
        raise ValueError("phase curve requires at least one finite measurement")

    baseline = float(np.median(folded_flux))
    in_window = (phase >= phase_min) & (phase < phase_max)
    window_phase = phase[in_window]
    window_flux = folded_flux[in_window]
    if window_phase.size == 0:
        raise ValueError("phase curve window contains no measurements")

    edges = np.linspace(phase_min, phase_max, bin_count + 1)
    bin_indices = np.searchsorted(edges, window_phase, side="right") - 1
    output_phase: list[float] = []
    output_flux: list[float] = []
    output_scatter: list[float] = []
    output_count: list[int] = []
    for index in range(bin_count):
        values = window_flux[bin_indices == index]
        if values.size == 0:
            continue
        median = float(np.median(values))
        robust_scatter = float(1.4826 * np.median(np.abs(values - median)))
        output_phase.append(round(float((edges[index] + edges[index + 1]) / 2), 6))
        output_flux.append(round((median - baseline) * 1_000_000, 2))
        output_scatter.append(round(robust_scatter * 1_000_000, 2))
        output_count.append(int(values.size))

    return {
        "schema_version": 1,
        "source": "actual normalized residual TESS photometry",
        "phase_min": float(phase_min),
        "phase_max": float(phase_max),
        "bin_count": int(bin_count),
        "phase": output_phase,
        "median_residual_flux_ppm": output_flux,
        "scatter_ppm": output_scatter,
        "count": output_count,
        "measurements_total": int(phase.size),
        "measurements_in_range": int(window_phase.size),
    }


def harmonic_diagnostics(
    period_grid: np.ndarray,
    power: np.ndarray,
    best_period: float,
    *,
    window_fraction: float = 0.005,
) -> list[dict[str, float | str | bool]]:
    """Report BLS power near simple fractions and multiples of a period."""

    periods = np.asarray(period_grid, dtype=float)
    powers = np.asarray(power, dtype=float)
    best_power = float(np.nanmax(powers))
    relations = (
        ("one-third", 1.0 / 3.0),
        ("half", 0.5),
        ("double", 2.0),
        ("triple", 3.0),
    )
    diagnostics: list[dict[str, float | str | bool]] = []
    for name, factor in relations:
        reference = best_period * factor
        if reference < np.nanmin(periods) or reference > np.nanmax(periods):
            continue
        window = np.abs(periods - reference) / reference <= window_fraction
        if not np.any(window):
            index = int(np.nanargmin(np.abs(periods - reference)))
        else:
            candidates = np.flatnonzero(window)
            index = int(candidates[np.nanargmax(powers[candidates])])
        relative_power = float(powers[index] / best_power) if best_power > 0 else float("nan")
        diagnostics.append(
            {
                "relation_to_strongest": name,
                "expected_period_days": float(reference),
                "nearby_peak_period_days": float(periods[index]),
                "relative_power": relative_power,
                "plausible_alias": bool(relative_power >= 0.5),
            }
        )
    return diagnostics


def independent_period_peaks(
    period_grid: np.ndarray,
    power: np.ndarray,
    *,
    count: int = 5,
    separation_fraction: float = 0.02,
) -> list[dict[str, float]]:
    """Return separated high-power periods for human inspection."""

    periods = np.asarray(period_grid, dtype=float)
    powers = np.asarray(power, dtype=float)
    best_power = float(np.nanmax(powers))
    selected: list[int] = []
    for index in np.argsort(np.nan_to_num(powers, nan=-np.inf))[::-1]:
        period = periods[index]
        if all(
            abs(period - periods[other]) / min(period, periods[other])
            >= separation_fraction
            for other in selected
        ):
            selected.append(int(index))
        if len(selected) >= count:
            break
    return [
        {
            "period_days": float(periods[index]),
            "power": float(powers[index]),
            "relative_power": float(powers[index] / best_power) if best_power > 0 else float("nan"),
        }
        for index in selected
    ]


def _epoch_in_time_base(epoch: float, time: np.ndarray) -> float:
    """Convert a full BJD epoch to BTJD when the light curve uses BTJD."""

    median_time = float(np.nanmedian(time))
    if epoch > 2_000_000 and median_time < 100_000:
        return epoch - 2_457_000.0
    return epoch


def mask_periodic_events(
    time: np.ndarray,
    flux: np.ndarray,
    events: list[dict[str, object]],
    *,
    width_factor: float = 1.5,
) -> tuple[np.ndarray, np.ndarray, list[dict[str, object]]]:
    """Remove catalogued transit windows from a light curve.

    Each event requires ``period_days``, ``epoch_bjd``, and ``duration_hours``.
    ``width_factor`` expands the catalog duration to allow for ephemeris drift,
    imperfect durations, and transit-timing variations.
    """

    if width_factor <= 0:
        raise ValueError("Mask width factor must be positive.")
    t, y = _clean_arrays(time, flux)
    keep = np.ones(t.size, dtype=bool)
    records: list[dict[str, object]] = []
    for event in events:
        try:
            period = float(event["period_days"])
            epoch = _epoch_in_time_base(float(event["epoch_bjd"]), t)
            duration_days = float(event["duration_hours"]) / 24.0
        except (KeyError, TypeError, ValueError):
            continue
        if not np.isfinite(period) or period <= 0 or not np.isfinite(epoch):
            continue
        if not np.isfinite(duration_days) or duration_days <= 0:
            continue
        phase_time = ((t - epoch + period / 2) % period) - period / 2
        in_window = np.abs(phase_time) <= (duration_days * width_factor / 2)
        newly_removed = keep & in_window
        keep &= ~in_window
        records.append(
            {
                **event,
                "epoch_in_light_curve_time": epoch,
                "mask_width_hours": duration_days * 24.0 * width_factor,
                "removed_measurements": int(np.count_nonzero(newly_removed)),
            }
        )
    if np.count_nonzero(keep) < 100:
        raise ValueError("Known-signal masks left fewer than 100 measurements.")
    return t[keep], y[keep], records


def inject_box_transit(
    time: np.ndarray,
    flux: np.ndarray,
    *,
    period_days: float,
    transit_time: float,
    duration_hours: float,
    depth_ppm: float,
) -> tuple[np.ndarray, np.ndarray, int]:
    """Inject a deterministic box-shaped transit into normalized flux."""

    t = np.asarray(time, dtype=float)
    y = np.asarray(flux, dtype=float).copy()
    duration_days = duration_hours / 24.0
    depth = depth_ppm / 1e6
    if period_days <= 0 or duration_days <= 0 or depth <= 0:
        raise ValueError("Injected period, duration, and depth must be positive.")
    phase_time = ((t - transit_time + period_days / 2) % period_days) - period_days / 2
    in_transit = np.abs(phase_time) <= duration_days / 2
    y[in_transit] -= depth
    event_numbers = np.rint((t[in_transit] - transit_time) / period_days).astype(int)
    return y, in_transit, int(np.unique(event_numbers).size)


def fixed_ephemeris_injection_sensitivity(
    time: np.ndarray,
    flux: np.ndarray,
    *,
    periods_days: tuple[float, ...] = (1.0, 3.0, 7.0, 12.0),
    depths_ppm: tuple[float, ...] = (250.0, 500.0, 1_000.0, 2_500.0, 5_000.0),
    duration_hours: float = 2.0,
    minimum_snr: float = 7.1,
) -> dict[str, object]:
    """Estimate local transit sensitivity using compact fixed-ephemeris probes.

    This is deliberately cheaper than a full blind injection/recovery campaign.
    It measures whether known synthetic events would be detectable in the actual
    light curve and must not be interpreted as proof that a star has no planet.
    """

    t, y = _clean_arrays(time, flux)
    if not periods_days or not depths_ppm:
        raise ValueError("Sensitivity probes require periods and depths.")
    if duration_hours <= 0 or minimum_snr <= 0:
        raise ValueError("Sensitivity duration and S/N threshold must be positive.")
    if any(period <= 0 for period in periods_days):
        raise ValueError("Sensitivity periods must be positive.")
    if any(depth <= 0 for depth in depths_ppm):
        raise ValueError("Sensitivity depths must be positive.")

    rows: list[dict[str, object]] = []
    start = float(np.nanmin(t))
    for period in periods_days:
        transit_time = start + min(period * 0.23, 0.7)
        threshold: float | None = None
        sampled_events = 0
        measured_snr = 0.0
        for depth in sorted(set(float(value) for value in depths_ppm)):
            injected, _, _ = inject_box_transit(
                t,
                y,
                period_days=float(period),
                transit_time=transit_time,
                duration_hours=duration_hours,
                depth_ppm=depth,
            )
            measured = evaluate_ephemeris(
                t,
                injected,
                period_days=float(period),
                transit_time=transit_time,
                duration_hours=duration_hours,
            )
            sampled_events = int(measured["sampled_transit_events"])
            measured_snr = float(measured["depth_snr"])
            if (
                measured["sampled"]
                and sampled_events >= 2
                and measured_snr >= minimum_snr
            ):
                threshold = depth
                break
        rows.append(
            {
                "period_days": float(period),
                "minimum_recovered_depth_ppm": threshold,
                "sampled_transit_events": sampled_events,
                "snr_at_threshold_or_max_depth": measured_snr,
            }
        )

    return {
        "schema_version": 1,
        "method": "fixed-ephemeris box injection sensitivity probe",
        "warning": (
            "This is not a blind completeness measurement and cannot establish "
            "that a star is planet-free."
        ),
        "duration_hours": float(duration_hours),
        "minimum_snr": float(minimum_snr),
        "depth_grid_ppm": sorted(set(float(value) for value in depths_ppm)),
        "periods": rows,
    }


def evaluate_ephemeris(
    time: np.ndarray,
    flux: np.ndarray,
    *,
    period_days: float,
    transit_time: float,
    duration_hours: float,
) -> dict[str, float | int | bool]:
    """Measure support for a fixed ephemeris in one light-curve segment.

    This is a sector-coherence screen, not a replacement for a joint transit
    fit. It answers whether the globally detected events are independently
    sampled with positive depth in a particular sector.
    """

    t, y = _clean_arrays(time, flux)
    duration_days = duration_hours / 24.0
    if period_days <= 0 or duration_days <= 0:
        raise ValueError("Period and duration must be positive.")
    phase_time = ((t - transit_time + period_days / 2) % period_days) - period_days / 2
    in_transit = np.abs(phase_time) <= duration_days / 2
    out_of_transit = np.abs(phase_time) >= duration_days
    in_count = int(np.count_nonzero(in_transit))
    out_count = int(np.count_nonzero(out_of_transit))
    event_numbers = np.rint((t[in_transit] - transit_time) / period_days).astype(int)
    sampled_events = int(np.unique(event_numbers).size)
    if in_count < 3 or out_count < 20:
        return {
            "sampled": False,
            "in_transit_cadences": in_count,
            "out_of_transit_cadences": out_count,
            "sampled_transit_events": sampled_events,
            "depth_ppm": 0.0,
            "depth_snr": 0.0,
        }
    baseline = float(np.nanmedian(y[out_of_transit]))
    depth = baseline - float(np.nanmedian(y[in_transit]))
    point_noise = _point_noise(y[out_of_transit])
    depth_snr = depth / (point_noise / np.sqrt(in_count))
    return {
        "sampled": True,
        "in_transit_cadences": in_count,
        "out_of_transit_cadences": out_count,
        "sampled_transit_events": sampled_events,
        "depth_ppm": float(depth * 1e6),
        "depth_snr": float(depth_snr),
    }
