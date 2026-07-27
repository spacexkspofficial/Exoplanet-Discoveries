"""Campaign target-list construction for the EXOHUNT command-line application.

This module owns the concern of deciding *which stars a campaign will look at*:
reading official TESS target lists and curated catalog selections, choosing
observing-sector subsets, ranking host stars for small-planet sensitivity, and
publishing the selected CSV together with its provenance manifest.

Selection happens before any search runs, so nothing here interprets photometry
or reaches a scientific verdict. The rankings are deterministic triage
heuristics, not occurrence-rate models or completeness results.

`_atomic_write_json` still lives in :mod:`exohunt.cli` and is resolved on the
live module at call time, matching :mod:`exohunt.campaign`, so CLI-side
monkeypatches stay authoritative.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .catalogs import check_tic, curated_cool_single_hosts, known_planet_host_tic_ids
from .metrics import read_events
from .photometry import _configured_lightkurve
from .screening import _known_transiting_periods


def _read_commented_csv(path: Path) -> list[dict[str, str]]:
    lines = [
        line
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return list(csv.DictReader(lines))

def _available_lightcurve_sectors(
    tic_id: int, cadence_seconds: float, author: str = "SPOC"
) -> list[int]:
    lk, _ = _configured_lightkurve()
    search = lk.search_lightcurve(
        f"TIC {tic_id}", mission="TESS", author=author, exptime=cadence_seconds
    )
    if len(search) == 0 or "mission" not in search.table.colnames:
        return []
    return sorted(
        {
            int(match.group(1))
            for mission in search.table["mission"]
            if (match := re.search(r"Sector\s+(\d+)", str(mission)))
        }
    )

def _compact_sector_subset(sectors: list[int], count: int) -> list[int]:
    """Choose a deterministic compact observing window from available sectors."""

    if count <= 0:
        raise ValueError("Sector count must be positive.")
    values = sorted(set(sectors))
    if len(values) <= count:
        return values
    windows = [values[index : index + count] for index in range(len(values) - count + 1)]
    return min(windows, key=lambda window: (window[-1] - window[0], window[0]))

def _latest_sector_subset(sectors: list[int], count: int) -> list[int]:
    """Choose the most recently numbered available sectors."""

    if count <= 0:
        raise ValueError("Sector count must be positive.")
    return sorted(set(sectors))[-count:]

def _make_targets(args: argparse.Namespace) -> int:
    criteria = {
        "dispositions": ["CP", "KP"],
        "unique_transiting_periods_across_all_toi_and_confirmed_rows": 1,
        "max_tmag": args.max_tmag,
        "max_teff_k": args.max_teff,
        "max_stellar_radius_solar": args.max_stellar_radius,
        "max_distance_pc": args.max_distance,
        "known_period_range_days": [args.known_min_period, args.known_max_period],
        "minimum_available_sectors": args.min_sectors,
        "selected_sectors_per_target": args.sectors_per_target,
        "cadence_seconds": args.cadence_seconds,
        "lightcurve_author": args.author,
        "minimum_latest_sector": args.min_latest_sector,
        "sector_strategy": args.sector_strategy,
        "ordering": "TESS magnitude ascending, then TIC ID",
    }
    catalog_rows = curated_cool_single_hosts(
        max_tmag=args.max_tmag,
        max_teff=args.max_teff,
        max_stellar_radius=args.max_stellar_radius,
        max_distance_pc=args.max_distance,
        min_period_days=args.known_min_period,
        max_period_days=args.known_max_period,
    )
    selected: list[dict[str, object]] = []
    checked = 0
    for row in catalog_rows[: args.pool_size]:
        checked += 1
        tic_id = int(float(row["tid"]))
        full_catalog = check_tic(tic_id)
        unique_periods = _known_transiting_periods(full_catalog)
        if len(unique_periods) != 1:
            continue
        sectors = _available_lightcurve_sectors(tic_id, args.cadence_seconds, args.author)
        if len(sectors) < args.min_sectors:
            continue
        if args.min_latest_sector is not None and max(sectors) < args.min_latest_sector:
            continue
        chosen = (
            _latest_sector_subset(sectors, args.sectors_per_target)
            if args.sector_strategy == "latest"
            else _compact_sector_subset(sectors, args.sectors_per_target)
        )
        selected.append(
            {
                "target": f"TIC {tic_id}",
                "tic_id": tic_id,
                "toi": row["toi"],
                "disposition": row["tfopwg_disp"],
                "tmag": row["st_tmag"],
                "teff_k": row["st_teff"],
                "stellar_radius_solar": row["st_rad"],
                "distance_pc": row["st_dist"],
                "known_period_days": row["pl_orbper"],
                "unique_transiting_signal_count": len(unique_periods),
                "available_sector_count": len(sectors),
                "sectors": ";".join(str(value) for value in chosen),
            }
        )
        print(
            f"selected TIC {tic_id} / TOI-{row['toi']}: sectors "
            + ",".join(str(value) for value in chosen)
        )
        if len(selected) >= args.limit:
            break
    if not selected:
        raise RuntimeError("No targets satisfied both the catalog and sector criteria.")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(selected[0].keys()))
        writer.writeheader()
        writer.writerows(selected)
    manifest = {
        "created_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": "NASA Exoplanet Archive TOI table plus MAST SPOC availability",
        "criteria": criteria,
        "catalog_rows_returned": len(catalog_rows),
        "catalog_rows_sector_checked": checked,
        "selected_count": len(selected),
        "targets": selected,
    }
    manifest_path = output_path.with_suffix(".json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nSaved {output_path} and {manifest_path}")
    return 0

def _optional_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None

def _small_planet_selection_tier(
    *,
    luminosity_class: str,
    stellar_radius_solar: float | None,
    teff_k: float | None,
    max_stellar_radius_solar: float,
    max_teff_k: float,
) -> int:
    """Rank target suitability without pretending this is survey completeness."""

    lumclass = luminosity_class.strip().upper()
    radius_is_small = (
        stellar_radius_solar is not None
        and 0.1 <= stellar_radius_solar <= max_stellar_radius_solar
    )
    temperature_is_supported = (
        teff_k is not None and 2500.0 <= teff_k <= max_teff_k
    )
    if lumclass == "DWARF":
        if (
            stellar_radius_solar is not None
            and 0.1 <= stellar_radius_solar <= 1.5
            and teff_k is not None
            and 2500.0 <= teff_k <= min(max_teff_k, 6500.0)
        ):
            return 0
        if radius_is_small and temperature_is_supported:
            return 1
        return 2
    if lumclass not in {"GIANT", "SUBGIANT"} and radius_is_small:
        return 3
    if lumclass == "SUBGIANT":
        return 4
    if lumclass == "GIANT":
        return 6
    return 5

def _small_planet_merit(
    *,
    stellar_radius_solar: float | None,
    tmag: float,
) -> float:
    """Deterministic depth/brightness heuristic; lower is more favorable."""

    if stellar_radius_solar is None or stellar_radius_solar <= 0:
        return 999.0
    return 2.0 * float(np.log10(stellar_radius_solar)) + 0.2 * tmag

def _make_sector_targets(args: argparse.Namespace) -> int:
    """Build a large, local-only campaign from an official TESS target list."""

    from . import cli as cli_module

    source_path = Path(args.target_list)
    source_rows = _read_commented_csv(source_path)
    excluded_tic_ids = {
        int(tic_id)
        for event in read_events(args.exclude_ledger)
        if event.get("kind") == "campaign_completed"
        for tic_id in event.get("tic_ids", [])
    }
    for exclude_path_text in args.exclude_list:
        exclude_path = Path(exclude_path_text)
        with exclude_path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                value = row.get("tic_id") or row.get("TICID") or row.get("target")
                match = re.search(r"\d+", str(value or ""))
                if match:
                    excluded_tic_ids.add(int(match.group()))

    groups: dict[tuple[int, int], list[dict[str, object]]] = {}
    seen: set[int] = set()
    for row in source_rows:
        tic_id = int(row["TICID"])
        tmag = float(row["Tmag"])
        if (
            tic_id in seen
            or tic_id in excluded_tic_ids
            or tmag < args.min_tmag
            or tmag > args.max_tmag
        ):
            continue
        seen.add(tic_id)
        camera = int(row["Camera"])
        ccd = int(row["CCD"])
        groups.setdefault((camera, ccd), []).append(
            {
                "target": f"TIC {tic_id}",
                "tic_id": tic_id,
                "sector": args.sector,
                "sectors": str(args.sector),
                "tmag": tmag,
                "ra_deg": _optional_float(row.get("RA")),
                "dec_deg": _optional_float(row.get("Dec")),
                "camera": camera,
                "ccd": ccd,
            }
        )
    prefer_small_stars = bool(getattr(args, "prefer_small_stars", False))
    if prefer_small_stars:
        from astroquery.mast import Catalogs

        candidates = [row for values in groups.values() for row in values]
        tic_ids = [int(row["tic_id"]) for row in candidates]
        metadata_by_tic: dict[int, object] = {}
        batch_size = max(1, int(getattr(args, "tic_query_batch_size", 500)))
        for start in range(0, len(tic_ids), batch_size):
            table = Catalogs.query_criteria(
                catalog="TIC", ID=tic_ids[start : start + batch_size]
            )
            for metadata in table:
                metadata_by_tic[int(str(metadata["ID"]))] = metadata
        max_radius = float(getattr(args, "max_stellar_radius", 2.0))
        max_teff = float(getattr(args, "max_teff", 7000.0))
        for row in candidates:
            metadata = metadata_by_tic.get(int(row["tic_id"]))
            if metadata is None:
                luminosity_class = "UNKNOWN"
                radius = None
                teff = None
                distance = None
            else:
                raw_lumclass = str(metadata["lumclass"]).strip().upper()
                luminosity_class = (
                    raw_lumclass
                    if raw_lumclass not in {"", "--", "NAN", "NONE"}
                    else "UNKNOWN"
                )
                radius = _optional_float(metadata["rad"])
                teff = _optional_float(metadata["Teff"])
                distance = _optional_float(metadata["d"])
            row.update(
                {
                    "teff_k": teff,
                    "stellar_radius_solar": radius,
                    "distance_pc": distance,
                    "luminosity_class": luminosity_class,
                    "stellar_selection_tier": _small_planet_selection_tier(
                        luminosity_class=luminosity_class,
                        stellar_radius_solar=radius,
                        teff_k=teff,
                        max_stellar_radius_solar=max_radius,
                        max_teff_k=max_teff,
                    ),
                    "small_planet_merit": round(
                        _small_planet_merit(
                            stellar_radius_solar=radius,
                            tmag=float(row["tmag"]),
                        ),
                        6,
                    ),
                }
            )
    for values in groups.values():
        values.sort(
            key=lambda row: (
                int(row.get("stellar_selection_tier", 0)),
                float(row.get("small_planet_merit", 0.0)),
                float(row["tmag"]),
                int(row["tic_id"]),
            )
        )

    selected: list[dict[str, object]] = []
    if prefer_small_stars:
        # Reserve one quarter of an equal-share allocation for every detector, then
        # spend the remaining sample on the strongest host-star merit globally.
        # This preserves broad detector coverage without forcing weak giant-star
        # targets into the list merely because one CCD has fewer suitable dwarfs.
        per_group_quota = max(1, args.limit // (max(1, len(groups)) * 4))
        selected_ids: set[int] = set()
        for rank in range(per_group_quota):
            for key in sorted(groups):
                if rank < len(groups[key]) and len(selected) < args.limit:
                    row = groups[key][rank]
                    selected.append(row)
                    selected_ids.add(int(row["tic_id"]))
        remaining = sorted(
            (
                row
                for values in groups.values()
                for row in values
                if int(row["tic_id"]) not in selected_ids
            ),
            key=lambda row: (
                int(row.get("stellar_selection_tier", 0)),
                float(row.get("small_planet_merit", 0.0)),
                float(row["tmag"]),
                int(row["tic_id"]),
            ),
        )
        selected.extend(remaining[: max(0, args.limit - len(selected))])
    else:
        rank = 0
        while len(selected) < args.limit:
            added = False
            for key in sorted(groups):
                if rank < len(groups[key]):
                    selected.append(groups[key][rank])
                    added = True
                    if len(selected) == args.limit:
                        break
            if not added:
                break
            rank += 1
    if len(selected) < args.limit:
        raise RuntimeError(
            f"Only {len(selected)} unsearched targets met the magnitude criteria; "
            f"{args.limit} were requested."
        )
    for index, row in enumerate(selected, start=1):
        row["selection_rank"] = index

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(selected[0]))
        writer.writeheader()
        writer.writerows(selected)
    manifest = {
        "created_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_target_list": str(source_path),
        "source_target_list_sha256": hashlib.sha256(
            source_path.read_bytes()
        ).hexdigest(),
        "output_csv_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
        "sector": args.sector,
        "criteria": {
            "tmag_range": [args.min_tmag, args.max_tmag],
            "prefer_small_stars": prefer_small_stars,
            "max_stellar_radius_solar": (
                float(args.max_stellar_radius) if prefer_small_stars else None
            ),
            "max_teff_k": float(args.max_teff) if prefer_small_stars else None,
            "excluded_completed_campaign_tic_ids": len(excluded_tic_ids),
            "exclude_ledger": str(args.exclude_ledger),
            "exclude_lists": [
                str(Path(value)) for value in args.exclude_list
            ],
            "ordering": (
                (
                    "quarter-share camera/CCD quota followed by a global fill; "
                    "small-planet stellar tier then approximate radius/brightness "
                    "merit then TESS magnitude and TIC ID"
                )
                if prefer_small_stars
                else (
                    "round-robin across camera/CCD groups; within each group, "
                    "TESS magnitude ascending then TIC ID"
                )
            ),
            "small_planet_merit_warning": (
                "The deterministic radius/brightness ranking improves target "
                "triage but is not an occurrence-rate model or completeness result."
                if prefer_small_stars
                else None
            ),
            "catalog_handling": (
                "NASA TOI and confirmed-planet rows are checked per target during "
                "batch-hunt; known ephemerides are masked before the residual search"
            ),
        },
        "source_rows": len(source_rows),
        "eligible_rows": sum(len(values) for values in groups.values()),
        "selected_count": len(selected),
        "targets": selected,
        "warning": (
            "Catalog absence and automated transit screening are not proof of a "
            "new planet; pixel, neighbor, TCE, literature, and multi-sector checks "
            "remain required."
        ),
    }
    cli_module._atomic_write_json(output_path.with_suffix(".json"), manifest)
    print(f"Selected {len(selected)} Sector {args.sector} stars for the campaign.")
    print(f"Saved {output_path} and {output_path.with_suffix('.json')}")
    return 0

def _make_blank_targets(args: argparse.Namespace) -> int:
    """Select small Sector target-list stars with no catalogued planet host entry."""

    from astroquery.mast import Catalogs

    target_list_path = Path(args.target_list)
    target_rows = _read_commented_csv(target_list_path)
    excluded_tic_ids: set[int] = set()
    for exclude_path_text in args.exclude_list:
        exclude_path = Path(exclude_path_text)
        with exclude_path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                value = row.get("tic_id") or row.get("TICID") or row.get("target")
                if value is None:
                    continue
                match = re.search(r"\d+", str(value))
                if match:
                    excluded_tic_ids.add(int(match.group()))
    eligible_rows = sorted(
        (
            row
            for row in target_rows
            if args.min_tmag <= float(row["Tmag"]) <= args.max_tmag
        ),
        key=lambda row: (float(row["Tmag"]), int(row["TICID"])),
    )[: args.pool_size]
    if not eligible_rows:
        raise RuntimeError("No target-list rows satisfy the magnitude and pool criteria.")

    source_by_tic = {int(row["TICID"]): row for row in eligible_rows}
    tic_rows: dict[int, object] = {}
    tic_ids = list(source_by_tic)
    for start in range(0, len(tic_ids), 50):
        table = Catalogs.query_criteria(catalog="Tic", ID=tic_ids[start : start + 50])
        for row in table:
            tic_rows[int(row["ID"])] = row

    filtered: list[dict[str, object]] = []
    for tic_id, row in tic_rows.items():
        teff = _optional_float(row["Teff"])
        radius = _optional_float(row["rad"])
        distance = _optional_float(row["d"])
        tmag = _optional_float(row["Tmag"])
        if None in {teff, radius, distance, tmag}:
            continue
        if (
            teff > args.max_teff
            or radius > args.max_stellar_radius
            or distance > args.max_distance
        ):
            continue
        source = source_by_tic[tic_id]
        filtered.append(
            {
                "target": f"TIC {tic_id}",
                "tic_id": tic_id,
                "sector": args.sector,
                "sectors": str(args.sector),
                "tmag": tmag,
                "teff_k": teff,
                "stellar_radius_solar": radius,
                "distance_pc": distance,
                "ra_deg": _optional_float(row["ra"]),
                "dec_deg": _optional_float(row["dec"]),
                "camera": source.get("Camera"),
                "ccd": source.get("CCD"),
                "known_planet_host_rows": 0,
            }
        )
    known = known_planet_host_tic_ids([int(row["tic_id"]) for row in filtered])
    selected = sorted(
        (
            row
            for row in filtered
            if int(row["tic_id"]) not in known
            and int(row["tic_id"]) not in excluded_tic_ids
        ),
        key=lambda row: (float(row["tmag"]), int(row["tic_id"])),
    )[: args.limit]
    if not selected:
        raise RuntimeError("No zero-catalogued-planet stars satisfied all target criteria.")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(selected[0]))
        writer.writeheader()
        writer.writerows(selected)
    manifest = {
        "created_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_target_list": str(target_list_path),
        "sector": args.sector,
        "criteria": {
            "no_nasa_toi_or_confirmed_planet_host_row": True,
            "tmag_range": [args.min_tmag, args.max_tmag],
            "max_teff_k": args.max_teff,
            "max_stellar_radius_solar": args.max_stellar_radius,
            "max_distance_pc": args.max_distance,
            "input_pool_size": args.pool_size,
            "excluded_target_lists": args.exclude_list,
            "excluded_tic_ids": len(excluded_tic_ids),
            "ordering": "TESS magnitude ascending, then TIC ID",
        },
        "target_list_rows": len(target_rows),
        "tic_rows_queried": len(eligible_rows),
        "stellar_rows_passing": len(filtered),
        "known_hosts_excluded": len(known),
        "selected_count": len(selected),
        "targets": selected,
        "warning": (
            "NASA catalog absence is not proof of novelty; live ExoFOP, TCE, "
            "literature, neighbor, and pixel checks remain required."
        ),
    }
    manifest_path = output_path.with_suffix(".json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Selected {len(selected)} zero-catalogued-planet Sector {args.sector} stars.")
    print(f"Saved {output_path} and {manifest_path}")
    return 0
