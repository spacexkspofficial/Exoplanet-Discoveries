"""Checks against public MAST TESS threshold-crossing-event statistics."""

from __future__ import annotations

import csv
import io
import re
import urllib.parse
import urllib.request
from pathlib import Path


TCE_INDEX = "https://archive.stsci.edu/tess/bulk_downloads/bulk_downloads_tce.html"


def _read_url(url: str, timeout: int = 60) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "exohunt-starter/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def _catalog_urls(sectors: list[int]) -> list[str]:
    html = _read_url(TCE_INDEX)
    hrefs = re.findall(r'href=["\']([^"\']+_dvr-tcestats\.csv)["\']', html)
    urls = [urllib.parse.urljoin(TCE_INDEX, href) for href in hrefs]
    selected: list[str] = []
    for sector in sorted(set(sectors)):
        marker = f"-s{sector:04d}-s{sector:04d}_dvr-tcestats.csv"
        match = next((url for url in urls if marker in url), None)
        if match:
            selected.append(match)

    # Also use the tightest available multi-sector search covering the complete
    # observing set. These are especially valuable when no single sector has
    # enough transits to pass the official pipeline threshold.
    low, high = min(sectors), max(sectors)
    covering: list[tuple[int, str]] = []
    for url in urls:
        match = re.search(r"-s(\d{4})-s(\d{4})_dvr-tcestats\.csv", url)
        if not match:
            continue
        start, stop = int(match.group(1)), int(match.group(2))
        if start != stop and start <= low and stop >= high:
            covering.append((stop - start, url))
    if covering:
        selected.append(min(covering)[1])
    return list(dict.fromkeys(selected))


def _tce_rows(url: str, tic_id: int) -> list[dict[str, str]]:
    text = _read_url(url)
    data_lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    if not data_lines:
        return []
    return [row for row in csv.DictReader(io.StringIO("\n".join(data_lines))) if row.get("ticid") == str(tic_id)]


def _period_relation(found: float, candidate: float, tolerance: float = 0.01) -> tuple[str, float]:
    relations = (
        ("exact", 1.0),
        ("half-period alias", 0.5),
        ("double-period alias", 2.0),
        ("one-third-period alias", 1.0 / 3.0),
        ("triple-period alias", 3.0),
    )
    name, error = min(
        (
            (name, abs(found - candidate * factor) / (candidate * factor))
            for name, factor in relations
        ),
        key=lambda item: item[1],
    )
    return (name if error <= tolerance else "none"), error


def check_tces(
    tic_id: int,
    sectors: list[int],
    candidate_period_days: float,
) -> dict[str, object]:
    """Return relevant official TCE rows and candidate-period matches."""

    if tic_id <= 0 or not sectors or candidate_period_days <= 0:
        raise ValueError("A positive TIC, at least one sector, and a positive period are required.")
    sources: list[dict[str, object]] = []
    matches: list[dict[str, object]] = []
    for url in _catalog_urls(sectors):
        rows = _tce_rows(url, tic_id)
        compact_rows: list[dict[str, object]] = []
        for row in rows:
            period = float(row["tce_period"])
            relation, fractional_error = _period_relation(period, candidate_period_days)
            compact = {
                "tce_id": row.get("tceid"),
                "sectors": row.get("sectors"),
                "period_days": period,
                "transit_time_btjd": float(row["tce_time0bt"]),
                "duration_hours": float(row["tce_duration"]),
                "depth_ppm": float(row["tce_depth"]),
                "model_snr": float(row["tce_model_snr"]),
                "relation_to_candidate": relation,
                "fractional_period_error": fractional_error,
            }
            compact_rows.append(compact)
            if fractional_error <= 0.01:
                matches.append({"catalog_url": url, **compact})
        sources.append({"url": url, "target_tce_count": len(rows), "target_tces": compact_rows})
    return {
        "source_index": TCE_INDEX,
        "tic_id": tic_id,
        "searched_sectors": sorted(set(sectors)),
        "candidate_period_days": candidate_period_days,
        "catalogs_checked": len(sources),
        "sources": sources,
        "matching_tces": matches,
        "candidate_period_absent_from_checked_tces": not matches,
        "warning": (
            "Absence from these public TCE tables is not proof of novelty. Check live "
            "ExoFOP holdings, DV reports, alternate pipelines, literature, and aliases."
        ),
    }
