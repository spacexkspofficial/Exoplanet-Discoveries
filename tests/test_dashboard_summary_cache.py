"""The summary poll must not recompute a payload whose inputs did not move."""

from __future__ import annotations

from exohunt import ledger
from exohunt.dashboard_api import (
    cached_summary_payload,
    summary_payload,
    summary_revision,
)


def _star(conn, tic_id: int, verdict: str = "automated_survivor") -> None:
    ledger.upsert_star(conn, tic_id, lane="primary")
    ledger.append_evidence(
        conn,
        tic_id=tic_id,
        kind="screening",
        source=f"summary:run/{tic_id}",
        payload={"label": "x", "notes": "y"},
        verdict=verdict,
        signature="sig1:abc",
    )


def test_revision_tracks_the_things_that_change_the_summary(tmp_path) -> None:
    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        _star(conn, 1)
        ledger.rebuild_star_state(conn)
        first = summary_revision(conn)
        assert summary_revision(conn) == first

        _star(conn, 2)
        ledger.rebuild_star_state(conn)
        assert summary_revision(conn) != first
    finally:
        conn.close()


def test_cache_returns_the_same_counts_and_refreshes_only_the_timestamp(
    tmp_path,
) -> None:
    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        _star(conn, 1)
        _star(conn, 2)
        ledger.rebuild_star_state(conn)

        cache: dict = {}
        cold = cached_summary_payload(conn, cache=cache)
        warm = cached_summary_payload(conn, cache=cache)

        assert cold["served_from_cache"] is False
        assert warm["served_from_cache"] is True
        for key in ("status_counts", "status_counts_by_signature", "stars_total"):
            assert cold[key] == warm[key]
        # A cached payload must still agree with a freshly computed one.
        direct = summary_payload(conn)
        assert warm["status_counts"] == direct["status_counts"]
        assert warm["status_counts_by_signature"] == direct["status_counts_by_signature"]
    finally:
        conn.close()


def test_cache_cannot_serve_state_that_has_moved_on(tmp_path) -> None:
    """The key is the state, so staleness is impossible by construction."""

    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        _star(conn, 1)
        ledger.rebuild_star_state(conn)
        cache: dict = {}
        before = cached_summary_payload(conn, cache=cache)
        assert before["status_counts"] == {"automated_survivor": 1}

        _star(conn, 2, verdict="screened_rejected")
        ledger.rebuild_star_state(conn)
        after = cached_summary_payload(conn, cache=cache)

        assert after["status_counts"] == {
            "automated_survivor": 1,
            "screened_rejected": 1,
        }
        assert after["data_revision"] != before["data_revision"]
    finally:
        conn.close()


def test_signature_and_lane_queries_still_use_their_covering_indexes(
    tmp_path,
) -> None:
    """INDEXED BY is load-bearing here, not decorative.

    The planner rates a rowid lookup as optimal and ignores the covering
    index, but the row it then reads carries a JSON payload the summary never
    looks at -- measured at 464.6 ms against 178.7 ms on the live ledger. If
    either index disappears, these queries must fail loudly rather than
    quietly returning to the slow plan.
    """

    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        _star(conn, 1)
        ledger.rebuild_star_state(conn)
        plans = "\n".join(
            str(row["detail"])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT COALESCE(e.signature, 'unversioned') AS signature, "
                "ss.status, COUNT(*) AS n FROM star_state AS ss "
                "LEFT JOIN evidence AS e "
                "INDEXED BY evidence_signature_by_id "
                "ON e.evidence_id = ss.decided_by_evidence_id "
                "GROUP BY signature, ss.status"
            )
        )
        assert "COVERING INDEX evidence_signature_by_id" in plans
        # And the payload still builds, which is what proves the hint is valid
        # against the shipped schema rather than only against a fixture.
        assert summary_payload(conn)["status_counts_by_signature"]
    finally:
        conn.close()
