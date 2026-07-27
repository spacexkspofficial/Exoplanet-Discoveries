"""Characterization and regression tests for campaign detrending."""

from __future__ import annotations

import math

import numpy as np

from exohunt.detrending import (
    DEFAULT_DETRENDING,
    build_detrending_plan,
    edge_safe_mask,
    flatten_edge_safe,
)


CADENCE_DAYS = 120.0 / 86_400.0


class _Values:
    def __init__(self, values: np.ndarray):
        self.value = values


class _FakeLightCurve:
    def __init__(
        self,
        time: np.ndarray,
        *,
        flatten_calls: list[dict[str, int]] | None = None,
    ):
        self.time = _Values(np.asarray(time, dtype=float))
        self.flux = _Values(np.ones_like(self.time.value))
        self.flatten_calls = flatten_calls if flatten_calls is not None else []

    def flatten(self, **kwargs):
        self.flatten_calls.append(kwargs)
        return _FakeLightCurve(self.time.value.copy(), flatten_calls=self.flatten_calls)

    def __getitem__(self, selection):
        return _FakeLightCurve(
            self.time.value[selection],
            flatten_calls=self.flatten_calls,
        )


def test_two_minute_plan_does_not_split_an_ordinary_short_gap() -> None:
    before = np.arange(0.0, 2.0, CADENCE_DAYS)
    # A 13-minute interruption exceeded the old five-cadence tolerance.
    after = np.arange(before[-1] + 13.0 / 1_440.0, 4.0, CADENCE_DAYS)
    time = np.concatenate([before, after])

    plan = build_detrending_plan(time)
    keep, segment_count = edge_safe_mask(time, plan)

    assert np.max(np.diff(time)) > 5 * plan.cadence_days
    assert np.max(np.diff(time)) < plan.segment_gap_threshold_days
    assert plan.window_cadences == 1441
    # Expressed against the configured threshold rather than a cadence count, so
    # tuning segment_gap_days does not break a test about gap tolerance.
    assert plan.break_tolerance_cadences >= math.ceil(
        DEFAULT_DETRENDING.segment_gap_days / plan.cadence_days
    )
    assert plan.segment_gap_threshold_days >= DEFAULT_DETRENDING.segment_gap_days
    assert segment_count == 1
    assert np.count_nonzero(keep) > 0


def test_true_downlink_gap_is_split_and_each_edge_is_guarded() -> None:
    first = np.arange(0.0, 10.0, CADENCE_DAYS)
    second = np.arange(11.0, 21.0, CADENCE_DAYS)
    time = np.concatenate([first, second])
    plan = build_detrending_plan(time)

    keep, segment_count = edge_safe_mask(time, plan)

    assert segment_count == 2
    assert not keep[np.argmin(np.abs(time - 0.4))]
    assert keep[np.argmin(np.abs(time - 5.0))]
    assert not keep[np.argmin(np.abs(time - 9.6))]
    assert not keep[np.argmin(np.abs(time - 11.4))]
    assert keep[np.argmin(np.abs(time - 16.0))]
    assert not keep[np.argmin(np.abs(time - 20.6))]


def test_sector_start_and_gap_epochs_lack_symmetric_trend_support() -> None:
    first = np.arange(4074.0, 4080.5, CADENCE_DAYS)
    second = np.arange(4081.2, 4090.0, CADENCE_DAYS)
    time = np.concatenate([first, second])
    plan = build_detrending_plan(time)

    keep, _ = edge_safe_mask(time, plan)

    # The partial campaign's shared events lie in precisely these unsupported
    # edge zones; an interior astrophysical event remains searchable.
    assert not keep[np.argmin(np.abs(time - 4074.4))]
    assert not keep[np.argmin(np.abs(time - 4080.2))]
    assert not keep[np.argmin(np.abs(time - 4081.5))]
    assert keep[np.argmin(np.abs(time - 4077.0))]


def test_shared_flatten_path_records_reproducible_settings() -> None:
    time = np.concatenate(
        [
            np.arange(0.0, 10.0, CADENCE_DAYS),
            np.arange(11.0, 21.0, CADENCE_DAYS),
        ]
    )
    light_curve = _FakeLightCurve(time)

    flattened, metadata = flatten_edge_safe(light_curve)

    assert light_curve.flatten_calls == [
        {
            "window_length": metadata["window_cadences"],
            "break_tolerance": metadata["break_tolerance_cadences"],
        }
    ]
    assert metadata["method"] == "savitzky_golay_edge_safe"
    assert metadata["segment_count"] == 2
    assert metadata["edge_cadences_removed"] > 0
    assert metadata["retained_cadences"] == flattened.time.value.size
    assert flattened.time.value.min() > 0.9
    assert flattened.time.value.max() < 20.1


def test_detrending_belongs_to_scientific_identity() -> None:
    """A different detrending pass must invalidate report reuse.

    Detrending decides which cadences BLS ever sees. If it were absent from the
    scientific settings, a resumed campaign would reuse reports produced under
    the previous segmentation rule and mix two reductions in one catalog.
    """

    import argparse
    from dataclasses import replace

    from exohunt.cli import _scientific_settings
    from exohunt.detrending import DEFAULT_DETRENDING

    args = argparse.Namespace(
        author="auto",
        cadence_seconds=120.0,
        min_period=0.5,
        max_period=10.0,
        mask_width=1.5,
        allow_no_known=True,
    )
    settings = _scientific_settings(args)

    assert settings["detrending"]["segment_gap_days"] == (
        DEFAULT_DETRENDING.segment_gap_days
    )
    assert settings["data_pipeline_version"] == "processed-lc-v3-edge-safe"

    # A report written before detrending was recorded cannot match.
    legacy = {key: value for key, value in settings.items() if key != "detrending"}
    legacy["data_pipeline_version"] = "processed-lc-v2"
    assert legacy != settings

    # Nor can one written under a different segmentation threshold.
    other = dict(settings)
    other["detrending"] = {
        **settings["detrending"],
        "segment_gap_days": replace(DEFAULT_DETRENDING, segment_gap_days=0.5).segment_gap_days,
    }
    assert other != settings


def test_short_gap_creates_a_guarded_boundary() -> None:
    """The measured Sector 100 regression: a 3.8-hour gap must split.

    Leaving that interruption inside one segment is what let the shared event
    near BTJD 4080.87 survive edge guarding.
    """

    first = np.arange(4074.3, 4080.708, CADENCE_DAYS)
    second = np.arange(4080.866, 4086.4, CADENCE_DAYS)
    time = np.concatenate([first, second])

    plan = build_detrending_plan(time)
    keep, segment_count = edge_safe_mask(time, plan)

    observed_gap = float(np.max(np.diff(time)))
    assert 0.15 < observed_gap < 0.17
    assert observed_gap > plan.segment_gap_threshold_days
    assert segment_count == 2
    # The artifact epoch now sits inside a guarded edge zone.
    assert not keep[np.argmin(np.abs(time - 4080.87))]
