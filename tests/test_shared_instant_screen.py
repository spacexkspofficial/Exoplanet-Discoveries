"""The shared-instant screen (correction 85).

The shared-ephemeris screen requires period agreement *and* phase agreement,
deliberately, because sharing merely some instant is common by chance. That
makes it blind to a single instrumental event: different stars fit it at
different periods, each placing one transit on the event, so every one of them
shares an instant with the others and a full ephemeris with none of them.

Measured on the v5 calibration: 32 targets with a transit inside one 30-minute
bin against an expectation of 3.3, p = 7e-21, sharing no common period. The
ephemeris screen called all of them `independent_timing`.
"""

from __future__ import annotations

import math

from exohunt.commonmode import (
    MIN_INSTRUMENTAL_EVENT_FRACTION,
    _poisson_upper_tail,
    instrumental_instants,
    screen_campaign,
    screen_shared_instants,
)


def _row(tic_id: int, period: float, epoch: float, duration: float = 2.0) -> dict:
    return {
        "tic_id": tic_id,
        "period_days": period,
        "transit_time": epoch,
        "duration_hours": duration,
        "ra_deg": 150.0 + (tic_id % 40),
        "dec_deg": -20.0 + (tic_id % 30),
    }


def test_the_poisson_tail_survives_catastrophic_cancellation() -> None:
    """`1 - lower_sum` floors at machine epsilon and silently lied.

    At 32 observed against 3.32 expected it returned 4.4e-16 for a true
    7.2e-21, and at 90 against 3.57 it returned 6.7e-16 for 1.1e-90. The
    flagging decision survived, because everything floors well under any
    Bonferroni threshold, but the reported probability is a number people quote.
    """

    assert math.isclose(_poisson_upper_tail(32, 3.32), 7.249058e-21, rel_tol=1e-6)
    assert math.isclose(_poisson_upper_tail(90, 3.57), 1.084290e-90, rel_tol=1e-6)
    # And the ordinary regime still agrees. rel_tol matches the significant
    # figures in the literal, not the precision of the function -- against scipy
    # the agreement is ~1e-13 relative across this whole range.
    assert math.isclose(_poisson_upper_tail(12, 2.94), 5.914451e-05, rel_tol=1e-6)
    assert _poisson_upper_tail(0, 3.0) == 1.0
    assert _poisson_upper_tail(5, 0.0) == 0.0


def test_one_instant_fitted_at_many_periods_is_caught() -> None:
    """The case the ephemeris screen structurally cannot see.

    Forty stars each place a transit at BTJD 1000.0 and every one of them has a
    different period, so no two share an ephemeris.
    """

    event = 1000.0
    rows = [_row(9000 + i, 3.0 + 0.37 * i, event) for i in range(40)]

    ephemeris = screen_campaign(rows, metadata=None)
    shared = {
        tic
        for tic, v in ephemeris.items()
        if v["verdict"] == "common_mode_systematic"
    }
    instants = screen_shared_instants(rows, metadata=None, window=(999.0, 1002.0))
    flagged = {
        tic
        for tic, v in instants.items()
        if v["verdict"] == "shared_instant_systematic"
    }

    assert shared == set(), "the ephemeris screen is expected to miss this"
    assert len(flagged) >= 35


def test_a_real_planet_is_not_flagged_for_one_coincidence() -> None:
    """A five-transit planet may put one event on a momentum dump.

    Only a fit whose events are *mostly* shared is a signal assembled from the
    observatory's timeline. Measured on the calibration: 0 of 67 real injected
    planets flagged.
    """

    event = 1000.0
    rows = [_row(9000 + i, 3.0 + 0.37 * i, event) for i in range(40)]
    # A planet with ten transits, one of which lands on the shared instant.
    rows.append(_row(12345, 1.0, event))

    verdicts = screen_shared_instants(
        rows, metadata=None, window=(999.0, 1009.0)
    )

    planet = verdicts[12345]
    assert planet["observed_events"] >= 5
    assert planet["shared_event_fraction"] < MIN_INSTRUMENTAL_EVENT_FRACTION
    assert planet["verdict"] == "independent_instants"


def test_phase_uniform_ephemerides_produce_no_instants() -> None:
    """The null. Nothing shared means nothing flagged."""

    rows = [_row(7000 + i, 5.0 + 0.11 * i, 1000.0 + 0.31 * i) for i in range(60)]

    report = instrumental_instants(
        [
            type(
                "E",
                (),
                {
                    "tic_id": r["tic_id"],
                    "period": r["period_days"],
                    "epoch": r["transit_time"],
                    "duration_hours": r["duration_hours"],
                },
            )()
            for r in rows
        ],
        window=(1000.0, 1030.0),
    )

    assert report["instants"] == []


def test_the_verdict_records_its_evidence_rather_than_just_a_label() -> None:
    event = 1000.0
    rows = [_row(9000 + i, 3.0 + 0.37 * i, event) for i in range(40)]

    verdicts = screen_shared_instants(rows, metadata=None, window=(999.0, 1002.0))
    sample = next(
        v for v in verdicts.values() if v["verdict"] == "shared_instant_systematic"
    )

    assert sample["events_on_shared_instants"] >= 1
    assert sample["strongest_instant_targets"] >= 35
    assert sample["instants_searched"] > 0


def test_the_trials_correction_scales_with_the_number_of_bins() -> None:
    """A longer window searches more instants and must demand more evidence."""

    rows = [_row(9000 + i, 3.0 + 0.37 * i, 1000.0) for i in range(40)]

    narrow = instrumental_instants(
        [
            type(
                "E",
                (),
                {
                    "tic_id": r["tic_id"],
                    "period": r["period_days"],
                    "epoch": r["transit_time"],
                    "duration_hours": r["duration_hours"],
                },
            )()
            for r in rows
        ],
        window=(999.0, 1001.0),
    )
    wide = instrumental_instants(
        [
            type(
                "E",
                (),
                {
                    "tic_id": r["tic_id"],
                    "period": r["period_days"],
                    "epoch": r["transit_time"],
                    "duration_hours": r["duration_hours"],
                },
            )()
            for r in rows
        ],
        window=(900.0, 1100.0),
    )

    assert wide["bins_searched"] > narrow["bins_searched"]
    assert "Bonferroni" in wide["trials_correction"]
