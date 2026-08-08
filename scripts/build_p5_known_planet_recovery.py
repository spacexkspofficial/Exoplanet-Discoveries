"""Build a known-planet recovery cohort from hosts the survey has ALREADY
searched, so sectors are known and the photometry is already cached.

Only planets that are IN PRINCIPLE recoverable are put in the scored cohort:
the period must lie inside the search range and at least `min_transits` events
must fit the star's real observed baseline. Everything else is written to a
companion file so the exclusion is auditable rather than silent -- a recovery
rate computed over planets the search cannot reach would measure nothing.

Schema matches targets/p3_known_planets_20.csv (run_p3_known_planets.py input).
"""

import csv, glob, json, os, re, sys
from collections import Counter

ROOT = r"E:\Agentic AI\Exoplanet Server\Exoplanet-Discoveries"
snap = os.path.expandvars(r"%LOCALAPPDATA%\exohunt\snapshots")

SEARCH_MIN_P, SEARCH_MAX_P = 0.5, 20.0
MIN_TRANSITS = 3
R_EARTH_OVER_R_SUN = 1.0 / 109.076


def latest(name):
    return os.path.join(sorted(glob.glob(os.path.join(snap, name, "*")))[-1], "data.csv")


def f(v):
    try:
        x = float(v)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


planets = {}
with open(latest("nasa_ps"), encoding="utf-8", newline="") as fh:
    for r in csv.DictReader(fh):
        if str(r.get("tran_flag") or "").strip() != "1":
            continue
        t = str(r.get("tic_id") or "").replace("TIC", "").strip()
        if not t:
            continue
        try:
            tic = int(float(t))
        except ValueError:
            continue
        per = f(r.get("pl_orbper"))
        if not per or per <= 0:
            continue
        dep = f(r.get("pl_trandep"))            # percent
        depth_ppm = dep * 10000.0 if dep else None
        depth_src = "catalog_pl_trandep"
        if depth_ppm is None:                    # derive from radii
            rp, rs = f(r.get("pl_rade")), f(r.get("st_rad"))
            if rp and rs and rs > 0:
                ratio = (rp * R_EARTH_OVER_R_SUN) / rs
                depth_ppm = ratio * ratio * 1e6
                depth_src = "derived_from_pl_rade_st_rad"
        if not depth_ppm or depth_ppm <= 0:
            continue
        planets.setdefault(tic, []).append(
            {"planet": r.get("pl_name") or f"TIC {tic} b", "period": per,
             "depth_ppm": depth_ppm, "depth_src": depth_src,
             "tmag": f(r.get("sy_tmag"))}
        )
print(f"confirmed transiting planets with a usable depth: "
      f"{sum(len(v) for v in planets.values())} on {len(planets)} hosts")

# Extend with validated TOIs. Dispositions CP (confirmed planet) and KP (known
# planet) carry vetted ephemerides; PC is an unvetted candidate and FP/FA are
# rejections, so neither belongs in a recovery denominator. This matters for
# the shallow end: the confirmed-planet table skews large, and these rows are
# where sub-1000 ppm statistics come from. TOI pl_trandep is already ppm --
# nasa_ps pl_trandep is a percentage, and mixing the two would inflate depths
# 10,000-fold.
VALID_TOI = {"CP", "KP"}
toi_added = 0
with open(latest("nasa_toi"), encoding="utf-8", newline="") as fh:
    for r in csv.DictReader(fh):
        if str(r.get("tfopwg_disp") or "").strip().upper() not in VALID_TOI:
            continue
        t = f(r.get("tid"))
        per = f(r.get("pl_orbper"))
        dep = f(r.get("pl_trandep"))
        if t is None or not per or per <= 0 or not dep or dep <= 0:
            continue
        tic = int(t)
        # Do not double-count a planet the confirmed table already supplies.
        if any(abs(p["period"] - per) / per < 0.01 for p in planets.get(tic, [])):
            continue
        planets.setdefault(tic, []).append(
            {
                "planet": f"TOI-{str(r.get('toi') or '').strip()}",
                "period": per,
                "depth_ppm": dep,
                "depth_src": "catalog_toi_pl_trandep",
                "tmag": f(r.get("st_tmag")),
                "st_rad": f(r.get("st_rad")),
                "st_teff": f(r.get("st_teff")),
            }
        )
        toi_added += 1
