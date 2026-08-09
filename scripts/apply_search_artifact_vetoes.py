"""Promote the three computed-but-unused artifact flags to vetoes (decision 2a).

``spacecraft_harmonic``, ``duration_at_grid_rail`` and
``period_at_search_ceiling`` have been recorded per target by the common-mode
screen since P2 and nothing has ever acted on them. Correction 72 measured
them across 84,374 screened targets: enriched 1.63x / 2.92x / 2.40x among
automated survivors, together flagging 311 of 990 survivors against 14.8% of
the population. The owner approved promoting them.

Scope, and why it is not the whole ledger
-----------------------------------------
Applying the flags to every star that carries one would move **12,504** stars,
of which only 311 are survivors. It would reclassify 81% of
``common_mode_systematic``, 74% of ``known_variable_star_review``, 67% of
``known_eb_rediscovery`` and even a ``science_vetted_lead`` -- overwriting
specific, established verdicts with a vaguer one. That is precisely correction
71's failure mode: a screen that looks justified on its own evidence while
silently retracting conclusions elsewhere.

A veto's job is to stop things being *promoted*. The stars currently promoted
are the automated survivors, and the measured enrichment is an enrichment
*among survivors*. So this script writes evidence only for stars whose current
projected status is ``automated_survivor``. Everything else keeps the more
specific verdict it already has.

``search_artifact_rejected`` sits at the ``population_screen`` stage, so the
ledger's existing stage-then-precedence fold applies it over the
``in_light_curve`` ``automated_survivor`` without any special-casing here.

Run with no arguments for a dry run. Pass ``--apply`` to write.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict

from exohunt import ledger
from exohunt.paths import default_db_path

FLAGS = ("spacecraft_harmonic", "duration_at_grid_rail", "period_at_search_ceiling")
VERDICT = "search_artifact_rejected"
KIND = "search_artifact"
SOURCE_PREFIX = "decision2a:common_mode_flags"


def flagged_survivors(conn: sqlite3.Connection) -> dict[int, dict]:
    """Survivors carrying at least one artifact flag, with the reasons."""

    survivors = {
        int(r["tic_id"])
        for r in conn.execute(
            "SELECT tic_id FROM star_state WHERE status = 'automated_survivor'"
        )
    }
    latest: dict[int, dict] = {}
    for r in conn.execute(
        "SELECT tic_id, payload FROM evidence WHERE kind = 'common_mode' "
        "ORDER BY tic_id, evidence_id"
    ):
        tic = int(r["tic_id"])
        if tic not in survivors:
            continue
        try:
            payload = json.loads(r["payload"])
        except Exception:
            continue
        # The flags live under `screen`, not at the payload top level. Reading
        # the top level yields zero hits for every star, which is absence
        # wearing the costume of "nothing is flagged".
        screen = payload.get("screen")
        if isinstance(screen, dict):
            latest[tic] = screen

    hits: dict[int, dict] = {}
    for tic, screen in latest.items():
        reasons = {}
        for flag in FLAGS:
            value = screen.get(flag)
            if value is None or value is False:
                continue
            reasons[flag] = value
        if reasons:
            hits[tic] = {
                "reasons": reasons,
                "campaign": screen.get("campaign"),
                "screen_report": screen.get("screen_report"),
            }
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write evidence and rebuild state. Without this it is a dry run.",
    )
    args = parser.parse_args()

    db = default_db_path()
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    before = {
        str(r["status"]): int(r["n"])
        for r in conn.execute(
            "SELECT status, COUNT(*) n FROM star_state GROUP BY status"
        )
    }
    hits = flagged_survivors(conn)
    print(f"database: {db}")
    print(f"automated_survivor before: {before.get('automated_survivor', 0):,}")
    print(f"survivors carrying >=1 artifact flag: {len(hits):,}")

    per_flag: dict[str, int] = defaultdict(int)
    for entry in hits.values():
        for flag in entry["reasons"]:
            per_flag[flag] += 1
    for flag in FLAGS:
        print(f"  {flag:<28} {per_flag[flag]:>5,}")

    if not args.apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply.")
        return 0

    written = 0
    for tic, entry in sorted(hits.items()):
        row = ledger.append_evidence(
            conn,
            tic_id=tic,
            kind=KIND,
            source=f"{SOURCE_PREFIX}#tic:{tic}",
            payload={
                "label": "Search artifact - fit sits on an instrument or grid rail",
                "notes": (
                    "Promoted from the common-mode screen's recorded flags by "
                    "owner decision 2a: "
                    + ", ".join(sorted(entry["reasons"]))
                ),
                "flags": entry["reasons"],
                "campaign": entry.get("campaign"),
                "screen_report": entry.get("screen_report"),
            },
            verdict=VERDICT,
            affects_state=True,
        )
        if row is not None:
            written += 1
    conn.commit()
    print(f"\nevidence rows written: {written:,} (idempotent; 0 means already applied)")

    counts = ledger.rebuild_star_state(conn)
    after = dict(counts)

    # The transition matrix, not the headline counts. Correction 71: a screen
    # can improve every summary statistic while destroying established
    # verdicts, and only the old->new comparison shows it.
    print("\n=== status counts, before -> after ===")
    for status in sorted(set(before) | set(after)):
        b, a = before.get(status, 0), after.get(status, 0)
        if b != a:
            print(f"  {status:<38} {b:>7,} -> {a:>7,}  ({a - b:+,})")
    unchanged = sum(1 for s in set(before) | set(after) if before.get(s, 0) == after.get(s, 0))
    print(f"  ({unchanged} statuses unchanged)")

    moved = before.get("automated_survivor", 0) - after.get("automated_survivor", 0)
    gained = after.get(VERDICT, 0) - before.get(VERDICT, 0)
    print(f"\nsurvivors removed: {moved:,}   {VERDICT} gained: {gained:,}")
    if moved != gained:
        print(
            "  WARNING: these should match. A mismatch means the fold moved "
            "stars this script did not intend to touch."
        )

    # A bulk ledger write leaves the WAL large enough to 503 the dashboard.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.commit()
    print("WAL checkpointed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
