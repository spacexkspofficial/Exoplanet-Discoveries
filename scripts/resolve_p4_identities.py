"""Resolve canonical identities for the backlog (MASTER_PLAN.md section 4.1).

"The canonical node is a (TIC ID, Gaia DR3 source_id) pair resolved once at T0
via the TIC's own cross-match, stored with provenance."

The campaign path never needed sky positions -- it works from TIC IDs and
downloaded light curves -- so the ledger has coordinates for only the stars
whose target lists happened to carry them. Every sample-scoped catalog extract
needs a position, and proper-motion-aware matching needs a proper motion, so
this resolves both from the TIC itself in bulk rather than per star.

    python scripts/resolve_p4_identities.py --out results/p4/readjudication_v1
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from exohunt import identity, ledger, snapshots  # noqa: E402
from exohunt.config import CURRENT_IDENTITY  # noqa: E402
from exohunt.paths import default_db_path  # noqa: E402

TIC_TABLE = '"IV/39/tic82"'
TIC_COLUMNS = "TIC, RAJ2000, DEJ2000, pmRA, pmDE, Tmag, GAIA, Teff, Rad, Dist"
# TIC identifiers are short, so a larger batch than the position-scoped queries
# still produces a query the service accepts comfortably.
BATCH = 300

BACKLOG_LANES = (
    "automated_survivor",
    "single_event_lead",
    "known_eb_host_residual_review",
    "catalog_coverage_gap",
)


def _f(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result


def _int(value: Any) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("results/p4/readjudication_v1"))
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)

    conn = ledger.connect(default_db_path())
    try:
        placeholders = ",".join("?" for _ in BACKLOG_LANES)
        tics = [
            int(row[0])
            for row in conn.execute(
                f"SELECT tic_id FROM star_state WHERE status IN ({placeholders}) "
                "ORDER BY tic_id",
                BACKLOG_LANES,
            )
        ]
        print(f"backlog stars: {len(tics)}")

        resolved: dict[int, dict[str, Any]] = {}
        url = snapshots.SERVICES["vizier_tap"]
        for start in range(0, len(tics), BATCH):
            batch = tics[start : start + BATCH]
            joined = ",".join(str(value) for value in batch)
            query = (
                f"select {TIC_COLUMNS} from {TIC_TABLE} where TIC in ({joined})"
            )
            began = time.monotonic()
            text = snapshots._tap_sync(url, query, timeout=args.timeout)
            rows, _ = snapshots._parse_csv(text)
            for row in rows:
                tic = _int(row.get("TIC"))
                if tic is None:
                    continue
                resolved[tic] = row
            print(
                f"  [{start + len(batch):>5}/{len(tics)}] "
                f"+{len(rows)} rows in {time.monotonic() - began:.1f}s"
            )

        print(f"resolved: {len(resolved)} of {len(tics)}")

        written = 0
        nodes = 0
        for tic, row in sorted(resolved.items()):
            ra, dec = _f(row.get("RAJ2000")), _f(row.get("DEJ2000"))
            gaia = _int(row.get("GAIA"))
            ledger.upsert_star(
                conn,
                tic,
                ra_deg=ra,
                dec_deg=dec,
                tmag=_f(row.get("Tmag")),
                teff_k=_f(row.get("Teff")),
                stellar_radius_solar=_f(row.get("Rad")),
                distance_pc=_f(row.get("Dist")),
                gaia_source_id=gaia,
            )
            written += 1
            if ra is None or dec is None:
                continue
            # The TIC's own Gaia cross-match is a catalog claim, not a
            # positional one: record it with that basis and full confidence in
            # the source, leaving the positional scene to the Gaia extract.
            position = identity.SkyPosition(
                ra_deg=ra,
                dec_deg=dec,
                epoch_jyear=CURRENT_IDENTITY.gaia_reference_epoch_jyear,
                pmra_mas_yr=_f(row.get("pmRA")),
                pmdec_mas_yr=_f(row.get("pmDE")),
            )
            identity.upsert_node(
                conn,
                identity.IdentityNode(
                    tic_id=tic,
                    gaia_source_id=gaia,
                    position=position,
                    resolution=(
                        identity.RESOLUTION_UNIQUE
                        if gaia
                        else identity.RESOLUTION_UNRESOLVED
                    ),
                    candidate_count=1 if gaia else 0,
                    provenance={
                        "source": "tic_v8.2",
                        "table": TIC_TABLE,
                        "basis": identity.MATCH_BASIS_CATALOG,
                        "note": (
                            "the TIC's own Gaia cross-match; the positional "
                            "neighbour scene is a separate, later claim"
                        ),
                    },
                ),
            )
            nodes += 1
            if gaia:
                identity.add_edge(
                    conn,
                    tic_id=tic,
                    identifier_type="gaia_dr3",
                    identifier=str(gaia),
                    source="tic_v8.2",
                    confidence=1.0,
                    match_basis=identity.MATCH_BASIS_CATALOG,
                )
        conn.commit()
        print(f"stars enriched: {written}; identity nodes: {nodes}")
    finally:
        conn.close()

    args.out.mkdir(parents=True, exist_ok=True)
    positions = args.out / "backlog_positions.csv"
    with open(positions, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["tic_id", "ra", "dec", "pmra", "pmdec", "tmag"])
        count = 0
        for tic, row in sorted(resolved.items()):
            if _f(row.get("RAJ2000")) is None:
                continue
            writer.writerow(
                [
                    tic,
                    row.get("RAJ2000"),
                    row.get("DEJ2000"),
                    row.get("pmRA"),
                    row.get("pmDE"),
                    row.get("Tmag"),
                ]
            )
            count += 1
    print(f"[written] {positions} ({count} positions)")

    (args.out / "identity_resolution.json").write_text(
        json.dumps(
            {
                "backlog_stars": len(tics),
                "resolved": len(resolved),
                "positions_written": count,
                "source_table": TIC_TABLE,
                "service": snapshots.SERVICES["vizier_tap"],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
