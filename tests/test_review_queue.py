"""The review queue (MASTER_PLAN.md section 8): short, ranked, and honest."""

from __future__ import annotations

from exohunt import ledger
from exohunt.dashboard_api import review_queue_payload

GENERATION = "p4_readjudication:v3:vet1-abc"


def _entry(
    conn,
    tic_id: int,
    *,
    t3: str = "passes",
    status: str | None = "unresolved_transit_like_signal",
    conflicts: int = 0,
    blocked: str | None = None,
    period: float | None = 3.0,
    source: str = GENERATION,
) -> None:
    ledger.upsert_star(conn, tic_id)
    ledger.append_evidence(
        conn,
        tic_id=tic_id,
        kind="t5_readjudication",
        source=source,
        payload={
            "t3_regate": {"verdict": t3},
            "ephemeris": {"period_days": period},
            "t5_adjudication": {
                "recommended_status": status,
                "conflicts": [{"kind": "disagreeing_object_classes"}] * conflicts,
                "blocked_reason": blocked,
                "relations": [],
            },
        },
        verdict=None,
        affects_state=False,
        signature="vet1:abc",
    )


def test_stars_the_calibrated_gate_killed_are_not_queued(tmp_path) -> None:
    """A review queue must not spend the scarcest resource on settled cases."""

    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        _entry(conn, 1, t3="fails_calibrated_red_noise_floor")
        _entry(conn, 2, t3="passes")
        conn.commit()
        payload = review_queue_payload(conn)
        assert [item["tic_id"] for item in payload["entries"]] == [2]
        assert payload["total"] == 1
    finally:
        conn.close()


def test_contested_evidence_outranks_everything_else(tmp_path) -> None:
    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        _entry(conn, 10, status="unresolved_transit_like_signal")
        _entry(conn, 11, blocked="matches a catalogued planet")
        _entry(conn, 12, conflicts=1)
        conn.commit()
        order = [item["tic_id"] for item in review_queue_payload(conn)["entries"]]
        assert order == [12, 11, 10]
    finally:
        conn.close()


def test_each_entry_states_what_it_is_waiting_for(tmp_path) -> None:
    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        _entry(conn, 20, status="catalog_coverage_gap")
        _entry(conn, 21, period=None)
        conn.execute(
            "INSERT INTO identity_node (tic_id, resolution, candidate_count, "
            "provenance, resolved_at_utc) VALUES (22, 'ambiguous', 2, '{}', 'now')"
        )
        _entry(conn, 22)
        conn.commit()

        payload = review_queue_payload(conn)
        waiting = {item["tic_id"]: item["waiting_on"] for item in payload["entries"]}
        assert "catalog coverage" in waiting[20]
        assert "no ephemeris" in waiting[21]
        assert "ambiguous identity in pixel" in waiting[22]
        # And the aggregate tells the operator where the queue is actually stuck.
        assert payload["waiting_on"]["catalog coverage"] == 1
    finally:
        conn.close()


def test_only_the_newest_generation_is_queued(tmp_path) -> None:
    """Same reason the vetting panel scopes: generations must not be summed."""

    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        _entry(conn, 30, source="p4_readjudication:v2:vet1-old")
        _entry(conn, 31, source="p4_readjudication:v2:vet1-old")
        conn.commit()
        _entry(conn, 30, source=GENERATION)
        conn.commit()

        payload = review_queue_payload(conn)
        assert payload["generation"] == GENERATION
        assert [item["tic_id"] for item in payload["entries"]] == [30]
    finally:
        conn.close()


def test_the_queue_is_bounded(tmp_path) -> None:
    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        for tic_id in range(100, 130):
            _entry(conn, tic_id)
        conn.commit()
        payload = review_queue_payload(conn, limit=5)
        assert len(payload["entries"]) == 5
        assert payload["total"] == 30
    finally:
        conn.close()


def test_the_queue_says_it_holds_leads_not_candidates(tmp_path) -> None:
    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        _entry(conn, 40)
        conn.commit()
        payload = review_queue_payload(conn)
        assert payload["affects_status_counts"] is False
        assert "not a candidate" in payload["note"]
        # Opening the queue really does move nothing.
        assert ledger.rebuild_star_state(conn) == {}
    finally:
        conn.close()


def test_an_empty_ledger_returns_an_empty_queue(tmp_path) -> None:
    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        payload = review_queue_payload(conn)
        assert payload["entries"] == []
        assert payload["generation"] is None
    finally:
        conn.close()


def test_endpoint_is_registered(tmp_path) -> None:
    from exohunt import dashboard_server

    dashboard = tmp_path / "dashboard"
    (dashboard / "dist").mkdir(parents=True)
    (dashboard / "dist" / "index.html").write_text("x", encoding="utf-8")
    path = tmp_path / "ledger.db"
    ledger.connect(path).close()
    app = dashboard_server.create_app(workspace=tmp_path, db_path=path)
    assert "/api/review-queue" in {route.path for route in app.routes}
