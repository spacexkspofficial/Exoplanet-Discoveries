"""Coverage for the calibration -> dashboard progress bridge."""

import json
from pathlib import Path

from scripts import publish_calibration_progress as publisher


def _log(tmp_path: Path, *lines: str) -> Path:
    directory = tmp_path / "calib"
    directory.mkdir(exist_ok=True)
    path = directory / "calibration_v3-20260810-175946.stdout.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return directory


def test_the_latest_progress_line_wins(tmp_path: Path) -> None:
    """The log grows all run; only the last line is current."""

    directory = _log(
        tmp_path,
        "Scientific signature: sig1:abc",
        "P3 100/6760 searches; 2/1000 stars; 50/hr; ETA 99 h; errors 0",
        "P3 774/6760 searches; 18/1000 stars; 203/hr; ETA 29.4 h; errors 3",
    )

    progress = publisher.read_progress(directory)

    assert progress == {
        "searches": 774,
        "searches_total": 6760,
        "stars": 18,
        "stars_total": 1000,
        "errors": 3,
    }


def test_elapsed_runs_from_the_first_log_not_the_restarted_one(
    tmp_path: Path,
) -> None:
    """A resume must not divide cumulative work by the new process's clock.

    The driver skips stars it already has, so a resumed run's first progress
    line already reads 1333/6760 searches. Measuring elapsed from the restart
    reported 953,657 searches/hour -- the cold-start trap wearing a different
    hat.
    """

    directory = tmp_path / "calib"
    directory.mkdir()
    first = directory / "calibration_v3-20260810-175946.stdout.log"
    first.write_text("P3 1/6760 searches; 0/1000 stars; 1/hr; ETA 1 h; errors 0\n", encoding="utf-8")
    second = directory / "calibration_v3-20260810-222514.stdout.log"
    second.write_text(
        "P3 1333/6760 searches; 31/1000 stars; 953657/hr; ETA 0.006 h; errors 0\n",
        encoding="utf-8",
    )
    import os

    # Make the restart log unambiguously newer than the original.
    os.utime(first, (1, 1))

    started = publisher._started_at(directory)

    assert started is not None
    # 17:59:46, the original launch -- not 22:25:14.
    assert started.hour == 17 and started.minute == 59
    # And the progress still comes from the newest log.
    assert publisher.read_progress(directory)["searches"] == 1333


def test_a_log_without_a_progress_line_is_not_an_error(tmp_path: Path) -> None:
    """Startup writes a signature and a catalog warm before any search."""

    directory = _log(tmp_path, "Scientific signature: sig1:abc", "Warming catalog...")

    assert publisher.read_progress(directory) is None


def test_the_rolling_rate_differences_samples_rather_than_dividing_elapsed() -> None:
    """Correction 55's trap: a wall clock divided by a count is not a rate.

    The first hour here completes nothing, as a cold start does. Averaged over
    the whole two hours that reads 50/h; a 60-minute rolling window drops the
    dead hour and reports the 100/h the run is actually sustaining.
    """

    samples = [
        {"at": 0.0, "searches": 0.0},
        {"at": 3600.0, "searches": 0.0},
        {"at": 7200.0, "searches": 100.0},
    ]

    rolling, count = publisher._rolling_rate(samples, 60.0, "searches")
    over_everything, _ = publisher._rolling_rate(samples, 24 * 60.0, "searches")

    assert rolling == 100.0
    assert count == 2
    # The trap it is avoiding, kept visible rather than described.
    assert over_everything == 50.0


def test_a_missing_counter_reports_no_rate_rather_than_a_confident_zero() -> None:
    """An absent measurement is "measuring", not "stopped".

    The sampler recorded only `searches`, so differencing `stars` defaulted both
    ends to 0.0 and published rolling_stars_per_hour: 0.0 for a run doing 16.9
    stars/hour. The panel's live rate read zero on a healthy run.
    """

    samples = [
        {"at": 0.0, "searches": 0.0},
        {"at": 3600.0, "searches": 600.0},
    ]

    stars_rate, _ = publisher._rolling_rate(samples, 60.0, "stars")
    searches_rate, _ = publisher._rolling_rate(samples, 60.0, "searches")

    assert stars_rate is None
    assert searches_rate == 600.0


def test_samples_from_a_different_calibration_are_discarded(tmp_path: Path) -> None:
    """The publish directory is reused across runs; the rate history must not be.

    v4's samples file opened holding v3's counters. Differencing across that
    boundary invents a rate out of a step change between unrelated runs.
    """

    samples_path = tmp_path / "rate_samples.json"
    samples_path.write_text(
        json.dumps(
            {
                "calibration_dir": "results/p5/calibration_v3",
                "samples": [{"at": 1.0, "searches": 774.0, "stars": 35.0}],
            }
        ),
        encoding="utf-8",
    )

    assert publisher.load_samples(samples_path, Path("results/p5/calibration_v4")) == []
    kept = publisher.load_samples(samples_path, Path("results/p5/calibration_v3"))
    assert len(kept) == 1


def test_a_legacy_bare_list_of_samples_is_discarded(tmp_path: Path) -> None:
    """It carries no provenance, so it cannot be shown to belong to this run."""

    samples_path = tmp_path / "rate_samples.json"
    samples_path.write_text(
        json.dumps([{"at": 1.0, "searches": 774.0}]), encoding="utf-8"
    )

    assert publisher.load_samples(samples_path, Path("anything")) == []


def test_a_single_sample_reports_no_rate_rather_than_zero() -> None:
    rate, count = publisher._rolling_rate([{"at": 0.0, "searches": 5.0}], 30.0, "searches")

    assert rate is None
    assert count == 1


def test_the_published_state_keeps_it_out_of_the_exporter(tmp_path: Path) -> None:
    """A monitoring artifact must not become a synthetic campaign.

    `dashboard.py` folds any checkpoint in running/finalizing/retry_pending into
    `active_results` and `_sector_coverage`. Publishing `calibrating`, with no
    target list and no result rows, stays visible to the live panel and
    invisible to exported science.
    """

    directory = _log(
        tmp_path, "P3 774/6760 searches; 18/1000 stars; 203/hr; ETA 29 h; errors 0"
    )

    checkpoint = publisher.build_checkpoint(directory, [])

    assert checkpoint is not None
    assert checkpoint["state"] not in {"running", "finalizing", "retry_pending"}
    assert checkpoint["target_list"] == ""
    assert checkpoint["results"] == []


def test_stars_per_hour_carries_stars_not_searches(tmp_path: Path) -> None:
    """The label has to mean what it says.

    Injection stars cost 43 searches each and the rest cost 3, so searches/hour
    is roughly 40x stars/hour early on. Putting the wrong one behind a field the
    dashboard renders as `stars_per_hour` is the exact family of mislabelling
    this project keeps finding.
    """

    directory = _log(
        tmp_path, "P3 774/6760 searches; 18/1000 stars; 203/hr; ETA 29 h; errors 0"
    )

    checkpoint = publisher.build_checkpoint(directory, [])
    assert checkpoint is not None
    performance = checkpoint["runtime"]["performance"]
    elapsed = performance["elapsed_hours"]

    assert elapsed is not None and elapsed > 0
    # 18 stars over the elapsed window, not 774 searches over it.
    assert abs(performance["average_stars_per_hour"] - 18 / elapsed) < 1e-6
    assert abs(
        checkpoint["calibration"]["average_searches_per_hour"] - 774 / elapsed
    ) < 1e-6
