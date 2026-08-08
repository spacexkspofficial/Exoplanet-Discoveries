"""Extend the known-planet recovery cohort to hosts the survey has not searched.

`build_p5_known_planet_recovery.py` can only use hosts that already have a
residual report, because that is where it reads sectors from -- which caps the
cohort at the ~491 known hosts this survey happens to have searched. This
resolves TESS sector coverage from MAST for hosts outside that set.

It deliberately prioritises SHALLOW transits. Recovery is depth-limited
(correction 62: 0.36 below 250 ppm against 0.95 at 5-10k ppm) and the confirmed
planet catalogue skews large, so the existing cohort is thin exactly where the
survey operates. Adding more deep planets would move the headline rate and
teach us nothing.

One MAST query per host. Be polite: keep --workers low and --limit bounded.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from exohunt.targets import (  # noqa: E402
    _available_lightcurve_sectors,
    _compact_sector_subset,
)

SNAP = Path(os.path.expandvars(r"%LOCALAPPDATA%\exohunt\snapshots"))
R_EARTH_OVER_R_SUN = 1.0 / 109.076
SEARCH_MIN_P, SEARCH_MAX_P = 0.5, 20.0
MIN_TRANSITS = 3
SECTOR_DAYS = 27.0


def latest(name: str) -> Path:
    return sorted((SNAP / name).glob("*"))[-1] / "data.csv"


def f(value):
    try:
        x = float(value)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


def load_pool() -> dict[int, list[dict]]:
    planets: dict[int, list[dict]] = {}
    with open(latest("nasa_ps"), encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if str(r.get("tran_flag") or "").strip() != "1":
                continue
            raw = str(r.get("tic_id") or "").replace("TIC", "").strip()
            per = f(r.get("pl_orbper"))
            if not raw or not per or per <= 0:
                continue
            dep = f(r.get("pl_trandep"))
            depth = dep * 10000.0 if dep else None
            src = "catalog_pl_trandep"
            if depth is None:
                rp, rs = f(r.get("pl_rade")), f(r.get("st_rad"))
                if rp and rs and rs > 0:
                    ratio = (rp * R_EARTH_OVER_R_SUN) / rs
                    depth, src = ratio * ratio * 1e6, "derived_from_pl_rade_st_rad"
            if not depth or depth <= 0:
                continue
            planets.setdefault(int(float(raw)), []).append(
                {"planet": r.get("pl_name") or "", "period": per, "depth_ppm": depth,
                 "depth_src": src, "tmag": f(r.get("sy_tmag")),
                 "st_rad": f(r.get("st_rad")), "st_teff": f(r.get("st_teff"))}
            )
    with open(latest("nasa_toi"), encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if str(r.get("tfopwg_disp") or "").strip().upper() not in {"CP", "KP"}:
                continue
            t, per, dep = f(r.get("tid")), f(r.get("pl_orbper")), f(r.get("pl_trandep"))
            if t is None or not per or per <= 0 or not dep or dep <= 0:
                continue
            tic = int(t)
            if any(abs(p["period"] - per) / per < 0.01 for p in planets.get(tic, [])):
                continue
            planets.setdefault(tic, []).append(
                {"planet": f"TOI-{str(r.get('toi') or '').strip()}", "period": per,
                 "depth_ppm": dep, "depth_src": "catalog_toi_pl_trandep",
                 "tmag": f(r.get("st_tmag")), "st_rad": f(r.get("st_rad")),
                 "st_teff": f(r.get("st_teff"))}
            )
    return planets


def already_covered() -> set[int]:
    covered = set()
    for name in ("p5_known_planet_recovery.csv", "p5_known_planet_recovery_excluded.csv"):
        path = ROOT / "targets" / name
        if path.exists():
            with path.open(encoding="utf-8", newline="") as fh:
                covered |= {int(float(r["tic_id"])) for r in csv.DictReader(fh)}
    pat = re.compile(r"TIC_(\d+)_")
    for p in glob.glob(str(ROOT / "results" / "**" / "*_residual.json"), recursive=True):
        m = pat.search(os.path.basename(p))
        if m:
            covered.add(int(m.group(1)))
    return covered


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", default="targets/p5_known_planet_recovery_expansion.csv")
    ap.add_argument("--limit", type=int, default=250, help="hosts to ADD")
    ap.add_argument("--pool-size", type=int, default=600, help="hosts to query")
    ap.add_argument("--min-depth-ppm", type=float, default=250.0)
    ap.add_argument("--max-depth-ppm", type=float, default=2500.0)
    ap.add_argument("--author", default="SPOC")
    ap.add_argument("--cadence-seconds", type=float, default=120.0)
    ap.add_argument("--sectors-per-target", type=int, default=3)
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    planets = load_pool()
    covered = already_covered()
    print(f"pool: {sum(len(v) for v in planets.values())} planets on {len(planets)} hosts")
    print(f"already covered (in a cohort or searched): {len(covered)} TICs")

    # Target the MARGINAL depth band, not the shallowest planets. Sorting
    # shallowest-first returns 13-34 ppm Kepler-class objects an order of
    # magnitude below anything TESS can reach; they would recover at ~0% and
    # measure nothing, the same way scoring a 232 d period against a 20 d
    # search ceiling measures nothing. The informative band is where recovery
    # is genuinely uncertain -- correction 62 puts that between roughly 250 and
    # 2500 ppm. Within the band, order by TIC so the draw is unbiased in depth
    # rather than piling up at one edge.
    candidates = []
    for tic, rows in planets.items():
        if tic in covered:
            continue
        fits = [p for p in rows
                if SEARCH_MIN_P <= p["period"] <= SEARCH_MAX_P
                and args.min_depth_ppm <= p["depth_ppm"] <= args.max_depth_ppm]
        if fits:
            candidates.append((0.0, tic, max(fits, key=lambda p: p["depth_ppm"])))
    candidates.sort(key=lambda c: c[1])
    print(f"uncovered hosts with a planet in "
          f"{args.min_depth_ppm:g}-{args.max_depth_ppm:g} ppm: {len(candidates)}")
    candidates = candidates[: args.pool_size]
    print(f"querying MAST for sectors on {len(candidates)} of them "
          f"({args.workers} concurrent) ...", flush=True)

    def resolve(item):
        depth, tic, planet = item
        try:
            sectors = _available_lightcurve_sectors(tic, args.cadence_seconds, args.author)
        except Exception as exc:
            return tic, None, f"{type(exc).__name__}: {exc}", planet
        return tic, sectors, None, planet

    rows, errors, skipped = [], 0, 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(resolve, c) for c in candidates]
        for n, fut in enumerate(as_completed(futures), start=1):
            tic, sectors, err, planet = fut.result()
            if err:
                errors += 1
                if errors <= 5:
                    print(f"  query failed TIC {tic}: {err}", flush=True)
                continue
            if not sectors:
                skipped += 1
                continue
            chosen = _compact_sector_subset(sectors, args.sectors_per_target)
            baseline = SECTOR_DAYS * len(chosen)
            if planet["period"] * MIN_TRANSITS > baseline:
                skipped += 1
                continue
            rows.append({
                "target": f"TIC {tic}", "tic_id": tic,
                "planet": planet["planet"] or f"TIC {tic} b",
                "expected_period_days": f"{planet['period']:.8g}",
                "expected_depth_ppm": f"{planet['depth_ppm']:.6g}",
                "tmag": "" if planet["tmag"] is None else f"{planet['tmag']:.4g}",
                "teff_k": "" if planet["st_teff"] is None else f"{planet['st_teff']:.5g}",
                "stellar_radius_solar": f"{planet['st_rad']:.6g}" if planet["st_rad"] else "",
                "stellar_mass_solar": "",
                "source_rowupdate": planet["depth_src"],
                "purpose": "known-planet recovery, shallow-end expansion",
                "sector": chosen[0], "sectors": ";".join(str(s) for s in chosen),
                "author": args.author, "cadence_seconds": f"{args.cadence_seconds:g}",
                "observed_baseline_days": f"{baseline:.4g}",
                "expected_transits": f"{baseline / planet['period']:.2f}",
            })
            if len(rows) >= args.limit:
                break
            if n % 50 == 0:
                print(f"  {n}/{len(candidates)} queried, {len(rows)} usable", flush=True)

    print(f"\nresolved usable hosts: {len(rows)}")
    print(f"  no {args.author} coverage or too few transits: {skipped}")
    print(f"  query errors: {errors}")
    if not rows:
        print("nothing to write")
        return 1
    out = ROOT / args.output
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    depths = sorted(float(r["expected_depth_ppm"]) for r in rows)
    print(f"wrote {out} ({len(rows)})")
    print(f"depth ppm: min={depths[0]:.0f} median={depths[len(depths)//2]:.0f} max={depths[-1]:.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
