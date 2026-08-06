from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from exohunt import ledger
from exohunt.dashboard_api import (
    ops_payload,
    star_detail_payload,
    star_page_payload,
    summary_payload,
    systematics_payload,
)
from exohunt.dashboard_server import create_app


def _screening_result(tic_id: int, *, status: str = "survivor") -> dict:
    return {
        "target": f"TIC {tic_id}",
        "tic_id": tic_id,
        "sectors": [100, 101],
        "status": status,
        "period_days": 3.21,
        "depth_ppm": 1200.0,
        "depth_snr": 9.5,
        "duration_hours": 2.0,
        "observed_transits": 4,
        "rejection_reasons": "",
        "followup_priority": 80,
        "followup_reasons": "localize the signal",
        "vetting_tier": "high_priority_followup",
        "phase_curve_available": True,
    }


def _seed_ledger(path: Path) -> None:
    conn = ledger.connect(path)
    try:
        ledger.upsert_star(
            conn,
            1,
            name="TIC 1",
            ra_deg=12.5,
            dec_deg=-20.0,
            distance_pc=42.0,
            tmag=11.2,
            lane="validation",
        )
        ledger.append_evidence(
            conn,
            tic_id=1,
            kind="screening",
            source="summary:test#event:e1",
            payload={
                "label": "Automated transit-like survivor",
                "notes": "localize the signal",
                "result": _screening_result(1),
            },
            verdict="automated_survivor",
            signature="legacy:processed-lc-v2",
        )
        ledger.append_evidence(
            conn,
            tic_id=1,
            kind="screening",
            source="summary:older#event:e0",
            payload={
                "label": "Screened rejection",
                "notes": "older result",
                "result": _screening_result(1, status="rejected"),
            },
            verdict="screened_rejected",
            affects_state=False,
            signature="legacy:older",
        )
        ledger.append_evidence(
            conn,
            tic_id=1,
            kind="common_mode",
            source="common_mode:test#tic:1",
            payload={
                "label": "Common-mode systematic",
                "notes": "shared by 30 targets",
                "screen": {
                    "verdict": "common_mode_systematic",
                    "shared_targets": 30,
                    "expected_shared_targets": 2.0,
                    "enrichment": 15.0,
                    "cameras_spanned": 4,
                    "sky_spread_deg": 8.0,
                    "spacecraft_harmonic": "1/2",
                    "duration_at_grid_rail": True,
                    "period_at_search_ceiling": False,
                },
            },
            verdict="common_mode_systematic",
        )
        ledger.upsert_star(conn, 2, name="TIC 2", lane="faint_m")
        ledger.append_evidence(
            conn,
            tic_id=2,
            kind="screening",
            source="summary:test#event:e1",
            payload={
                "label": "Search error",
                "notes": "archive unavailable",
                "result": _screening_result(2, status="error"),
            },
            verdict="search_error",
            signature="legacy:processed-lc-v2",
        )
        conn.commit()
        ledger.rebuild_star_state(conn)
        assert (
            ledger.acquire_db_lease(
                conn,
                name="coordinator",
                holder="pid 123 on test-host",
            )
            == "acquired"
        )
    finally:
        conn.close()


