"""Fetch P4 catalog snapshots (MASTER_PLAN.md section 4.1).

This is the only code in the project that downloads catalogs in bulk, and it
is deliberately a script rather than something a campaign can trigger: the
owner authorizes catalog traffic explicitly, and a snapshot refresh changes
what every subsequent adjudication means.

Whole-catalog sources need no arguments::

    python scripts/fetch_p4_snapshots.py --sources nasa_toi nasa_ps tess_eb

Sample-scoped sources need the position list they are scoped over. Positions
come from a CSV with ``ra``/``dec`` columns (any of the usual spellings)::

    python scripts/fetch_p4_snapshots.py --sources gaia_dr3 vsx \
        --positions results/p4/known_objects_v1/positions.csv

Nothing is overwritten: each run writes a new immutable generation, prunes the
bulk rows of older ones, and registers the generation in the ledger.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from exohunt import ledger, snapshots  # noqa: E402

_RA_KEYS = ("ra", "ra_deg", "radeg", "ra_icrs", "raj2000")
_DEC_KEYS = ("dec", "de", "dec_deg", "dedeg", "de_icrs", "dej2000")


def read_positions(path: Path) -> list[tuple[float, float]]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise SystemExit(f"{path} has no header row.")
        lookup = {name.strip().lower(): name for name in reader.fieldnames}
        ra_key = next((lookup[key] for key in _RA_KEYS if key in lookup), None)
        dec_key = next((lookup[key] for key in _DEC_KEYS if key in lookup), None)
        if ra_key is None or dec_key is None:
            raise SystemExit(
                f"{path} needs right-ascension and declination columns; saw "
                + ", ".join(reader.fieldnames)
            )
        positions: list[tuple[float, float]] = []
        for row in reader:
            try:
                positions.append((float(row[ra_key]), float(row[dec_key])))
            except (TypeError, ValueError):
                continue
    if not positions:
        raise SystemExit(f"{path} contained no usable positions.")
    return positions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sources",
        nargs="+",
        required=True,
        help="Snapshot source names, or 'all-whole-catalog'.",
    )
    parser.add_argument("--positions", type=Path, default=None)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Write a JSON summary of what each fetch produced.",
    )
    args = parser.parse_args(argv)

    requested = list(args.sources)
    if requested == ["all-whole-catalog"]:
        requested = [
            name
            for name, source in snapshots.SNAPSHOT_SOURCES.items()
            if source.scope == "whole_catalog"
        ]

    unknown = [name for name in requested if name not in snapshots.SNAPSHOT_SOURCES]
    if unknown:
        raise SystemExit(f"Unknown snapshot sources: {', '.join(unknown)}")

    positions = read_positions(args.positions) if args.positions else None
    conn = ledger.connect()
    results: list[dict[str, object]] = []
    failures = 0
    try:
        for name in requested:
            source = snapshots.SNAPSHOT_SOURCES[name]
            needs_scope = source.scope == "position_list"
            if needs_scope and not positions:
                print(f"[skip] {name}: sample-scoped source needs --positions")
                results.append({"source": name, "status": "skipped_no_scope"})
                failures += 1
                continue
            started = time.monotonic()
            print(f"[fetch] {name} ({source.scope}) ...", flush=True)
            try:
                manifest = snapshots.fetch(
                    name,
                    positions=positions if needs_scope else None,
                    root=args.root,
                    timeout=args.timeout,
                    conn=conn,
                )
            except snapshots.SnapshotError as exc:
                print(f"[fail]  {name}: {exc}")
                results.append({"source": name, "status": "failed", "error": str(exc)})
                failures += 1
                continue
            conn.commit()
            elapsed = time.monotonic() - started
            print(
                f"[ok]    {name}: {manifest.row_count} rows, "
                f"{len(manifest.columns)} columns, {elapsed:.1f}s, "
                f"hash {manifest.content_hash[:16]}"
            )
            results.append(
                {
                    "source": name,
                    "status": "ok",
                    "version": manifest.version,
                    "content_hash": manifest.content_hash,
                    "row_count": manifest.row_count,
                    "column_count": len(manifest.columns),
                    "columns": list(manifest.columns),
                    "scope": manifest.scope,
                    "scope_hash": manifest.scope_hash,
                    "scope_size": manifest.scope_size,
                    "seconds": round(elapsed, 1),
                }
            )
    finally:
        conn.close()

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(
                {
                    "results": results,
                    "coverage": snapshots.coverage(root=args.root),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        print(f"[report] {args.report}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
