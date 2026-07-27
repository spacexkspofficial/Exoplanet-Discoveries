"""Shared, edge-safe detrending for TESS light curves.

Savitzky–Golay estimates are least constrained at the beginning and end of a
continuous segment.  Feeding those edge estimates into BLS manufactured shared
events at sector starts and around downlink gaps.  This module makes the
segmentation rule explicit and removes the half-window whose trend estimate
lacks symmetric support.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True, slots=True)
class DetrendingConfig:
    """Named science settings for the campaign detrending pass."""

    window_days: float = 2.0
    minimum_window_cadences: int = 101
    # A gap at least this long starts an independently detrended segment, whose
    # edges are then guarded below.
    #
    # Measured on real Sector 100 photometry: a half-day threshold left the
    # 3.80-hour gap at BTJD 4080.708 and the 5.20-hour gap at 4094.105 inside a
    # single segment, so no guard was applied around them and the shared
    # artificial event near BTJD 4080.87 survived for 3 of 14 sampled targets.
    # At 0.10 days those interruptions do create boundaries and all three moved
    # off the artifact epoch. The cost is real -- retention fell from 83 to 67
    # percent of cadences, charged against long-period sensitivity -- so it is
    # recorded per target in the report metadata rather than hidden here.
    segment_gap_days: float = 0.10
    minimum_break_tolerance_cadences: int = 5
    # A Savitzky–Golay value needs samples on both sides.  Half the window is
    # therefore excluded at each physical or downlink-created segment edge.
    edge_guard_window_fraction: float = 0.5


DEFAULT_DETRENDING = DetrendingConfig()


@dataclass(frozen=True, slots=True)
class DetrendingPlan:
    cadence_days: float
    window_cadences: int
    break_tolerance_cadences: int
    edge_guard_days: float
    segment_gap_threshold_days: float


def build_detrending_plan(
    time_values: np.ndarray,
    config: DetrendingConfig = DEFAULT_DETRENDING,
) -> DetrendingPlan:
    """Derive cadence-scaled parameters from a finite, ordered time axis."""

    time = np.asarray(time_values, dtype=float)
    finite = time[np.isfinite(time)]
    if finite.size < 3:
        raise ValueError("At least three finite time samples are required.")
    differences = np.diff(finite)
    positive = differences[np.isfinite(differences) & (differences > 0)]
    if positive.size == 0:
        raise ValueError("Time samples must contain a positive cadence.")
    cadence_days = float(np.nanmedian(positive))
    window = max(
        config.minimum_window_cadences,
        int(round(config.window_days / cadence_days)),
    )
    if window % 2 == 0:
        window += 1
    break_tolerance = max(
        config.minimum_break_tolerance_cadences,
        int(math.ceil(config.segment_gap_days / cadence_days)),
    )
    return DetrendingPlan(
        cadence_days=cadence_days,
        window_cadences=window,
        break_tolerance_cadences=break_tolerance,
        edge_guard_days=(
            config.edge_guard_window_fraction * window * cadence_days
        ),
        segment_gap_threshold_days=break_tolerance * cadence_days,
    )


def edge_safe_mask(
    time_values: np.ndarray,
    plan: DetrendingPlan,
) -> tuple[np.ndarray, int]:
    """Keep only samples with symmetric trend support inside their segment."""

    time = np.asarray(time_values, dtype=float)
    if time.ndim != 1:
        raise ValueError("Time samples must be one-dimensional.")
    if time.size == 0:
        return np.zeros(0, dtype=bool), 0
    finite = np.isfinite(time)
    if not np.all(np.diff(time[finite]) > 0):
        raise ValueError("Finite time samples must be strictly increasing.")

    split_after = np.flatnonzero(
        np.diff(time) > plan.segment_gap_threshold_days
    )
    boundaries = [0, *(int(index) + 1 for index in split_after), time.size]
    keep = np.zeros(time.size, dtype=bool)
    for start, stop in zip(boundaries, boundaries[1:]):
        segment = time[start:stop]
        if segment.size == 0:
            continue
        supported_start = segment[0] + plan.edge_guard_days
        supported_end = segment[-1] - plan.edge_guard_days
        if supported_end < supported_start:
            continue
        keep[start:stop] = (
            np.isfinite(segment)
            & (segment >= supported_start)
            & (segment <= supported_end)
        )
    return keep, len(boundaries) - 1


def flatten_edge_safe(
    normalized_light_curve: Any,
    config: DetrendingConfig = DEFAULT_DETRENDING,
) -> tuple[Any, dict[str, Any]]:
    """Flatten once, then remove trend-unsupported segment boundaries."""

    time = np.asarray(normalized_light_curve.time.value, dtype=float)
    plan = build_detrending_plan(time, config)
    flattened = normalized_light_curve.flatten(
        window_length=plan.window_cadences,
        break_tolerance=plan.break_tolerance_cadences,
    )
    flattened_time = np.asarray(flattened.time.value, dtype=float)
    keep, segment_count = edge_safe_mask(flattened_time, plan)
    kept = int(np.count_nonzero(keep))
    if kept < 3:
        raise RuntimeError(
            "No edge-safe light-curve baseline remains after detrending."
        )
    metadata = {
        "method": "savitzky_golay_edge_safe",
        "config": asdict(config),
        "cadence_days": plan.cadence_days,
        "window_cadences": plan.window_cadences,
        "break_tolerance_cadences": plan.break_tolerance_cadences,
        "segment_gap_threshold_days": plan.segment_gap_threshold_days,
        "edge_guard_days": plan.edge_guard_days,
        "segment_count": segment_count,
        "input_cadences": int(flattened_time.size),
        "retained_cadences": kept,
        "edge_cadences_removed": int(flattened_time.size - kept),
    }
    return flattened[keep], metadata