def test_dashboard_endpoints_read_projection_and_page_stars(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    _seed_ledger(db_path)
    (tmp_path / "dashboard" / "dist").mkdir(parents=True)
    (tmp_path / "dashboard" / "dist" / "index.html").write_text(
        "dashboard", encoding="utf-8"
    )

    readonly = ledger.connect_readonly(db_path)
    try:
        summary = summary_payload(readonly)
        assert "stars" not in summary
        assert summary["stars_total"] == 2
        assert summary["status_counts"] == {
            "common_mode_systematic": 1,
            "search_error": 1,
        }
        assert summary["status_counts_by_signature"] == {
            "legacy:processed-lc-v2": {"search_error": 1},
            "unversioned": {"common_mode_systematic": 1},
        }

        first_page = star_page_payload(readonly, page=1, page_size=1)
        assert first_page["total"] == 2
        assert first_page["pages"] == 2
        assert first_page["items"][0]["tic_id"] == 1
        assert first_page["items"][0]["status"] == "common_mode_systematic"
        assert first_page["items"][0]["period_days"] == 3.21
        assert first_page["items"][0]["sectors"] == [100, 101]

        filtered = star_page_payload(
            readonly,
            status="search_error",
            lane="faint_m",
            page=1,
            page_size=50,
        )
        assert [row["tic_id"] for row in filtered["items"]] == [2]

        detail = star_detail_payload(readonly, 1)
        assert detail is not None
        assert detail["current_state"]["status"] == "common_mode_systematic"
        assert len(detail["evidence"]) == 3
        assert any(
            row["affects_state"] is False for row in detail["evidence"]
        )
    finally:
        readonly.close()

    routes = {route.path for route in create_app(tmp_path, db_path=db_path).routes}
    assert {
        "/api/summary",
        "/api/stars",
        "/api/star/{tic_id}",
        "/api/ops",
        "/api/systematics",
    } <= routes


def test_ops_liveness_comes_only_from_heartbeat_age(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    conn = ledger.connect(db_path)
    try:
        moment = datetime(2026, 7, 27, tzinfo=timezone.utc)
        assert (
            ledger.acquire_db_lease(
                conn,
                name="coordinator",
                holder="worker",
                now=moment - timedelta(seconds=44),
            )
            == "acquired"
        )
        assert ops_payload(conn, now=moment)["liveness"] == "live"
        conn.execute(
            "UPDATE lease SET heartbeat_at_utc = ? WHERE name = 'coordinator'",
            ((moment - timedelta(seconds=45)).isoformat(),),
        )
        conn.commit()
        stale = ops_payload(conn, now=moment)
        assert stale["liveness"] == "stale"
        assert stale["live"] is False
        assert stale["heartbeat_age_seconds"] == 45.0
    finally:
        conn.close()

    # A checkpoint string cannot make the API claim that a process is alive.
    progress = tmp_path / "results" / "campaign" / "phantom" / "batch_progress.json"
    progress.parent.mkdir(parents=True)
    progress.write_text(json.dumps({"state": "running"}), encoding="utf-8")
    empty_db = tmp_path / "empty.db"
    empty_conn = ledger.connect(empty_db)
    try:
        absent = ops_payload(empty_conn)
        assert absent["liveness"] == "absent"
        assert absent["live"] is False
    finally:
        empty_conn.close()


def test_summary_surfaces_only_a_stored_trusted_p3_release(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    conn = ledger.connect(db_path)
    report_path = tmp_path / "release.json"
    report_path.write_text("{}", encoding="utf-8")
    report = {
        "scientific_signature": "sig1:released",
        "code_version": "git:abc123",
        "release_gate_passes": True,
        "execution_complete": True,
        "errors": [],
        "calibration_gate": {
            "passes": True,
            "counts": {"baseline": 500, "inverted": 500, "scrambled": 500},
            "gates": {"inverted_survivor_rate": {"value": 0.0, "passes": True}},
        },
        "known_planet_gate": {
            "passes": True,
            "counts": {"total": 20, "passed": 20, "failed": 0, "errors": 0},
        },
    }
    try:
        before = summary_payload(conn)
        assert before["health_flags"]["diagnostic_only"] is True
        assert before["trusted_release"] is None

        ledger.store_release_report(
            conn,
            signature="sig1:released",
            report_path=report_path,
            payload=report,
        )
        conn.commit()
        after = summary_payload(conn)
    finally:
        conn.close()

    assert after["health_flags"]["diagnostic_only"] is False
    assert after["health_flags"]["calibration_gate_complete"] is True
    assert after["trusted_release"] == {
        "status": "trusted_release",
        "scientific_signature": "sig1:released",
        "code_version": "git:abc123",
        "created_at_utc": after["trusted_release"]["created_at_utc"],
        "report_sha256": after["trusted_release"]["report_sha256"],
        "calibration_counts": {
            "baseline": 500,
            "inverted": 500,
            "scrambled": 500,
        },
        "calibration_gates": {
            "inverted_survivor_rate": {"value": 0.0, "passes": True}
        },
        "known_planet_counts": {
            "total": 20,
            "passed": 20,
            "failed": 0,
            "errors": 0,
        },
    }
    assert after["warnings"][0].startswith("P3 calibration is complete")


def test_systematics_endpoint_reads_common_mode_evidence(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    _seed_ledger(db_path)
    conn = ledger.connect_readonly(db_path)
    try:
        payload = systematics_payload(conn)
    finally:
        conn.close()
    assert payload["screened_targets"] == 1
    assert payload["flagged_targets"] == 1
    assert payload["common_mode_systematic"] == 1
    assert payload["on_spacecraft_harmonic"] == 1
    assert payload["duration_at_grid_rail"] == 1


def test_readonly_connection_cannot_write(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    conn = ledger.connect(db_path)
    conn.close()

    readonly = ledger.connect_readonly(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            readonly.execute(
                "INSERT INTO event_log (kind, payload, created_at_utc) "
                "VALUES ('write_attempt', '{}', '2026-07-27T00:00:00+00:00')"
            )
    finally:
        readonly.close()
