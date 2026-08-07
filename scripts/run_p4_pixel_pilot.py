"""Run pixel vetting v2 against real pixels, on a bounded pilot cohort.

MASTER_PLAN.md section 4.4, measured rather than asserted. Everything in
`pixel.py` v2 was built and tested against synthetic scenes where the dimming
source was placed by construction; this is the first time it meets data whose
answer nobody knows.

The cohort is chosen for where pixel vetting is *decisive*, not at random: a
backlog star that survived the calibrated T3 re-gate, carries an ephemeris,
and has more than one plausible Gaia counterpart inside its own TESS pixel.
Those are exactly the stars where the current stack cannot say which object is
dimming, and where a neighbour extraction can settle it.

Downloads are bounded and per-target. Run only with explicit owner
authorization for MAST traffic.

    python scripts/run_p4_pixel_pilot.py --limit 60
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402

from exohunt import identity, ledger, pixel, snapshots  # noqa: E402
from exohunt.config import (  # noqa: E402
    CURRENT_IDENTITY,
    CURRENT_PIXEL_VET,
    match_radius_arcsec,
    module_digest,
)
from exohunt.paths import default_db_path  # noqa: E402

CUTOUT_PIXELS = 11


def _f(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result


def select_cohort(conn, limit: int) -> list[dict[str, Any]]:
    """Backlog stars where pixel vetting can actually change the answer."""

    records_path = Path("results/p4/readjudication_v1/readjudication_records.json")
    if not records_path.is_file():
        raise SystemExit("Run scripts/run_p4_readjudication.py first.")
    records = json.loads(records_path.read_text(encoding="utf-8"))

    ambiguous = {
        int(row["tic_id"])
        for row in conn.execute(
            "SELECT tic_id FROM identity_node WHERE resolution = ?",
            (identity.RESOLUTION_AMBIGUOUS,),
        )
    }
    positions = {
        int(row["tic_id"]): (float(row["ra_deg"]), float(row["dec_deg"]))
        for row in conn.execute(
            "SELECT tic_id, ra_deg, dec_deg FROM star "
            "WHERE ra_deg IS NOT NULL AND dec_deg IS NOT NULL"
        )
    }

    cohort: list[dict[str, Any]] = []
    for record in records:
        tic = int(record["tic_id"])
        if record["t3_regate"]["verdict"] == "fails_calibrated_red_noise_floor":
            continue
        ephemeris = record.get("ephemeris") or {}
        if not (ephemeris.get("period_days") and ephemeris.get("duration_hours")):
            continue
        if tic not in ambiguous or tic not in positions:
            continue
        # The sector the signal was actually found in. Without it the pilot
        # downloads whichever cutout the archive lists first -- sectors 2-12
        # for targets whose signals live in 98-105 -- and every photometric
        # verdict is measured on data that never contained the transit.
        sectors: list[int] = []
        for row in conn.execute(
            "SELECT payload FROM evidence WHERE tic_id = ? AND kind = 'screening' "
            "ORDER BY evidence_id DESC",
            (tic,),
        ):
            result = (json.loads(row["payload"]).get("result") or {})
            if result.get("sectors"):
                sectors = [
                    int(value)
                    for value in re.findall(r"\d+", str(result["sectors"]))
                ]
                break
        if not sectors:
            continue
        cohort.append(
            {
                "tic_id": tic,
                "prior_status": record["prior_status"],
                "ephemeris": ephemeris,
                "discovery_sectors": sectors,
                "ra_deg": positions[tic][0],
                "dec_deg": positions[tic][1],
                "t3": record["t3_regate"]["verdict"],
            }
        )
    # Deterministic: brightest-first would bias toward easy pixels, so order
    # by TIC and take a prefix. The cohort identity is recorded either way.
    cohort.sort(key=lambda item: item["tic_id"])
    return cohort[:limit]


def gaia_neighbours(conn, tic_id: int) -> list[dict[str, Any]]:
    """Ranked Gaia counterparts for a star, with sky positions."""

    manifest = snapshots.latest("gaia_dr3")
    if manifest is None:
        return []
    edges = identity.edges_for(conn, tic_id, identifier_type="gaia_dr3")
    if not edges:
        return []
    wanted = {str(edge["identifier"]) for edge in edges}
    rows = snapshots.load_rows(manifest)
    ra_key, dec_key = snapshots._choose_position_columns(manifest.columns)
    found: list[dict[str, Any]] = []
    for row in rows:
        source = str(row.get("Source", "")).strip()
        if source not in wanted:
            continue
        found.append(
            {
                "identifier": source,
                "ra_deg": _f(row.get(ra_key)),
                "dec_deg": _f(row.get(dec_key)),
                "g_mag": _f(row.get("Gmag")),
            }
        )
    return found


def _sky_to_pixel(tpf, ra_deg: float, dec_deg: float) -> tuple[float, float] | None:
    middle = len(tpf.time) // 2
    try:
        ra_grid, dec_grid = tpf.get_coordinates(cadence=middle)
        return pixel.target_pixel_from_sky_grid(
            np.asarray(ra_grid, dtype=float),
            np.asarray(dec_grid, dtype=float),
            ra_deg,
            dec_deg,
        )
    except Exception:  # noqa: BLE001
        return None


def vet_one(conn, entry: dict[str, Any], *, author: str) -> dict[str, Any]:
    """Download one target's pixels and run every v2 check on them."""

    import lightkurve as lk

    tic_id = entry["tic_id"]
    target = f"TIC {tic_id}"
    ephemeris = entry["ephemeris"]
    period = float(ephemeris["period_days"])
    epoch = float(ephemeris["epoch_btjd"] or 0.0)
    duration = float(ephemeris["duration_hours"])

    wanted = entry.get("discovery_sectors") or []
    if not wanted:
        return {"tic_id": tic_id, "state": "no_discovery_sector", "author": author}
    # Only the discovery sector can test this ephemeris. A cutout from another
    # sector may not contain the transit at all, and its verdict would be
    # about the wrong data rather than about the signal.
    search = lk.search_tesscut(target, sector=wanted[0])
    if len(search) == 0:
        return {
            "tic_id": tic_id,
            "state": "no_pixel_data_in_discovery_sector",
            "author": author,
            "discovery_sectors": wanted,
        }
    tpf = search[0].download(cutout_size=CUTOUT_PIXELS, quality_bitmask="default")
    if tpf is None:
        return {"tic_id": tic_id, "state": "download_failed", "author": author}
    observed = int(getattr(tpf, "sector", 0) or 0)
    if observed and observed not in wanted:
        return {
            "tic_id": tic_id,
            "state": "sector_mismatch",
            "author": author,
            "requested_sector": wanted[0],
            "downloaded_sector": observed,
        }

    times = np.asarray(tpf.time.value, dtype=float)
    cube = np.asarray(tpf.flux.value, dtype=float)
    target_pixel = _sky_to_pixel(tpf, entry["ra_deg"], entry["dec_deg"])
    if target_pixel is None:
        return {"tic_id": tic_id, "state": "no_wcs", "author": author}
    target_row, target_column = target_pixel

    result: dict[str, Any] = {
        "tic_id": tic_id,
        "state": "measured",
        "author": author,
        "sector": observed,
        "discovery_sectors": wanted,
        "cadences": int(times.size),
        "target_pixel": [target_row, target_column],
        "ephemeris": ephemeris,
        "prior_status": entry["prior_status"],
    }

    try:
        result["aperture_growth"] = pixel.aperture_depth_curve(
            times,
            cube,
            period_days=period,
            transit_time=epoch,
            duration_hours=duration,
            center_row=target_row,
            center_column=target_column,
        )
    except ValueError as exc:
        result["aperture_growth"] = {"verdict": "not_evaluable", "reason": str(exc)}

    try:
        centroid = pixel.bootstrap_centroid(
            times,
            cube,
            period_days=period,
            transit_time=epoch,
            duration_hours=duration,
        )
        result["centroid"] = centroid
        result["localization"] = pixel.localization_offset(
            centroid, target_row, target_column
        )
    except ValueError as exc:
        result["centroid"] = {"verdict": "not_evaluable", "reason": str(exc)}

    neighbours = gaia_neighbours(conn, tic_id)
    candidates = [
        {
            "identifier": f"TIC {tic_id}",
            "row": target_row,
            "column": target_column,
            "is_target": True,
        }
    ]
    for neighbour in neighbours:
        if neighbour["ra_deg"] is None or neighbour["dec_deg"] is None:
            continue
        located = _sky_to_pixel(tpf, neighbour["ra_deg"], neighbour["dec_deg"])
        if located is None:
            continue
        if abs(located[0] - target_row) < 0.5 and abs(located[1] - target_column) < 0.5:
            continue  # same pixel as the target; not an independent aperture
        candidates.append(
            {
                "identifier": f"Gaia DR3 {neighbour['identifier']}",
                "row": located[0],
                "column": located[1],
            }
        )
    result["counterparts_tested"] = len(candidates)
    if len(candidates) > 1:
        try:
            result["neighbour_extraction"] = pixel.neighbour_transit_extraction(
                times,
                cube,
                period_days=period,
                transit_time=epoch,
                duration_hours=duration,
                candidates=candidates,
            )
        except ValueError as exc:
            result["neighbour_extraction"] = {
                "verdict": "not_evaluable",
                "reason": str(exc),
            }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--author", default="TESScut")
    parser.add_argument("--out", type=Path, default=Path("results/p4/pixel_pilot_v1"))
    args = parser.parse_args(argv)

    conn = ledger.connect(default_db_path())
    try:
        cohort = select_cohort(conn, args.limit)
        print(f"cohort: {len(cohort)} stars (ambiguous identity, T3-surviving)")
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "cohort.json").write_text(
            json.dumps(cohort, indent=2, sort_keys=True), encoding="utf-8"
        )

        results: list[dict[str, Any]] = []
        for index, entry in enumerate(cohort, start=1):
            began = time.monotonic()
            try:
                result = vet_one(conn, entry, author=args.author)
            except Exception as exc:  # noqa: BLE001 - one bad target must not stop the pilot
                result = {
                    "tic_id": entry["tic_id"],
                    "state": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(limit=3),
                }
            result["seconds"] = round(time.monotonic() - began, 1)
            results.append(result)
            verdict = (
                result.get("neighbour_extraction", {}).get("verdict")
                or result.get("localization", {}).get("verdict")
                or result["state"]
            )
            print(
                f"[{index:>3}/{len(cohort)}] TIC {entry['tic_id']:<12} "
                f"{result['state']:<16} {verdict} ({result['seconds']}s)",
                flush=True,
            )
            (args.out / "pixel_pilot_records.json").write_text(
                json.dumps(results, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )
    finally:
        conn.close()

    measured = [item for item in results if item.get("state") == "measured"]
    reassigned = [
        item
        for item in measured
        if item.get("neighbour_extraction", {}).get("verdict")
        == "signal_belongs_to_neighbour"
    ]
    contaminated = [
        item
        for item in measured
        if item.get("aperture_growth", {}).get("verdict") == "contaminating_neighbour"
    ]
    off_target = [
        item
        for item in measured
        if item.get("localization", {}).get("verdict") == "off_target"
    ]
    summary = {
        "cohort_size": len(cohort),
        "measured": len(measured),
        "failed": len(results) - len(measured),
        "signal_belongs_to_neighbour": len(reassigned),
        "aperture_growth_contaminated": len(contaminated),
        "centroid_off_target": len(off_target),
        "match_radius_arcsec": match_radius_arcsec(),
        "pixel_vet_policy": CURRENT_PIXEL_VET.policy_version,
        "identity_policy": CURRENT_IDENTITY.policy_version,
        "code": "modules:" + module_digest("pixel.py", "identity.py"),
        "reassigned_tics": [item["tic_id"] for item in reassigned],
    }
    (args.out / "pixel_pilot_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    print()
    for key, value in summary.items():
        if isinstance(value, (int, float, str)):
            print(f"  {key}: {value}")
    print(f"[written] {args.out / 'pixel_pilot_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
