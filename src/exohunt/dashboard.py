"""Export the append-only search ledger as a browser-friendly spatial dataset."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .statuses import (
    COMMON_MODE_LABELS,
    CONTEXT_LABELS,
    SCIENCE_LABELS,
    SCREENING_LABELS,
    STATUS_REGISTRY,
    resolve_status,
)


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


def _screening_class(result: dict[str, object]) -> str:
    explicit = str(result.get("screening_class") or "")
    if explicit in {
        "automated_survivor",
        "single_event_lead",
        "screened_rejected",
        "no_transit_detected",
        "search_error",
    }:
        return explicit
    status = str(result.get("status") or "")
    if status == "error":
        return "search_error"
    if status == "survivor":
        return "automated_survivor"
    reasons = {
        value.strip()
        for value in str(result.get("rejection_reasons") or "").split(";")
        if value.strip()
    }
    snr = _optional_float(result.get("depth_snr")) or 0.0
    if (
        "fewer than two transit events are represented" in reasons
        and snr >= 7.1
        and not reasons.intersection(
            {
                "odd and even transit depths differ by more than 3 sigma",
                "a secondary eclipse is detected above 3 sigma",
                "the fitted transit duty cycle exceeds 15 percent",
                "the fitted transit depth exceeds 5 percent",
            }
        )
    ):
        return "single_event_lead"
    if reasons == {"white-noise BLS depth S/N is below 7.1"}:
        return "no_transit_detected"
    if status == "rejected":
        return "screened_rejected"
    return "searched"


def _resolve_status_evidence(
    candidates: list[tuple[str, str, str]],
) -> tuple[str, str, str]:
    """Select the authoritative status while retaining its label and notes."""

    selected = resolve_status(status for status, _, _ in candidates)
    return next(
        candidate for candidate in reversed(candidates) if candidate[0] == selected
    )


def _common_mode_by_tic(results_root: Path) -> dict[int, dict[str, object]]:
    """Load the saved common-mode screen, newest file wins per TIC.

    The screen asks how many unrelated targets in the same campaign carry the
    same fitted ephemeris. A signal shared by hundreds of stars across several
    cameras was produced by the observatory, which no later per-star check can
    undo -- sector coherence in particular cannot, because a spacecraft event
    recurs identically in every sector.
    """

    screens: dict[int, dict[str, object]] = {}
    if not results_root.exists():
        return screens
    for path in sorted(results_root.rglob("common_mode_screen.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            verdicts = payload.get("verdicts", {})
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(verdicts, dict):
            continue
        for raw_tic, verdict in verdicts.items():
            if not isinstance(verdict, dict):
                continue
            try:
                tic_id = int(raw_tic)
            except (TypeError, ValueError):
                continue
            screens[tic_id] = {"screen_report": str(path), **verdict}
    return screens


def _common_mode_notes(screen: dict[str, object]) -> str:
    """State the shared-ephemeris evidence in plain language."""

    shared = screen.get("shared_targets")
    expected = _optional_float(screen.get("expected_shared_targets"))
    cameras = screen.get("cameras_spanned")
    spread = _optional_float(screen.get("sky_spread_deg"))
    notes: list[str] = []
    if isinstance(shared, int) and expected is not None:
        notes.append(
            f"{shared} unrelated targets share this ephemeris "
            f"({expected:.1f} expected by chance)"
        )
    if isinstance(cameras, int) and cameras > 0:
        notes.append(f"spanning {cameras} camera{'s' if cameras != 1 else ''}")
    if spread is not None and spread >= 1.0:
        notes.append(f"over {spread:.0f} degrees of sky")
    return "; ".join(notes)


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


# Retaining every campaign's full checkpoint parse was the largest memory
# consumer in a live coordinator, which runs this exporter on a thread every
# 120 s. A finished 64,614-target run's `batch_progress.json` is 116 MB of JSON
# that expands to several GB of Python objects, and `coverage_artifacts` held
# one of those per campaign simultaneously. Measured 2026-08-16, mid-run: the
# coordinator's working set was 8.2 GB with 1.4 GB of physical memory free and
# 41.7 GB committed against 31.9 GB of RAM, and *both* the download and the
# analysis median had roughly doubled -- the machine was paging, and throughput
# had halved from 3,488 to 1,808 stars/hour.
#
# `_sector_coverage` reads four fields per artifact and a handful per result
# row; everything else was retained for nothing. Projecting to what is actually
# read, and caching that projection on (mtime, size) because a finished
# campaign's checkpoint never changes again, removes the re-parse and the
# retention together. Active campaigns are deliberately never cached: their file
# changes every few seconds, so a hit would serve stale coverage.
_COVERAGE_FIELDS = ("state", "target_list", "updated_at_utc", "total_targets")
# `_tic_id` accepts three spellings and `_sectors` two, so all of them survive
# the projection or coverage would silently lose rows.
_COVERAGE_ROW_FIELDS = ("tic_id", "TICID", "ID", "sectors", "sector", "status")
_ACTIVE_STATES = frozenset({"running", "finalizing", "retry_pending"})
_coverage_cache: dict[Path, tuple[int, int, dict[str, object]]] = {}


def _coverage_projection(progress: dict[str, object]) -> dict[str, object]:
    """Reduce a campaign checkpoint to the fields `_sector_coverage` reads."""

    projected: dict[str, object] = {
        key: progress.get(key) for key in _COVERAGE_FIELDS
    }
    rows: list[dict[str, object]] = []
    for row in progress.get("results", []):
        if not isinstance(row, dict):
            continue
        rows.append({key: row[key] for key in _COVERAGE_ROW_FIELDS if key in row})
    projected["results"] = rows
    return projected


def _cached_coverage(path: Path) -> dict[str, object] | None:
    """Return a previously projected artifact when the file has not changed."""

    try:
        stat = path.stat()
    except OSError:
        return None
    cached = _coverage_cache.get(path)
    if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    return None


def _store_coverage(path: Path, projection: dict[str, object]) -> None:
    try:
        stat = path.stat()
    except OSError:
        return
    _coverage_cache[path] = (stat.st_mtime_ns, stat.st_size, projection)


def _sector_coverage(
    root: Path,
    artifacts: dict[Path, dict[str, object]],
) -> list[dict[str, object]]:
    """Summarize local target-plan completion without claiming sky completeness.

    A TESS sector contains far more stars than this project targets. Coverage
    therefore uses unique TICs in campaign target lists as the denominator and
    successful, non-error result rows as the numerator. Blue/completed is
    reserved for an entirely successful sector-scoped local target plan.
    """

    targeted: dict[int, set[int]] = {}
    analyzed: dict[int, set[int]] = {}
    sector_scoped: set[int] = set()
    active: dict[int, dict[str, object]] = {}

    for artifact_dir, artifact in artifacts.items():
        results = [
            row
            for row in artifact.get("results", [])
            if isinstance(row, dict)
        ]
        result_sectors_by_tic = {
            tic_id: _sectors(row.get("sectors"))
            for row in results
            if (tic_id := _tic_id(row)) is not None
        }
        target_rows: list[dict[str, object]] = []
        target_text = str(artifact.get("target_list") or "")
        target_path = root / target_text if target_text else None
        if target_path is not None and target_path.exists():
            try:
                with target_path.open(newline="", encoding="utf-8-sig") as handle:
                    target_rows = [
                        dict(row)
                        for row in csv.DictReader(handle)
                    ]
            except (OSError, csv.Error, UnicodeDecodeError):
                target_rows = []

        campaign_targets: dict[int, set[int]] = {}
        rows_for_plan = target_rows or results
        for row in rows_for_plan:
            tic_id = _tic_id(row)
            if tic_id is None:
                continue
            row_sectors = _sectors(row.get("sectors") or row.get("sector"))
            if not row_sectors:
                row_sectors = result_sectors_by_tic.get(tic_id, [])
            for sector in row_sectors:
                campaign_targets.setdefault(sector, set()).add(tic_id)
                targeted.setdefault(sector, set()).add(tic_id)

        campaign_sector_values = set(campaign_targets)
        if len(campaign_sector_values) == 1:
            sector_scoped.update(campaign_sector_values)

        campaign_analyzed: dict[int, set[int]] = {}
        for row in results:
            if str(row.get("status") or "") == "error":
                continue
            tic_id = _tic_id(row)
            if tic_id is None:
                continue
            row_sectors = _sectors(row.get("sectors"))
            if not row_sectors:
                row_sectors = result_sectors_by_tic.get(tic_id, [])
            for sector in row_sectors:
                campaign_analyzed.setdefault(sector, set()).add(tic_id)
                analyzed.setdefault(sector, set()).add(tic_id)

        state = str(artifact.get("state") or "completed")
        if state in {"running", "finalizing"}:
            updated = str(artifact.get("updated_at_utc") or "")
            for sector, tic_ids in campaign_targets.items():
                campaign_total = (
                    int(artifact.get("total_targets", 0))
                    if len(campaign_targets) == 1
                    else 0
                )
                candidate = {
                    "campaign": artifact_dir.name,
                    "targeted_stars": max(len(tic_ids), campaign_total),
                    "analyzed_stars": len(campaign_analyzed.get(sector, set())),
                    "updated_at_utc": updated,
                }
                previous = active.get(sector)
                if previous is None or updated >= str(
                    previous.get("updated_at_utc") or ""
                ):
                    active[sector] = candidate

    coverage: list[dict[str, object]] = []
    for sector in sorted(set(targeted) | set(analyzed) | set(active)):
        active_campaign = active.get(sector)
        if active_campaign is not None:
            targeted_count = int(active_campaign["targeted_stars"])
            analyzed_count = int(active_campaign["analyzed_stars"])
            state = "active"
        else:
            targeted_count = len(targeted.get(sector, set()))
            analyzed_count = len(analyzed.get(sector, set()))
            if (
                sector in sector_scoped
                and targeted_count > 0
                and analyzed_count >= targeted_count
            ):
                state = "completed"
            elif analyzed_count > 0:
                state = "partial"
            else:
                state = "unsearched"
        progress = (
            min(1.0, analyzed_count / targeted_count)
            if targeted_count > 0
            else 0.0
        )
        coverage.append(
            {
                "sector": sector,
                "state": state,
                "targeted_stars": targeted_count,
                "analyzed_stars": analyzed_count,
                "progress_fraction": round(progress, 6),
                "active_campaign": (
                    active_campaign.get("campaign")
                    if active_campaign is not None
                    else None
                ),
                "updated_at_utc": (
                    active_campaign.get("updated_at_utc")
                    if active_campaign is not None
                    else None
                ),
                "scope": "local campaign target plan, not every star in the TESS sector",
            }
        )
    return coverage


def _parse_utc(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _vetting_progress_campaign(
    progress_path: Path,
    progress: dict[str, object],
    *,
    workflow: str,
) -> dict[str, object]:
    """Normalize context/science checkpoints for the live dashboard panel."""

    total = int(progress.get("total_targets", 0))
    completed = int(progress.get("completed_targets", 0))
    runtime = progress.get("runtime", {})
    runtime = runtime if isinstance(runtime, dict) else {}
    counts = progress.get("counts", {})
    counts = counts if isinstance(counts, dict) else {}
    if not counts:
        # The single-threaded science runner records flat totals instead of the
        # nested counts/runtime blocks the concurrent context runner writes.
        counts = {
            "completed": completed,
            "error": int(progress.get("error_targets", 0)),
            "remaining": int(
                progress.get("remaining_targets", max(0, total - completed))
            ),
        }
    remaining = int(counts.get("remaining", max(0, total - completed)))
    workers = int(runtime.get("workers", 1))
    products = int(
        runtime.get(
            "science_products_downloaded",
            progress.get("science_products_downloaded", 0),
        )
        or 0
    )
    started = _parse_utc(progress.get("started_at_utc"))
    updated = _parse_utc(progress.get("updated_at_utc"))
    elapsed_hours = (
        max(0.0, (updated - started).total_seconds() / 3600.0)
        if started is not None and updated is not None
        else 0.0
    )
    average_rate = completed / elapsed_hours if elapsed_hours > 0 else None
    eta_hours = (
        remaining / average_rate
        if average_rate is not None and average_rate > 0
        else None
    )
    estimated_completion = (
        (updated + timedelta(hours=eta_hours))
        .replace(microsecond=0)
        .isoformat()
        if updated is not None and eta_hours is not None
        else None
    )
    scope_name = (
        progress_path.parent.parent.name
        if progress_path.parent.parent.name
        else progress_path.parent.name
    )
    return {
        "name": f"{scope_name}_{workflow}",
        "workflow": workflow,
        "state": progress.get("state"),
        "target_list": progress.get("queue"),
        "sectors": [],
        "total_targets": total,
        "completed_targets": completed,
        "counts": counts,
        "runtime": {
            "analysis_workers": workers,
            "download_workers": 0,
            "prefetch_targets": 0,
            "downloads_in_flight": 0,
            "analyses_in_flight": min(workers, remaining)
            if progress.get("state") == "running"
            else 0,
            "downloaded_waiting": 0,
            "targets_remaining": remaining,
            "science_products_downloaded": products,
            "performance": {
                "average_stars_per_hour": average_rate,
                "rolling_stars_per_hour": None,
                "rolling_window_minutes": None,
                "rolling_samples": 0,
                "elapsed_hours": elapsed_hours,
                "eta_hours": eta_hours,
                "estimated_completion_utc": estimated_completion,
            },
            "vetting_coverage": {
                "eligible_targets": total,
                "measured_targets": completed,
                "legacy_unmeasured_targets": 0,
                "coverage_fraction": completed / total if total else None,
                "warning": None,
            },
        },
        "started_at_utc": progress.get("started_at_utc"),
        "updated_at_utc": progress.get("updated_at_utc"),
    }


def _science_disposition(
    on_target: bool | None, gate_passed: bool | None
) -> str | None:
    """Classify a lead from its two measured science gates.

    Difference-image localization is the stronger statement: light lost more
    than one TESS pixel from the target did not come from the target, so that
    verdict is reported ahead of sector coherence. A signal is only promoted
    when both gates were measured and both passed, and even then it remains a
    lead rather than a planet candidate.
    """

    if on_target is False:
        return "pixel_offset_contamination"
    if on_target is None or gate_passed is None:
        return None
    return "science_vetted_lead" if gate_passed else "single_sector_unconfirmed"


def _is_survey_source(name: str) -> bool:
    """Report whether a results filename is an input to the survey snapshot."""

    return name.endswith(
        (
            "_progress.json",
            "batch_summary.json",
            "_sector_vet.json",
            "_pixel.json",
            "common_mode_screen.json",
            "_cross_mission_context.json",
        )
    )


def survey_source_mtime_ns(root: str | Path = ".") -> int:
    """Return the newest mtime among every input the snapshot derives from.

    ``os.scandir`` is used instead of ``Path.rglob`` plus ``Path.stat`` because
    a directory entry already carries its timestamp, so a large vetting tree
    costs one enumeration rather than an extra stat call per file.
    """

    base = Path(root)
    newest = 0
    for path in (
        base / "metrics" / "events.jsonl",
        base / "metrics" / "current_stats.json",
    ):
        try:
            newest = max(newest, path.stat().st_mtime_ns)
        except OSError:
            continue
    pending = [base / "results" / "campaign", base / "results" / "vetting"]
    while pending:
        try:
            entries = list(os.scandir(pending.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif _is_survey_source(entry.name):
                    newest = max(newest, entry.stat().st_mtime_ns)
            except OSError:
                continue
    return newest


def _science_notes(science: dict[str, object]) -> str:
    """Describe the measured science gates in plain language."""

    notes: list[str] = []
    offset = _optional_float(science.get("science_centroid_offset_arcsec"))
    if science.get("science_on_target") is False:
        notes.append(
            f"Difference-image centroid is {offset:.0f} arcsec from the target"
            if offset is not None
            else "Difference-image centroid is more than one pixel from the target"
        )
    elif science.get("science_on_target") is True:
        notes.append(
            f"Difference-image centroid is on target within {offset:.0f} arcsec"
            if offset is not None
            else "Difference-image centroid is on target within one pixel"
        )
    supported = science.get("science_supported_sector_count")
    tested = science.get("science_sectors_tested")
    if isinstance(supported, int) and isinstance(tested, int) and tested:
        notes.append(
            f"{supported} of {tested} tested sectors support the fixed ephemeris"
        )
    return "; ".join(notes)


def _science_vetting_by_tic(results_root: Path) -> dict[int, dict[str, object]]:
    """Collect pixel-localization and sector-coherence verdicts per TIC.

    These reports are the deepest stage this project runs. They are written by
    ``pixel-vet``/``sector-vet`` under a science queue directory and were
    previously invisible to the dashboard, which left every science-vetted star
    displaying its earlier metadata-only classification.
    """

    science: dict[int, dict[str, object]] = {}
    if not results_root.exists():
        return science

    # The same target can be vetted more than once into different output
    # directories. Keep the most recent verdict rather than whichever path
    # happens to sort last.
    newest_sector: dict[int, int] = {}
    newest_pixel: dict[int, int] = {}

    for path in sorted(results_root.rglob("*_sector_vet.json")):
        try:
            modified = path.stat().st_mtime_ns
            report = json.loads(path.read_text(encoding="utf-8"))
            tic_id = int(report.get("tic_id") or 0)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if tic_id <= 0 or modified < newest_sector.get(tic_id, -1):
            continue
        newest_sector[tic_id] = modified
        sectors = [
            row for row in report.get("sectors", []) if isinstance(row, dict)
        ]
        row = science.setdefault(tic_id, {})
        row.update(
            {
                "science_sector_gate_passed": bool(
                    report.get("passes_distinct_sector_gate")
                ),
                "science_supported_sector_count": int(
                    report.get("supported_sector_count") or 0
                ),
                "science_minimum_supporting_sectors": int(
                    report.get("minimum_supporting_sectors") or 0
                ),
                "science_sectors_tested": len(sectors),
                "science_supporting_sectors": sorted(
                    int(entry["sector"])
                    for entry in sectors
                    if entry.get("supports_signal") and entry.get("sector") is not None
                ),
                "science_sector_report": str(path),
            }
        )

    for path in sorted(results_root.rglob("*_pixel.json")):
        try:
            modified = path.stat().st_mtime_ns
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        tic_id = _tic_id(report)
        if tic_id is None:
            match = re.search(r"TIC[_ ]?(\d+)", str(report.get("target") or ""))
            tic_id = int(match.group(1)) if match else None
        if tic_id is None or tic_id <= 0:
            continue
        if "on_target_within_one_pixel" not in report:
            continue
        if modified < newest_pixel.get(tic_id, -1):
            continue
        newest_pixel[tic_id] = modified
        row = science.setdefault(tic_id, {})
        row.update(
            {
                "science_on_target": bool(report.get("on_target_within_one_pixel")),
                "science_centroid_offset_pixels": _optional_float(
                    report.get("centroid_offset_pixels")
                ),
                "science_centroid_offset_arcsec": _optional_float(
                    report.get("centroid_offset_arcsec_approx")
                ),
                "science_pixel_sector": (
                    int(report["sector"])
                    if str(report.get("sector") or "").strip().lstrip("-").isdigit()
                    else None
                ),
                "science_pixel_report": str(path),
            }
        )

    for tic_id, row in science.items():
        disposition = _science_disposition(
            row.get("science_on_target"),  # type: ignore[arg-type]
            row.get("science_sector_gate_passed"),  # type: ignore[arg-type]
        )
        row["science_disposition"] = disposition
        row["science_vetted"] = disposition is not None
    return science


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

    # Sampled before anything is read so a checkpoint written mid-export is
    # recorded as newer than this snapshot and triggers one more refresh.
    source_mtime_ns = survey_source_mtime_ns(root)

    ledger_path = root / "metrics" / "events.jsonl"
    if events is None:
        events = (
            [
                json.loads(line)
                for line in ledger_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if ledger_path.exists()
            else []
        )
    if stats is None:
        stats_path = root / "metrics" / "current_stats.json"
        stats = (
            json.loads(stats_path.read_text(encoding="utf-8"))
            if stats_path.exists()
            else {"campaign_runs_logged": 0}
        )

    invalidated = {
        str(event["invalidates_event_id"])
        for event in events
        if event.get("kind") == "event_invalidated"
        and event.get("invalidates_event_id")
    }
    active = [event for event in events if event.get("event_id") not in invalidated]

    active_campaigns: list[dict[str, object]] = []
    active_results: list[dict[str, object]] = []
    coverage_artifacts: dict[Path, dict[str, object]] = {}
    results_root = root / "results"
    if results_root.exists():
        for progress_path in sorted(results_root.rglob("batch_progress.json")):
            # A finished campaign contributes coverage and nothing else, so once
            # its projection is cached the 116 MB parse never happens again.
            reused = _cached_coverage(progress_path)
            if reused is not None:
                coverage_artifacts[progress_path.parent] = reused
                continue
            try:
                progress = json.loads(progress_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            projection = _coverage_projection(progress)
            coverage_artifacts[progress_path.parent] = projection
            if progress.get("state") not in _ACTIVE_STATES:
                _store_coverage(progress_path, projection)
                # Drop the full parse before the next file is read, so peak
                # memory is one checkpoint rather than all of them at once.
                del progress
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
                    "runtime": progress.get("runtime", {}),
                    "started_at_utc": progress.get("started_at_utc"),
                    "updated_at_utc": progress.get("updated_at_utc"),
                }
            )
        for filename, workflow in (
            ("context_vet_progress.json", "context_vet"),
            ("science_vet_progress.json", "science_vet"),
        ):
            for progress_path in sorted(results_root.rglob(filename)):
                try:
                    progress = json.loads(
                        progress_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    continue
                if progress.get("state") not in {
                    "running",
                    "finalizing",
                    "retry_pending",
                }:
                    continue
                active_campaigns.append(
                    _vetting_progress_campaign(
                        progress_path,
                        progress,
                        workflow=workflow,
                    )
                )

    # Keep a live coordinator last for compatibility with already-loaded
    # dashboards that historically selected the final checkpoint in this list.
    # Within the same state class, the freshest checkpoint wins.
    active_campaigns.sort(
        key=lambda campaign: (
            campaign.get("state") in {"running", "finalizing"},
            str(campaign.get("updated_at_utc") or ""),
        )
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
        # Projected for the same reason as the checkpoints above: coverage reads
        # a few fields and this retains one dict per campaign for the whole
        # export. The full `summary` is still used below, but only in this loop.
        if summary_path.parent not in coverage_artifacts:
            coverage_artifacts[summary_path.parent] = _coverage_projection(summary)
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
                "screening_class": _screening_class(result),
                "rejection_reasons": result.get("rejection_reasons", ""),
                "followup_priority": int(result.get("followup_priority", 0)),
                "followup_reasons": result.get("followup_reasons", ""),
                "vetting_tier": result.get(
                    "vetting_tier", "legacy_unmeasured"
                ),
                "deeper_vetting_flags": result.get(
                    "deeper_vetting_flags", ""
                ),
                "recommended_data_sources": result.get(
                    "recommended_data_sources", ""
                ),
                "planet_free": False,
                "sensitivity_3d_ppm": _optional_float(
                    result.get("sensitivity_3d_ppm")
                ),
                "sensitivity_12d_ppm": _optional_float(
                    result.get("sensitivity_12d_ppm")
                ),
                "red_noise_adjusted_snr": _optional_float(
                    result.get("red_noise_adjusted_snr")
                ),
                "event_coverage_fraction": _optional_float(
                    result.get("event_coverage_fraction")
                ),
                "positive_depth_event_fraction": _optional_float(
                    result.get("positive_depth_event_fraction")
                ),
                "sectors": row_sectors,
                "phase_curve_available": bool(
                    result.get("phase_curve_available", False)
                ),
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
            "screening_class": _screening_class(result),
            "rejection_reasons": result.get("rejection_reasons", ""),
            "followup_priority": int(result.get("followup_priority", 0)),
            "followup_reasons": result.get("followup_reasons", ""),
            "vetting_tier": result.get("vetting_tier", "legacy_unmeasured"),
            "deeper_vetting_flags": result.get("deeper_vetting_flags", ""),
            "recommended_data_sources": result.get(
                "recommended_data_sources", ""
            ),
            "planet_free": False,
            "sensitivity_3d_ppm": _optional_float(
                result.get("sensitivity_3d_ppm")
            ),
            "sensitivity_12d_ppm": _optional_float(
                result.get("sensitivity_12d_ppm")
            ),
            "red_noise_adjusted_snr": _optional_float(
                result.get("red_noise_adjusted_snr")
            ),
            "event_coverage_fraction": _optional_float(
                result.get("event_coverage_fraction")
            ),
            "positive_depth_event_fraction": _optional_float(
                result.get("positive_depth_event_fraction")
            ),
            "sectors": row_sectors,
            "phase_curve_available": bool(
                result.get("phase_curve_available", False)
            ),
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

    context_by_tic: dict[int, dict[str, object]] = {}
    if results_root.exists():
        for context_path in sorted(
            results_root.rglob("TIC_*_cross_mission_context.json")
        ):
            try:
                context = json.loads(context_path.read_text(encoding="utf-8"))
                tic = context.get("tic", {})
                classification = context.get("context_classification", {})
                tic_id = int(tic.get("tic_id", 0))
            except (
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                continue
            if (
                tic_id <= 0
                or not isinstance(classification, dict)
                or classification.get("disposition") not in CONTEXT_LABELS
            ):
                continue
            previous = context_by_tic.get(tic_id)
            generated = str(context.get("generated_at_utc") or "")
            if previous is None or generated >= str(
                previous.get("generated_at_utc") or ""
            ):
                context_by_tic[tic_id] = {
                    "generated_at_utc": generated,
                    "report": str(context_path),
                    **classification,
                }

    science_by_tic = _science_vetting_by_tic(results_root)
    common_mode_by_tic = _common_mode_by_tic(results_root)

    stars: list[dict[str, object]] = []
    for tic_id in searched_ids:
        row = metadata[tic_id]
        ra = _optional_float(row.get("ra_deg"))
        dec = _optional_float(row.get("dec_deg"))
        distance = _optional_float(row.get("distance_pc"))
        direction_is_estimated = ra is None or dec is None
        distance_is_estimated = distance is None or distance <= 0
        if ra is None or dec is None:
            ra, dec = _deterministic_direction(tic_id)
        if distance is None or distance <= 0:
            distance = 35.0 + tic_id % 110
        if direction_is_estimated:
            coordinate_source = "Estimated display direction and distance"
        elif distance_is_estimated:
            coordinate_source = "TIC sky position; estimated display distance"
        else:
            coordinate_source = "TIC sky position and distance"

        signal = signals.get(tic_id, {})
        screening_class = str(signal.get("screening_class") or "searched")
        status = screening_class if screening_class in SCREENING_LABELS else "searched"
        label = SCREENING_LABELS[status]
        notes = str(signal.get("followup_reasons") or "")
        status_evidence = [(status, label, notes)]
        context = context_by_tic.get(tic_id)
        if context is not None:
            disposition = str(context.get("disposition"))
            if disposition in CONTEXT_LABELS:
                status_evidence.append(
                    (
                        disposition,
                        CONTEXT_LABELS[disposition],
                        "; ".join(
                            str(value)
                            for value in context.get("reasons", [])
                            if str(value).strip()
                        ),
                    )
                )
        science = science_by_tic.get(tic_id)
        if science is not None:
            disposition = science.get("science_disposition")
            if disposition is not None and str(disposition) in SCIENCE_LABELS:
                science_status = str(disposition)
                status_evidence.append(
                    (
                        science_status,
                        SCIENCE_LABELS[science_status],
                        _science_notes(science),
                    )
                )
        screen = common_mode_by_tic.get(tic_id)
        if screen is not None:
            verdict = str(screen.get("verdict") or "")
            if verdict in COMMON_MODE_LABELS:
                status_evidence.append(
                    (
                        verdict,
                        COMMON_MODE_LABELS[verdict],
                        _common_mode_notes(screen),
                    )
                )
        for outcome in outcomes.get(tic_id, []):
            kind = str(outcome.get("kind"))
            if kind not in STATUS_REGISTRY:
                continue
            current_label = _resolve_status_evidence(status_evidence)[1]
            status_evidence.append(
                (
                    kind,
                    str(outcome.get("label") or current_label),
                    str(outcome.get("notes") or ""),
                )
            )
        status, label, notes = _resolve_status_evidence(status_evidence)
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
            "distance_is_estimated": distance_is_estimated,
            "direction_is_estimated": direction_is_estimated,
            "coordinate_source": coordinate_source,
            "tmag": _optional_float(row.get("tmag")),
            "teff_k": _optional_float(row.get("teff_k")),
            "stellar_radius_solar": _optional_float(row.get("stellar_radius_solar")),
            "sectors": sectors,
            "phase_curve_available": False,
            "context_disposition": (
                context.get("disposition") if context is not None else None
            ),
            "context_followup_lane": (
                context.get("followup_lane") if context is not None else None
            ),
            "context_source_states": (
                context.get("source_states") if context is not None else {}
            ),
            "context_report": (
                context.get("report") if context is not None else None
            ),
            "science_vetted": bool(science.get("science_vetted")) if science else False,
            "science_disposition": (
                science.get("science_disposition") if science is not None else None
            ),
            "science_on_target": (
                science.get("science_on_target") if science is not None else None
            ),
            "science_centroid_offset_arcsec": (
                science.get("science_centroid_offset_arcsec")
                if science is not None
                else None
            ),
            "science_sector_gate_passed": (
                science.get("science_sector_gate_passed")
                if science is not None
                else None
            ),
            "science_supported_sector_count": (
                science.get("science_supported_sector_count")
                if science is not None
                else None
            ),
            "science_sectors_tested": (
                science.get("science_sectors_tested") if science is not None else None
            ),
            "science_supporting_sectors": (
                science.get("science_supporting_sectors", [])
                if science is not None
                else []
            ),
            "common_mode_verdict": (
                screen.get("verdict") if screen is not None else None
            ),
            "common_mode_shared_targets": (
                screen.get("shared_targets") if screen is not None else None
            ),
            "common_mode_expected_targets": (
                screen.get("expected_shared_targets") if screen is not None else None
            ),
            "common_mode_enrichment": (
                screen.get("enrichment") if screen is not None else None
            ),
            "common_mode_cameras_spanned": (
                screen.get("cameras_spanned") if screen is not None else None
            ),
            "common_mode_sky_spread_deg": (
                screen.get("sky_spread_deg") if screen is not None else None
            ),
            "spacecraft_harmonic": (
                screen.get("spacecraft_harmonic") if screen is not None else None
            ),
            "spacecraft_harmonic_period_days": (
                screen.get("spacecraft_harmonic_period_days")
                if screen is not None
                else None
            ),
            "duration_at_grid_rail": (
                bool(screen.get("duration_at_grid_rail")) if screen is not None else False
            ),
            "period_at_search_ceiling": (
                bool(screen.get("period_at_search_ceiling"))
                if screen is not None
                else False
            ),
            **signal,
            **_cartesian(ra, dec, distance),
        }
        stars.append(star)

    counts: dict[str, int] = {}
    for star in stars:
        key = str(star["status"])
        counts[key] = counts.get(key, 0) + 1

    vetted_science = [row for row in science_by_tic.values() if row.get("science_vetted")]
    science_summary = {
        "vetted_targets": len(vetted_science),
        "on_target": sum(row.get("science_on_target") is True for row in vetted_science),
        "off_target": sum(
            row.get("science_on_target") is False for row in vetted_science
        ),
        "sector_gate_passed": sum(
            row.get("science_sector_gate_passed") is True for row in vetted_science
        ),
        "passed_both_gates": sum(
            row.get("science_disposition") == "science_vetted_lead"
            for row in vetted_science
        ),
        "scope": (
            "Measured pixel localization and fixed-ephemeris sector coherence. "
            "Passing both gates is a screening result, not a planet candidate."
        ),
    }
    screened = [row for row in common_mode_by_tic.values() if row.get("verdict")]
    flagged = [
        row for row in screened if row.get("verdict") in COMMON_MODE_LABELS
    ]
    common_mode_summary = {
        "screened_targets": len(screened),
        "flagged_targets": len(flagged),
        "observatory_systematic": sum(
            row.get("verdict") == "common_mode_systematic" for row in screened
        ),
        "localized_coincidence": sum(
            row.get("verdict") == "localized_coincidence" for row in screened
        ),
        "flagged_fraction": (
            round(len(flagged) / len(screened), 4) if screened else None
        ),
        "on_spacecraft_harmonic": sum(
            bool(row.get("spacecraft_harmonic")) for row in screened
        ),
        "duration_at_grid_rail": sum(
            bool(row.get("duration_at_grid_rail")) for row in screened
        ),
        "period_at_search_ceiling": sum(
            bool(row.get("period_at_search_ceiling")) for row in screened
        ),
        "scope": (
            "Fraction of searched targets whose fitted ephemeris is shared by "
            "many unrelated stars observed at the same time. Sharing is evidence "
            "the observatory produced the dimming, not the star."
        ),
    }
    payload = {
        "schema_version": 2,
        "generated_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "source_mtime_ns": source_mtime_ns,
        "stats": stats,
        "status_counts": counts,
        "science_vetting": science_summary,
        "common_mode_screen": common_mode_summary,
        "observed_sectors": sorted(observed_sectors),
        "sector_coverage": _sector_coverage(root, coverage_artifacts),
        "campaigns": campaigns,
        "active_campaigns": active_campaigns,
        "stars": stars,
        "warnings": [
            "Automated survivors are not planet candidates.",
            "No transit detected in a searched window does not mean planet-free.",
            "Single-event leads require a longer observing baseline.",
            "A rediscovery is explicitly not a new planet.",
            "A known binary host can still retain a separate residual/circumbinary follow-up lane.",
            "An unresolved context-vetted signal is still not a planet candidate.",
            "Passing pixel localization and sector coherence is a screening result, not a planet candidate.",
            "An off-target difference-image centroid indicates the light was lost near, not necessarily at, the target.",
            "An ephemeris shared by many unrelated stars was produced by the observatory, not by any of them.",
            "Surviving the shared-ephemeris screen is not evidence that a signal is a planet.",
            "Display-fallback coordinates are labeled and should be replaced by TIC data.",
        ],
    }
    output = dashboard / "public" / "data" / "survey.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(
        f"{output.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    # Written compactly: this is a derived browser payload, not a file anyone
    # reads. Indentation roughly doubled its size, and the browser refetches it
    # every few seconds.
    temporary.write_text(
        json.dumps(payload, separators=(",", ":")), encoding="utf-8"
    )
    for attempt in range(8):
        try:
            temporary.replace(output)
            break
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.05 * (2**attempt))
    return output
