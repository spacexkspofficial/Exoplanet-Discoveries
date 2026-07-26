"""Build and run the bounded all-campaign TESS science follow-up queue.

This runner is intentionally conservative:

* it requires a complete, zero-error metadata context pass;
* it admits only unresolved transit-like signals with complete source states;
* it processes one EXOHUNT command at a time;
* it validates durable sector/pixel reports before reusing them; and
* it prunes storage and checkpoints atomically after every target.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


MAX_WORKSPACE_BYTES = 20_000_000_000
QUEUE_CAP = 100
VALID_SOURCE_STATES = {"completed", "not_available"}
SIGNAL_KEYS = ("period_days", "transit_time", "duration_hours")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected a JSON object in {path}.")
    return payload


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


def workspace_bytes(root: Path) -> int:
    total = 0
    for directory, _, filenames in os.walk(root):
        for filename in filenames:
            try:
                total += (Path(directory) / filename).stat().st_size
            except OSError:
                continue
    return total


def sector_values(value: Any) -> list[int]:
    if value is None:
        return []
    values: Iterable[Any]
    if isinstance(value, str):
        values = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = [value]
    sectors: set[int] = set()
    for raw in values:
        try:
            sector = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if sector > 0:
            sectors.add(sector)
    return sorted(sectors)


def signal_from_source(source: dict[str, Any]) -> dict[str, Any]:
    signal = source.get("strongest_residual_signal") or source.get(
        "strongest_signal"
    )
    if not isinstance(signal, dict):
        raise RuntimeError("Source report has no strongest signal.")
    return signal


def signals_match(left: Any, right: Any) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    for key in SIGNAL_KEYS:
        try:
            a = float(left[key])
            b = float(right[key])
        except (KeyError, TypeError, ValueError):
            return False
        if not math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-10):
            return False
    return True


def same_path(left: Any, right: Path, root: Path) -> bool:
    if not left:
        return False
    raw = Path(str(left))
    resolved = raw if raw.is_absolute() else root / raw
    return resolved.resolve() == right.resolve()


def choose_sectors(
    discovery_sectors: list[int], available_sectors: list[int]
) -> tuple[int, list[int]]:
    if not discovery_sectors:
        raise RuntimeError("No discovery sector is recorded.")
    discovery = discovery_sectors[0]
    independent = [
        sector for sector in available_sectors if sector not in discovery_sectors
    ]
    later = sorted(
        (sector for sector in independent if sector > max(discovery_sectors)),
        reverse=True,
    )
    earlier = sorted(
        (sector for sector in independent if sector <= max(discovery_sectors)),
        reverse=True,
    )
    selected = [discovery, *(later + earlier)[:2]]
    return discovery, selected


def complete_source_states(classification: dict[str, Any]) -> bool:
    states = classification.get("source_states")
    if not isinstance(states, dict) or not states:
        return False
    required = {
        "tess_eclipsing_binary_catalog",
        "simbad",
        "gaia_dr3",
        "tess_tce",
    }
    if not required.issubset(states):
        return False
    return all(str(value) in VALID_SOURCE_STATES for value in states.values())


def build_queue(
    root: Path,
    context_queue_path: Path,
    context_summary_path: Path,
    context_dir: Path,
    queue_path: Path,
) -> dict[str, Any]:
    context_queue = read_json(context_queue_path)
    context_summary = read_json(context_summary_path)
    counts = context_summary.get("counts")
    if not isinstance(counts, dict):
        raise RuntimeError("Context summary has no counts.")
    if (
        context_summary.get("state") != "completed"
        or int(counts.get("error", -1)) != 0
        or int(counts.get("completed", -1))
        != int(context_summary.get("total_targets", -2))
    ):
        raise RuntimeError("Context vetting is not complete with zero errors.")

    raw_targets = context_queue.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise RuntimeError("Context queue has no targets.")

    eligible: list[dict[str, Any]] = []
    for row in raw_targets:
        if not isinstance(row, dict) or not row.get("tic_id"):
            continue
        tic_id = int(row["tic_id"])
        context_path = context_dir / f"TIC_{tic_id}_cross_mission_context.json"
        if not context_path.exists():
            raise RuntimeError(f"Missing context report for TIC {tic_id}.")
        context = read_json(context_path)
        classification = context.get("context_classification")
        if not isinstance(classification, dict):
            raise RuntimeError(f"Missing classification for TIC {tic_id}.")
        if classification.get("disposition") != "unresolved_transit_like_signal":
            continue
        if not complete_source_states(classification):
            continue

        initial_priority = int(row.get("followup_priority") or 0)
        context_priority = int(classification.get("followup_priority") or 0)
        priority = max(initial_priority, context_priority)
        if priority < 75:
            continue

        source_raw = row.get("report")
        if not source_raw:
            reports = row.get("source_reports")
            source_raw = reports[0] if isinstance(reports, list) and reports else None
        if not source_raw:
            raise RuntimeError(f"TIC {tic_id} has no source report.")
        source_path = Path(str(source_raw))
        source_path = source_path if source_path.is_absolute() else root / source_path
        if not source_path.exists():
            raise RuntimeError(f"Missing source report for TIC {tic_id}: {source_path}")
        source = read_json(source_path)
        source_tic = int(source.get("data", {}).get("tic_id") or 0)
        if source_tic != tic_id:
            raise RuntimeError(f"Source report TIC mismatch for TIC {tic_id}.")

        source_data = source.get("data", {})
        discovery_sectors = sector_values(
            source_data.get("requested_sectors")
            or source_data.get("downloaded_sectors")
            or row.get("sectors")
        )
        mast = context.get("mast_holdings", {})
        tess = mast.get("tess", {}) if isinstance(mast, dict) else {}
        available_sectors = sector_values(
            tess.get("all_sectors") if isinstance(tess, dict) else None
        )
        discovery_sector, selected_sectors = choose_sectors(
            discovery_sectors, available_sectors
        )
        eligible.append(
            {
                "tic_id": tic_id,
                "target": str(row.get("target") or f"TIC {tic_id}"),
                "priority": priority,
                "initial_followup_priority": initial_priority,
                "context_followup_priority": context_priority,
                "source_report": str(source_path.relative_to(root)),
                "context_report": str(context_path.relative_to(root)),
                "discovery_sector": discovery_sector,
                "initial_sectors": discovery_sectors,
                "available_tess_sectors": available_sectors,
                "selected_sectors": selected_sectors,
                "signal": signal_from_source(source),
                "source_states": classification.get("source_states"),
                "context_disposition": classification.get("disposition"),
            }
        )

    eligible.sort(key=lambda row: (-int(row["priority"]), int(row["tic_id"])))
    selected = eligible[:QUEUE_CAP]
    payload = {
        "schema_version": 1,
        "generated_at_utc": utc_now(),
        "warning": (
            "This is a bounded science-vetting queue, not a planet-candidate list. "
            "Lower-ranked eligible targets beyond the cap remain deferred, not rejected."
        ),
        "source_context_queue": str(context_queue_path.relative_to(root)),
        "source_context_summary": str(context_summary_path.relative_to(root)),
        "criteria": {
            "context_disposition": "unresolved_transit_like_signal",
            "source_states": sorted(VALID_SOURCE_STATES),
            "minimum_max_followup_priority": 75,
            "sort": "priority descending, TIC ID ascending",
            "first_pass_cap": QUEUE_CAP,
        },
        "eligible_targets_before_cap": len(eligible),
        "deferred_by_cap": max(0, len(eligible) - len(selected)),
        "total_targets": len(selected),
        "targets": selected,
    }
    if queue_path.exists():
        existing = read_json(queue_path)
        existing_ids = [int(row["tic_id"]) for row in existing.get("targets", [])]
        selected_ids = [int(row["tic_id"]) for row in selected]
        if existing_ids != selected_ids:
            raise RuntimeError(
                "Existing science queue does not match the authoritative context results."
            )
        return existing
    atomic_write_json(queue_path, payload)
    return payload


def latest_sector_attempts(
    log_path: Path,
) -> dict[int, dict[str, list[int]]]:
    """Return the last requested/successful sector sequence for each TIC."""

    if not log_path.exists():
        return {}
    command_pattern = re.compile(
        r"\] .*?sector-vet --report .*?TIC_(\d+)_s\d+_residual\.json "
        r"--sector ((?:\d+\s+)+)--author"
    )
    success_pattern = re.compile(r"^Sector\s+(\d+):")
    attempts: dict[int, dict[str, list[int]]] = {}
    current_tic: int | None = None
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        command_match = command_pattern.search(line)
        if command_match:
            current_tic = int(command_match.group(1))
            requested = [
                int(value) for value in command_match.group(2).split()
            ]
            attempts[current_tic] = {"requested": requested, "succeeded": []}
            continue
        success_match = success_pattern.match(line)
        if success_match and current_tic is not None:
            attempts[current_tic]["succeeded"].append(int(success_match.group(1)))
    return attempts


def public_tesscut_sectors(target: str) -> list[int]:
    """Query currently downloadable TESScut sectors without downloading data."""

    import lightkurve as lk

    search = lk.search_tesscut(target)
    if len(search) == 0:
        return []
    values: set[int] = set()
    table = search.table
    if "sequence_number" in table.colnames:
        for raw in table["sequence_number"]:
            try:
                sector = int(raw)
            except (TypeError, ValueError):
                continue
            if sector > 0:
                values.add(sector)
    if not values and "mission" in table.colnames:
        for raw in table["mission"]:
            match = re.search(r"Sector\s+(\d+)", str(raw))
            if match:
                values.add(int(match.group(1)))
    return sorted(values)


def repair_error_sector_selection(
    queue: dict[str, Any],
    queue_path: Path,
    progress_path: Path,
    stdout_path: Path,
) -> dict[str, Any]:
    """Replace unavailable/failed sectors using actual public TESScut holdings."""

    if not progress_path.exists():
        return queue
    progress = read_json(progress_path)
    error_tics = {
        int(row["tic_id"])
        for row in progress.get("results", [])
        if isinstance(row, dict) and row.get("status") == "error"
    }
    if not error_tics:
        return queue
    attempts = latest_sector_attempts(stdout_path)
    rows = queue.get("targets", [])
    if not isinstance(rows, list):
        raise RuntimeError("Science queue targets are invalid.")

    repaired = 0
    for row in rows:
        if not isinstance(row, dict) or int(row.get("tic_id") or 0) not in error_tics:
            continue
        tic_id = int(row["tic_id"])
        attempt = attempts.get(tic_id, {})
        requested = sector_values(
            attempt.get("requested") or row.get("selected_sectors")
        )
        succeeded = sector_values(attempt.get("succeeded"))
        failed: list[int] = []
        for sector in requested:
            if sector not in succeeded:
                failed = [sector]
                break
        history = row.get("sector_attempt_history")
        history = history if isinstance(history, list) else []
        known_failed = {
            int(sector)
            for event in history
            if isinstance(event, dict)
            for sector in sector_values(event.get("failed_sectors"))
        }
        known_failed.update(failed)
        public = public_tesscut_sectors(str(row["target"]))
        discovery = int(row["discovery_sector"])
        if discovery not in public:
            raise RuntimeError(
                f"Discovery sector {discovery} is not public for TIC {tic_id}."
            )
        safe_succeeded = [
            sector
            for sector in succeeded
            if sector in public and sector not in known_failed
        ]
        candidates = [
            sector
            for sector in public
            if sector != discovery
            and sector not in known_failed
            and sector not in safe_succeeded
        ]
        later = sorted(
            (sector for sector in candidates if sector > discovery), reverse=True
        )
        earlier = sorted(
            (sector for sector in candidates if sector < discovery), reverse=True
        )
        selected = [discovery]
        for sector in [*safe_succeeded, *later, *earlier]:
            if sector not in selected:
                selected.append(sector)
            if len(selected) == 3:
                break
        if selected == sector_values(row.get("selected_sectors")):
            continue
        history.append(
            {
                "updated_at_utc": utc_now(),
                "requested_sectors": requested,
                "successful_before_failure": succeeded,
                "failed_sectors": sorted(known_failed),
                "public_tesscut_sectors": public,
                "replacement_sectors": selected,
            }
        )
        row["sector_attempt_history"] = history
        row["selected_sectors"] = selected
        repaired += 1
    if repaired:
        queue["updated_at_utc"] = utc_now()
        queue["sector_selection_repairs"] = int(
            queue.get("sector_selection_repairs") or 0
        ) + repaired
        atomic_write_json(queue_path, queue)
    return queue


def expected_paths(root: Path, row: dict[str, Any]) -> tuple[Path, Path]:
    tic_id = int(row["tic_id"])
    base = root / "results" / "vetting" / "all_campaigns" / "science" / f"TIC_{tic_id}"
    sector_report = base / "sector" / f"TIC_{tic_id}_sector_vet.json"
    pixel_report = (
        base
        / "pixel"
        / f"TIC_{tic_id}_s{int(row['discovery_sector'])}_pixel.json"
    )
    return sector_report, pixel_report


def valid_sector_report(root: Path, row: dict[str, Any], path: Path) -> bool:
    if not path.exists():
        return False
    try:
        report = read_json(path)
        source_path = root / str(row["source_report"])
        sectors = sector_values(
            [
                value.get("sector")
                for value in report.get("sectors", [])
                if isinstance(value, dict)
            ]
        )
        plot = Path(str(report["plot"]))
        plot = plot if plot.is_absolute() else root / plot
        return bool(
            int(report.get("tic_id") or 0) == int(row["tic_id"])
            and same_path(report.get("source_report"), source_path, root)
            and signals_match(report.get("candidate_signal"), row.get("signal"))
            and sectors == sector_values(row.get("selected_sectors"))
            and plot.exists()
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def valid_pixel_report(root: Path, row: dict[str, Any], path: Path) -> bool:
    if not path.exists():
        return False
    try:
        report = read_json(path)
        source_path = root / str(row["source_report"])
        plot = Path(str(report["plot"]))
        plot = plot if plot.is_absolute() else root / plot
        return bool(
            str(row["tic_id"]) in str(report.get("target", ""))
            and int(report.get("sector") or 0) == int(row["discovery_sector"])
            and same_path(report.get("source_report"), source_path, root)
            and signals_match(report.get("candidate_signal"), row.get("signal"))
            and plot.exists()
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def product_count(root: Path, queue_rows: list[dict[str, Any]]) -> int:
    count = 0
    for row in queue_rows:
        sector_path, pixel_path = expected_paths(root, row)
        if valid_sector_report(root, row, sector_path):
            count += len(read_json(sector_path).get("sectors", []))
        if valid_pixel_report(root, row, pixel_path):
            count += 1
    return count


def run_command(
    command: list[str], root: Path, stdout_path: Path, stderr_path: Path
) -> dict[str, Any]:
    started = time.monotonic()
    with stdout_path.open("a", encoding="utf-8") as stdout, stderr_path.open(
        "a", encoding="utf-8"
    ) as stderr:
        stdout.write(f"\n[{utc_now()}] {' '.join(command)}\n")
        stdout.flush()
        completed = subprocess.run(
            command,
            cwd=root,
            stdout=stdout,
            stderr=stderr,
            check=False,
        )
    return {
        "command": command,
        "returncode": completed.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "finished_at_utc": utc_now(),
    }


def summarize_result(
    root: Path,
    row: dict[str, Any],
    *,
    run_state: str,
    command_outcomes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sector_path, pixel_path = expected_paths(root, row)
    sector_valid = valid_sector_report(root, row, sector_path)
    pixel_valid = valid_pixel_report(root, row, pixel_path)
    return {
        "tic_id": int(row["tic_id"]),
        "priority": int(row["priority"]),
        "status": "completed" if sector_valid and pixel_valid else "error",
        "run_state": run_state,
        "source_report": row["source_report"],
        "context_report": row["context_report"],
        "chosen_sectors": row["selected_sectors"],
        "discovery_sector": int(row["discovery_sector"]),
        "sector_report": str(sector_path.relative_to(root)),
        "pixel_report": str(pixel_path.relative_to(root)),
        "sector_report_valid": sector_valid,
        "pixel_report_valid": pixel_valid,
        "command_outcomes": command_outcomes or {},
        "updated_at_utc": utc_now(),
    }


def publish(
    root: Path,
    path: Path,
    queue_path: Path,
    rows: list[dict[str, Any]],
    results: dict[int, dict[str, Any]],
    *,
    state: str,
    started_at: str,
    current: dict[str, Any] | None,
    peak_workspace_bytes: int,
) -> int:
    ordered = [
        results[int(row["tic_id"])]
        for row in rows
        if int(row["tic_id"]) in results
    ]
    completed = sum(row["status"] == "completed" for row in ordered)
    errors = sum(row["status"] == "error" for row in ordered)
    size = workspace_bytes(root)
    peak = max(peak_workspace_bytes, size)
    atomic_write_json(
        path,
        {
            "schema_version": 1,
            "state": state,
            "queue": str(queue_path.relative_to(root)),
            "started_at_utc": started_at,
            "updated_at_utc": utc_now(),
            "total_targets": len(rows),
            "completed_targets": completed,
            "error_targets": errors,
            "remaining_targets": len(rows) - len(ordered),
            "current_tic": int(current["tic_id"]) if current else None,
            "current_source_report": current.get("source_report") if current else None,
            "current_context_report": current.get("context_report") if current else None,
            "current_chosen_sectors": current.get("selected_sectors") if current else [],
            "science_products_downloaded": product_count(root, rows),
            "workspace_bytes": size,
            "peak_workspace_bytes": peak,
            "results": ordered,
        },
    )
    return peak


def write_summary(
    root: Path,
    progress_path: Path,
    summary_json: Path,
    summary_csv: Path,
) -> None:
    progress = read_json(progress_path)
    atomic_write_json(summary_json, progress)
    rows = progress.get("results", [])
    temporary = summary_csv.with_name(f"{summary_csv.name}.{os.getpid()}.tmp")
    fields = [
        "tic_id",
        "priority",
        "status",
        "run_state",
        "source_report",
        "context_report",
        "chosen_sectors",
        "discovery_sector",
        "sector_report",
        "pixel_report",
        "sector_report_valid",
        "pixel_report_valid",
        "updated_at_utc",
    ]
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            copied = dict(row)
            copied["chosen_sectors"] = ";".join(
                str(value) for value in copied.get("chosen_sectors", [])
            )
            writer.writerow(copied)
    temporary.replace(summary_csv)


def run(root: Path, prepare_only: bool, repair_sectors: bool) -> int:
    context_queue_path = root / "results/vetting/all_campaigns/context_queue.json"
    context_dir = root / "results/vetting/all_campaigns/context"
    context_summary_path = context_dir / "context_vet_summary.json"
    queue_path = root / "results/vetting/all_campaigns/science_followup_queue.json"
    science_dir = root / "results/vetting/all_campaigns/science"
    progress_path = science_dir / "science_vet_progress.json"
    summary_json = science_dir / "science_vet_summary.json"
    summary_csv = science_dir / "science_vet_summary.csv"
    logs = root / "logs"
    stdout_path = logs / "all_campaigns_science_vet.stdout.log"
    stderr_path = logs / "all_campaigns_science_vet.stderr.log"
    logs.mkdir(parents=True, exist_ok=True)
    science_dir.mkdir(parents=True, exist_ok=True)

    queue = build_queue(
        root,
        context_queue_path,
        context_summary_path,
        context_dir,
        queue_path,
    )
    if repair_sectors:
        queue = repair_error_sector_selection(
            queue,
            queue_path,
            progress_path,
            stdout_path,
        )
    rows = queue.get("targets", [])
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("The focused science queue is empty.")
    if prepare_only:
        print(
            f"Prepared {len(rows)} science target(s); "
            f"{queue.get('deferred_by_cap', 0)} deferred by cap."
        )
        return 0

    started_at = utc_now()
    results: dict[int, dict[str, Any]] = {}
    if progress_path.exists():
        old = read_json(progress_path)
        started_at = str(old.get("started_at_utc") or started_at)
        for result in old.get("results", []):
            if isinstance(result, dict) and result.get("tic_id"):
                results[int(result["tic_id"])] = result

    for row in rows:
        tic_id = int(row["tic_id"])
        sector_path, pixel_path = expected_paths(root, row)
        if valid_sector_report(root, row, sector_path) and valid_pixel_report(
            root, row, pixel_path
        ):
            results[tic_id] = summarize_result(root, row, run_state="reused")

    peak = publish(
        root,
        progress_path,
        queue_path,
        rows,
        results,
        state="running",
        started_at=started_at,
        current=None,
        peak_workspace_bytes=0,
    )
    executable = root / ".venv/Scripts/exohunt.exe"
    if not executable.exists():
        raise RuntimeError(f"Missing EXOHUNT executable: {executable}")

    for row in rows:
        tic_id = int(row["tic_id"])
        existing = results.get(tic_id)
        if existing and existing.get("status") == "completed":
            continue
        if workspace_bytes(root) > MAX_WORKSPACE_BYTES:
            prune = run_command(
                [
                    str(executable),
                    "storage-prune",
                    "--cache-max-gb",
                    "10",
                    "--results-dir",
                    "results",
                ],
                root,
                stdout_path,
                stderr_path,
            )
            if workspace_bytes(root) > MAX_WORKSPACE_BYTES:
                publish(
                    root,
                    progress_path,
                    queue_path,
                    rows,
                    results,
                    state="storage_blocked",
                    started_at=started_at,
                    current=row,
                    peak_workspace_bytes=peak,
                )
                raise RuntimeError(
                    "Workspace remains above 20,000,000,000 bytes after pruning."
                )
            if prune["returncode"] != 0:
                raise RuntimeError("Storage pruning failed before science vetting.")

        peak = publish(
            root,
            progress_path,
            queue_path,
            rows,
            results,
            state="running",
            started_at=started_at,
            current=row,
            peak_workspace_bytes=peak,
        )
        source_report = str(row["source_report"])
        selected = [str(value) for value in row["selected_sectors"]]
        sector_path, pixel_path = expected_paths(root, row)
        outcomes: dict[str, Any] = {}
        if not valid_sector_report(root, row, sector_path):
            outcomes["sector_vet"] = run_command(
                [
                    str(executable),
                    "sector-vet",
                    "--report",
                    source_report,
                    "--sector",
                    *selected,
                    "--author",
                    "TESScut",
                    "--cadence-seconds",
                    "158",
                    "--output-dir",
                    str(sector_path.parent.relative_to(root)),
                ],
                root,
                stdout_path,
                stderr_path,
            )
        else:
            outcomes["sector_vet"] = {"reused": True, "returncode": 0}

        if not valid_pixel_report(root, row, pixel_path):
            outcomes["pixel_vet"] = run_command(
                [
                    str(executable),
                    "pixel-vet",
                    "--report",
                    source_report,
                    "--sector",
                    str(row["discovery_sector"]),
                    "--author",
                    "TESScut",
                    "--cadence-seconds",
                    "158",
                    "--output-dir",
                    str(pixel_path.parent.relative_to(root)),
                ],
                root,
                stdout_path,
                stderr_path,
            )
        else:
            outcomes["pixel_vet"] = {"reused": True, "returncode": 0}

        outcomes["storage_prune"] = run_command(
            [
                str(executable),
                "storage-prune",
                "--cache-max-gb",
                "10",
                "--results-dir",
                "results",
            ],
            root,
            stdout_path,
            stderr_path,
        )
        size = workspace_bytes(root)
        peak = max(peak, size)
        results[tic_id] = summarize_result(
            root,
            row,
            run_state="completed"
            if valid_sector_report(root, row, sector_path)
            and valid_pixel_report(root, row, pixel_path)
            else "error",
            command_outcomes=outcomes,
        )
        peak = publish(
            root,
            progress_path,
            queue_path,
            rows,
            results,
            state="running",
            started_at=started_at,
            current=None,
            peak_workspace_bytes=peak,
        )
        if size > MAX_WORKSPACE_BYTES:
            publish(
                root,
                progress_path,
                queue_path,
                rows,
                results,
                state="storage_blocked",
                started_at=started_at,
                current=None,
                peak_workspace_bytes=peak,
            )
            raise RuntimeError("Workspace exceeded 20,000,000,000 bytes after pruning.")

    errors = sum(result.get("status") == "error" for result in results.values())
    final_state = "completed" if errors == 0 else "retry_pending"
    publish(
        root,
        progress_path,
        queue_path,
        rows,
        results,
        state=final_state,
        started_at=started_at,
        current=None,
        peak_workspace_bytes=peak,
    )
    write_summary(root, progress_path, summary_json, summary_csv)
    return 0 if errors == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--repair-sectors", action="store_true")
    args = parser.parse_args()
    return run(
        args.root.resolve(),
        bool(args.prepare_only),
        bool(args.repair_sectors),
    )


if __name__ == "__main__":
    sys.exit(main())
