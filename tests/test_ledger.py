"""The evidence ledger: append-only rows, projections, and leases."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from exohunt import ledger


@pytest.fixture()
def conn(tmp_path: Path):
    connection = ledger.connect(tmp_path / "ledger.db")
    yield connection
    connection.close()


def test_connect_enables_wal(conn) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_append_evidence_is_idempotent(conn) -> None:
    first = ledger.append_evidence(
        conn,
        tic_id=1,
        kind="screening",
        source="summary:a",
        verdict="automated_survivor",
        payload={"snr": 9.0},
    )
    duplicate = ledger.append_evidence(
        conn,
        tic_id=1,
        kind="screening",
        source="summary:a",
        verdict="automated_survivor",
        payload={"snr": 9.0},
    )
    assert first is not None and duplicate is None
    count = conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    assert count == 1


def test_unknown_verdicts_are_rejected(conn) -> None:
    with pytest.raises(ValueError, match="status registry"):
        ledger.append_evidence(
            conn,
            tic_id=1,
            kind="screening",
            source="x",
            verdict="definitely_a_planet",
            payload={},
        )


def test_upsert_star_enriches_without_blanking(conn) -> None:
    ledger.upsert_star(conn, 42, name="TIC 42", tmag=11.5)
    ledger.upsert_star(conn, 42, teff_k=3400.0)
    row = conn.execute("SELECT * FROM star WHERE tic_id = 42").fetchone()
    assert row["name"] == "TIC 42"
    assert row["tmag"] == 11.5
    assert row["teff_k"] == 3400.0


def test_projection_follows_stage_precedence_and_later_wins(conn) -> None:
    def add(tic, kind, source, verdict):
        ledger.append_evidence(
            conn, tic_id=tic, kind=kind, source=source, verdict=verdict,
            payload={"label": verdict, "notes": ""},
        )

    # Star 1: population screen outranks measured science; human outranks all.
    add(1, "screening", "s1", "automated_survivor")
    add(1, "science", "sci1", "science_vetted_lead")
    add(1, "common_mode", "cm1", "common_mode_systematic")
    # Star 2: human outcome overrides the population screen.
    add(2, "screening", "s1", "automated_survivor")
    add(2, "common_mode", "cm1", "common_mode_systematic")
    add(2, "human_outcome", "log:1", "false_positive")
    # Star 3: equal-rank human outcomes -- later evidence wins.
    add(3, "screening", "s1", "automated_survivor")
    add(3, "human_outcome", "log:2", "false_positive")
    add(3, "human_outcome", "log:3", "rediscovery")
    # Star 4: rows excluded from state do not vote.
    add(4, "screening", "s1", "no_transit_detected")
    excluded = ledger.append_evidence(
        conn, tic_id=4, kind="screening", source="s2",
        verdict="automated_survivor", payload={}, affects_state=False,
    )
    assert excluded is not None

    counts = ledger.rebuild_star_state(conn)
    states = {
        row["tic_id"]: row["status"]
        for row in conn.execute("SELECT tic_id, status FROM star_state")
    }
    assert states[1] == "common_mode_systematic"
    assert states[2] == "false_positive"
    assert states[3] == "rediscovery"
    assert states[4] == "no_transit_detected"
    assert counts == ledger.status_counts(conn)
    assert sum(counts.values()) == 4


def test_evidence_counts_report_the_historical_reading(conn) -> None:
    for source in ("a", "b", "c"):
        ledger.append_evidence(
            conn, tic_id=7, kind="screening", source=source,
            verdict="automated_survivor", payload={},
            affects_state=source == "c",
        )
    ledger.rebuild_star_state(conn)
    # One star is currently a survivor, but three survivor conclusions were
    # logged -- the 541-versus-3,939 distinction, preserved by design.
    assert ledger.status_counts(conn) == {"automated_survivor": 1}
    assert ledger.evidence_counts(conn) == {"automated_survivor": 3}


def test_lease_lifecycle_with_takeover_audit(conn) -> None:
    now = datetime.now(timezone.utc)
    assert (
        ledger.acquire_db_lease(
            conn, name="coordinator", holder="alpha", now=now
        )
        == "acquired"
    )
    assert (
        ledger.acquire_db_lease(
            conn, name="coordinator", holder="beta", now=now
        )
        == "denied"
    )
    assert (
        ledger.acquire_db_lease(
            conn, name="coordinator", holder="alpha", now=now
        )
        == "refreshed"
    )
    later = now + timedelta(seconds=600)
    assert (
        ledger.acquire_db_lease(
            conn, name="coordinator", holder="beta", ttl_seconds=300, now=later
        )
        == "taken_over"
    )
    takeovers = conn.execute(
        "SELECT payload FROM event_log WHERE kind = 'lease_takeover'"
    ).fetchall()
    assert len(takeovers) == 1
    payload = json.loads(takeovers[0]["payload"])
    assert payload["previous_holder"] == "alpha"
    assert payload["new_holder"] == "beta"
    status = ledger.lease_status(conn, "coordinator", now=later)
    assert status is not None and status["holder"] == "beta"
    assert ledger.heartbeat_db_lease(conn, name="coordinator", holder="beta")
    assert not ledger.heartbeat_db_lease(
        conn, name="coordinator", holder="alpha"
    )
    assert ledger.release_db_lease(conn, name="coordinator", holder="beta")
    assert ledger.lease_status(conn, "coordinator") is None


def test_second_writer_waits_or_fails_cleanly(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    first = ledger.connect(db)
    second = ledger.connect(db, busy_timeout_ms=100)
    try:
        first.execute("BEGIN IMMEDIATE")
        first.execute(
            "INSERT INTO event_log (kind, payload, created_at_utc) "
            "VALUES ('x', '{}', 'now')"
        )
        with pytest.raises(sqlite3.OperationalError):
            ledger.append_evidence(
                second, tic_id=1, kind="screening", source="s",
                verdict=None, payload={},
            )
            second.commit()
        first.commit()
        assert (
            ledger.append_evidence(
                second, tic_id=1, kind="screening", source="s",
                verdict=None, payload={},
            )
            is not None
        )
        second.commit()
    finally:
        first.close()
        second.close()


_CRASH_WRITER = """
import sys, json
from exohunt import ledger
conn = ledger.connect(sys.argv[1])
i = 0
print("started", flush=True)
while True:
    ledger.append_evidence(
        conn, tic_id=i, kind="screening", source=f"s{i}",
        verdict="no_transit_detected", payload={"i": i},
    )
    conn.commit()
    i += 1
"""


def test_database_survives_a_killed_writer(tmp_path: Path) -> None:
    import os

    db = tmp_path / "ledger.db"
    src = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src) + os.pathsep + env.get("PYTHONPATH", "")
    child = subprocess.Popen(
        [sys.executable, "-c", _CRASH_WRITER, str(db)],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "started"
        time.sleep(1.0)
    finally:
        child.kill()
        child.wait(timeout=30)
    conn = ledger.connect(db)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        rows = conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        assert rows > 0, "committed rows must survive the crash"
        # The ledger remains writable after an unclean writer death.
        assert (
            ledger.append_evidence(
                conn, tic_id=10**9, kind="screening", source="after-crash",
                verdict=None, payload={},
            )
            is not None
        )
        conn.commit()
    finally:
        conn.close()
