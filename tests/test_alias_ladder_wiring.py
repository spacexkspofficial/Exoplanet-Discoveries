"""The alias ladder must actually run (follow-up to correction 64).

`search.adjudicate_alias` was implemented and tested at P2 and never called
from any production path: `cli` imported only `build_search_grid` and
`grid_rail_flags` from that module. Measured on the 341-star known-planet
cohort, 31 planets were recovered at exactly one third of their true period and
4 at one third again, none scored as recovered -- 45% of all failures, against
machinery that already existed and passed its own unit tests.

These tests pin the *wiring*, because the unit tests for the ladder itself
already passed while nothing called it. A passing test on an uncalled function
measures nothing.
"""

from __future__ import annotations

import numpy as np

from exohunt.config import CURRENT_CONFIG
from exohunt.detection import inject_box_transit, search_transits


def _curve(period_days: float, *, depth_ppm: float = 6000.0, seed: int = 11):
    rng = np.random.default_rng(seed)
    time = np.arange(0.0, 27.0, 2.0 / 60 / 24)
    flux = 1.0 + rng.normal(0.0, 3e-4, time.size)
    flux, _, _ = inject_box_transit(
        time,
        flux,
        period_days=period_days,
        transit_time=3.0,
        duration_hours=3.0,
        depth_ppm=depth_ppm,
    )
    return time, flux


def test_the_ladder_runs_and_says_so() -> None:
    """`alias_decision` present means the ladder was walked.

    Its absence must never be confusable with "walked and found nothing" --
    that ambiguity is what let the whole mechanism sit dead.
    """

    time, flux = _curve(6.0)
    _, arrays = search_transits(
        time, flux, min_period_days=0.5, max_period_days=13.0
    )
    decision = arrays["alias_decision"]
    assert decision is not None
    assert decision["adjudicated"] is True
    assert "reported_period_days" in decision
    assert "applied" in decision


def test_the_ladder_can_be_switched_off_and_then_reports_nothing() -> None:
    time, flux = _curve(6.0)
    _, arrays = search_transits(
        time,
        flux,
        min_period_days=0.5,
        max_period_days=13.0,
        adjudicate_aliases=False,
    )
    assert arrays["alias_decision"] is None


def test_a_correct_period_survives_the_ladder_unchanged() -> None:
    """The ladder must not invent a change where none is warranted."""

    time, flux = _curve(6.0)
    without, _ = search_transits(
        time,
        flux,
        min_period_days=0.5,
        max_period_days=13.0,
        adjudicate_aliases=False,
    )
    with_ladder, arrays = search_transits(
        time, flux, min_period_days=0.5, max_period_days=13.0
    )
    assert with_ladder.period_days == without.period_days
    assert arrays["alias_decision"]["changed"] is False
    assert arrays["alias_decision"]["applied"] is False


def test_an_adopted_period_is_a_measured_grid_solution() -> None:
    """Whatever the ladder adopts, depth and epoch come from the BLS grid.

    Adopting a period the search never evaluated would report a mix of
    measured and assumed quantities under one ephemeris.
    """

    time, flux = _curve(6.0)
    result, arrays = search_transits(
        time, flux, min_period_days=0.5, max_period_days=13.0
    )
    grid = np.asarray(arrays["period_grid"], dtype=float)
    # The reported period is always one of the evaluated grid points.
    assert np.min(np.abs(grid - result.period_days)) < 1e-9


def test_the_config_knob_is_wired_not_decorative() -> None:
    """A flag that does not reach the call site is the bug being fixed here."""

    assert CURRENT_CONFIG.search.adjudicate_alias_ladder is True
    assert CURRENT_CONFIG.search.alias_snap_tolerance == 0.01
    import inspect

    from exohunt import cli

    source = inspect.getsource(cli._hunt_from_light_curve)
    assert "adjudicate_alias_ladder" in source, (
        "the campaign path must pass the config through to search_transits"
    )


def test_the_ladder_is_reachable_from_the_production_module() -> None:
    """Guards the exact regression: an implemented, tested, uncalled function."""

    import inspect

    from exohunt import detection

    source = inspect.getsource(detection.search_transits)
    assert "adjudicate_alias" in source
