"""Finish publication from an already-durable batch summary.

This is the recovery path for a coordinator interrupted after writing
``batch_summary.json`` but before the dip registry, CSV, metrics revision, and
terminal checkpoint. It performs no downloads, analyses, or retention.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from exohunt import cli
from exohunt.campaign import _publish_dip_registries
from exohunt.metrics import record_campaign
from exohunt.progress import STAGES


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_commit() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def finalize(summary_path: Path) -> dict[str, object]:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    results = [row for row in summary.get("results", []) if isinstance(row, dict)]
    counts = cli._campaign_counts(results)
    if len(results) != sum(int(value) for value in counts.values()):
        raise RuntimeError("Campaign results and status counts do not reconcile.")
    if counts["error"]:
        raise RuntimeError(
            f"Refusing to publish a completed closeout with {counts['error']} error rows."
        )

    output_dir = summary_path.parent
    registry = _publish_dip_registries(output_dir, results)

    csv_path = output_dir / "batch_summary.csv"
    fieldnames = sorted({key for row in results for key in row})
    temporary_csv = csv_path.with_name(csv_path.name + ".tmp")
    with temporary_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    cli._replace_with_retry(temporary_csv, csv_path)

    _, stats = record_campaign(summary_path)
    progress_path = output_dir / "batch_progress.json"
    try:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        progress = {}
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    started = str(progress.get("started_at_utc") or now)
    runtime = dict(progress.get("runtime") or {})
    runtime.update(
        {
            "downloads_in_flight": 0,
            "analyses_in_flight": 0,
            "downloaded_waiting": 0,
            "targets_remaining": 0,
            "in_flight": [],
            "stages": list(STAGES),
            "performance": cli._performance_snapshot(
                results,
                started_at_utc=started,
                total_targets=len(results),
            ),
            "vetting_coverage": cli._vetting_coverage(results),
        }
    )
    progress.update(
        {
            "schema_version": 1,
            "state": "completed",
            "started_at_utc": started,
            "updated_at_utc": now,
            "target_list": summary.get("target_list"),
            "output_dir": str(output_dir),
            "total_targets": len(results),
            "completed_targets": len(results),
            "settings": summary.get("settings", {}),
            "runtime": runtime,
            "counts": counts,
            "results": results,
        }
    )
    cli._atomic_write_json(progress_path, progress)
    status = {
        key: progress[key]
        for key in (
            "schema_version",
            "state",
            "started_at_utc",
            "updated_at_utc",
            "target_list",
            "total_targets",
            "completed_targets",
            "counts",
            "runtime",
        )
    } | {
        "sectors": sorted(
            {
                int(value)
                for row in results
                for value in str(row.get("sectors") or "").split(";")
                if value.strip().isdigit()
            }
        )
    }
    status_path = output_dir / "batch_status.json"
    cli._atomic_write_json(status_path, status)
    cli._publish_followup_queue(output_dir, results)

    artifacts = [
        summary_path,
        csv_path,
        output_dir / "dip_registry.json",
        progress_path,
        status_path,
    ]
    target_list = Path(str(summary.get("target_list") or ""))
    if target_list.exists():
        artifacts.insert(0, target_list)
    manifest = {
        "schema_version": 1,
        "created_at_utc": now,
        "state": "completed",
        "git_commit": _git_commit(),
        "target_list": str(target_list),
        "counts": counts,
        "stars_contributing_to_dip_registry": registry.get("stars_contributing"),
        "metrics_snapshot": stats,
        "artifacts": {
            str(path): {"bytes": path.stat().st_size, "sha256": _sha256(path)}
            for path in artifacts
            if path.exists()
        },
    }
    manifest_path = output_dir / "closeout_manifest.json"
    cli._atomic_write_json(manifest_path, manifest)
    return {**manifest, "manifest_path": str(manifest_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(finalize(args.summary), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
