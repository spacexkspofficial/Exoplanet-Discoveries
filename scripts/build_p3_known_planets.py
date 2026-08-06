"""Freeze a diverse 20-planet production-path regression cohort."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from exohunt.catalogs import TAP_URL, _tap_csv  # noqa: E402
from exohunt.benchmarks import BENCHMARKS  # noqa: E402
from exohunt.photometry import _configured_lightkurve  # noqa: E402


QUERY = (
    "select pl_name,hostname,tic_id,pl_orbper,pl_trandep,sy_tmag,"
    "disc_facility,rowupdate from pscomppars"
)

MANDATORY = {
    "WASP-18 b": 2,
    "pi Men c": 1,
    "HD 209458 b": 56,
    # Deliberate single-sector half-period-alias stress case.
    "TOI-700 c": 3,
}


def _tic_id(value: object) -> int | None:
    match = re.search(r"(\d+)", str(value or ""))
    return int(match.group(1)) if match else None


def _float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _sector_for(row: dict[str, object]) -> int | None:
    planet = str(row.get("planet") or row.get("pl_name"))
    if planet in MANDATORY:
        return MANDATORY[planet]
    tic_id = int(row["tic_id"])
    lk, _ = _configured_lightkurve()
    search = lk.search_lightcurve(
        f"TIC {tic_id}", mission="TESS", author="SPOC", exptime=120
    )
    if search is None or len(search) == 0:
        return None
    sectors = sorted(
        {
            int(value)
            for value in search.table["sequence_number"]
            if value is not None and int(value) > 0
        }
    )
    return sectors[0] if sectors else None


def _all_spoc_sectors(row: dict[str, object]) -> list[int]:
    tic_id = int(row["tic_id"])
    lk, _ = _configured_lightkurve()
    search = lk.search_lightcurve(
        f"TIC {tic_id}", mission="TESS", author="SPOC", exptime=120
    )
    if search is None or len(search) == 0:
        fallback = int(row["sector"])
        return [fallback]
    sectors = sorted(
        {
            int(value)
            for value in search.table["sequence_number"]
            if value is not None and int(value) > 0
        }
    )
    return sectors or [int(row["sector"])]


def _best_contiguous_run(sectors: list[int], *, maximum: int = 3) -> list[int]:
    runs: list[list[int]] = []
    for sector in sorted(set(sectors)):
        if runs and sector == runs[-1][-1] + 1:
            runs[-1].append(sector)
        else:
            runs.append([sector])
    best = max(runs, key=lambda run: (min(len(run), maximum), run[-1]))
    return best[-maximum:]


def _diverse_order(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    matrix = np.asarray(
        [
            [
                math.log10(float(row["expected_period_days"])),
                math.log10(float(row["expected_depth_ppm"])),
                float(row["tmag"]) if row.get("tmag") is not None else np.nan,
            ]
            for row in rows
        ]
    )
    if np.any(~np.isfinite(matrix[:, 2])):
        finite_tmag = matrix[np.isfinite(matrix[:, 2]), 2]
        matrix[~np.isfinite(matrix[:, 2]), 2] = (
            float(np.median(finite_tmag)) if finite_tmag.size else 11.0
        )
    matrix = (matrix - np.mean(matrix, axis=0)) / np.where(
        np.std(matrix, axis=0) > 0, np.std(matrix, axis=0), 1
    )
    chosen: list[int] = []
    mandatory_names = set(MANDATORY)
    for index, row in enumerate(rows):
        if row["planet"] in mandatory_names:
            chosen.append(index)
    if not chosen:
        chosen.append(int(np.argmax(np.sum(matrix**2, axis=1))))
    minimum = np.full(len(rows), np.inf)
    for index in chosen:
        minimum = np.minimum(minimum, np.sum((matrix - matrix[index]) ** 2, axis=1))
    while len(chosen) < len(rows):
        candidate = min(
            (index for index in range(len(rows)) if index not in chosen),
            key=lambda index: (-minimum[index], str(rows[index]["planet"])),
        )
        chosen.append(candidate)
        minimum = np.minimum(
            minimum, np.sum((matrix - matrix[candidate]) ** 2, axis=1)
        )
    return [rows[index] for index in chosen]


def _cached_source_rows(
    cache_dir: Path,
    target_pools: list[Path],
) -> tuple[list[dict[str, object]], dict[int, dict[str, str]], str]:
    targets: dict[int, dict[str, str]] = {}
    for pool in target_pools:
        if not pool.is_file():
            continue
        with pool.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                try:
                    tic_id = int(row["tic_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                current = targets.setdefault(tic_id, {})
                current.update(
                    {
                        key: value
                        for key, value in row.items()
                        if value not in (None, "")
                    }
                )

    source_rows: list[dict[str, object]] = []
    snapshot_hash = hashlib.sha256()
    # Empty official snapshots are ~1 KB. Listing metadata once and opening
    # only larger candidates avoids hydrating/reading 80k known-empty JSON
    # files from the synced workspace.
    candidate_paths = sorted(
        Path(entry.path)
        for entry in os.scandir(cache_dir)
        if entry.name.startswith("TIC_")
        and entry.name.endswith(".json")
        and entry.stat().st_size > 1_100
    )
    for path in candidate_paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            result = payload["result"]
            tic_id = int(result["tic_id"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if tic_id not in targets:
            continue
        known_rows = [
            row
            for row in result.get("tois", [])
            if isinstance(row, dict) and row.get("tfopwg_disp") in {"KP", "CP"}
        ]
        if not known_rows:
            continue
        snapshot_hash.update(path.name.encode("utf-8"))
        snapshot_hash.update(hashlib.sha256(path.read_bytes()).digest())
        confirmed = [
            row
            for row in result.get("confirmed_planets", [])
            if isinstance(row, dict) and str(row.get("tran_flag")) == "1"
        ]
        for toi in known_rows:
            period = _float(toi.get("pl_orbper"))
            if period is None:
                continue
            named = None
            if confirmed:
                named = min(
                    confirmed,
                    key=lambda row: abs((_float(row.get("pl_orbper")) or math.inf) - period),
                )
                named_period = _float(named.get("pl_orbper"))
                if named_period is None or abs(named_period - period) / period > 0.05:
                    named = None
            source_rows.append(
                {
                    "pl_name": (
                        named.get("pl_name") if named else f"TOI-{toi.get('toi')}"
                    ),
                    "hostname": named.get("hostname") if named else f"TIC {tic_id}",
                    "tic_id": f"TIC {tic_id}",
                    "pl_orbper": toi.get("pl_orbper"),
                    # TOI pl_trandep is already ppm; the live PS/PSCompPars
                    # field is percent. Mark the unit for the common parser.
                    "pl_trandep": toi.get("pl_trandep"),
                    "depth_unit": "ppm",
                    "sy_tmag": targets[tic_id].get("tmag"),
                    "rowupdate": toi.get("rowupdate"),
                    "sector": targets[tic_id].get("sectors") or targets[tic_id].get("sector"),
                    "snapshot_generated_at_utc": payload.get("generated_at_utc"),
                }
            )
    return source_rows, targets, snapshot_hash.hexdigest()


def build(
    output_csv: Path,
    *,
    limit: int = 20,
    offline_cache: Path | None = None,
    target_pools: list[Path] | None = None,
) -> dict[str, object]:
    snapshot_hash = None
    target_index: dict[int, dict[str, str]] = {}
    if offline_cache is not None:
        source_rows, target_index, snapshot_hash = _cached_source_rows(
            offline_cache, target_pools or []
        )
        source_label = f"official per-TIC snapshots under {offline_cache}"
    else:
        source_rows = _tap_csv(QUERY)
        source_label = TAP_URL
    parsed: list[dict[str, object]] = []
    by_tic: set[int] = set()
    for row in source_rows:
        tic_id = _tic_id(row.get("tic_id"))
        period = _float(row.get("pl_orbper"))
        raw_depth = _float(row.get("pl_trandep"))
        tmag = _float(row.get("sy_tmag"))
        if tic_id is None or period is None or raw_depth is None:
            continue
        depth_ppm = raw_depth if row.get("depth_unit") == "ppm" else raw_depth * 10_000.0
        in_design_box = (
            0.5 <= period <= 15
            and 200 <= depth_ppm <= 20_000
            and (tmag is None or 8 <= tmag <= 14)
        )
        if not in_design_box and str(row.get("pl_name")) not in MANDATORY:
            continue
        # Keep one expected dominant planet per light curve. Mandatory alias
        # cases are retained even when a sibling is deeper.
        if tic_id in by_tic and str(row.get("pl_name")) not in MANDATORY:
            continue
        parsed.append(
            {
                "target": f"TIC {tic_id}",
                "tic_id": tic_id,
                "planet": str(row["pl_name"]),
                "expected_period_days": period,
                "expected_depth_ppm": depth_ppm,
                "tmag": tmag,
                "source_rowupdate": row.get("rowupdate"),
                "purpose": (
                    "deliberate single-sector alias stress"
                    if str(row["pl_name"]) == "TOI-700 c"
                    else "period/depth/magnitude coverage"
                ),
            }
        )
        by_tic.add(tic_id)

    names = {str(row["planet"]) for row in parsed}
    missing = set(MANDATORY) - names
    if missing:
        # Mandatory objects outside the nominal magnitude/period box are
        # queried explicitly and remain labelled as boundary controls.
        for row in source_rows:
            if str(row.get("pl_name")) not in missing:
                continue
            tic_id = _tic_id(row.get("tic_id"))
            period = _float(row.get("pl_orbper"))
            raw_depth = _float(row.get("pl_trandep"))
            if tic_id is None or period is None or raw_depth is None:
                continue
            parsed.append(
                {
                    "target": f"TIC {tic_id}",
                    "tic_id": tic_id,
                    "planet": str(row["pl_name"]),
                    "expected_period_days": period,
                    "expected_depth_ppm": (
                        raw_depth if row.get("depth_unit") == "ppm" else raw_depth * 10_000.0
                    ),
                    "tmag": _float(row.get("sy_tmag")),
                    "source_rowupdate": row.get("rowupdate"),
                    "purpose": "boundary/alias control",
                }
            )

    # Historical mandatory controls that were searched before the locked pool
    # are added from their already-frozen NASA values.
    names = {str(row["planet"]) for row in parsed}
    for benchmark in BENCHMARKS:
        if benchmark["planet"] not in MANDATORY or benchmark["planet"] in names:
            continue
        parsed.append(
            {
                "target": f"TIC {int(benchmark['tic_id'])}",
                "tic_id": int(benchmark["tic_id"]),
                "planet": benchmark["planet"],
                "expected_period_days": float(benchmark["expected_period_days"]),
                "expected_depth_ppm": float(benchmark["expected_depth_ppm"]),
                "tmag": None,
                "source_rowupdate": "frozen NASA benchmark queried 2026-07-22",
                "purpose": "boundary/alias control",
                "sector": int(benchmark["sector"]),
            }
        )
    ordered = _diverse_order(parsed)
    # Resolve only a generous front of the diversity ranking. MAST lookups are
    # read-only but rate-limited, so four concurrent searches are sufficient.
    candidates = ordered[: max(limit * 4, 80)]
    if offline_cache is not None:
        sectors = [
            int(str(row.get("sector") or "").split(";")[0])
            if str(row.get("sector") or "").split(";")[0].isdigit()
            else _sector_for(row)
            for row in candidates
        ]
    else:
        with ThreadPoolExecutor(max_workers=4) as executor:
            sectors = list(executor.map(_sector_for, candidates))
    selected: list[dict[str, object]] = []
    selected_tics: set[int] = set()
    for row, sector in zip(candidates, sectors):
        if sector is None or int(row["tic_id"]) in selected_tics:
            continue
        selected.append(
            {
                **row,
                "sector": sector,
                "sectors": str(sector),
                "author": "SPOC",
                "cadence_seconds": 120.0,
            }
        )
        selected_tics.add(int(row["tic_id"]))
        if len(selected) == limit:
            break
    if len(selected) != limit:
        raise RuntimeError(f"Only {len(selected)} suitable SPOC controls were resolved.")
    if not set(MANDATORY).issubset({str(row["planet"]) for row in selected}):
        raise RuntimeError("Diversity selection dropped a mandatory control.")

    # Periods near a sector's half-baseline can have only one usable event for
    # an unlucky phase. Freeze every available 120-s SPOC sector for the same
    # preselected hosts; this is deterministic from product availability and
    # does not replace a target after seeing a recovery outcome.
    with ThreadPoolExecutor(max_workers=4) as executor:
        sector_sets = list(executor.map(_all_spoc_sectors, selected))
    for row, sector_set in zip(selected, sector_sets):
        planet = str(row["planet"])
        if planet in MANDATORY:
            chosen_sectors = [MANDATORY[planet]]
        elif float(row["expected_period_days"]) >= 8.0:
            chosen_sectors = _best_contiguous_run(sector_set)
        else:
            chosen_sectors = sector_set[:1]
        row["sector"] = chosen_sectors[0]
        row["sectors"] = ";".join(str(value) for value in chosen_sectors)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    fields = list(selected[0])
    with output_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(selected)
    source_hash = hashlib.sha256(
        json.dumps(source_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    output_hash = hashlib.sha256(output_csv.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "status": "amended_after_two_measurements_without_target_replacement",
        "amendment": (
            "The initial first-sector execution recovered 18/20 at the correct "
            "alias. A second execution reached 19/20 and showed that selecting "
            "the first two non-contiguous products did not guarantee long-period "
            "event support. The same 20 planet/TIC identities are retained; the "
            "final rule uses the longest contiguous public run."
        ),
        "source": source_label,
        "source_query": QUERY,
        "source_response_sha256": source_hash,
        "official_snapshot_set_sha256": snapshot_hash,
        "retrieved_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "selection": (
            "four mandatory historical controls plus deterministic farthest-point "
            "coverage in log(period), log(depth), and Tmag; one planet per TIC; "
            "fixed historical sectors for mandatory controls, otherwise one "
            "120-s SPOC sector below 8 d and up to three sectors from the "
            "longest contiguous public run at or above 8 d"
        ),
        "mandatory_planets": MANDATORY,
        "rows": len(selected),
        "output_csv": str(output_csv),
        "output_csv_sha256": output_hash,
        "period_days_range": [
            min(float(row["expected_period_days"]) for row in selected),
            max(float(row["expected_period_days"]) for row in selected),
        ],
        "depth_ppm_range": [
            min(float(row["expected_depth_ppm"]) for row in selected),
            max(float(row["expected_depth_ppm"]) for row in selected),
        ],
        "tmag_range": [
            min(float(row["tmag"]) for row in selected if row["tmag"] is not None),
            max(float(row["tmag"]) for row in selected if row["tmag"] is not None),
        ],
    }
    output_csv.with_suffix(".json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-csv", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offline-cache", type=Path)
    parser.add_argument("--target-pool", action="append", type=Path, default=[])
    args = parser.parse_args()
    print(
        json.dumps(
            build(
                args.output_csv,
                limit=args.limit,
                offline_cache=args.offline_cache,
                target_pools=args.target_pool,
            ),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
