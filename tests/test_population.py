"""The absolute-time dip registry."""

from __future__ import annotations

import numpy as np

from exohunt.population import build_dip_registry

RNG = np.random.default_rng(99)


def _cohort(
    stars: int,
    *,
    shared_dip_fraction: float = 0.0,
    shared_dip_at: float = 2.0,
) -> list[tuple[int, np.ndarray, np.ndarray]]:
    time = np.arange(0.0, 5.0, 10.0 / (24 * 60))
    cohort = []
    dip_count = int(round(stars * shared_dip_fraction))
    for index in range(stars):
        flux = 1.0 + RNG.normal(0.0, 500e-6, time.size)
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
