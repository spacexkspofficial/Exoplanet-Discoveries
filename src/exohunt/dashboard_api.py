"""Read-only dashboard projections over the SQLite evidence ledger.

The browser-facing path is deliberately separate from the legacy
``survey.json`` exporter:

* summary polling is a handful of indexed aggregate queries;
* stars are paged and reconstructed only from ``star``, ``star_state``, and
  effective ``evidence`` rows;
* one-star detail includes the complete append-only evidence chain;
* operational liveness comes only from coordinator lease heartbeat age.

No function in this module reads campaign files or writes the database.  The
old exporter remains an offline analysis artifact and a parallel-run parity
oracle until this path has proved equivalent on frozen inputs.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from . import ledger
from .dashboard import (
    _cartesian,
    _deterministic_direction,
    _optional_float,
    _screening_class,
    _sectors,
)
from .statuses import export_label

LIVE_HEARTBEAT_SECONDS = 45.0
STALE_HEARTBEAT_ALARM_SECONDS = 120.0
DEFAULT_PAGE_SIZE = 250
MAX_PAGE_SIZE = 1_000


def _utc_now(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.replace(microsecond=0).isoformat()


def _payload(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        value = json.loads(str(row["payload"]))
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _latest_effective_by_kind(
    rows: Iterable[sqlite3.Row],
) -> dict[str, sqlite3.Row]:
    latest: dict[str, sqlite3.Row] = {}
    for row in rows:
        latest[str(row["kind"])] = row
    return latest


def _coerce_sectors(value: object) -> list[int]:
    """Accept both legacy scalar strings and JSON-preserved arrays."""

    if isinstance(value, (list, tuple, set)):
        sectors: set[int] = set()
        for item in value:
            try:
                sectors.add(int(item))
            except (TypeError, ValueError):
                continue
        return sorted(sectors)
    return _sectors(value)


def _star_evidence(
    conn: sqlite3.Connection, tic_ids: list[int]
) -> dict[int, list[sqlite3.Row]]:
    if not tic_ids:
        return {}
    placeholders = ",".join("?" for _ in tic_ids)
    rows = conn.execute(
        "SELECT evidence_id, tic_id, kind, verdict, affects_state, signature, "
        "source, payload, created_at_utc FROM evidence "
        "WHERE (affects_state = 1 OR kind IN ('common_mode', 'science')) "
        f"AND tic_id IN ({placeholders}) "
        "ORDER BY tic_id, evidence_id",
        tic_ids,
    ).fetchall()
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(int(row["tic_id"]), []).append(row)
    return grouped


def _signal_fields(screening: sqlite3.Row | None) -> dict[str, Any]:
    result = _payload(screening).get("result") if screening is not None else None
    result = result if isinstance(result, dict) else {}
    return {
        "period_days": _optional_float(result.get("period_days")),
        "depth_ppm": _optional_float(result.get("depth_ppm")),
        "snr": _optional_float(result.get("depth_snr")),
        "duration_hours": _optional_float(result.get("duration_hours")),
        "observed_transits": result.get("observed_transits"),
        "screening_status": result.get("status"),
        "screening_class": (
            _screening_class(result) if result else None
        ),
        "rejection_reasons": str(result.get("rejection_reasons") or ""),
        "followup_priority": int(result.get("followup_priority") or 0),
        "followup_reasons": str(result.get("followup_reasons") or ""),
        "vetting_tier": str(result.get("vetting_tier") or "legacy_unmeasured"),
        "deeper_vetting_flags": str(result.get("deeper_vetting_flags") or ""),
        "recommended_data_sources": str(
            result.get("recommended_data_sources") or ""
        ),
        "planet_free": False,
        "sensitivity_3d_ppm": _optional_float(
            result.get("sensitivity_3d_ppm")
        ),
        "sensitivity_12d_ppm": _optional_float(
            result.get("sensitivity_12d_ppm")
        ),
        "red_noise_adjusted_snr": _optional_float(
            result.get("red_noise_adjusted_snr")
        ),
        "event_coverage_fraction": _optional_float(
            result.get("event_coverage_fraction")
        ),
        "positive_depth_event_fraction": _optional_float(
            result.get("positive_depth_event_fraction")
        ),
        "sectors": _coerce_sectors(result.get("sectors")),
        "phase_curve_available": bool(
            result.get("phase_curve_available", False)
        ),
    }


def _display_star(
    state: sqlite3.Row, evidence_rows: list[sqlite3.Row]
) -> dict[str, Any]:
    latest = _latest_effective_by_kind(evidence_rows)
    signal = _signal_fields(latest.get("screening"))

    context_payload = _payload(latest.get("context"))
    context = context_payload.get("classification")
    context = context if isinstance(context, dict) else {}

    science_payload = _payload(latest.get("science"))
    science = science_payload.get("science")
    science = science if isinstance(science, dict) else {}

    common_payload = _payload(latest.get("common_mode"))
    common = common_payload.get("screen")
    common = common if isinstance(common, dict) else {}

    tic_id = int(state["tic_id"])
    ra = _optional_float(state["ra_deg"])
    dec = _optional_float(state["dec_deg"])
    distance = _optional_float(state["distance_pc"])
    direction_is_estimated = ra is None or dec is None
    distance_is_estimated = distance is None or distance <= 0
    if ra is None or dec is None:
        ra, dec = _deterministic_direction(tic_id)
    if distance is None or distance <= 0:
        distance = 35.0 + tic_id % 110
    if direction_is_estimated:
        coordinate_source = "Estimated display direction and distance"
    elif distance_is_estimated:
        coordinate_source = "TIC sky position; estimated display distance"
    else:
        coordinate_source = "TIC sky position and distance"

    status = str(state["status"])
    label = str(state["status_label"] or export_label(status))
    context_row = latest.get("context")
    context_source = (
        str(context_row["source"]).removeprefix("context:")
        if context_row is not None
        else None
    )
    return {
        "tic_id": tic_id,
        "name": str(state["name"] or f"TIC {tic_id}"),
        "status": status,
        "status_label": label,
        "notes": str(state["state_notes"] or ""),
        "ra_deg": round(ra, 7),
        "dec_deg": round(dec, 7),
        "distance_pc": round(distance, 4),
        "distance_is_estimated": distance_is_estimated,
        "direction_is_estimated": direction_is_estimated,
        "coordinate_source": coordinate_source,
        "tmag": _optional_float(state["tmag"]),
        "teff_k": _optional_float(state["teff_k"]),
        "stellar_radius_solar": _optional_float(
            state["stellar_radius_solar"]
        ),
        "lane": state["lane"],
        "context_disposition": context.get("disposition"),
        "context_followup_lane": context.get("followup_lane"),
        "context_source_states": (
            context.get("source_states") if context else {}
        ),
        "context_report": context_source,
        "science_vetted": bool(science.get("science_vetted")),
        "science_disposition": science.get("science_disposition"),
        "science_on_target": science.get("science_on_target"),
        "science_centroid_offset_arcsec": science.get(
            "science_centroid_offset_arcsec"
        ),
        "science_sector_gate_passed": science.get(
            "science_sector_gate_passed"
        ),
        "science_supported_sector_count": science.get(
            "science_supported_sector_count"
        ),
        "science_sectors_tested": science.get("science_sectors_tested"),
        "science_supporting_sectors": science.get(
            "science_supporting_sectors", []
        ),
        "common_mode_verdict": common.get("verdict"),
        "common_mode_shared_targets": common.get("shared_targets"),
        "common_mode_expected_targets": common.get(
            "expected_shared_targets"
        ),
        "common_mode_enrichment": common.get("enrichment"),
        "common_mode_cameras_spanned": common.get("cameras_spanned"),
        "common_mode_sky_spread_deg": common.get("sky_spread_deg"),
        "spacecraft_harmonic": common.get("spacecraft_harmonic"),
        "spacecraft_harmonic_period_days": common.get(
            "spacecraft_harmonic_period_days"
        ),
        "duration_at_grid_rail": bool(
            common.get("duration_at_grid_rail", False)
        ),
        "period_at_search_ceiling": bool(
            common.get("period_at_search_ceiling", False)
        ),
        **signal,
        **_cartesian(ra, dec, distance),
    }


_STAR_SELECT = """
SELECT
    ss.tic_id AS tic_id,
    ss.status AS status,
    ss.label AS status_label,
    ss.notes AS state_notes,
    ss.decided_by_evidence_id AS decided_by_evidence_id,
    ss.rebuilt_at_utc AS rebuilt_at_utc,
    s.name AS name,
    s.ra_deg AS ra_deg,
    s.dec_deg AS dec_deg,
    s.distance_pc AS distance_pc,
    s.tmag AS tmag,
    s.teff_k AS teff_k,
    s.stellar_radius_solar AS stellar_radius_solar,
    s.lane AS lane
