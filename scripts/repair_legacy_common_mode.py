"""One-time, auditable repair for the invalid v3 campaign midpoint veto.

The v3 batch finalizer treated fitted BLS reference epochs as if they were
cadence-level common-mode evidence. At large campaign sizes that rule rejected
otherwise passing rows merely because many arbitrary reference epochs landed
within 0.75 day. This migration removes only that reason, preserves every
independent per-target rejection, updates JSON/CSV summaries, and records
append-only ledger invalidations before logging corrected campaign events.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from exohunt.metrics import invalidate_event, read_events, record_campaign


LEGACY_REASONS = {
    "transit midpoint is shared by at least five campaign targets within 0.75 day",
    "transit midpoint is shared by at least three campaign targets",
}
PIPELINE_VERSION = "tesscut-bgsub-commonmode-quarantined-v4"


def _atomic_json(path: Path, payload: object) -> None:
    temporary = path.with_name(path.name + ".repair.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _replace_with_retry(temporary, path)


def _replace_with_retry(source: Path, destination: Path) -> None:
    for attempt in range(8):
        try:
            source.replace(destination)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.05 * (2**attempt))


def _without_legacy_reason(value: object) -> tuple[list[str], list[str]]:
    raw_parts = (
        list(value)
        if isinstance(value, (list, tuple))
        else str(value or "").split(";")
    )
    original = [str(part).strip() for part in raw_parts if str(part).strip()]
    removed = [reason for reason in original if reason in LEGACY_REASONS]
    return [reason for reason in original if reason not in LEGACY_REASONS], removed


def _repair_rows(
    rows: list[dict[str, Any]],
    *,
    settings: dict[str, Any],
    corrected_at_utc: str,
) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        if row.get("status") == "error":
            counts["error"] += 1
            continue
        reasons, removed = _without_legacy_reason(row.get("rejection_reasons"))
        row["rejection_reasons"] = "; ".join(reasons)
        row["status"] = "rejected" if reasons else "survivor"
        row.pop("common_mode_peer_count", None)
        row["campaign_common_mode_screen"] = "not applied"
        plot = Path(str(row.get("plot") or ""))
        row["plot_retained"] = bool(row.get("plot")) and plot.exists()
        counts[str(row["status"])] += 1

        report_value = row.get("report")
        if not report_value:
            continue
        report_path = Path(str(report_value))
        if not report_path.exists():
            raise FileNotFoundError(f"Missing report required for repair: {report_path}")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        triage = report.get("automated_triage")
        if not isinstance(triage, dict):
            raise ValueError(f"Missing automated_triage in {report_path}")
        report_reasons, report_removed = _without_legacy_reason(
            triage.get("rejection_reasons")
        )
        if set(removed) != set(report_removed):
            raise ValueError(
                f"Summary/report legacy-reason mismatch for {report_path}"
            )
        triage["rejection_reasons"] = report_reasons
        triage["passes"] = not report_reasons
        report["search_configuration"] = {
            key: value
            for key, value in settings.items()
            if key != "storage_retention"
        }
        if report_removed or not report.get("scientific_audit"):
            report["scientific_audit"] = {
                "corrected_at_utc": corrected_at_utc,
                "legacy_pipeline_version": "tesscut-bgsub-commonmode-v2-or-v3",
                "removed_rejection_reasons": report_removed,
                "explanation": (
                    "The campaign-level single-midpoint density veto was "
                    "quarantined because a fitted BLS reference epoch is not "
                    "cadence-level common-mode evidence. Independent per-target "
                    "triage is unchanged."
                ),
            }
        _atomic_json(report_path, report)
    return counts


def repair_campaign(campaign_dir: Path) -> dict[str, Any]:
    summary_path = campaign_dir / "batch_summary.json"
    progress_path = campaign_dir / "batch_progress.json"
    csv_path = campaign_dir / "batch_summary.csv"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    progress = (
        json.loads(progress_path.read_text(encoding="utf-8"))
        if progress_path.exists()
        else None
    )
    if progress is not None and summary.get("target_list") != progress.get("target_list"):
        raise ValueError(f"Summary/checkpoint target mismatch in {campaign_dir}")
    if progress is not None and len(summary.get("results", [])) != int(
        progress.get("total_targets", -1)
    ):
        raise ValueError(f"Summary/checkpoint total mismatch in {campaign_dir}")

    corrected_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    settings = dict(summary.get("settings") or {})
    settings["data_pipeline_version"] = PIPELINE_VERSION
    counts = _repair_rows(
        list(summary["results"]),
        settings=settings,
        corrected_at_utc=corrected_at,
    )
    count_payload = {
        status: int(counts.get(status, 0))
        for status in ("survivor", "rejected", "error")
    }
    if sum(count_payload.values()) != len(summary["results"]):
        raise ValueError(f"Corrected counts do not total campaign rows in {campaign_dir}")

    screen = {
        "status": "quarantined",
        "automatic_rejection_applied": False,
        "reason": (
            "The former single-midpoint density rule is invalid at campaign scale. "
            "Common-mode rejection now requires cadence-level detector or "
            "background evidence."
        ),
    }
    summary["settings"] = settings
    summary["counts"] = count_payload
    summary["campaign_level_screening"] = {"common_mode": screen}
    if progress is not None:
        progress["settings"] = settings
        progress["counts"] = count_payload
        progress["results"] = summary["results"]
        progress["campaign_level_screening"] = {"common_mode": screen}
        progress["updated_at_utc"] = corrected_at

    _atomic_json(summary_path, summary)
    if progress is not None:
        _atomic_json(progress_path, progress)
    fieldnames = sorted({key for row in summary["results"] for key in row})
    temporary_csv = csv_path.with_name(csv_path.name + ".repair.tmp")
    with temporary_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summary["results"])
    _replace_with_retry(temporary_csv, csv_path)

    return {
        "summary_path": summary_path,
        "target_list": summary["target_list"],
        "counts": count_payload,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("campaign_dirs", nargs="+", type=Path)
    args = parser.parse_args()
    repaired = [repair_campaign(path) for path in args.campaign_dirs]

    events = read_events()
    invalidated_ids = {
        str(event.get("invalidates_event_id"))
        for event in events
        if event.get("kind") == "event_invalidated"
    }
    for item in repaired:
        normalized_summary = str(item["summary_path"]).replace("/", "\\")
        for event in events:
            if (
                event.get("kind") == "campaign_completed"
                and not event.get("campaign_id")
                and str(event.get("summary_path", "")).replace("/", "\\")
                == normalized_summary
                and event.get("event_id") not in invalidated_ids
            ):
                invalidate_event(
                    str(event["event_id"]),
                    reason=(
                        "Invalid v3 single-midpoint campaign veto was removed; "
                        "superseded by an auditable v4 corrected summary."
                    ),
                    source="scripts/repair_legacy_common_mode.py",
                )
        record_campaign(item["summary_path"])

    for item in repaired:
        print(
            json.dumps(
                {
                    "summary_path": str(item["summary_path"]),
                    "target_list": item["target_list"],
                    "counts": item["counts"],
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
