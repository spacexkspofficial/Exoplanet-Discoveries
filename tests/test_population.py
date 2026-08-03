"""The absolute-time dip registry."""

from __future__ import annotations

import numpy as np

from exohunt.population import (
    CohortDipRegistries,
    DipRegistryAccumulator,
    build_dip_registry,
    cohort_key,
    registry_windows,
    star_bin_dips,
)

RNG = np.random.default_rng(99)


def _cohort(
    stars: int,
    *,
    shared_dip_fraction: float = 0.0,
    shared_dip_at: float = 2.0,
    start: float = 0.0,
    span: float = 5.0,
    seed: int | None = None,
) -> list[tuple[int, np.ndarray, np.ndarray]]:
    time = np.arange(start, start + span, 10.0 / (24 * 60))
    rng = RNG if seed is None else np.random.default_rng(seed)
    cohort = []
    dip_count = int(round(stars * shared_dip_fraction))
    for index in range(stars):
        flux = 1.0 + rng.normal(0.0, 500e-6, time.size)
        if index < dip_count:
            window = (time >= shared_dip_at) & (time <= shared_dip_at + 0.02)
            flux[window] -= 3_000e-6
        cohort.append((index + 1, time, flux))
    return cohort


def test_shared_dips_become_registered_windows() -> None:
    registry = build_dip_registry(
        _cohort(60, shared_dip_fraction=0.3, shared_dip_at=2.0)
    )
    assert registry["stars"] == 60
    assert len(registry["windows"]) == 1
    window = registry["windows"][0]
    assert window["start"] <= 2.0 <= window["stop"]
    assert window["peak_fraction"] >= 0.25
    assert registry["window_spans"] == [(window["start"], window["stop"])]


def test_unshared_noise_registers_nothing() -> None:
    registry = build_dip_registry(_cohort(60))
    assert registry["windows"] == []


def test_small_cohorts_cannot_register_windows() -> None:
    # Ten stars all dipping together is still below the minimum-star floor:
    # a small campaign must not manufacture systematic windows from chance.
    registry = build_dip_registry(
        _cohort(10, shared_dip_fraction=1.0, shared_dip_at=2.0)
    )
    assert registry["windows"] == []


def test_empty_cohort_is_handled() -> None:
    registry = build_dip_registry([])
    assert registry["windows"] == []
    assert registry["stars"] == 0


# --------------------------------------------------------------------------
# Characterization: streaming accumulation must equal the one-shot builder.
# These pin the behaviour the campaign wiring depends on, and were written
# before that wiring existed.
# --------------------------------------------------------------------------


def test_accumulating_one_star_at_a_time_equals_the_one_shot_builder() -> None:
    # The campaign cannot hold every light curve to build the registry at the
    # end, so it folds stars in as they finish. That must not change the
    # answer.
    cohort = _cohort(60, shared_dip_fraction=0.3, shared_dip_at=2.0, seed=7)
    one_shot = build_dip_registry(cohort)
    accumulator = DipRegistryAccumulator()
    for _tic, time, flux in cohort:
        accumulator.add_curve(time, flux)
    assert accumulator.build() == one_shot


def test_registry_does_not_depend_on_completion_order() -> None:
    # Targets finish in whatever order downloads and workers deliver them.
    cohort = _cohort(60, shared_dip_fraction=0.3, shared_dip_at=2.0, seed=11)
    forward = build_dip_registry(cohort)
    reversed_order = build_dip_registry(list(reversed(cohort)))
    assert forward == reversed_order


def test_windows_are_anchored_in_absolute_time_not_cohort_start() -> None:
    # Real cohorts start near BTJD 4070, not zero, and the accumulator has no
    # cohort minimum available when the first star arrives.
    offset = 4070.0
    cohort = _cohort(
        60,
        shared_dip_fraction=0.3,
        shared_dip_at=offset + 2.0,
        start=offset,
        span=5.0,
        seed=13,
    )
    registry = build_dip_registry(cohort)
    assert len(registry["windows"]) == 1
    window = registry["windows"][0]
    assert window["start"] <= offset + 2.0 <= window["stop"]


def test_star_bin_dips_reports_only_bins_the_star_actually_covers() -> None:
    # Absence must mean "not observed" rather than "observed and steady", so
    # an unobserved bin never lands in the denominator.
    cohort = _cohort(1, span=1.0, seed=17)
    _tic, time, flux = cohort[0]
    flags = star_bin_dips(time, flux)
    covered = len(flags)
    assert covered > 0
    shifted = star_bin_dips(time + 10.0, flux)
    assert len(shifted) == covered
    assert set(shifted).isdisjoint(set(flags))


def test_too_short_a_curve_contributes_nothing() -> None:
    time = np.linspace(0.0, 0.05, 5)
    assert star_bin_dips(time, np.ones_like(time)) == {}


# --------------------------------------------------------------------------
# Characterization: cohorts are per sector-camera-CCD (MASTER_PLAN 3.6).
# --------------------------------------------------------------------------


