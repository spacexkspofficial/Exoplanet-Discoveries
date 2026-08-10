"""Coverage for the monotransit false-event threshold calibration."""

import json
from pathlib import Path

from scripts import calibrate_monotransit_threshold as calibration


def test_a_resumed_run_retries_the_stars_that_errored(tmp_path: Path) -> None:
    """A throttled query and a missing light curve raise the same error.

    The first full pass lost 25 stars whose FITS were already cached, to
    parallel MAST pressure. Treating an error as "done" would bake that loss
    into the artifact; retrying it under lighter load is what tells the two
    apart.
    """

    journal = tmp_path / "stars.jsonl"
    journal.write_text(
        json.dumps({"tic_id": 1, "inverted_events": [], "direct_events": []})
        + "\n"
        + json.dumps({"tic_id": 2, "error": "download: RuntimeError: nope"})
        + "\n",
        encoding="utf-8",
    )

    completed = calibration.load_completed(journal)

    assert set(completed) == {1}


def test_a_truncated_journal_line_does_not_lose_the_whole_run(
    tmp_path: Path,
) -> None:
    """A half-written last line is a kill, not a reason to re-measure 900 stars."""

    journal = tmp_path / "stars.jsonl"
    journal.write_text(
        json.dumps({"tic_id": 1, "inverted_events": [], "direct_events": []})
        + "\n"
        + '{"tic_id": 2, "inverted_ev',
        encoding="utf-8",
    )

    assert set(calibration.load_completed(journal)) == {1}


def test_no_journal_is_an_empty_resume_not_an_error(tmp_path: Path) -> None:
    assert calibration.load_completed(tmp_path / "absent.jsonl") == {}


def _star(tic_id: int, inverted: list[tuple[float, bool]]) -> dict:
    return {
        "tic_id": tic_id,
        "inverted_events": [
            {"significance": significance, "passes": passes}
            for significance, passes in inverted
        ],
        "direct_events": [],
    }


def test_a_vetoed_event_is_not_counted_as_a_false_alarm() -> None:
    """The detector already rejects it, so it is not something it would report."""

    stars = [_star(1, [(12.0, False), (11.0, False)]), _star(2, [])]

    curve = calibration.threshold_curve(stars, floor=8.0, ceiling=8.0, step=1.0)

    assert curve[0]["false_events"] == 0
    assert curve[0]["false_events_per_star"] == 0.0


def test_the_rate_is_per_star_not_per_event() -> None:
    """Two false events on one star is 1.0/star over two stars, not 2.0."""

    stars = [_star(1, [(9.0, True), (10.0, True)]), _star(2, [])]

    point = calibration.threshold_curve(stars, floor=8.0, ceiling=8.0, step=1.0)[0]

    assert point["false_events"] == 2
    assert point["false_events_per_star"] == 1.0
    assert point["stars_with_a_false_event"] == 1


def test_the_curve_is_monotonic_in_the_threshold() -> None:
    """Raising the threshold can only remove events, never add them."""

    stars = [
        _star(1, [(8.5, True), (9.5, True), (14.0, True)]),
        _star(2, [(11.0, True)]),
    ]

    curve = calibration.threshold_curve(stars, floor=8.0, ceiling=15.0, step=0.5)
    rates = [point["false_events_per_star"] for point in curve]

    assert rates == sorted(rates, reverse=True)


def test_the_calibrated_threshold_is_the_first_that_meets_the_budget() -> None:
    """The lowest passing threshold, because a higher one costs recovery."""

    stars = [_star(index, [(8.0 + index * 0.5, True)]) for index in range(10)]

    curve = calibration.threshold_curve(stars, floor=8.0, ceiling=13.0, step=0.5)
    passing = calibration._first_passing(curve)

    assert passing is not None
    assert passing["false_events_per_star"] <= calibration.FALSE_EVENT_BUDGET_PER_STAR
    earlier = [
        point for point in curve if point["threshold"] < passing["threshold"]
    ]
    assert all(
        point["false_events_per_star"] > calibration.FALSE_EVENT_BUDGET_PER_STAR
        for point in earlier
    )


def test_no_passing_threshold_reports_none_rather_than_the_ceiling() -> None:
    """A budget that cannot be met must not be reported as met at the ceiling."""

    stars = [_star(index, [(30.0, True)]) for index in range(10)]

    curve = calibration.threshold_curve(stars, floor=8.0, ceiling=12.0, step=1.0)

    assert calibration._first_passing(curve) is None


def test_a_star_that_errored_is_excluded_from_the_denominator() -> None:
    """Dividing by stars that were never searched would flatter the rate."""

    stars = [
        _star(1, [(9.0, True)]),
        {"tic_id": 2, "error": "download: RuntimeError: no light curve"},
    ]

    point = calibration.threshold_curve(stars, floor=8.0, ceiling=8.0, step=1.0)[0]

    assert point["false_events_per_star"] == 1.0
