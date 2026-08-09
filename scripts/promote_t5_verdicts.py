"""Cast the newest T5 generation's verdicts (owner decision 3).

Decision 3 settled the question correction 38 raised: the ledger's
stage-then-precedence fold is authoritative. Until it was settled,
`run_p4_readjudication.py` wrote every row with `verdict=None`, so all P4
vetting evidence was recorded and none of it could change a star's status.

That script now casts verdicts under `--promote`, but re-running it means
re-querying every catalog snapshot. The adjudications already in the ledger are
sound -- what they lacked was permission to vote. This script grants it by
appending a new generation carrying the same payloads with their
`recommended_status` as the verdict.

Appending rather than editing is deliberate. Evidence is immutable and
supersession is expressed by later rows; `set_affects_state` exists for
projection scoping, but changing a row's *verdict* would be rewriting what it
said.

Scope, and the two guards on it
-------------------------------
* **Newest generation only.** Six generations hold the same 1,363 stars.
  Folding all six would let a retired policy outvote the current one purely by
  landing later in `evidence_id` order.
* **Only a recommendation naming a real registry status votes.** 26 stars in
  the v4 generation recommend `None` and are all `resolved: false`. Absence is
  not a verdict.

Nothing here decides what a star becomes. `resolve_status` does, and it cannot
downgrade an evidence stage -- verified in the dry run: every one of the 1,007
moves is to an equal or higher stage.

Run with no arguments for a dry run. Pass ``--apply`` to write.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict

from exohunt import ledger
from exohunt.paths import default_db_path
from exohunt.statuses import STATUS_REGISTRY, resolve_status

SOURCE_SUFFIX = "decision3-promoted"


def newest_generation(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT source FROM evidence WHERE kind = 't5_readjudication' "
        "AND source NOT LIKE ? ORDER BY evidence_id DESC LIMIT 1",
        (f"%{SOURCE_SUFFIX}%",),
    ).fetchone()
    return str(row["source"]).split("#")[0] if row else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write and rebuild.")
    args = parser.parse_args()

    db = default_db_path()
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    generation = newest_generation(conn)
    if generation is None:
        print("no t5_readjudication generation found; nothing to promote.")
        return 1
    print(f"database:   {db}")
    print(f"generation: {generation}\n")

    rows = conn.execute(
        "SELECT tic_id, payload, signature FROM evidence "
        "WHERE kind = 't5_readjudication' AND source LIKE ? "
        "ORDER BY tic_id, evidence_id",
        (generation + "%",),
    ).fetchall()

    candidates: dict[int, tuple[dict, str, str | None]] = {}
    skipped_no_recommendation = 0
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except Exception:
            continue
        recommended = (payload.get("t5_adjudication") or {}).get("recommended_status")
        if recommended is None or str(recommended) not in STATUS_REGISTRY:
            skipped_no_recommendation += 1
            continue
        candidates[int(row["tic_id"])] = (
            payload,
            str(recommended),
            row["signature"],
        )

    print(f"rows in generation:            {len(rows):,}")
    print(f"would vote:                    {len(candidates):,}")
    print(f"skipped (no real recommendation): {skipped_no_recommendation:,}")

    current = {
        int(r["tic_id"]): str(r["status"])
        for r in conn.execute("SELECT tic_id, status FROM star_state")
    }
    transitions: dict[tuple[str, str], int] = defaultdict(int)
    for tic, (_, recommended, _sig) in candidates.items():
        now = current.get(tic)
        if now is None:
            continue
        folded = resolve_status([now, recommended])
        if folded != now:
            transitions[(now, folded)] += 1
    print(f"stars whose status would move: {sum(transitions.values()):,}\n")

    downgrades = [
        (old, new, n)
        for (old, new), n in transitions.items()
        if STATUS_REGISTRY[old].evidence_stage > STATUS_REGISTRY[new].evidence_stage
    ]
    if downgrades:
        print("*** REFUSING: these moves downgrade an evidence stage ***")
        for old, new, n in downgrades:
            print(f"    {old} -> {new} ({n:,})")
        return 1

    if not args.apply:
        for (old, new), n in sorted(transitions.items(), key=lambda kv: -kv[1]):
            print(f"  {old:<34} -> {new:<34} {n:>6,}")
        print("\nDRY RUN -- nothing written. Re-run with --apply.")
        return 0

    before = {
        str(r["status"]): int(r["n"])
        for r in conn.execute(
            "SELECT status, COUNT(*) n FROM star_state GROUP BY status"
        )
    }
    written = 0
    for tic, (payload, recommended, signature) in sorted(candidates.items()):
        row_id = ledger.append_evidence(
            conn,
            tic_id=tic,
            kind="t5_readjudication",
            source=f"{generation}:{SOURCE_SUFFIX}#tic:{tic}",
            payload=payload,
            verdict=recommended,
            affects_state=True,
            signature=signature,
        )
        if row_id is not None:
            written += 1
    conn.commit()
    print(f"voting rows written: {written:,} (idempotent; 0 means already applied)")

    after = dict(ledger.rebuild_star_state(conn))
    print("\n=== status counts, before -> after ===")
    for status in sorted(set(before) | set(after)):
        b, a = before.get(status, 0), after.get(status, 0)
        if b != a:
            print(f"  {status:<38} {b:>7,} -> {a:>7,}  ({a - b:+,})")
    total_before, total_after = sum(before.values()), sum(after.values())
    print(f"\n  total stars: {total_before:,} -> {total_after:,}")
    if total_before != total_after:
        print("  WARNING: the projection gained or lost stars; it should only")
        print("  reclassify them.")

    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.commit()
    print("WAL checkpointed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
