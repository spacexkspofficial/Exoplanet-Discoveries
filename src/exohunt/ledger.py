"""The append-only evidence ledger and its derived state projection.

Design, per MASTER_PLAN.md sections 1.2 and 7:

* **Evidence is append-only.** Every tier execution writes an immutable row;
  supersession is expressed by later rows, never by edits. The historical
  reading ("3,939 survivor conclusions were logged") and the current-best
  reading ("541 stars are in survivor state today") are two queries over one
  store and cannot silently diverge.
* **State is a projection.** A star's current status is a fold of its
  effective evidence rows through the registry's stage/precedence rules
  (:func:`exohunt.statuses.resolve_status`) -- the same function the
  dashboard exporter uses, so ledger and dashboard agree by construction.
* **SQLite in WAL mode, one writer.** A desktop daily-driver does not want a
  database service; WAL gives crash consistency and concurrent readers, and
  the schema is plain relational if the project ever outgrows the machine.
  The database lives outside the OneDrive-synced tree (see
  :mod:`exohunt.paths`).
* **Leases are rows with heartbeats.** Liveness is heartbeat age, never a
  ``state`` string in a file; takeovers of stale leases are recorded in the
  event log.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .paths import default_db_path
from .statuses import STATUS_REGISTRY, resolve_status

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS star (
    tic_id INTEGER PRIMARY KEY,
    name TEXT,
    gaia_source_id INTEGER,
    ra_deg REAL,
    dec_deg REAL,
    distance_pc REAL,
    tmag REAL,
    teff_k REAL,
    stellar_radius_solar REAL,
    lane TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tic_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    verdict TEXT,
    affects_state INTEGER NOT NULL DEFAULT 1,
    signature TEXT,
    source TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE (tic_id, kind, source)
);
CREATE INDEX IF NOT EXISTS evidence_by_tic ON evidence (tic_id, evidence_id);
CREATE INDEX IF NOT EXISTS evidence_by_signature ON evidence (signature);
CREATE INDEX IF NOT EXISTS evidence_by_kind_state
ON evidence (kind, affects_state, evidence_id);
CREATE INDEX IF NOT EXISTS evidence_by_kind_tic
ON evidence (kind, tic_id, evidence_id);
CREATE INDEX IF NOT EXISTS evidence_by_kind_source ON evidence (kind, source);
CREATE INDEX IF NOT EXISTS evidence_by_verdict ON evidence (verdict);
CREATE TABLE IF NOT EXISTS star_state (
    tic_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    label TEXT,
    notes TEXT,
    decided_by_evidence_id INTEGER,
    rebuilt_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lease (
    name TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    acquired_at_utc TEXT NOT NULL,
    heartbeat_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_log (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    path TEXT,
    created_at_utc TEXT NOT NULL,
    UNIQUE (source, version, content_hash)
);
CREATE TABLE IF NOT EXISTS release_report (
    signature TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    report_path TEXT NOT NULL,
    report_sha256 TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_utc(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def connect(
    db_path: str | Path | None = None,
    *,
    busy_timeout_ms: int = 5_000,
) -> sqlite3.Connection:
    """Open (and migrate) the ledger database."""

    path = Path(db_path) if db_path is not None else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def connect_readonly(
    db_path: str | Path | None = None,
    *,
    busy_timeout_ms: int = 5_000,
) -> sqlite3.Connection:
    """Open an existing ledger without creating, migrating, or writing it.

    Dashboard requests use this path.  Keeping it separate from
    :func:`connect` makes the read-only control-plane boundary structural:
    serving a page cannot create a missing database, run schema DDL, or
    accidentally publish derived state.
    """

    path = (Path(db_path) if db_path is not None else default_db_path()).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"EXOHUNT ledger does not exist: {path}")
    uri = f"{path.as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA query_only=ON")
    version = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    if version is None:
        conn.close()
        raise RuntimeError("EXOHUNT ledger has no schema version.")
    return conn


def upsert_star(conn: sqlite3.Connection, tic_id: int, **fields: Any) -> None:
    """Create or enrich a star row; unspecified fields are never blanked."""

    allowed = {
        "name",
        "gaia_source_id",
        "ra_deg",
        "dec_deg",
        "distance_pc",
        "tmag",
        "teff_k",
        "stellar_radius_solar",
        "lane",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unknown star fields: {sorted(unknown)}")
    now = _utc_now()
    conn.execute(
        "INSERT INTO star (tic_id, created_at_utc, updated_at_utc) "
        "VALUES (?, ?, ?) ON CONFLICT (tic_id) DO NOTHING",
        (int(tic_id), now, now),
    )
    updates = {
        key: value for key, value in fields.items() if value is not None
    }
    if updates:
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE star SET {assignments}, updated_at_utc = ? WHERE tic_id = ?",
            (*updates.values(), now, int(tic_id)),
        )


def append_evidence(
    conn: sqlite3.Connection,
    *,
    tic_id: int,
    kind: str,
    source: str,
    payload: dict[str, Any],
    verdict: str | None = None,
    affects_state: bool = True,
    signature: str | None = None,
) -> int | None:
    """Append one immutable evidence row.

    Idempotent on ``(tic_id, kind, source)``: re-importing the same file can
    never duplicate history. Returns the new row id, or ``None`` when the row
    already existed (the existing row is left untouched -- evidence is
    append-only).
    """

    if verdict is not None and verdict not in STATUS_REGISTRY:
        raise ValueError(
            f"Verdict {verdict!r} is not in the status registry; store "
            "non-status measurements in the payload with verdict=None."
        )
    cursor = conn.execute(
        "INSERT OR IGNORE INTO evidence "
        "(tic_id, kind, verdict, affects_state, signature, source, payload, "
        "created_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(tic_id),
            kind,
            verdict,
            1 if affects_state else 0,
            signature,
            source,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            _utc_now(),
        ),
    )
    return cursor.lastrowid if cursor.rowcount else None


def store_release_report(
    conn: sqlite3.Connection,
    *,
    signature: str,
    report_path: str | Path,
    payload: dict[str, Any],
) -> None:
    """Authorize one exact signature only after every P3 gate passes."""

    import hashlib

    if payload.get("scientific_signature") != signature:
        raise ValueError("Release report signature does not match its key.")
    if payload.get("release_gate_passes") is not True:
        raise ValueError("Release report is not passing; signature remains diagnostic.")
    if payload.get("execution_complete") is not True or payload.get("errors"):
        raise ValueError("Release report is incomplete or contains execution errors.")
    known = payload.get("known_planet_gate")
    if not isinstance(known, dict) or known.get("passes") is not True:
        raise ValueError("Known-planet production-path gate has not passed.")
    path = Path(report_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    conn.execute(
        "INSERT INTO release_report "
        "(signature, status, report_path, report_sha256, payload, created_at_utc) "
        "VALUES (?, 'trusted', ?, ?, ?, ?) "
        "ON CONFLICT(signature) DO UPDATE SET "
        "status=excluded.status, report_path=excluded.report_path, "
        "report_sha256=excluded.report_sha256, payload=excluded.payload, "
        "created_at_utc=excluded.created_at_utc",
        (
            signature,
            str(path.resolve()),
            digest,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            _utc_now(),
        ),
    )


def release_report_for_signature(
    conn: sqlite3.Connection, signature: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM release_report WHERE signature = ? AND status = 'trusted'",
        (signature,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["payload"] = json.loads(result["payload"])
    return result


def require_released_signature(conn: sqlite3.Connection, signature: str) -> None:
    if release_report_for_signature(conn, signature) is None:
        raise RuntimeError(
            f"Scientific signature {signature} has no passing stored release report; "
            "trusted first-pass enqueue is blocked. Diagnostic/calibration work is allowed."
        )


def set_affects_state(
    conn: sqlite3.Connection, evidence_id: int, affects_state: bool
) -> None:
    """Mark whether a row participates in status resolution.

    This is projection scoping, not history editing: the row itself is
    immutable, but only the currently-selected row of each superseding chain
    (for example, the newest of several context reports) should vote.
    """

    conn.execute(
        "UPDATE evidence SET affects_state = ? WHERE evidence_id = ?",
        (1 if affects_state else 0, int(evidence_id)),
    )


def rebuild_star_state(conn: sqlite3.Connection) -> dict[str, int]:
    """Recompute every star's current-best status from effective evidence.

    Evidence rows vote in insertion order through the registry's
    stage-then-precedence rules; within equal rank, later evidence wins --
    exactly the dashboard exporter's semantics. Returns status counts.
    """

    rows = conn.execute(
        "SELECT evidence_id, tic_id, verdict, payload FROM evidence "
        "WHERE affects_state = 1 AND verdict IS NOT NULL "
        "ORDER BY tic_id, evidence_id"
    ).fetchall()
    now = _utc_now()
    conn.execute("DELETE FROM star_state")
    counts: dict[str, int] = {}

    def flush(tic_id: int, candidates: list[sqlite3.Row]) -> None:
        status = resolve_status(row["verdict"] for row in candidates)
        chosen = next(
            row for row in reversed(candidates) if row["verdict"] == status
        )
        payload = json.loads(chosen["payload"])
        conn.execute(
            "INSERT INTO star_state "
            "(tic_id, status, label, notes, decided_by_evidence_id, "
            "rebuilt_at_utc) VALUES (?, ?, ?, ?, ?, ?)",
            (
                tic_id,
                status,
                payload.get("label"),
                payload.get("notes"),
                chosen["evidence_id"],
                now,
            ),
        )
        counts[status] = counts.get(status, 0) + 1

    current_tic: int | None = None
    bucket: list[sqlite3.Row] = []
    for row in rows:
        if current_tic is not None and row["tic_id"] != current_tic:
            flush(current_tic, bucket)
            bucket = []
        current_tic = row["tic_id"]
        bucket.append(row)
    if current_tic is not None and bucket:
        flush(current_tic, bucket)
    conn.commit()
    return dict(sorted(counts.items()))


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        row["status"]: row["n"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM star_state "
            "GROUP BY status ORDER BY status"
        )
    }


def evidence_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Ledger-reading summaries: conclusions logged, not current states."""

    return {
        row["verdict"]: row["n"]
        for row in conn.execute(
            "SELECT verdict, COUNT(*) AS n FROM evidence "
            "WHERE verdict IS NOT NULL GROUP BY verdict ORDER BY verdict"
        )
    }


