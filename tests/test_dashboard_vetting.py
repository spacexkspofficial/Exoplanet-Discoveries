"""The P4 vetting panel: visible progress for work that moves no status."""

from __future__ import annotations

import json

import pytest

from exohunt import ledger, snapshots
from exohunt.dashboard_api import vetting_payload


def _seed(conn) -> None:
    snapshots.register(
        conn,
        snapshots.SnapshotManifest(
            source="nasa_toi",
            version="20260101T000000Z",
            content_hash="hash-old",
            row_count=10,
            columns=("toi",),
            scope="whole_catalog",
            scope_hash=None,
            scope_size=None,
            service_url="https://example.invalid",
            query="select * from toi",
            fetched_at_utc="2026-01-01T00:00:00+00:00",
            rows_present=True,
        ),
    )
    snapshots.register(
        conn,
        snapshots.SnapshotManifest(
            source="nasa_toi",
            version="20260202T000000Z",
            content_hash="hash-new",
            row_count=12,
            columns=("toi",),
            scope="whole_catalog",
            scope_hash=None,
            scope_size=None,
            service_url="https://example.invalid",
            query="select * from toi",
            fetched_at_utc="2026-02-02T00:00:00+00:00",
            rows_present=True,
        ),
    )
    for tic, resolution in ((1, "unique"), (2, "ambiguous"), (3, "ambiguous")):
        conn.execute(
            "INSERT INTO identity_node (tic_id, resolution, candidate_count, "
            "provenance, resolved_at_utc) VALUES (?, ?, 1, '{}', 'now')",
            (tic, resolution),
        )
    for tic, t3, status, resolved in (
        (1, "fails_calibrated_red_noise_floor", None, True),
        (2, "passes", "unresolved_transit_like_signal", True),
        (3, "not_evaluable", None, False),
    ):
        ledger.append_evidence(
            conn,
            tic_id=tic,
            kind="t5_readjudication",
            source="p4_readjudication:v3:vet1-abc",
            payload={
                "t3_regate": {"verdict": t3},
                "t5_adjudication": {"recommended_status": status},
                "resolved": resolved,
            },
            verdict=None,
            affects_state=False,
            signature="vet1:abc",
        )
    conn.commit()


def test_panel_reports_coverage_identity_and_readjudication(tmp_path) -> None:
    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        _seed(conn)
        payload = vetting_payload(conn)

        # Newest generation per source is the one an adjudication would cite.
        assert payload["snapshot_sources"] == 1
        assert payload["snapshots"]["nasa_toi"]["content_hash"] == "hash-new"

        assert payload["identity"]["resolution"] == {"ambiguous": 2, "unique": 1}
        assert payload["identity"]["ambiguous_fraction"] == pytest.approx(
            2 / 3, abs=1e-4
        )

        readjudication = payload["readjudication"]
        assert readjudication["stars"] == 3
        assert readjudication["resolved"] == 2
        assert readjudication["resolved_fraction"] == pytest.approx(2 / 3, abs=1e-4)
        assert readjudication["t3_regate"]["fails_calibrated_red_noise_floor"] == 1
        assert readjudication["vetting_signatures"] == ["vet1:abc"]
    finally:
        conn.close()


def test_panel_states_that_it_has_moved_no_status(tmp_path) -> None:
    """The whole reason this panel exists.

    Vetting evidence is non-voting and carries no verdict, so an operator
    watching `status_counts` sees a phase run and nothing change. The panel
    must say that rather than let the silence imply nothing happened -- or,
    worse, let the numbers here be mistaken for status changes.
    """

    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        _seed(conn)
        payload = vetting_payload(conn)
        assert payload["affects_status_counts"] is False
        assert "non-voting" in payload["note"]
        # The counts really are absent from the projection.
        assert ledger.rebuild_star_state(conn) == {}
        assert ledger.status_counts(conn) == {}
    finally:
        conn.close()


def test_only_the_newest_generation_is_summarized(tmp_path) -> None:
    """Re-adjudication is append-only, so generations must not be added up.

    Every policy or snapshot change writes a new row per star. Counting all
    `t5_readjudication` rows tallied the same 1,363-star backlog as 6,815
    stars on the live ledger -- the mixed-signature aggregation the plan's
    risk register alarms on.
    """

    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        # The superseded generation is written first, as it was in reality:
        # "newest" means most recently executed, which is what an operator
        # wants the panel to reflect.
        for tic in (1, 2, 3):
            ledger.append_evidence(
                conn,
                tic_id=tic,
                kind="t5_readjudication",
                source="p4_readjudication:v2:vet1-old",
                payload={
                    "t3_regate": {"verdict": "not_evaluable"},
                    "t5_adjudication": {"recommended_status": None},
                    "resolved": False,
                },
                verdict=None,
                affects_state=False,
                signature="vet1:old",
            )
        conn.commit()
        _seed(conn)  # generation v3: 3 stars, 2 resolved

        payload = vetting_payload(conn)
        readjudication = payload["readjudication"]
        # Six rows exist; three stars are reported.
        assert conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE kind = 't5_readjudication'"
        ).fetchone()[0] == 6
        assert readjudication["stars"] == 3
        assert readjudication["resolved"] == 2
        assert readjudication["vetting_signatures"] == ["vet1:abc"]
        assert readjudication["generation"] == "p4_readjudication:v3:vet1-abc"
        # The superseded generation's answers do not leak into the counts.
        assert readjudication["t3_regate"].get("not_evaluable", 0) == 1
    finally:
        conn.close()


def test_panel_is_empty_but_valid_before_any_vetting_runs(tmp_path) -> None:
    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        payload = vetting_payload(conn)
        assert payload["snapshot_sources"] == 0
        assert payload["readjudication"]["stars"] == 0
        assert payload["readjudication"]["resolved_fraction"] is None
        assert payload["identity"]["ambiguous_fraction"] is None
        assert json.dumps(payload)  # serializes for the HTTP layer
    finally:
        conn.close()


def test_endpoint_is_registered(tmp_path) -> None:
    """Route registration only; this venv has no httpx, so no TestClient.

    The payload itself is exercised directly above, which is where the logic
    lives -- the server layer only wraps it in `read_ledger`.
    """

    from exohunt import dashboard_server

    dashboard = tmp_path / "dashboard"
    (dashboard / "dist").mkdir(parents=True)
    (dashboard / "dist" / "index.html").write_text("dashboard", encoding="utf-8")
    path = tmp_path / "ledger.db"
    ledger.connect(path).close()

    app = dashboard_server.create_app(workspace=tmp_path, db_path=path)
    assert "/api/vetting" in {route.path for route in app.routes}
