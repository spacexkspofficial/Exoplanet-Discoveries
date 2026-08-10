"""Backfill missing RA/Dec from the TIC for stars that are actually displayed.

64,239 of 84,555 ledger stars have no coordinates, because most were searched
from `targets/full_remaining_pool.csv`, whose only columns are
`target,tic_id,sectors`. The dashboard does not leave those stars out of the
sky view -- it synthesizes a display direction and labels it
``coordinate_source: "Estimated display direction and distance"``. A viewer
reading the 3D map has no way to tell a real position from a placeholder.

Scope is deliberate. 64,230 of the 64,239 are `screened_rejected` or
`no_transit_detected` -- stars the pipeline has already discarded, which nobody
inspects on the map. Querying the TIC 64,000 times to place them would be a
large amount of MAST traffic for no scientific gain. This script backfills only
stars whose *current projected status* is something a person would look at, and
prints exactly which ones it touched.

Read-only against MAST (a catalog cone lookup per star, not photometry).
Run with no arguments for a dry run; pass ``--apply`` to write.
"""

from __future__ import annotations

import argparse
import sqlite3

from exohunt import ledger
from exohunt.paths import default_db_path

# Everything except the two bulk discard states. A star in any of these is
# either a lead, a review case, or a human verdict -- all of them things an
# operator opens the map to find.
DISPLAYED_STATUSES = (
    "automated_survivor",
    "single_event_lead",
    "unresolved_transit_like_signal",
    "science_vetted_lead",
    "packet_ready_for_review",
    "known_tce_rediscovery",
    "known_eb_rediscovery",
    "known_eb_host_residual_review",
    "known_variable_star_review",
    "crowding_contamination_review",
    "pixel_offset_contamination",
    "single_sector_unconfirmed",
    "common_mode_systematic",
    "localized_coincidence",
    "search_artifact_rejected",
    "false_positive",
    "vetted_candidate",
    "confirmed_planet",
    "rediscovery",
    "catalog_coverage_gap",
    "context_incomplete",
    "search_error",
)


def missing(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    placeholders = ",".join("?" * len(DISPLAYED_STATUSES))
    return [
        (int(row[0]), str(row[1]))
        for row in conn.execute(
            "SELECT s.tic_id, s.status FROM star_state s "
            "JOIN star t ON t.tic_id = s.tic_id "
            f"WHERE t.ra_deg IS NULL AND s.status IN ({placeholders}) "
            "ORDER BY s.tic_id",
            DISPLAYED_STATUSES,
        )
    ]


def query_tic(tic_ids: list[int]) -> dict[int, tuple[float, float]]:
    """One TIC lookup per star. Returns {tic_id: (ra_deg, dec_deg)}."""

    from astroquery.mast import Catalogs

    found: dict[int, tuple[float, float]] = {}
    for tic_id in tic_ids:
        try:
            table = Catalogs.query_criteria(catalog="TIC", ID=int(tic_id))
        except Exception as exc:  # pragma: no cover - network
            print(f"  TIC {tic_id}: query failed ({exc})")
            continue
        if table is None or len(table) == 0:
            print(f"  TIC {tic_id}: not in the TIC")
            continue
        try:
            found[int(tic_id)] = (float(table["ra"][0]), float(table["dec"][0]))
        except (KeyError, IndexError, TypeError, ValueError):
            print(f"  TIC {tic_id}: row carried no usable ra/dec")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write to the ledger.")
    parser.add_argument(
        "--limit", type=int, default=None, help="Cap how many stars are queried."
    )
    args = parser.parse_args()

    db = default_db_path()
    conn = sqlite3.connect(str(db))
    targets = missing(conn)
    if args.limit:
        targets = targets[: args.limit]

    total_missing = conn.execute(
        "SELECT COUNT(*) FROM star WHERE ra_deg IS NULL"
    ).fetchone()[0]
    print(f"database: {db}")
    print(f"stars with no coordinates at all : {total_missing:,}")
    print(f"...of which are displayed states : {len(targets):,}")
    for tic_id, status in targets:
        print(f"    TIC {tic_id:<12} {status}")
    if not targets:
        print("\nNothing to backfill.")
        return 0
    if not args.apply:
        print("\nDRY RUN -- no MAST queries issued, nothing written.")
        print("Re-run with --apply.")
        return 0

    print(f"\nquerying the TIC for {len(targets)} star(s)...")
    found = query_tic([tic for tic, _ in targets])
    written = 0
    for tic_id, (ra, dec) in sorted(found.items()):
        ledger.upsert_star(conn, tic_id, ra_deg=ra, dec_deg=dec)
        print(f"  TIC {tic_id:<12} ra={ra:.6f}  dec={dec:.6f}")
        written += 1
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.commit()
    print(f"\nbackfilled {written} of {len(targets)}; WAL checkpointed.")
    remaining = conn.execute(
        "SELECT COUNT(*) FROM star WHERE ra_deg IS NULL"
    ).fetchone()[0]
    print(f"stars still without coordinates: {remaining:,} (bulk discard states)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