def log_event(
    conn: sqlite3.Connection, kind: str, payload: dict[str, Any]
) -> int:
    cursor = conn.execute(
        "INSERT INTO event_log (kind, payload, created_at_utc) VALUES (?, ?, ?)",
        (kind, json.dumps(payload, sort_keys=True), _utc_now()),
    )
    return int(cursor.lastrowid)


def record_snapshot(
    conn: sqlite3.Connection,
    *,
    source: str,
    version: str,
    content_hash: str,
    path: str | None = None,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO snapshot "
        "(source, version, content_hash, path, created_at_utc) "
        "VALUES (?, ?, ?, ?, ?)",
        (source, version, content_hash, path, _utc_now()),
    )


def acquire_db_lease(
    conn: sqlite3.Connection,
    *,
    name: str,
    holder: str,
    ttl_seconds: float = 300.0,
    now: datetime | None = None,
) -> str:
    """Claim, refresh, or take over a named lease. Returns what happened.

    ``acquired``: the lease was free. ``refreshed``: the caller already held
    it (heartbeat updated). ``taken_over``: the previous holder's heartbeat
    was older than the TTL; the takeover is recorded in the event log with the
    dead holder's identity. ``denied``: a live holder exists.
    """

    moment = now or datetime.now(timezone.utc)
    stamp = moment.replace(microsecond=0).isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT holder, heartbeat_at_utc FROM lease WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO lease (name, holder, acquired_at_utc, "
                "heartbeat_at_utc) VALUES (?, ?, ?, ?)",
                (name, holder, stamp, stamp),
            )
            conn.commit()
            return "acquired"
        if row["holder"] == holder:
            conn.execute(
                "UPDATE lease SET heartbeat_at_utc = ? WHERE name = ?",
                (stamp, name),
            )
            conn.commit()
            return "refreshed"
        heartbeat = _parse_utc(row["heartbeat_at_utc"])
        age = (
            (moment - heartbeat).total_seconds()
            if heartbeat is not None
            else float("inf")
        )
        if age <= ttl_seconds:
            conn.commit()
            return "denied"
        conn.execute(
            "UPDATE lease SET holder = ?, acquired_at_utc = ?, "
            "heartbeat_at_utc = ? WHERE name = ?",
            (holder, stamp, stamp, name),
        )
        conn.execute(
            "INSERT INTO event_log (kind, payload, created_at_utc) "
            "VALUES (?, ?, ?)",
            (
                "lease_takeover",
                json.dumps(
                    {
                        "lease": name,
                        "previous_holder": row["holder"],
                        "previous_heartbeat_utc": row["heartbeat_at_utc"],
                        "new_holder": holder,
                        "stale_seconds": round(age, 1),
                    },
                    sort_keys=True,
                ),
                stamp,
            ),
        )
        conn.commit()
        return "taken_over"
    except BaseException:
        conn.rollback()
        raise


