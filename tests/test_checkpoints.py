"""Truthful repair of orphaned worker checkpoints."""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from exohunt.checkpoints import repair_stale_checkpoints
from exohunt.lease import acquire_machine_lock


def _write_checkpoint(
    path: Path,
    *,
    state: str,
    minutes_ago: float,
    extra: dict | None = None,
) -> dict:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    payload = {
        "schema_version": 1,
        "state": state,
        "started_at_utc": (moment - timedelta(hours=2)).isoformat(),
        "updated_at_utc": moment.isoformat(),
        "total_targets": 5000,
        "completed_targets": 24,
        "counts": {"survivor": 2, "rejected": 22, "error": 0},
        **(extra or {}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    stamp = moment.timestamp()
    os.utime(path, (stamp, stamp))
    return payload


def _unique_lock() -> str:
    return f"exohunt-test-{uuid.uuid4().hex}"


def test_stale_running_checkpoint_is_repaired_with_audit(tmp_path: Path) -> None:
    results = tmp_path / "results"
    stale = results / "campaign" / "sector100_spoc" / "batch_progress.json"
    original = _write_checkpoint(stale, state="running", minutes_ago=47.0)

    report = repair_stale_checkpoints(
        results,
        lock_name=_unique_lock(),
        lock_directory=tmp_path / "locks",
        force_file_lock=True,
    )

    assert not report["refused"]
    assert [row["previous_state"] for row in report["repaired"]] == ["running"]
    repaired = json.loads(stale.read_text(encoding="utf-8"))
    assert repaired["state"] == "interrupted"
    assert repaired["repair"]["previous_state"] == "running"
    # Everything except the state and the audit block is untouched.
    assert repaired["counts"] == original["counts"]
    assert repaired["completed_targets"] == original["completed_targets"]
    assert repaired["updated_at_utc"] == original["updated_at_utc"]


def test_fresh_and_terminal_checkpoints_are_left_alone(tmp_path: Path) -> None:
    results = tmp_path / "results"
    fresh = results / "vetting" / "context_vet_progress.json"
    _write_checkpoint(fresh, state="running", minutes_ago=1.0)
    done = results / "campaign" / "old" / "batch_progress.json"
    _write_checkpoint(done, state="completed", minutes_ago=600.0)

    report = repair_stale_checkpoints(
        results,
        lock_name=_unique_lock(),
        lock_directory=tmp_path / "locks",
        force_file_lock=True,
    )

    assert report["repaired"] == []
    assert json.loads(fresh.read_text(encoding="utf-8"))["state"] == "running"
    assert json.loads(done.read_text(encoding="utf-8"))["state"] == "completed"


def test_dry_run_reports_without_writing(tmp_path: Path) -> None:
    results = tmp_path / "results"
    stale = results / "campaign" / "x" / "science_vet_progress.json"
    _write_checkpoint(stale, state="finalizing", minutes_ago=120.0)
    before = stale.read_text(encoding="utf-8")

    report = repair_stale_checkpoints(
        results,
        dry_run=True,
        lock_name=_unique_lock(),
        lock_directory=tmp_path / "locks",
        force_file_lock=True,
    )

    assert len(report["repaired"]) == 1
    assert stale.read_text(encoding="utf-8") == before


def test_repair_refuses_while_a_coordinator_is_live(tmp_path: Path) -> None:
    results = tmp_path / "results"
    stale = results / "campaign" / "x" / "batch_progress.json"
    _write_checkpoint(stale, state="running", minutes_ago=99.0)

    name = _unique_lock()
    live = acquire_machine_lock(
        name, directory=tmp_path / "locks", force_file_lock=True
    )
    assert live is not None
    try:
        report = repair_stale_checkpoints(
            results,
            lock_name=name,
            lock_directory=tmp_path / "locks",
            force_file_lock=True,
        )
    finally:
        live.release()

    assert report["refused"]
    assert json.loads(stale.read_text(encoding="utf-8"))["state"] == "running"


def test_mtime_newer_than_updated_at_counts_as_activity(tmp_path: Path) -> None:
    """A checkpoint being actively rewritten is not stale even if its
    recorded timestamp lags (the writer throttles updated_at to one write
    per five seconds)."""

    results = tmp_path / "results"
    path = results / "campaign" / "y" / "batch_progress.json"
    _write_checkpoint(path, state="running", minutes_ago=45.0)
    now = time.time()
    os.utime(path, (now, now))

    report = repair_stale_checkpoints(
        results,
        lock_name=_unique_lock(),
        lock_directory=tmp_path / "locks",
        force_file_lock=True,
    )

    assert report["repaired"] == []