print(f"validated TOIs (CP/KP) added: {toi_added}")
print(f"total pool: {sum(len(v) for v in planets.values())} planets "
      f"on {len(planets)} hosts")

pat = re.compile(r"TIC_(\d+)_")
best = {}
for path in glob.glob(os.path.join(ROOT, "results", "**", "*_residual.json"), recursive=True):
    m = pat.search(os.path.basename(path))
    if not m:
        continue
    tic = int(m.group(1))
    if tic not in planets:
        continue
    prev = best.get(tic)
    if prev is None or os.path.getmtime(path) > os.path.getmtime(prev):
        best[tic] = path
print(f"already-searched known hosts with a report: {len(best)}")

scored, excluded = [], []
authors, why = Counter(), Counter()
for tic, path in sorted(best.items()):
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        why["unreadable_report"] += 1
        continue
    data = d.get("data") or {}
    sectors = data.get("downloaded_sectors") or data.get("requested_sectors") or []
    author = data.get("author")
    cad = data.get("resolved_cadence_seconds") or data.get("requested_cadence_seconds") or 120.0
    win = d.get("observation_window") or {}
    start, end = f(win.get("start_btjd")), f(win.get("end_btjd"))
    if not sectors or not author:
        why["no_sectors_or_author"] += 1
        continue
    baseline = (end - start) if (start is not None and end is not None) else 27.0 * len(sectors)

    # Prefer the planet most likely to be recoverable: deepest among those that fit.
    fits = [p for p in planets[tic]
            if SEARCH_MIN_P <= p["period"] <= SEARCH_MAX_P
            and p["period"] * MIN_TRANSITS <= baseline]
    row_for = lambda p: {
        "target": f"TIC {tic}", "tic_id": tic, "planet": p["planet"],
        "expected_period_days": f"{p['period']:.8g}",
        "expected_depth_ppm": f"{p['depth_ppm']:.6g}",
        "tmag": "" if p["tmag"] is None else f"{p['tmag']:.4g}",
        "source_rowupdate": p["depth_src"],
        "purpose": "survey-wide known-planet recovery rate",
        "sector": sectors[0], "sectors": ";".join(str(s) for s in sectors),
        "author": author, "cadence_seconds": f"{float(cad):g}",
        "observed_baseline_days": f"{baseline:.4g}",
        "expected_transits": f"{baseline / p['period']:.2f}",
    }
    if fits:
        p = max(fits, key=lambda x: x["depth_ppm"])
        authors[author] += 1
        scored.append(row_for(p))
    else:
        p = min(planets[tic], key=lambda x: x["period"])
        r = row_for(p)
        r["purpose"] = (
            "excluded: period outside search range"
            if not (SEARCH_MIN_P <= p["period"] <= SEARCH_MAX_P)
            else f"excluded: fewer than {MIN_TRANSITS} transits fit the baseline"
        )
        why[r["purpose"]] += 1
        excluded.append(r)

print(f"\nSCORED cohort   : {len(scored)}")
print(f"EXCLUDED         : {len(excluded)}")
for k, v in why.most_common():
    print(f"   {v:4d}  {k}")
print(f"authors: {dict(authors)}")
print(f"depth provenance: {Counter(r['source_rowupdate'] for r in scored)}")

for name, rows in (("p5_known_planet_recovery.csv", scored),
                   ("p5_known_planet_recovery_excluded.csv", excluded)):
    if not rows:
        continue
    out = os.path.join(ROOT, "targets", name)
    with open(out, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {out} ({len(rows)})")

q = lambda v, p: sorted(v)[min(len(v) - 1, int(p * len(v)))]
per = [float(r["expected_period_days"]) for r in scored]
dep = [float(r["expected_depth_ppm"]) for r in scored]
tr = [float(r["expected_transits"]) for r in scored]
print(f"\nscored period d  : min={min(per):.3g} p50={q(per,.5):.3g} max={max(per):.4g}")
print(f"scored depth ppm : min={min(dep):.4g} p50={q(dep,.5):.5g} max={max(dep):.5g}")
print(f"expected transits: min={min(tr):.1f} p50={q(tr,.5):.1f} max={max(tr):.0f}")