def heartbeat_db_lease(
    conn: sqlite3.Connection, *, name: str, holder: str
) -> bool:
    """Refresh a held lease; False means the caller no longer holds it."""

    cursor = conn.execute(
        "UPDATE lease SET heartbeat_at_utc = ? WHERE name = ? AND holder = ?",
        (_utc_now(), name, holder),
    )
    conn.commit()
    return cursor.rowcount == 1


def release_db_lease(
    conn: sqlite3.Connection, *, name: str, holder: str
) -> bool:
    cursor = conn.execute(
        "DELETE FROM lease WHERE name = ? AND holder = ?", (name, holder)
    )
    conn.commit()
    return cursor.rowcount == 1


def lease_status(
    conn: sqlite3.Connection, name: str, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Describe a lease for operators: holder plus heartbeat age in seconds."""

    row = conn.execute(
        "SELECT holder, acquired_at_utc, heartbeat_at_utc FROM lease "
        "WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    moment = now or datetime.now(timezone.utc)
    heartbeat = _parse_utc(row["heartbeat_at_utc"])
    return {
        "name": name,
        "holder": row["holder"],
        "acquired_at_utc": row["acquired_at_utc"],
        "heartbeat_at_utc": row["heartbeat_at_utc"],
        "heartbeat_age_seconds": (
            round((moment - heartbeat).total_seconds(), 1)
            if heartbeat is not None
            else None
        ),
    }
