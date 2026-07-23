"""Export the append-only search ledger as a browser-friendly spatial dataset."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _optional_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _tic_id(row: dict[str, object]) -> int | None:
    value = row.get("tic_id") or row.get("TICID") or row.get("ID")
    if value in {None, ""}:
        return None
    try:
        return int(float(str(value)))
    except ValueError:
        return None


def _sectors(value: object) -> list[int]:
    if value in {None, ""}:
        return []
    values: list[int] = []
    for item in str(value).replace(",", ";").split(";"):
        try:
            values.append(int(item.strip()))
        except ValueError:
            continue
    return sorted(set(values))


def _read_catalog_cache(path: Path) -> dict[int, dict[str, object]]:
    if not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {int(row["tic_id"]): row for row in rows}


def _refresh_tic_catalog(
    tic_ids: list[int], cache_path: Path
) -> dict[int, dict[str, object]]:
    from astroquery.mast import Catalogs

    catalog = _read_catalog_cache(cache_path)
    missing = [tic_id for tic_id in tic_ids if tic_id not in catalog]
    for start in range(0, len(missing), 50):
        table = Catalogs.query_criteria(catalog="Tic", ID=missing[start : start + 50])
        for row in table:
            tic_id = int(row["ID"])
            catalog[tic_id] = {
                "tic_id": tic_id,
                "ra_deg": _optional_float(row["ra"]),
                "dec_deg": _optional_float(row["dec"]),
                "distance_pc": _optional_float(row["d"]),
                "tmag": _optional_float(row["Tmag"]),
                "teff_k": _optional_float(row["Teff"]),
                "stellar_radius_solar": _optional_float(row["rad"]),
            }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            sorted(catalog.values(), key=lambda row: int(row["tic_id"])),
            indent=2,
        ),
        encoding="utf-8",
    )
    return catalog


def _deterministic_direction(tic_id: int) -> tuple[float, float]:
    digest = hashlib.sha256(str(tic_id).encode("ascii")).digest()
    ra = int.from_bytes(digest[:4], "big") / 2**32 * 360.0
    dec = math.degrees(math.asin(int.from_bytes(digest[4:8], "big") / 2**32 * 2 - 1))
    return ra, dec


def _cartesian(ra_deg: float, dec_deg: float, distance_pc: float) -> dict[str, float]:
    """Return Sun-centered Galactic Cartesian coordinates in parsecs.

    The fixed rotation is the standard ICRS/J2000 to Galactic transformation:
    +X points toward the Galactic center, +Y toward Galactic longitude 90
    degrees, and +Z toward the north Galactic pole.
    """

    ra = math.radians(ra_deg)
    dec = math.radians(dec_deg)
    equatorial_x = math.cos(dec) * math.cos(ra)
    equatorial_y = math.cos(dec) * math.sin(ra)
    equatorial_z = math.sin(dec)
    galactic_x = (
        -0.0548755604 * equatorial_x
        - 0.8734370902 * equatorial_y
        - 0.4838350155 * equatorial_z
    )
    galactic_y = (
        0.4941094279 * equatorial_x
        - 0.4448296300 * equatorial_y
        + 0.7469822445 * equatorial_z
    )
    galactic_z = (
        -0.8676661490 * equatorial_x
        - 0.1980763734 * equatorial_y
        + 0.4559837762 * equatorial_z
    )
    return {
        "x": distance_pc * galactic_x,
        "y": distance_pc * galactic_y,
        "z": distance_pc * galactic_z,
    }


def export_dashboard_data(
    workspace: str | Path = ".",
    *,
    refresh_catalog: bool = False,
    events: list[dict[str, Any]] | None = None,
    stats: dict[str, Any] | None = None,
) -> Path | None:
    """Write dashboard/public/data/survey.json from the current project state."""

    root = Path(workspace).resolve()
    dashboard = root / "dashboard"
    if not dashboard.exists():
        return None

    ledger_path = root / "metrics" / "events.jsonl"
    if events is None:
        events = [
            json.loads(line)
            for line in ledger_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    if stats is None:
        stats_path = root / "metrics" / "current_stats.json"
        stats = json.loads(stats_path.read_text(encoding="utf-8"))

    invalidated = {
        str(event["invalidates_event_id"])
        for event in events
        if event.get("kind") == "event_invalidated"
        and event.get("invalidates_event_id")
    }
    active = [event for event in events if event.get("event_id") not in invalidated]

    active_campaigns: list[dict[str, object]] = []
    active_results: list[dict[str, object]] = []
    results_root = root / "results"
    if results_root.exists():
        for progress_path in sorted(results_root.rglob("batch_progress.json")):
            try:
                progress = json.loads(progress_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if progress.get("state") not in {"running", "finalizing"}:
                continue
            progress_results = list(progress.get("results", []))
            active_results.extend(progress_results)
            progress_sectors = sorted(
                {
                    sector
                    for result in progress_results
                    for sector in _sectors(result.get("sectors"))
                }
            )
            active_campaigns.append(
                {
                    "name": progress_path.parent.name,
                    "state": progress.get("state"),
                    "target_list": progress.get("target_list"),
                    "sectors": progress_sectors,
                    "total_targets": int(progress.get("total_targets", 0)),
                    "completed_targets": int(
                        progress.get("completed_targets", len(progress_results))
                    ),
                    "counts": progress.get("counts", {}),
                    "started_at_utc": progress.get("started_at_utc"),
                    "updated_at_utc": progress.get("updated_at_utc"),
                }
            )

    searched_ids = sorted(
        {
            int(tic_id)
            for event in active
            if event.get("kind") == "campaign_completed"
            for tic_id in event.get("tic_ids", [])
        }
        | {
            int(tic_id)
            for result in active_results
            if (tic_id := _tic_id(result)) is not None
        }
    )

    metadata: dict[int, dict[str, object]] = {
        tic_id: {"tic_id": tic_id, "target": f"TIC {tic_id}"} for tic_id in searched_ids
    }
    for csv_path in sorted((root / "targets").glob("*.csv")):
        try:
            with csv_path.open(newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    tic_id = _tic_id(row)
                    if tic_id not in metadata:
                        continue
                    current = metadata[tic_id]
                    aliases = {
                        "target": row.get("target"),
                        "tmag": row.get("tmag") or row.get("Tmag"),
                        "teff_k": row.get("teff_k") or row.get("Teff"),
                        "stellar_radius_solar": row.get("stellar_radius_solar")
                        or row.get("rad"),
                        "distance_pc": row.get("distance_pc") or row.get("d"),
                        "ra_deg": row.get("ra_deg") or row.get("ra"),
                        "dec_deg": row.get("dec_deg") or row.get("dec"),
                        "sectors": row.get("sectors") or row.get("sector"),
                    }
                    for key, value in aliases.items():
                        if value not in {None, ""}:
                            current[key] = value
        except (OSError, csv.Error, UnicodeDecodeError):
            continue

    cache_path = root / "data" / "dashboard_tic_catalog.json"
    catalog = (
        _refresh_tic_catalog(searched_ids, cache_path)
        if refresh_catalog
        else _read_catalog_cache(cache_path)
    )
    for tic_id, row in catalog.items():
        if tic_id in metadata:
            for key, value in row.items():
                if value is not None:
                    metadata[tic_id][key] = value

    signals: dict[int, dict[str, object]] = {}
    observed_sectors: set[int] = set()
    campaigns: list[dict[str, object]] = []
    for event in active:
        if event.get("kind") != "campaign_completed":
            continue
        summary_text = str(event.get("summary_path", ""))
        summary_path = root / summary_text
        campaign = {
            "event_id": event.get("event_id"),
            "targets": int(event.get("targets", 0)),
            "survivors": int(event.get("automated_survivors", 0)),
            "rejected": int(event.get("rejected", 0)),
            "errors": int(event.get("errors", 0)),
            "timestamp_utc": event.get("timestamp_utc"),
        }
        campaigns.append(campaign)
        if not summary_path.exists():
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for result in summary.get("results", []):
            tic_id = _tic_id(result)
            if tic_id is None:
                continue
            row_sectors = _sectors(result.get("sectors"))
            observed_sectors.update(row_sectors)
            signals[tic_id] = {
                "period_days": _optional_float(result.get("period_days")),
                "depth_ppm": _optional_float(result.get("depth_ppm")),
                "snr": _optional_float(result.get("depth_snr")),
                "duration_hours": _optional_float(result.get("duration_hours")),
                "observed_transits": result.get("observed_transits"),
                "screening_status": result.get("status"),
                "sectors": row_sectors,
            }

    # Active checkpoints override an older completed result for the same TIC so
    # the browser reflects the newest per-star run before the permanent ledger
    # receives the final, idempotent campaign event.
    for result in active_results:
        tic_id = _tic_id(result)
        if tic_id is None:
            continue
        row_sectors = _sectors(result.get("sectors"))
        observed_sectors.update(row_sectors)
        signals[tic_id] = {
            "period_days": _optional_float(result.get("period_days")),
            "depth_ppm": _optional_float(result.get("depth_ppm")),
            "snr": _optional_float(result.get("depth_snr")),
            "duration_hours": _optional_float(result.get("duration_hours")),
            "observed_transits": result.get("observed_transits"),
            "screening_status": result.get("status"),
            "sectors": row_sectors,
        }

    outcomes: dict[int, list[dict[str, object]]] = {}
    for event in active:
        if event.get("tic_id") is None:
            continue
        tic_id = int(event["tic_id"])
        outcomes.setdefault(tic_id, []).append(
            {
                "kind": event.get("kind"),
                "label": event.get("label"),
                "notes": event.get("notes"),
                "source": event.get("source"),
                "timestamp_utc": event.get("timestamp_utc"),
            }
        )

    priorities = {
        "searched": 0,
        "false_positive": 1,
        "rediscovery": 2,
        "known_tce_rediscovery": 3,
        "vetted_candidate": 4,
        "confirmed_planet": 5,
    }
    stars: list[dict[str, object]] = []
    for tic_id in searched_ids:
        row = metadata[tic_id]
        ra = _optional_float(row.get("ra_deg"))
        dec = _optional_float(row.get("dec_deg"))
        distance = _optional_float(row.get("distance_pc"))
        coordinate_source = "TIC"
        if ra is None or dec is None:
            ra, dec = _deterministic_direction(tic_id)
            coordinate_source = "display fallback"
        if distance is None or distance <= 0:
            distance = 35.0 + tic_id % 110
            coordinate_source = "display fallback"

        status = "searched"
        label = "Searched — no vetted signal"
        notes = ""
        for outcome in outcomes.get(tic_id, []):
            kind = str(outcome.get("kind"))
            if priorities.get(kind, -1) >= priorities.get(status, -1):
                status = kind
                label = str(outcome.get("label") or label)
                notes = str(outcome.get("notes") or "")
        signal = signals.get(tic_id, {})
        sectors = signal.get("sectors") or _sectors(row.get("sectors"))
        observed_sectors.update(int(value) for value in sectors)
        star = {
            "tic_id": tic_id,
            "name": str(row.get("target") or f"TIC {tic_id}"),
            "status": status,
            "status_label": label,
            "notes": notes,
            "ra_deg": round(ra, 7),
            "dec_deg": round(dec, 7),
            "distance_pc": round(distance, 4),
            "coordinate_source": coordinate_source,
            "tmag": _optional_float(row.get("tmag")),
            "teff_k": _optional_float(row.get("teff_k")),
            "stellar_radius_solar": _optional_float(row.get("stellar_radius_solar")),
            "sectors": sectors,
            **signal,
            **_cartesian(ra, dec, distance),
        }
        stars.append(star)

    counts: dict[str, int] = {}
    for star in stars:
        key = str(star["status"])
        counts[key] = counts.get(key, 0) + 1
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "stats": stats,
        "status_counts": counts,
        "observed_sectors": sorted(observed_sectors),
        "campaigns": campaigns,
        "active_campaigns": active_campaigns,
        "stars": stars,
        "warnings": [
            "Automated survivors are not planet candidates.",
            "A rediscovery is explicitly not a new planet.",
            "Display-fallback coordinates are labeled and should be replaced by TIC data.",
        ],
    }
    output = dashboard / "public" / "data" / "survey.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return output
