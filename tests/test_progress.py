"""In-flight stage tracking for the dashboard's live panel."""

from __future__ import annotations

import threading

from exohunt.progress import STAGE_MODULES, STAGES, StageTracker


def test_a_target_moves_through_stages_and_clears() -> None:
    tracker = StageTracker()
    assert tracker.snapshot() == []

    tracker.begin(12345, target="TIC 12345", stage="downloading")
    rows = tracker.snapshot()
    assert len(rows) == 1
    assert rows[0]["tic_id"] == 12345
    assert rows[0]["stage"] == "downloading"
    assert rows[0]["module"] == STAGE_MODULES["downloading"]
    assert rows[0]["stage_index"] == STAGES.index("downloading")
    assert rows[0]["stage_count"] == len(STAGES)

    tracker.stage(12345, "searching")
    assert tracker.snapshot()[0]["stage"] == "searching"
    assert tracker.snapshot()[0]["module"] == STAGE_MODULES["searching"]

    tracker.finish(12345)
    assert tracker.snapshot() == []


def test_instrumentation_never_raises_on_unexpected_input() -> None:
    # A failure here would kill a twelve-hour campaign for a progress label.
    tracker = StageTracker()
    tracker.stage(999, "searching")      # never registered
    tracker.finish(999)                  # never registered
    tracker.begin(None)                  # no tic id
    tracker.stage(None, "searching")
    tracker.finish(None)
    tracker.begin(7, target="TIC 7")
    tracker.stage(7, "not-a-real-stage")  # unknown stage
    row = tracker.snapshot()[0]
    assert row["stage"] == "not-a-real-stage"
    assert row["stage_index"] == len(STAGES) - 1  # sorts last, does not crash
    tracker.finish(7)
    tracker.finish(7)                     # double finish


def test_stage_elapsed_resets_but_total_elapsed_does_not() -> None:
    tracker = StageTracker()
    tracker.begin(1, stage="downloading")
    # Drive time forward explicitly rather than sleeping.
    entry = tracker._targets[1]
    entry["started_at"] -= 100.0
    entry["stage_started_at"] -= 100.0
    before = tracker.snapshot()[0]
    assert before["elapsed_seconds"] >= 100.0
    assert before["stage_elapsed_seconds"] >= 100.0

    tracker.stage(1, "searching")
    after = tracker.snapshot()[0]
    assert after["elapsed_seconds"] >= 100.0      # total keeps counting
    assert after["stage_elapsed_seconds"] < 1.0   # per-stage restarts


def test_repeated_begin_resets_rather_than_duplicating() -> None:
    tracker = StageTracker()
    tracker.begin(5, stage="downloading")
    tracker.begin(5, stage="preparing")
    rows = tracker.snapshot()
    assert len(rows) == 1
    assert rows[0]["stage"] == "preparing"


def test_concurrent_workers_do_not_corrupt_the_registry() -> None:
    # Four analysis threads plus two download threads share one tracker.
    tracker = StageTracker()

    def worker(base: int) -> None:
        for i in range(200):
            tic = base * 1000 + i
            tracker.begin(tic, target=f"TIC {tic}")
            tracker.stage(tic, "searching")
            tracker.snapshot()
            tracker.finish(tic)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert tracker.snapshot() == []


def test_snapshot_orders_longest_running_first() -> None:
    tracker = StageTracker()
    tracker.begin(1)
    tracker.begin(2)
    tracker._targets[2]["started_at"] -= 50.0
    rows = tracker.snapshot()
    assert [row["tic_id"] for row in rows] == [2, 1]


def test_every_stage_names_a_module() -> None:
    # The panel shows the module beside the stage; a gap would render blank.
    assert set(STAGES) <= set(STAGE_MODULES)