FROM star_state AS ss
LEFT JOIN star AS s ON s.tic_id = ss.tic_id
"""


def star_page_payload(
    conn: sqlite3.Connection,
    *,
    lane: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """Return one deterministic page of current-best star projections."""

    if page < 1:
        raise ValueError("page must be at least 1")
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(
            f"page_size must be between 1 and {MAX_PAGE_SIZE}"
        )
    where: list[str] = []
    values: list[Any] = []
    if lane:
        where.append("s.lane = ?")
        values.append(lane)
    if status:
        where.append("ss.status = ?")
        values.append(status)
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    total = int(
        conn.execute(
            "SELECT COUNT(*) FROM star_state AS ss "
            "LEFT JOIN star AS s ON s.tic_id = ss.tic_id" + where_sql,
            values,
        ).fetchone()[0]
    )
    offset = (page - 1) * page_size
    states = conn.execute(
        _STAR_SELECT
        + where_sql
        + " ORDER BY ss.tic_id LIMIT ? OFFSET ?",
        [*values, page_size, offset],
    ).fetchall()
    tic_ids = [int(row["tic_id"]) for row in states]
    evidence = _star_evidence(conn, tic_ids)
    items = [
        _display_star(row, evidence.get(int(row["tic_id"]), []))
        for row in states
    ]
    pages = math.ceil(total / page_size) if total else 0
    return {
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "total": total,
        "items": items,
    }


def all_star_payloads(
    conn: sqlite3.Connection, *, page_size: int = MAX_PAGE_SIZE
) -> list[dict[str, Any]]:
    """Materialize current star rows for offline parity checks."""

    first = star_page_payload(conn, page=1, page_size=page_size)
    items = list(first["items"])
    for page in range(2, int(first["pages"]) + 1):
        items.extend(
            star_page_payload(
                conn, page=page, page_size=page_size
            )["items"]
        )
    return items


def star_detail_payload(
    conn: sqlite3.Connection, tic_id: int
) -> dict[str, Any] | None:
    """Return current state plus every immutable evidence row for one star."""

    state = conn.execute(
        _STAR_SELECT + " WHERE ss.tic_id = ?", (int(tic_id),)
    ).fetchone()
    if state is None:
        return None
    evidence_rows = conn.execute(
        "SELECT evidence_id, kind, verdict, affects_state, signature, source, "
        "payload, created_at_utc FROM evidence WHERE tic_id = ? "
        "ORDER BY evidence_id",
        (int(tic_id),),
    ).fetchall()
    star = _display_star(
        state, [row for row in evidence_rows if row["affects_state"]]
    )
    return {
        "tic_id": int(tic_id),
        "current_state": {
            "status": state["status"],
            "label": state["status_label"],
            "notes": state["state_notes"],
            "decided_by_evidence_id": state["decided_by_evidence_id"],
            "rebuilt_at_utc": state["rebuilt_at_utc"],
        },
        "star": star,
        "evidence": [
            {
                "evidence_id": int(row["evidence_id"]),
                "kind": row["kind"],
                "verdict": row["verdict"],
                "affects_state": bool(row["affects_state"]),
                "signature": row["signature"],
                "source": row["source"],
                "payload": _payload(row),
                "created_at_utc": row["created_at_utc"],
            }
            for row in evidence_rows
        ],
        "warning": (
            "This is an evidence history and current-best projection. "
            "A surviving automated screen is not a planet detection."
        ),
    }


def _status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        str(row["status"]): int(row["n"])
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM star_state "
            "GROUP BY status ORDER BY status"
        )
    }


def _status_counts_by_signature(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, int]]:
    rows = conn.execute(
        "SELECT COALESCE(e.signature, 'unversioned') AS signature, "
        "ss.status, COUNT(*) AS n FROM star_state AS ss "
        "LEFT JOIN evidence AS e "
        "ON e.evidence_id = ss.decided_by_evidence_id "
        "GROUP BY signature, ss.status ORDER BY signature, ss.status"
    )
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        result.setdefault(str(row["signature"]), {})[
            str(row["status"])
        ] = int(row["n"])
    return result


def _status_counts_by_lane(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, int]]:
    rows = conn.execute(
        "SELECT COALESCE(s.lane, 'unassigned') AS lane, ss.status, "
        "COUNT(*) AS n FROM star_state AS ss "
        "LEFT JOIN star AS s ON s.tic_id = ss.tic_id "
        "GROUP BY lane, ss.status ORDER BY lane, ss.status"
    )
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        result.setdefault(str(row["lane"]), {})[
            str(row["status"])
        ] = int(row["n"])
    return result


def _science_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT COUNT(*) AS vetted, "
        "SUM(json_extract(payload, '$.science.science_on_target') = 1) "
        "AS on_target, "
        "SUM(json_extract(payload, '$.science.science_on_target') = 0) "
        "AS off_target, "
        "SUM(json_extract(payload, "
        "'$.science.science_sector_gate_passed') = 1) AS sector_passed, "
        "SUM(json_extract(payload, '$.science.science_disposition') = "
        "'science_vetted_lead') AS passed_both "
        "FROM evidence "
        "WHERE kind = 'science' AND affects_state = 1"
    ).fetchone()
    return {
        "vetted_targets": int(row["vetted"] or 0),
        "on_target": int(row["on_target"] or 0),
        "off_target": int(row["off_target"] or 0),
        "sector_gate_passed": int(row["sector_passed"] or 0),
        "passed_both_gates": int(row["passed_both"] or 0),
        "scope": (
            "Current measured-science evidence. Passing both gates remains "
            "a diagnostic lead, not a planet candidate."
        ),
    }


def _common_mode_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT COUNT(DISTINCT tic_id) AS screened, "
        "SUM(verdict IN ('common_mode_systematic', 'localized_coincidence')) "
        "AS flagged, "
        "SUM(verdict = 'common_mode_systematic') AS common_mode, "
        "SUM(verdict = 'localized_coincidence') AS localized "
        "FROM evidence WHERE kind = 'common_mode'"
    ).fetchone()
    screened = int(row["screened"] or 0)
    flagged = int(row["flagged"] or 0)
    return {
        "screened_targets": screened,
        "flagged_targets": flagged,
        "common_mode_systematic": int(row["common_mode"] or 0),
        "localized_coincidence": int(row["localized"] or 0),
        "flagged_fraction": (
            round(flagged / screened, 4) if screened else None
        ),
        "scope": (
            "Latest shared-ephemeris verdict per star. Sharing is evidence "
            "of an observatory systematic, while non-sharing is not evidence "
            "that a signal is astrophysical."
        ),
    }


def _latest_trusted_release(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Return the compact, display-safe state of the newest P3 release."""

    row = conn.execute(
        "SELECT signature, report_sha256, payload, created_at_utc "
        "FROM release_report WHERE status = 'trusted' "
        "ORDER BY created_at_utc DESC LIMIT 1"
    ).fetchone()
    payload = _payload(row)
    if row is None or payload.get("release_gate_passes") is not True:
        return None
    calibration = payload.get("calibration_gate")
    known = payload.get("known_planet_gate")
    return {
        "status": "trusted_release",
        "scientific_signature": str(row["signature"]),
        "code_version": payload.get("code_version"),
        "created_at_utc": row["created_at_utc"],
        "report_sha256": row["report_sha256"],
        "calibration_counts": (
            calibration.get("counts") if isinstance(calibration, dict) else None
        ),
        "calibration_gates": (
            calibration.get("gates") if isinstance(calibration, dict) else None
        ),
        "known_planet_counts": (
            known.get("counts") if isinstance(known, dict) else None
        ),
    }


