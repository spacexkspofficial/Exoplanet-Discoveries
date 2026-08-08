"""Build the P5 primary-lane sample: faint M dwarfs with multi-sector coverage.

MASTER_PLAN.md section 6.1, under owner sign-off on lanes 6.1 and 6.2.

The sample definition is taken literally: Teff < 4,000 K, R* < 0.6 R-sun,
Tmag 12.5-15.0, at least three sectors of FFI coverage, contamination ratio
below threshold, and a dwarf by luminosity class.

**Coverage is satisfied geometrically rather than per target.** Asking the
archive how many sectors observe each of a million candidate stars is not
affordable, and it is not necessary: the TESS continuous viewing zones are
observed for a full year, so a cone around an ecliptic pole has far more than
three sectors of coverage by construction. The northern pole is chosen because
section 6.1 states a northern preference -- dec above roughly -28 degrees gives
ZTF overlap, and ground-based eclipsing-binary unmasking at one arcsecond
roughly doubles the vetting power for the brighter half of the lane.

Stars the ledger has already searched are excluded. Stars with a catalogued
ephemeris are **not**: section 6.1 is explicit that a known TOI host may still
yield a second signal, so those are signal-kills at T5, not sample exclusions.

    python scripts/build_p5_primary_lane.py --limit 1000
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from exohunt import ledger, snapshots  # noqa: E402
from exohunt.config import hash_target_list, kernel_version  # noqa: E402
from exohunt.paths import default_db_path  # noqa: E402

TIC_TABLE = '"IV/39/tic82"'
# Northern ecliptic pole. TESS observes a 12-degree-radius cone around each
# ecliptic pole continuously for a year, so every star inside it carries far
# more than the three sectors section 6.1 asks for.
NORTH_ECLIPTIC_POLE = (270.0, 66.56)
CVZ_RADIUS_DEG = 12.0

MAX_TEFF_K = 4000.0
MAX_RADIUS_SOLAR = 0.6
TMAG_RANGE = (12.5, 15.0)
# TIC `Rcont` is the ratio of contaminating flux to target flux in the
# photometric aperture. Above this the aperture is more neighbour than target
# and pixel vetting cannot recover it.
MAX_CONTAMINATION_RATIO = 1.0

# The sectors the first pass searches. Probing the cohort directly, every
# sampled star carries SPOC, TESS-SPOC and QLP light curves across sectors
# 14-26 -- the Cycle 2 northern campaign -- which both confirms the geometric
# coverage assumption with real data and gives the lane three independent
# reductions for T7 to use later.
#
# Three sectors, not twelve: section 6.1's criterion is *at least* three, and
# a first pass at ~1,000 stars is meant to measure the lane's rediscovery rate
# before any decision to scale. Downloading a full year per star to answer that
# question would cost roughly four times the current cache for no extra
# information.
FIRST_PASS_SECTORS = "14;15;16"

# `select *` rather than a column list. VizieR's TAP view of the TIC does not
# resolve every name that appears in the catalogue header -- Gmag among them --
# and a guessed column list fails the whole query rather than degrading. The
# cone is small enough that the extra columns cost nothing, and the fields
# actually used are read defensively below.
COLUMNS = "*"


def _f(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result


def query_candidates(*, timeout: int = 600) -> list[dict[str, str]]:
    ra, dec = NORTH_ECLIPTIC_POLE
    query = (
        f"select {COLUMNS} from {TIC_TABLE} where "
        f"Teff < {MAX_TEFF_K} and Rad < {MAX_RADIUS_SOLAR} "
        f"and Tmag >= {TMAG_RANGE[0]} and Tmag <= {TMAG_RANGE[1]} "
        f"and CONTAINS(POINT('ICRS',RAJ2000,DEJ2000),"
        f"CIRCLE('ICRS',{ra},{dec},{CVZ_RADIUS_DEG}))=1"
    )
    text = snapshots._tap_sync(
        snapshots.SERVICES["vizier_tap"], query, timeout=timeout
    )
    rows, _ = snapshots._parse_csv(text)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--out", type=Path, default=Path("targets"))
    parser.add_argument(
        "--name", default="p5_primary_m_dwarf_ncvz"
    )
    args = parser.parse_args(argv)

    print("querying the TIC for primary-lane candidates ...", flush=True)
    rows = query_candidates()
    print(f"  TIC returned {len(rows)} stars inside the northern CVZ cone")

    conn = ledger.connect_readonly(default_db_path())
    already = {int(r[0]) for r in conn.execute("SELECT tic_id FROM star")}
    conn.close()
    print(f"  ledger already holds {len(already)} stars")

    kept: list[dict[str, Any]] = []
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    for row in rows:
        tic = _f(row.get("TIC"))
        if tic is None:
            reject("no_tic")
            continue
        tic_id = int(tic)
        if tic_id in already:
            reject("already_searched")
            continue
        contamination = _f(row.get("Rcont"))
        if contamination is not None and contamination > MAX_CONTAMINATION_RATIO:
            reject("contamination_above_threshold")
            continue
        # Luminosity class, where the TIC assigns one. "DWARF" is the class we
        # want; a giant at this colour is a different star entirely and its
        # transit depths would mean something else.
        lclass = str(row.get("LClass") or "").strip().upper()
        if lclass and lclass not in {"DWARF", "D", ""}:
            reject(f"luminosity_class_{lclass.lower()}")
            continue
        if _f(row.get("Plx")) is None:
            reject("no_parallax")
            continue
        kept.append(
            {
                "target": f"TIC {tic_id}",
                "tic_id": tic_id,
                "sectors": FIRST_PASS_SECTORS,
                "ra": row.get("RAJ2000"),
                "dec": row.get("DEJ2000"),
                "tmag": row.get("Tmag"),
                "teff_k": row.get("Teff"),
                # These two names are load-bearing, not cosmetic. The campaign
                # lifts stellar parameters off the target-list row by exact key
                # (campaign.py `_batch_target_spec`), so a cohort that spells
                # radius anything other than `stellar_radius_solar` silently
                # leaves T3's depth_physicality and duration_density
                # `not_evaluable` for every star in it -- which is what happened
                # to the first 1,000-star pass of this lane (correction 57).
                # duration_density additionally needs the mass: density comes
                # from `catalog_stellar_mass_and_radius` or not at all.
                "stellar_radius_solar": row.get("Rad"),
                "stellar_mass_solar": row.get("Mass"),
                "distance_pc": row.get("Dist"),
                "contamination_ratio": row.get("Rcont"),
                "gaia_source_id": row.get("GAIA"),
            }
        )

    # Deterministic and physically meaningful: brightest first inside the
    # magnitude window, so the first cohort is the half where ZTF and ground
    # follow-up are most useful and the completeness is best understood.
    kept.sort(key=lambda item: (float(item["tmag"] or 99), item["tic_id"]))
    cohort = kept[: args.limit]

    args.out.mkdir(parents=True, exist_ok=True)
    target_path = args.out / f"{args.name}.csv"
    with open(target_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(cohort[0].keys()) if cohort else ["target"]
        )
        writer.writeheader()
        writer.writerows(cohort)

    manifest = {
        "lane": "6.1_primary_faint_m_dwarf",
        "kernel_version": kernel_version(),
        "target_list": str(target_path),
        "target_list_sha256": hash_target_list(target_path) if cohort else None,
        "selection": {
            "max_teff_k": MAX_TEFF_K,
            "max_radius_solar": MAX_RADIUS_SOLAR,
            "tmag_range": list(TMAG_RANGE),
            "max_contamination_ratio": MAX_CONTAMINATION_RATIO,
            "coverage": (
                "northern continuous viewing zone, "
                f"{CVZ_RADIUS_DEG} deg around RA {NORTH_ECLIPTIC_POLE[0]}, "
                f"Dec {NORTH_ECLIPTIC_POLE[1]} -- >=3 sectors by construction"
            ),
            "northern_preference": "ZTF overlap for ground EB unmasking (6.1)",
            "known_ephemerides": (
                "not excluded: a catalogued host may still yield a second "
                "signal, so these are signal-kills at T5 rather than sample "
                "exclusions"
            ),
        },
        "counts": {
            "tic_returned": len(rows),
            "eligible": len(kept),
            "cohort": len(cohort),
            "rejected": dict(sorted(rejected.items(), key=lambda kv: -kv[1])),
        },
    }
    manifest_path = args.out / f"{args.name}_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    print()
    print(f"  eligible: {len(kept)}   cohort: {len(cohort)}")
    for reason, count in manifest["counts"]["rejected"].items():
        print(f"    rejected {count:>6}  {reason}")
    if cohort:
        mags = [float(item["tmag"]) for item in cohort]
        print(f"  Tmag range: {min(mags):.2f} - {max(mags):.2f}")
    print(f"[written] {target_path}")
    print(f"[written] {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
