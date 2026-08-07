"""Give TIC 234994474 a real verdict -- P4's named exit item.

MASTER_PLAN.md section 9 lists this star explicitly: "TIC 234994474 (the
promised multi-sector QLP run, currently mislabeled-risk)", with the exit
criterion "TIC 234994474 carries a real verdict".

It currently carries `science_vetted_lead`, assigned by the pre-P4 two-gate
rule: difference-image centroid on target, and 2 of 3 sectors supporting the
ephemeris. That is a weaker test than section 4.5's four requirements, and the
existing "multi-sector QLP" artifact does not test this ephemeris at all --
`results/independent/TIC_234994474_qlp/` requested sectors 1, 28, 68, 95 and
102 but downloaded only sector 1, then ran a blind search that reported a
different signal entirely (4.78 d, 49,475 ppm, zero observed transits) under a
filename claiming five sectors.

This measures the *campaign's* ephemeris in every reduction the archive holds
for the sectors it was actually found in, and puts the result through the T7
gate.

    python scripts/run_p4_tic234994474.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402

from exohunt import crossreduction as t7  # noqa: E402
from exohunt.config import CURRENT_CROSS_REDUCTION, module_digest  # noqa: E402

TIC_ID = 234994474
# The campaign's ephemeris, from
# results/campaign/cool_single_hosts_10_v2/TIC_234994474_s95-102-104_residual.json
PERIOD_DAYS = 13.008806962313464
EPOCH_BTJD = 3893.8721015175224
DURATION_HOURS = 6.0
DISCOVERY_SECTORS = (95, 102, 104)
AUTHORS = ("SPOC", "TESS-SPOC", "QLP")
RETRY_SECONDS = 6.0


def measure(time_values, flux, *, period, epoch, duration):
    from exohunt import pixel

    finite = np.isfinite(time_values) & np.isfinite(flux)
    t, f = np.asarray(time_values)[finite], np.asarray(flux)[finite]
    if t.size < 50:
        return None
    in_mask, out_mask = pixel.transit_cadence_masks(t, period, epoch, duration)
    n_in, n_out = int(np.count_nonzero(in_mask)), int(np.count_nonzero(out_mask))
    if n_in < 3 or n_out < 20:
        return None
    baseline = float(np.nanmedian(f[out_mask]))
    if not np.isfinite(baseline) or baseline == 0:
        return None
    depth = 1.0 - float(np.nanmedian(f[in_mask])) / baseline
    scatter = float(np.nanstd(f[out_mask])) / baseline
    error = 1.253 * scatter / np.sqrt(n_in)
    if not np.isfinite(error) or error <= 0:
        return None
    return depth * 1e6, error * 1e6, n_in, n_out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("results/p4/tic234994474"))
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    import lightkurve as lk

    measurements: list[t7.ReductionDepth] = []
    per_sector: dict[int, list[dict[str, Any]]] = {}
    attempts: list[dict[str, Any]] = []

    for sector in DISCOVERY_SECTORS:
        per_sector[sector] = []
        for author in AUTHORS:
            curve = None
            note = "no_products"
            for attempt in range(1, 4):
                try:
                    search = lk.search_lightcurve(
                        f"TIC {TIC_ID}", mission="TESS", author=author, sector=sector
                    )
                    if len(search) == 0:
                        break
                    curve = search[0].download()
                    note = "downloaded"
                    break
                except Exception as exc:  # noqa: BLE001
                    note = f"{type(exc).__name__}"
                    if attempt < 3:
                        time.sleep(RETRY_SECONDS * attempt)
            attempts.append({"sector": sector, "author": author, "result": note})
            print(f"  s{sector} {author:<10} {note}", flush=True)
            if curve is None:
                continue

            columns = {name.lower() for name in curve.colnames}
            pairs = [(c, d) for c, d in (("pdcsap_flux", True), ("sap_flux", False)) if c in columns]
            if not pairs and "flux" in columns:
                pairs = [("flux", True)]
            for column, detrended in pairs:
                result = measure(
                    np.asarray(curve.time.value, dtype=float),
                    np.asarray(curve[column].value, dtype=float),
                    period=PERIOD_DAYS,
                    epoch=EPOCH_BTJD,
                    duration=DURATION_HOURS,
                )
                if result is None:
                    continue
                depth_ppm, error_ppm, n_in, n_out = result
                product = f"{author}_{column}"
                measurements.append(
                    t7.ReductionDepth(
                        product=f"{product}_s{sector}",
                        depth_ppm=depth_ppm,
                        depth_error_ppm=error_ppm,
                        detrended=detrended,
                    )
                )
                per_sector[sector].append(
                    {
                        "product": product,
                        "depth_ppm": round(depth_ppm, 1),
                        "error_ppm": round(error_ppm, 1),
                        "significance": round(depth_ppm / error_ppm, 2),
                        "in_transit_cadences": n_in,
                        "detrended": detrended,
                    }
                )
                print(
                    f"      {product:<24} {depth_ppm:9.1f} +- {error_ppm:6.1f} ppm "
                    f"({depth_ppm / error_ppm:5.2f} sigma, {n_in} in-transit)",
                    flush=True,
                )

    # A sector supports the signal when at least one reduction there sees it at
    # 3 sigma. No injected completeness exists for this star, so a sector that
    # does not see it must abstain rather than object.
    sectors = []
    for sector, rows in per_sector.items():
        detected = any(row["significance"] >= 3.0 for row in rows)
        sectors.append(
            t7.SectorSupport(sector, detected=detected, completeness=None)
        )

    decision = t7.evaluate(
        measurements=measurements,
        sectors=sectors,
        vetoes=None,  # stacked secondary / odd-even not re-measured here
    )

    report = {
        "tic_id": TIC_ID,
        "ephemeris": {
            "period_days": PERIOD_DAYS,
            "epoch_btjd": EPOCH_BTJD,
            "duration_hours": DURATION_HOURS,
            "campaign_depth_ppm": 220.60696598964674,
        },
        "discovery_sectors": list(DISCOVERY_SECTORS),
        "prior_status": "science_vetted_lead",
        "prior_basis": (
            "pre-P4 two-gate rule: centroid on target within 18 arcsec, and "
            "2 of 3 tested sectors supporting the fixed ephemeris"
        ),
        "prior_multi_sector_qlp_artifact": {
            "path": "results/independent/TIC_234994474_qlp/"
            "TIC_234994474_s1-28-68-95-102.json",
            "requested_sectors": [1, 28, 68, 95, 102],
            "downloaded_sectors": [1],
            "reported_signal_period_days": 4.782668753882266,
            "reported_depth_ppm": 49474.82393029552,
            "reported_observed_transits": 0,
            "assessment": (
                "not a multi-sector run and not a test of this ephemeris: one "
                "sector was downloaded, and the blind search reported an "
                "unrelated signal with zero observed transits under a filename "
                "naming five sectors"
            ),
        },
        "fetch_attempts": attempts,
        "per_sector_measurements": {str(k): v for k, v in per_sector.items()},
        "t7_decision": decision.to_dict(),
        "policy": CURRENT_CROSS_REDUCTION.policy_version,
        "code": "modules:" + module_digest("crossreduction.py"),
    }
    (args.out / "verdict.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    print()
    print(f"  products measured: {len(measurements)}")
    print(f"  promoted: {decision.promoted}  status: {decision.status}")
    for reason in decision.blocking:
        print(f"    blocked: {reason}")
    print(f"[written] {args.out / 'verdict.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