def test_cohort_key_is_stable_and_marks_missing_detectors() -> None:
    assert cohort_key(100, 1, 2) == "s100-cam1-ccd2"
    assert cohort_key(100, None, 2) == "s100-camunknown-ccd2"
    assert cohort_key() == "sunknown-camunknown-ccdunknown"
    # A float sector from a CSV must not produce a second, distinct cohort.
    assert cohort_key(100.0, 1.0, 2.0) == cohort_key(100, 1, 2)


def test_one_camera_dipping_does_not_register_a_window_on_another() -> None:
    # Scattered light hits a detector, not the sky. A window registered on
    # camera 1 must not veto events on camera 3.
    registries = CohortDipRegistries()
    for _tic, time, flux in _cohort(
        40, shared_dip_fraction=0.5, shared_dip_at=2.0, seed=23
    ):
        registries.add_curve(time, flux, sector=100, camera=1, ccd=1)
    for _tic, time, flux in _cohort(40, seed=29):
        registries.add_curve(time, flux, sector=100, camera=3, ccd=1)

    built = registries.build()
    assert set(built) == {"s100-cam1-ccd1", "s100-cam3-ccd1"}
    assert len(built["s100-cam1-ccd1"]["windows"]) == 1
    assert built["s100-cam3-ccd1"]["windows"] == []
    assert registries.windows_for("s100-cam3-ccd1") == []
    assert len(registries.windows_for("s100-cam1-ccd1")) == 1
    # Each registry names the cohort it was measured on.
    assert built["s100-cam1-ccd1"]["cohort"] == "s100-cam1-ccd1"


def test_pooling_two_cohorts_would_dilute_a_real_window() -> None:
    # The containment rule, stated as a measurement. Ten stars dipping inside
    # a 40-star cohort is 25% and registers; the same ten diluted across 160
    # stars is 6.25%, under the 10% floor, and the real systematic window
    # disappears. Pooling detectors does not average the evidence -- it
    # destroys it.
    dipping = _cohort(
        40, shared_dip_fraction=0.25, shared_dip_at=1.0, span=2.0, seed=31
    )
    unrelated = _cohort(120, span=2.0, seed=37)
    assert len(build_dip_registry(dipping)["windows"]) == 1
    assert build_dip_registry(dipping + unrelated)["windows"] == []


def test_unknown_cohort_returns_no_windows_rather_than_guessing() -> None:
    registries = CohortDipRegistries()
    for _tic, time, flux in _cohort(
        40, shared_dip_fraction=0.5, shared_dip_at=2.0, seed=41
    ):
        registries.add_curve(time, flux, sector=100, camera=1, ccd=1)
    assert registries.windows_for("s105-cam2-ccd3") == []


# --------------------------------------------------------------------------
# Characterization: what the registry does on pure noise. The T3 secondary
# scan taught this lesson the expensive way -- a rule that looks principled
# can still kill a third of pure-noise folds, so the noise rate is measured
# and capped rather than assumed.
# --------------------------------------------------------------------------


def test_pure_noise_cohorts_register_windows_far_below_one_percent() -> None:
    trials = 40
    registering = 0
    for trial in range(trials):
        registry = build_dip_registry(
            _cohort(30, span=3.0, seed=1000 + trial)
        )
        registering += int(bool(registry["windows"]))
    # Permanent regression cap. The floors (>=20 stars, >=10% dipping at
    # 3 sigma each) put the chance coincidence rate orders of magnitude below
    # this; the cap exists so a future threshold change cannot quietly make
    # the screen start inventing systematic windows.
    assert registering / trials <= 0.01, (
        f"{registering}/{trials} pure-noise cohorts registered a window"
    )


def test_a_small_cohort_cannot_register_even_when_every_star_dips() -> None:
    registry = build_dip_registry(
        _cohort(10, shared_dip_fraction=1.0, shared_dip_at=2.0, seed=53)
    )
    assert registry["windows"] == []


# --------------------------------------------------------------------------
# Characterization: reading a registry back must fail safe.
# --------------------------------------------------------------------------


def test_registry_windows_reads_spans_and_degrades_to_empty() -> None:
    registry = build_dip_registry(
        _cohort(60, shared_dip_fraction=0.3, shared_dip_at=2.0, seed=59)
    )
    spans = registry_windows(registry)
    assert spans == [tuple(pair) for pair in registry["window_spans"]]
    # An absent or malformed screen must read as "not applied", never as a
    # clean one.
    assert registry_windows(None) == []
    assert registry_windows({}) == []
    assert registry_windows({"windows": [{"start": 1.0}]}) == []
    assert registry_windows({"window_spans": [(2.0, 1.0)]}) == []
    assert registry_windows({"window_spans": [("a", "b")]}) == []
    assert registry_windows(
        {"window_spans": [(float("nan"), 1.0)]}
    ) == []


def test_registry_windows_recovers_spans_from_windows_alone() -> None:
    # Older payloads carry `windows` without the derived `window_spans`.
    payload = {"windows": [{"start": 1.0, "stop": 2.0}]}
    assert registry_windows(payload) == [(1.0, 2.0)]