def summary_payload(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> dict[str, Any]:
    """Small poll-friendly current-state summary, partitioned by signature."""

    counts = _status_counts(conn)
    rebuilt = conn.execute(
        "SELECT COALESCE(MAX(rebuilt_at_utc), '') FROM star_state"
    ).fetchone()[0]
    evidence_id = int(
        conn.execute(
            "SELECT COALESCE(MAX(evidence_id), 0) FROM evidence"
        ).fetchone()[0]
    )
    stars_total = int(
        conn.execute("SELECT COUNT(*) FROM star_state").fetchone()[0]
    )
    campaign_runs = int(
        conn.execute(
            "SELECT COUNT(DISTINCT source) FROM evidence "
            "WHERE kind = 'screening' AND source LIKE 'summary:%'"
        ).fetchone()[0]
    )
    conclusions_logged = int(
        conn.execute(
            "SELECT COUNT(verdict) FROM evidence"
        ).fetchone()[0]
    )
    signatures = [
        str(row[0] or "unversioned")
        for row in conn.execute(
            "SELECT DISTINCT signature FROM evidence ORDER BY signature"
        )
    ]
    trusted_release = _latest_trusted_release(conn)
    return {
        "schema_version": 3,
        "generated_at_utc": _utc_now(now),
        "data_revision": f"{rebuilt}:{evidence_id}:{stars_total}",
        "stars_total": stars_total,
        "stats": {
            "campaign_runs_logged": campaign_runs,
            "unique_stars_searched": stars_total,
            "known_planet_rediscoveries": counts.get(
            "known_planet_rediscovery", 0
            ),
        },
        "status_counts": counts,
        "status_counts_scope": "current best state",
        "status_counts_by_signature": _status_counts_by_signature(conn),
        "status_counts_by_lane": _status_counts_by_lane(conn),
        "conclusions_logged": conclusions_logged,
        "conclusions_logged_scope": "conclusions logged",
        "scientific_signatures": signatures,
        "throughput": {
            "available": False,
            "reason": (
                "Imported legacy evidence does not preserve work-item "
                "completion timestamps; scheduler throughput lands with the "
                "P2 work-item projection."
            ),
        },
        "health_flags": {
            "diagnostic_only": trusted_release is None,
            "calibration_gate_complete": trusted_release is not None,
            "unknown_signature_count": sum(
                not (
                    signature == "unversioned"
                    or signature.startswith("legacy:")
                    or signature.startswith("sig1:")
                )
                for signature in signatures
            ),
        },
        "science_vetting": _science_summary(conn),
        "common_mode_screen": _common_mode_summary(conn),
        # Sector and scheduler projections arrive with the P2 scheduler/work
        # item schema.  Empty arrays are explicit and keep this endpoint
        # file-free; star pages still carry every observed sector.
        "observed_sectors": [],
        "sector_coverage": [],
        "active_campaigns": [],
        "trusted_release": trusted_release,
        "warnings": [
            (
                "P3 calibration is complete only for the exact trusted signature; "
                "other signatures remain diagnostic."
                if trusted_release is not None
                else "Every result remains diagnostic until the Phase 3 calibration gate."
            ),
            "Automated survivors are not planet candidates.",
            "Current-best states and conclusions logged are distinct readings.",
            "Scientific evidence counts are partitioned by signature.",
        ],
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def ops_payload(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> dict[str, Any]:
    """Operational state whose liveness is defined only by heartbeat age."""

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    lease = ledger.lease_status(conn, "coordinator", now=moment)
    age = (
        lease.get("heartbeat_age_seconds")
        if isinstance(lease, dict)
        else None
    )
    live = isinstance(age, (int, float)) and age < LIVE_HEARTBEAT_SECONDS
    liveness = "live" if live else ("stale" if lease is not None else "absent")

    queue_depths: dict[str, dict[str, int]] = {}
    queue_available = _table_exists(conn, "work_item")
    if queue_available:
        for row in conn.execute(
            "SELECT tier, status, COUNT(*) AS n FROM work_item "
            "GROUP BY tier, status ORDER BY tier, status"
        ):
            queue_depths.setdefault(str(row["tier"]), {})[
                str(row["status"])
            ] = int(row["n"])

    takeovers = [
        {
            "created_at_utc": row["created_at_utc"],
            "payload": _payload(row),
        }
        for row in conn.execute(
            "SELECT payload, created_at_utc FROM event_log "
            "WHERE kind = 'lease_takeover' ORDER BY event_id DESC LIMIT 10"
        )
    ]
    alarms = []
    if isinstance(age, (int, float)) and age > STALE_HEARTBEAT_ALARM_SECONDS:
        alarms.append(
            {
                "kind": "heartbeat_stale",
                "heartbeat_age_seconds": age,
                "threshold_seconds": STALE_HEARTBEAT_ALARM_SECONDS,
            }
        )
    return {
        "generated_at_utc": _utc_now(moment),
        "liveness": liveness,
        "live": live,
        "heartbeat_age_seconds": age,
        "heartbeat_at_utc": (
            lease.get("heartbeat_at_utc") if lease is not None else None
        ),
        "holder": lease.get("holder") if lease is not None else None,
        "acquired_at_utc": (
            lease.get("acquired_at_utc") if lease is not None else None
        ),
        "live_threshold_seconds": LIVE_HEARTBEAT_SECONDS,
        "queue_depths": queue_depths,
        "queue_projection_available": queue_available,
        "lease_takeovers": takeovers,
        "alarms": alarms,
        "liveness_basis": (
            "Coordinator lease heartbeat age only; checkpoint state strings "
            "are never consulted."
        ),
    }


def systematics_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    """Summarize the latest population-screen evidence for every star."""

    rows = conn.execute(
        "WITH ranked AS ("
        " SELECT evidence_id, tic_id, verdict, signature, payload, "
        " ROW_NUMBER() OVER (PARTITION BY tic_id ORDER BY evidence_id DESC) AS rn"
        " FROM evidence WHERE kind = 'common_mode'"
        ") SELECT * FROM ranked WHERE rn = 1 ORDER BY tic_id"
    ).fetchall()
    parsed = [(_payload(row).get("screen"), row) for row in rows]
    screens = [
        (screen, row)
        for screen, row in parsed
        if isinstance(screen, dict)
    ]
    flagged = [
        (screen, row)
        for screen, row in screens
        if row["verdict"] in {
            "common_mode_systematic",
            "localized_coincidence",
        }
    ]
    by_signature: dict[str, dict[str, int]] = {}
    for _, row in screens:
        signature = str(row["signature"] or "unversioned")
        verdict = str(row["verdict"] or "independent_timing")
        signature_counts = by_signature.setdefault(signature, {})
        signature_counts[verdict] = signature_counts.get(verdict, 0) + 1
    epoch_histogram: dict[str, dict[str, Any]] = {}
    for screen, row in screens:
        epoch = _optional_float(screen.get("shared_epoch_btjd"))
        if epoch is None:
            continue
        key = f"{epoch:.3f}"
        bucket = epoch_histogram.setdefault(
            key,
            {
                "epoch_btjd": round(epoch, 3),
                "targets": 0,
                "flagged_targets": 0,
                "max_enrichment": None,
            },
        )
        bucket["targets"] += 1
        bucket["flagged_targets"] += int(
            row["verdict"]
            in {"common_mode_systematic", "localized_coincidence"}
        )
        enrichment = _optional_float(screen.get("enrichment"))
        if enrichment is not None:
            previous = bucket["max_enrichment"]
            bucket["max_enrichment"] = (
                enrichment
                if previous is None
                else max(float(previous), enrichment)
            )
    screening_rates = []
    for row in conn.execute(
        "SELECT COALESCE(signature, 'unversioned') AS signature, "
        "COUNT(*) AS searched, "
        "SUM(verdict = 'automated_survivor') AS survivors "
        "FROM evidence WHERE kind = 'screening' AND affects_state = 1 "
        "GROUP BY signature ORDER BY signature"
    ):
        searched = int(row["searched"] or 0)
        survivors = int(row["survivors"] or 0)
        screening_rates.append(
            {
                "signature": row["signature"],
                "searched": searched,
                "automated_survivors": survivors,
                "diagnostic_survivor_fraction": (
                    round(survivors / searched, 6) if searched else None
                ),
            }
        )
    dip_registries = []
    for row in conn.execute(
        "SELECT signature, payload, created_at_utc FROM evidence "
        "WHERE kind = 'dip_registry' ORDER BY evidence_id DESC LIMIT 20"
    ):
        dip_registries.append(
            {
                "signature": row["signature"] or "unversioned",
                "created_at_utc": row["created_at_utc"],
                "registry": _payload(row),
            }
        )
    screened_count = len(screens)
    flagged_count = len(flagged)
    return {
        "screened_targets": screened_count,
        "flagged_targets": flagged_count,
        "common_mode_systematic": sum(
            row["verdict"] == "common_mode_systematic"
            for _, row in screens
        ),
        "localized_coincidence": sum(
            row["verdict"] == "localized_coincidence"
            for _, row in screens
        ),
        "flagged_fraction": (
            round(flagged_count / screened_count, 4)
            if screened_count
            else None
        ),
        "on_spacecraft_harmonic": sum(
            bool(screen.get("spacecraft_harmonic"))
            for screen, _ in screens
        ),
        "duration_at_grid_rail": sum(
            bool(screen.get("duration_at_grid_rail"))
            for screen, _ in screens
        ),
        "period_at_search_ceiling": sum(
            bool(screen.get("period_at_search_ceiling"))
            for screen, _ in screens
        ),
        "by_signature": by_signature,
        "shared_epoch_histogram": sorted(
            epoch_histogram.values(),
            key=lambda bucket: (
                -int(bucket["flagged_targets"]),
                float(bucket["epoch_btjd"]),
            ),
        ),
        "dip_registries": dip_registries,
        "survivor_rate_control_chart": {
            "available": bool(screening_rates),
            "calibrated": False,
            "diagnostic_rates": screening_rates,
            "reason": (
                "These are legacy diagnostic rates partitioned by signature. "
                "Phase 3 null calibration is required before control limits "
                "or false-alarm meaning exist."
            ),
        },
        "epoch_grouping_scope": (
            "Legacy evidence preserves shared BTJD epochs but not a normalized "
            "sector-camera-CCD key. New dip-registry evidence carries that "
            "cohort scope explicitly."
        ),
        "scope": (
            "Latest shared-ephemeris verdict per star. Sharing is evidence "
            "of an observatory systematic, while non-sharing is not evidence "
            "that a signal is astrophysical."
        ),
    }
