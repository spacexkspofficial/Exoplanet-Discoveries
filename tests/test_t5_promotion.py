"""P4 vetting evidence may vote (owner decision 3, correction 38).

The bug this pins is the one `--promote` shipped with: it set `affects_state`
while `verdict` stayed `None`. `rebuild_star_state` selects on
`affects_state = 1 AND verdict IS NOT NULL`, so the flag announced a promotion
that could not happen -- and, like correction 57's dead vetoes, it reported as
*not changing anything* rather than as failing.
"""

from __future__ import annotations

from exohunt import ledger
from exohunt.statuses import resolve_status


def _star_with_screening(conn, tic_id: int, verdict: str) -> None:
    ledger.upsert_star(conn, tic_id)
    ledger.append_evidence(
        conn,
        tic_id=tic_id,
        kind="screening",
        source=f"campaign:test#tic:{tic_id}",
        payload={"label": "screened", "notes": ""},
        verdict=verdict,
        affects_state=True,
    )


def test_affects_state_without_a_verdict_changes_nothing(tmp_path) -> None:
    """The precise shape of the inert --promote flag."""

    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        _star_with_screening(conn, 1, "automated_survivor")
        ledger.append_evidence(
            conn,
            tic_id=1,
            kind="t5_readjudication",
            source="p4_readjudication:v4#tic:1",
            payload={"t5_adjudication": {"recommended_status": "unresolved_transit_like_signal"}},
            verdict=None,          # the bug
            affects_state=True,    # the flag that looked like it did something
        )
        conn.commit()
        counts = ledger.rebuild_star_state(conn)
        assert counts == {"automated_survivor": 1}
    finally:
        conn.close()


def test_a_cast_verdict_lets_the_fold_decide(tmp_path) -> None:
    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        _star_with_screening(conn, 1, "automated_survivor")
        ledger.append_evidence(
            conn,
            tic_id=1,
            kind="t5_readjudication",
            source="p4_readjudication:v4:decision3-promoted#tic:1",
            payload={"t5_adjudication": {"recommended_status": "unresolved_transit_like_signal"}},
            verdict="unresolved_transit_like_signal",
            affects_state=True,
        )
        conn.commit()
        counts = ledger.rebuild_star_state(conn)
        # catalog_context outranks in_light_curve, so the fold -- not the T5
        # row -- decides. The row only supplies a candidate.
        assert counts == {"unresolved_transit_like_signal": 1}
    finally:
        conn.close()


def test_the_fold_never_downgrades_an_evidence_stage(tmp_path) -> None:
    """A human outcome must survive any later automated adjudication.

    This is what made decision 3 safe to apply to 1,007 live stars: the
    promotion cannot overwrite a stronger conclusion with a weaker one, whatever
    order the rows land in.
    """

    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        _star_with_screening(conn, 1, "automated_survivor")
        ledger.append_evidence(
            conn,
            tic_id=1,
            kind="human_outcome",
            source="human:test#tic:1",
            payload={"label": "confirmed", "notes": ""},
            verdict="confirmed_planet",
            affects_state=True,
        )
        ledger.append_evidence(
            conn,
            tic_id=1,
            kind="t5_readjudication",
            source="p4_readjudication:v4:decision3-promoted#tic:1",
            payload={"t5_adjudication": {"recommended_status": "unresolved_transit_like_signal"}},
            verdict="unresolved_transit_like_signal",
            affects_state=True,
        )
        conn.commit()
        counts = ledger.rebuild_star_state(conn)
        assert counts == {"confirmed_planet": 1}
        assert (
            resolve_status(["confirmed_planet", "unresolved_transit_like_signal"])
            == "confirmed_planet"
        )
    finally:
        conn.close()


def test_a_ledger_decided_star_is_identifiable_for_the_parity_gate(tmp_path) -> None:
    """The join the parity gate uses to separate explained divergence.

    The exporter walks campaign files and cannot see T5 evidence, so a star it
    decides will differ between the two projections. `decided_by_evidence_id`
    names the exact row responsible, which is what makes that difference
    *accountable* rather than a reason to relax the gate (correction 50).
    """

    from exohunt.importer import LEDGER_ONLY_EVIDENCE_KINDS

    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        _star_with_screening(conn, 1, "automated_survivor")  # file-derived
        _star_with_screening(conn, 2, "automated_survivor")
        ledger.append_evidence(
            conn,
            tic_id=2,
            kind="t5_readjudication",
            source="p4_readjudication:v4:decision3-promoted#tic:2",
            payload={"t5_adjudication": {"recommended_status": "unresolved_transit_like_signal"}},
            verdict="unresolved_transit_like_signal",
            affects_state=True,
        )
        conn.commit()
        ledger.rebuild_star_state(conn)

        placeholders = ",".join("?" * len(LEDGER_ONLY_EVIDENCE_KINDS))
        decided = {
            int(row["tic_id"])
            for row in conn.execute(
                "SELECT s.tic_id AS tic_id FROM star_state s "
                "JOIN evidence e ON e.evidence_id = s.decided_by_evidence_id "
                f"WHERE e.kind IN ({placeholders})",
                tuple(sorted(LEDGER_ONLY_EVIDENCE_KINDS)),
            )
        }
        # Star 2 was decided by ledger-only evidence; star 1 was not.
        assert decided == {2}
    finally:
        conn.close()


def test_search_artifact_is_also_ledger_only() -> None:
    """Decision 2a's vetoes have the same property and must be listed too.

    They are derived from the common-mode screen's recorded flags rather than
    written by a campaign, so the exporter cannot reproduce them either.
    """

    from exohunt.importer import LEDGER_ONLY_EVIDENCE_KINDS

    assert "search_artifact" in LEDGER_ONLY_EVIDENCE_KINDS
    assert "t5_readjudication" in LEDGER_ONLY_EVIDENCE_KINDS
    # Adding a kind here weakens the gate; keep the set small and deliberate.
    assert len(LEDGER_ONLY_EVIDENCE_KINDS) == 2


def test_a_none_recommendation_is_absence_not_a_verdict() -> None:
    """26 stars in the live v4 generation recommend nothing at all.

    They are all `resolved: false`. Casting `None` as a verdict would turn "we
    could not adjudicate this" into a status, which is the failure mode the
    packet contract's `_is_measured` exists to prevent elsewhere.
    """

    from exohunt.statuses import STATUS_REGISTRY

    assert "None" not in STATUS_REGISTRY
    assert None not in STATUS_REGISTRY
