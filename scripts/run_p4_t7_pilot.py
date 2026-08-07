"""Run the T7 cross-reduction gate against real independent reductions.

MASTER_PLAN.md section 4.5, measured. For each pilot star this fetches every
light-curve reduction the archive holds for its *discovery* sector -- SPOC
PDCSAP, SPOC SAP, TESS-SPOC, QLP -- measures the depth of the fixed ephemeris
in each, and puts the result through `crossreduction.evaluate`.

The headline measurement is the undetrended one. SPOC publishes SAP alongside
PDCSAP in the same file, so the same pixels give both a detrended and an
undetrended fold. If a survivor's dip is absent from SAP, no amount of
agreement among detrended products means anything: every detrended reduction
inherits whatever the detrender invented, which is the direct lesson this
project learned the hard way.

Promotion is expected to be blocked for nearly everything, and that is not a
disappointment: these are single-sector signals with no injected completeness
and no stacked-fold vetoes, so the gate has no basis to promote. What the run
produces is the depth-agreement and undetrended evidence, per star.

    python scripts/run_p4_t7_pilot.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402

from exohunt import crossreduction as t7  # noqa: E402
from exohunt import pixel  # noqa: E402
from exohunt.config import CURRENT_CROSS_REDUCTION, module_digest  # noqa: E402

# Authors worth asking for, in the order the plan's product matrix prefers.
AUTHORS = ("SPOC", "TESS-SPOC", "QLP")
# This session has already pulled three 60-target pixel runs plus the
# catalog snapshots through MAST, and the archive began closing
# connections. Back off between retries and pause between targets: the
# archive is shared infrastructure, and a throttled session produces
# wrong science rather than merely slow science.
POLITE_RETRY_SECONDS = 5.0
POLITE_TARGET_SECONDS = 1.5


def measure_depth(
    time: np.ndarray,
    flux: np.ndarray,
    *,
    period_days: float,
    transit_time: float,
    duration_hours: float,
) -> tuple[float, float, int] | None:
    """Depth of a fixed ephemeris in one reduction, in ppm, with its error."""

    finite = np.isfinite(time) & np.isfinite(flux)
    time, flux = np.asarray(time)[finite], np.asarray(flux)[finite]
    if time.size < 50:
        return None
    in_mask, out_mask = pixel.transit_cadence_masks(
        time, period_days, transit_time, duration_hours
    )
    n_in = int(np.count_nonzero(in_mask))
    n_out = int(np.count_nonzero(out_mask))
    if n_in < 3 or n_out < 20:
        return None
    baseline = float(np.nanmedian(flux[out_mask]))
    if not np.isfinite(baseline) or baseline == 0:
        return None
    depth = 1.0 - float(np.nanmedian(flux[in_mask])) / baseline
    # Error on the in-transit median, from the out-of-transit scatter. The
    # 1.253 factor converts a standard error of the mean into one for a
    # median on normally distributed residuals.
    scatter = float(np.nanstd(flux[out_mask])) / baseline
    error = 1.253 * scatter / np.sqrt(n_in) if n_in else float("nan")
    if not np.isfinite(error) or error <= 0:
        return None
    return depth * 1e6, error * 1e6, n_in


measurements_failures: list[str] = []


def reductions_for(entry: dict[str, Any]) -> list[t7.ReductionDepth]:
    """Every reduction the archive has for this star in its discovery sector."""

    import lightkurve as lk

    tic_id = entry["tic_id"]
    sector = (entry.get("discovery_sectors") or [None])[0]
    ephemeris = entry["ephemeris"]
    period = float(ephemeris["period_days"])
    epoch = float(ephemeris["epoch_btjd"] or 0.0)
    duration = float(ephemeris["duration_hours"])

    measurements: list[t7.ReductionDepth] = []
    failures: list[str] = []
    for author in AUTHORS:
        curve = None
        # "No such product" and "the archive dropped the connection" are
        # completely different answers, and collapsing them into one silent
        # `continue` reported a throttled session as a cohort with no
        # alternate reductions. Connection faults are retried with backoff and
        # recorded; a genuine absence is recorded as an absence.
        for attempt in range(1, 4):
            try:
                search = lk.search_lightcurve(
                    f"TIC {tic_id}", mission="TESS", author=author, sector=sector
                )
                if len(search) == 0:
                    break
                curve = search[0].download()
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 3:
                    failures.append(f"{author}: {type(exc).__name__}")
                else:
                    time.sleep(POLITE_RETRY_SECONDS * attempt)
        if curve is None:
            continue

        columns = {name.lower() for name in curve.colnames}
        # SPOC publishes SAP and PDCSAP from the same pixels, which is exactly
        # the detrended/undetrended pair section 4.5 asks for.
        #
        # Only *SPOC's* sap_flux is undetrended. QLP ships its own
        # systematics-corrected photometry in a column of the same name, and
        # treating that as an undetrended fold both overstates the undetrended
        # evidence and starves the independent-reduction count -- it left
        # SPOC PDCSAP as the sole detrended product, so no star ever reached
        # the two independent reductions section 4.5 requires.
        for column, spoc_detrended in (("pdcsap_flux", True), ("sap_flux", False)):
            if column not in columns:
                continue
            detrended = spoc_detrended if author == "SPOC" else True
            try:
                flux = np.asarray(curve[column].value, dtype=float)
            except Exception:  # noqa: BLE001
                continue
            measured = measure_depth(
                np.asarray(curve.time.value, dtype=float),
                flux,
                period_days=period,
                transit_time=epoch,
                duration_hours=duration,
            )
            if measured is None:
                continue
            depth_ppm, error_ppm, _ = measured
            measurements.append(
                t7.ReductionDepth(
                    product=f"{author}_{column}",
                    depth_ppm=depth_ppm,
                    depth_error_ppm=error_ppm,
                    detrended=detrended,
                )
            )
        if not columns & {"pdcsap_flux", "sap_flux"} and "flux" in columns:
            measured = measure_depth(
                np.asarray(curve.time.value, dtype=float),
                np.asarray(curve["flux"].value, dtype=float),
                period_days=period,
                transit_time=epoch,
                duration_hours=duration,
            )
            if measured is not None:
                depth_ppm, error_ppm, _ = measured
                measurements.append(
                    t7.ReductionDepth(
                        product=f"{author}_flux",
                        depth_ppm=depth_ppm,
                        depth_error_ppm=error_ppm,
                        detrended=True,
                    )
                )
    if failures:
        measurements_failures.extend(failures)
    return measurements


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cohort",
        type=Path,
        default=Path("results/p4/pixel_pilot_v3/cohort.json"),
    )
    parser.add_argument("--out", type=Path, default=Path("results/p4/t7_pilot_v1"))
    parser.add_argument("--limit", type=int, default=60)
    args = parser.parse_args(argv)

    cohort = json.loads(args.cohort.read_text(encoding="utf-8"))[: args.limit]
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"cohort: {len(cohort)} stars")

    records: list[dict[str, Any]] = []
    for index, entry in enumerate(cohort, start=1):
        began = time.monotonic()
        measurements_failures.clear()
        time.sleep(POLITE_TARGET_SECONDS)
        try:
            measurements = reductions_for(entry)
        except Exception as exc:  # noqa: BLE001
            records.append(
                {
                    "tic_id": entry["tic_id"],
                    "state": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(limit=2),
                }
            )
            print(f"[{index:>3}/{len(cohort)}] TIC {entry['tic_id']:<12} error")
            continue

        sector = (entry.get("discovery_sectors") or [0])[0]
        # No injected completeness exists for these stars, so a sector that
        # did not see the signal must abstain rather than object -- and the
        # discovery sector did see it.
        sectors = [t7.SectorSupport(int(sector), detected=True, completeness=None)]
        decision = t7.evaluate(
            measurements=measurements,
            sectors=sectors,
            vetoes=None,  # single sector: no stacked fold to re-measure
        )
        detrended = sorted({m.product for m in measurements if m.detrended})
        undetrended = [m for m in measurements if not m.detrended]
        record = {
            "tic_id": entry["tic_id"],
            "state": "measured",
            "sector": sector,
            "products": [m.product for m in measurements],
            "search_failures": list(measurements_failures),
            "independent_detrended": len(detrended),
            "has_undetrended": bool(undetrended),
            "undetrended_significance": (
                max((m.significance() or 0.0) for m in undetrended)
                if undetrended
                else None
            ),
            "depths_ppm": {m.product: round(m.depth_ppm, 1) for m in measurements},
            "decision": decision.to_dict(),
            "seconds": round(time.monotonic() - began, 1),
        }
        records.append(record)
        agree = decision.checks.get("depth_agreement", {}).get("agrees")
        print(
            f"[{index:>3}/{len(cohort)}] TIC {entry['tic_id']:<12} "
            f"{len(measurements)} products, {len(detrended)} independent, "
            f"agree={agree}, undetrended={record['has_undetrended']} "
            f"({record['seconds']}s)",
            flush=True,
        )
        (args.out / "t7_pilot_records.json").write_text(
            json.dumps(records, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )

    measured = [r for r in records if r.get("state") == "measured"]
    enough = [r for r in measured if r["independent_detrended"] >= 2]
    agreeing = [
        r
        for r in enough
        if r["decision"]["checks"].get("depth_agreement", {}).get("agrees")
    ]
    with_raw = [r for r in measured if r["has_undetrended"]]
    raw_present = [
        r
        for r in with_raw
        if r["decision"]["checks"].get("undetrended", {}).get("present")
    ]
    promoted = [r for r in measured if r["decision"]["promoted"]]
    summary = {
        "cohort_size": len(cohort),
        "measured": len(measured),
        "errors": len(records) - len(measured),
        "with_two_independent_reductions": len(enough),
        "depths_agree": len(agreeing),
        "with_undetrended_sap": len(with_raw),
        "present_in_undetrended_sap": len(raw_present),
        "promoted": len(promoted),
        "policy": CURRENT_CROSS_REDUCTION.policy_version,
        "code": "modules:" + module_digest("crossreduction.py"),
    }
    (args.out / "t7_pilot_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    print()
    for key, value in summary.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
