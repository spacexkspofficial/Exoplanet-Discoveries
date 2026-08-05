"""Warm every sector in a multi-sector cohort, one sector at a time.

`--direct` derives product URLs from a sector's published index, so it handles
one sector per invocation. A cohort spanning eleven sectors therefore needs
eleven passes; doing them in ascending order matches how the cohort CSV is
grouped, so a campaign walking the file finds each sector already warm.

Catalogs are warmed once for the whole cohort up front: that path batches by
TIC and does not care about sectors.
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import time as clock
from collections import defaultdict
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PREFETCH = REPOSITORY_ROOT / "scripts" / "prefetch_light_curves.py"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--max-gb", type=float, default=42.0)
    parser.add_argument("--skip-catalogs", action="store_true")
    args = parser.parse_args(argv)

    rows = list(csv.DictReader(args.targets.open(encoding="utf-8-sig")))
    by_sector: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_sector[str(row["sectors"]).strip()].append(row)
    sectors = sorted(by_sector, key=lambda value: int(value))
    print(
        f"{len(rows)} targets across {len(sectors)} sectors: "
        f"{', '.join(sectors)}",
        flush=True,
    )

    started = clock.monotonic()
    if not args.skip_catalogs:
        print("\n=== catalogs (whole cohort) ===", flush=True)
        subprocess.run(
            [sys.executable, str(PREFETCH), "--targets", str(args.targets),
             "--catalogs-only"],
            check=False,
        )

    scratch = args.targets.parent / "_sector_slices"
    scratch.mkdir(parents=True, exist_ok=True)
    for sector in sectors:
        slice_path = scratch / f"{args.targets.stem}_s{sector}.csv"
        with slice_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["target", "tic_id", "sectors"])
            writer.writeheader()
            writer.writerows(by_sector[sector])
        elapsed = (clock.monotonic() - started) / 60
        print(
            f"\n=== sector {sector}: {len(by_sector[sector])} targets "
            f"({elapsed:.0f} min elapsed) ===",
            flush=True,
        )
        subprocess.run(
            [sys.executable, str(PREFETCH), "--targets", str(slice_path),
             "--direct", "--workers", str(args.workers),
             "--max-gb", str(args.max_gb)],
            check=False,
        )

    print(
        f"\nall sectors warmed in {(clock.monotonic()-started)/60:.1f} min",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
