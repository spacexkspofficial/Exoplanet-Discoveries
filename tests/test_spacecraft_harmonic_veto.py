"""The TESS-orbit harmonic veto (owner decision 2b, correction 72).

Correction 72 measured three computed-but-unused flags enriched among
automated survivors. Only this one is promoted into the kernel. The other two,
`duration_at_grid_rail` and `period_at_search_ceiling`, are defined in
`commonmode` against the retired fixed 0.25-6.0 h duration grid: the live
`duration_min_hours` is 0.5, so commonmode's lower rail is not reachable by any
current search. Promoting a stale grid constant into the frozen detection path
would be the correction-57 shape -- a check whose inputs no longer mean what
its name says. The campaign path already rejects rails against the live grid.
"""

from __future__ import annotations

import dataclasses

from exohunt.commonmode import (
    DURATION_GRID_HOURS,
    HARMONIC_TOLERANCE,
    TESS_ORBIT_DAYS,
    spacecraft_harmonic,
)
from exohunt.config import CURRENT_CONFIG
from exohunt.screening import _screening_flags


class _Result:
    """Minimal stand-in for a DetectionResult."""

    def __init__(self, period_days: float) -> None:
        self.period_days = period_days
        self.duration_hours = 3.0
        self.depth_snr = 12.0
        self.observed_transits = 4
        self.odd_even_depth_difference_sigma = 0.1
        self.secondary_snr = 0.1
        self.depth_ppm = 5000.0


def test_a_period_on_the_tess_orbit_is_flagged() -> None:
    flags = _screening_flags(_Result(TESS_ORBIT_DAYS))
    assert flags["period_on_spacecraft_harmonic"] is True


def test_the_half_harmonic_is_flagged() -> None:
    flags = _screening_flags(_Result(TESS_ORBIT_DAYS / 2))
    assert flags["period_on_spacecraft_harmonic"] is True


def test_an_ordinary_period_is_not_flagged() -> None:
    # 8.0 d sits in the gap between the 1/2 (6.85 d) and 2/3 (9.13 d) ratios.
    # Note how narrow the gaps are: 8.9 d, which looks unremarkable, is inside
    # the 2/3 band at an offset of 2.6%.
    period = 8.0
    assert spacecraft_harmonic(period) is None
    flags = _screening_flags(_Result(period))
    assert flags["period_on_spacecraft_harmonic"] is False


def test_the_veto_costs_a_measurable_slice_of_the_search_range() -> None:
    """Quantify what this veto removes, so decision 5B can price it.

    Eight ratios at +/-3% each is not a negligible exclusion, and the periods
    are not exotic: 6.85 d and 13.7 d are ordinary planet periods. This test
    exists to keep the cost visible rather than to enforce a threshold.
    """

    import numpy as np

    grid = np.linspace(0.5, 20.0, 20_000)
    vetoed = sum(1 for period in grid if spacecraft_harmonic(float(period)) is not None)
    fraction = vetoed / grid.size
    # Measured at ~19% of the 0.5-20 d range on a linear grid.
    assert 0.10 < fraction < 0.30


def test_the_veto_can_be_switched_off_to_measure_its_cost() -> None:
    """Decision 5B has to be able to price this veto's completeness loss.

    Planets do exist at 6.85 d; the veto's own module says so. A calibration
    run with the flag off measures the surface without it.
    """

    import exohunt.config as config_module

    original = CURRENT_CONFIG
    without = dataclasses.replace(
        original,
        search=dataclasses.replace(original.search, veto_spacecraft_harmonic=False),
    )
    config_module.CURRENT_CONFIG = without
    try:
        flags = _screening_flags(_Result(TESS_ORBIT_DAYS))
        assert flags["period_on_spacecraft_harmonic"] is False
    finally:
        config_module.CURRENT_CONFIG = original


def test_commonmodes_duration_rail_is_unreachable_under_the_live_grid() -> None:
    """Records why the other two flags were not promoted into the kernel.

    If this ever fails, the grids have been reconciled and the decision to
    leave `duration_at_grid_rail` out of the kernel should be revisited.
    """

    assert DURATION_GRID_HOURS[0] == 0.25
    assert CURRENT_CONFIG.search.duration_min_hours == 0.5
    assert DURATION_GRID_HOURS[0] < CURRENT_CONFIG.search.duration_min_hours


def test_the_harmonic_tolerance_is_the_one_that_was_measured() -> None:
    # Correction 72's 1.60x enrichment was measured at this tolerance; moving
    # it silently would invalidate the number the decision rests on.
    assert HARMONIC_TOLERANCE == 0.03
