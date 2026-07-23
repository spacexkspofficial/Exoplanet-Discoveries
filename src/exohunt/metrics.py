"""Append-only outcome logging and cumulative project statistics."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock


DEFAULT_LEDGER = Path("metrics/events.jsonl")
DEFAULT_SNAPSHOT = Path("metrics/current_stats.json")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    for attempt in range(8):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.05 * (2**attempt))


def _refresh_dashboard(
    ledger_path: str | Path,
    events: list[dict[str, Any]],
    stats: dict[str, Any],
) -> None:
    """Keep the local dashboard dataset current without risking search logging."""

    workspace = Path(ledger_path).resolve().parent.parent
    if not (workspace / "dashboard").exists():
        return
    try:
        from .dashboard import export_dashboard_data

        export_dashboard_data(workspace, events=events, stats=stats)
    except Exception:
        # The append-only ledger remains authoritative if dashboard export fails.
        return


def read_events(ledger_path: str | Path = DEFAULT_LEDGER) -> list[dict[str, Any]]:
    path = Path(ledger_path)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def _snapshot(events: list[dict[str, Any]]) -> dict[str, Any]:
    invalidated_ids = {
        str(event.get("invalidates_event_id"))
        for event in events
        if event.get("kind") == "event_invalidated" and event.get("invalidates_event_id")
    }
    stats: dict[str, Any] = {
        "last_updated_utc": _utc_now(),
        "events_logged": len(events),
        "invalidated_events": len(invalidated_ids),
        "campaign_runs_logged": 0,
        "target_search_runs": 0,
        "unique_targets_searched": 0,
        "automated_survivors": 0,
        "rejected_signals": 0,
        "search_errors": 0,
        "known_planet_rediscoveries": 0,
        "known_tce_rediscoveries": 0,
        "candidate_packets_created": 0,
        "vetted_new_candidates": 0,
        "confirmed_planets": 0,
        "false_positives_after_vetting": 0,
        "rejection_reasons": {},
    }
    unique_tics: set[int] = set()
    unique_known_tces: set[tuple[int, str]] = set()
    rejection_reasons: Counter[str] = Counter()
    for event in events:
        if event.get("event_id") in invalidated_ids:
            continue
        kind = event.get("kind")
        if kind == "campaign_completed":
            stats["campaign_runs_logged"] += 1
            stats["target_search_runs"] += int(event.get("targets", 0))
            stats["automated_survivors"] += int(event.get("automated_survivors", 0))
            stats["rejected_signals"] += int(event.get("rejected", 0))
            stats["search_errors"] += int(event.get("errors", 0))
            unique_tics.update(int(value) for value in event.get("tic_ids", []))
            rejection_reasons.update(event.get("rejection_reasons", {}))
        elif kind == "known_planet_validation":
            stats["known_planet_rediscoveries"] += int(event.get("rediscoveries", 0))
        elif kind == "candidate_packet_created":
            stats["candidate_packets_created"] += 1
        elif kind == "vetted_candidate":
            stats["vetted_new_candidates"] += 1
        elif kind == "confirmed_planet":
            stats["confirmed_planets"] += 1
        elif kind == "false_positive":
            stats["false_positives_after_vetting"] += 1
        elif kind == "rediscovery":
            stats["known_planet_rediscoveries"] += 1
        elif kind == "known_tce_rediscovery":
            unique_known_tces.add((int(event.get("tic_id", 0)), str(event.get("label", ""))))
    stats["unique_targets_searched"] = len(unique_tics)
    stats["known_tce_rediscoveries"] = len(unique_known_tces)
    stats["rejection_reasons"] = dict(sorted(rejection_reasons.items()))
    return stats


def append_event(
    event: dict[str, Any],
    *,
    ledger_path: str | Path = DEFAULT_LEDGER,
    snapshot_path: str | Path = DEFAULT_SNAPSHOT,
) -> tuple[bool, dict[str, Any]]:
    """Append a uniquely identified event and rebuild the current snapshot."""

    ledger = Path(ledger_path)
    snapshot = Path(snapshot_path)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(ledger.with_suffix(ledger.suffix + ".lock")))
    with lock.acquire(timeout=30):
        events = read_events(ledger)
        event = {"timestamp_utc": _utc_now(), **event}
        event.setdefault("event_id", _canonical_hash(event))
        added = not any(
            existing.get("event_id") == event["event_id"] for existing in events
        )
        if added:
            with ledger.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            events.append(event)
        stats = _snapshot(events)
        _atomic_write_json(snapshot, stats)
    _refresh_dashboard(ledger, events, stats)
    return added, stats


def record_campaign(
    summary_path: str | Path,
    *,
    ledger_path: str | Path = DEFAULT_LEDGER,
    snapshot_path: str | Path = DEFAULT_SNAPSHOT,
) -> tuple[bool, dict[str, Any]]:
    path = Path(summary_path)
    summary = json.loads(path.read_text(encoding="utf-8"))
    results = list(summary.get("results", []))
    reason_counts: Counter[str] = Counter()
    for row in results:
        for reason in str(row.get("rejection_reasons", "")).split(";"):
            if reason.strip():
                reason_counts[reason.strip()] += 1
    settings = dict(summary.get("settings") or {})
    settings.pop("storage_retention", None)
    campaign_identity = {
        "target_list": summary.get("target_list"),
        "settings": settings,
        "targets": [(row.get("tic_id"), row.get("sectors")) for row in results],
    }
    campaign_id = _canonical_hash(campaign_identity)
    outcome_identity = [
        (
            row.get("tic_id"),
            row.get("status"),
            row.get("rejection_reasons"),
            row.get("error"),
        )
        for row in results
    ]
    outcome_id = _canonical_hash(outcome_identity)
    base_event_id = "campaign-" + campaign_id + "-" + outcome_id
    counts = summary.get("counts", {})
    event = {
        "event_id": base_event_id,
        "campaign_id": campaign_id,
        "outcome_id": outcome_id,
        "kind": "campaign_completed",
        "summary_path": str(path),
        "target_list": summary.get("target_list"),
        "targets": len(results),
        "tic_ids": [int(row["tic_id"]) for row in results if row.get("tic_id")],
        "automated_survivors": int(counts.get("survivor", 0)),
        "survivor_tic_ids": [
            int(row["tic_id"])
            for row in results
            if row.get("status") == "survivor" and row.get("tic_id")
        ],
        "rejected": int(counts.get("rejected", 0)),
        "errors": int(counts.get("error", 0)),
        "rejection_reasons": dict(reason_counts),
    }
    existing_events = read_events(ledger_path)
    invalidated_ids = {
        str(existing.get("invalidates_event_id"))
        for existing in existing_events
        if existing.get("kind") == "event_invalidated"
    }
    for existing in existing_events:
        existing_id = str(existing.get("event_id") or "")
        if (
            existing.get("kind") == "campaign_completed"
            and existing.get("campaign_id") == campaign_id
            and existing_id not in invalidated_ids
            and (
                existing.get("outcome_id") == outcome_id
                or existing_id == base_event_id
                or existing_id.startswith(base_event_id + "-rev-")
            )
        ):
            event["event_id"] = existing_id
            return append_event(
                event, ledger_path=ledger_path, snapshot_path=snapshot_path
            )
    if base_event_id in invalidated_ids:
        revision_ids = {
            str(existing.get("event_id"))
            for existing in existing_events
            if str(existing.get("event_id") or "").startswith(
                base_event_id + "-rev-"
            )
        }
        event["event_id"] = f"{base_event_id}-rev-{len(revision_ids) + 1}"
    for existing in existing_events:
        if (
            existing.get("kind") == "campaign_completed"
            and existing.get("campaign_id") == campaign_id
            and existing.get("event_id") not in invalidated_ids
            and existing.get("event_id") != event["event_id"]
        ):
            invalidate_event(
                str(existing["event_id"]),
                reason="Superseded by a corrected or retried campaign summary.",
                source="record_campaign",
                ledger_path=ledger_path,
                snapshot_path=snapshot_path,
            )
    return append_event(event, ledger_path=ledger_path, snapshot_path=snapshot_path)


def invalidate_event(
    event_id: str,
    *,
    reason: str,
    source: str = "manual",
    ledger_path: str | Path = DEFAULT_LEDGER,
    snapshot_path: str | Path = DEFAULT_SNAPSHOT,
) -> tuple[bool, dict[str, Any]]:
    """Append an auditable invalidation without deleting the original event."""

    if not event_id.strip() or not reason.strip():
        raise ValueError("An event ID and reason are required.")
    event = {
        "event_id": "invalidation-"
        + _canonical_hash({"event_id": event_id, "reason": reason, "source": source}),
        "kind": "event_invalidated",
        "invalidates_event_id": event_id,
        "reason": reason,
        "source": source,
    }
    return append_event(event, ledger_path=ledger_path, snapshot_path=snapshot_path)


def record_validation(
    summary_path: str | Path,
    *,
    ledger_path: str | Path = DEFAULT_LEDGER,
    snapshot_path: str | Path = DEFAULT_SNAPSHOT,
) -> tuple[bool, dict[str, Any]]:
    path = Path(summary_path)
    summary = json.loads(path.read_text(encoding="utf-8"))
    benchmarks = list(summary.get("benchmarks", []))
    successes = [row for row in benchmarks if row.get("status") in {"exact", "harmonic_alias"}]
    identity = [(row.get("planet"), row.get("status")) for row in benchmarks]
    event = {
        "event_id": "validation-" + _canonical_hash(identity),
        "kind": "known_planet_validation",
        "summary_path": str(path),
        "benchmarks": len(benchmarks),
        "rediscoveries": len(successes),
        "rediscovered_planets": [row.get("planet") for row in successes],
    }
    return append_event(event, ledger_path=ledger_path, snapshot_path=snapshot_path)


def record_outcome(
    kind: str,
    *,
    tic_id: int,
    label: str,
    notes: str = "",
    source: str = "manual",
    ledger_path: str | Path = DEFAULT_LEDGER,
    snapshot_path: str | Path = DEFAULT_SNAPSHOT,
) -> tuple[bool, dict[str, Any]]:
    allowed = {
        "candidate_packet_created",
        "vetted_candidate",
        "confirmed_planet",
        "false_positive",
        "rediscovery",
        "known_tce_rediscovery",
    }
    if kind not in allowed:
        raise ValueError(f"Outcome kind must be one of: {', '.join(sorted(allowed))}")
    event = {
        "event_id": "outcome-"
        + _canonical_hash(
            {
                "kind": kind,
                "tic_id": int(tic_id),
                "label": label,
                "source": source,
            }
        ),
        "kind": kind,
        "tic_id": int(tic_id),
        "label": label,
        "notes": notes,
        "source": source,
    }
    return append_event(event, ledger_path=ledger_path, snapshot_path=snapshot_path)


def current_stats(
    *,
    ledger_path: str | Path = DEFAULT_LEDGER,
    snapshot_path: str | Path = DEFAULT_SNAPSHOT,
) -> dict[str, Any]:
    ledger = Path(ledger_path)
    snapshot = Path(snapshot_path)
    lock = FileLock(str(ledger.with_suffix(ledger.suffix + ".lock")))
    with lock.acquire(timeout=30):
        events = read_events(ledger)
        stats = _snapshot(events)
        _atomic_write_json(snapshot, stats)
    _refresh_dashboard(ledger_path, events, stats)
    return stats
