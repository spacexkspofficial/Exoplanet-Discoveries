import argparse
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from filelock import FileLock

import exohunt.cli as cli_module
from exohunt.cli import (
    LEGACY_COMMON_MODE_REASON,
    LEGACY_COMMON_MODE_REASONS,
    _batch_hunt,
    _is_transient_search_error,
    _load_reusable_report,
    _performance_snapshot,
    _quarantine_invalid_common_mode,
    _read_target_rows,
    _run_batch_hunt,
    _thread_safe_lightkurve_download,
    _vetting_coverage,
    _workspace_cache_dir,
)


def _args(output_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        output_dir=str(output_dir),
        author="TESScut",
        cadence_seconds=158.0,
        min_period=0.5,
        max_period=13.0,
        mask_width=1.5,
        allow_no_known=True,
    )


def test_common_mode_midpoint_density_never_rejects_targets() -> None:
    rows = [
        {
            "tic_id": index,
            "status": "rejected",
            "rejection_reasons": LEGACY_COMMON_MODE_REASON,
            "common_mode_peer_count": 100,
        }
        for index in range(100)
    ]

    report = _quarantine_invalid_common_mode(rows)

    assert report["automatic_rejection_applied"] is False
    assert report["legacy_rows_repaired"] == 100
    assert all(row["status"] == "survivor" for row in rows)
    assert all(row["rejection_reasons"] == "" for row in rows)
    assert all("common_mode_peer_count" not in row for row in rows)


def test_vetting_coverage_keeps_legacy_rows_explicit() -> None:
    coverage = _vetting_coverage(
        [
            {"vetting_tier": "passes_additional_checks"},
            {"vetting_tier": "legacy_unmeasured"},
            {"vetting_tier": "retry_required"},
        ]
    )

    assert coverage["eligible_targets"] == 2
    assert coverage["measured_targets"] == 1
    assert coverage["legacy_unmeasured_targets"] == 1
    assert coverage["coverage_fraction"] == 0.5
    assert coverage["warning"]


def test_workspace_cache_must_be_a_dedicated_project_data_child(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    expected = workspace / "data" / "lightkurve"

    assert (
        _workspace_cache_dir("data/lightkurve", workspace_root=workspace)
        == expected.resolve()
    )
    with pytest.raises(ValueError, match="inside the project data directory"):
        _workspace_cache_dir(tmp_path / "other-project", workspace_root=workspace)
    with pytest.raises(ValueError, match="dedicated child"):
        _workspace_cache_dir(workspace / "data", workspace_root=workspace)

    older_row = {
        "tic_id": 999,
        "status": "rejected",
        "rejection_reasons": (
            "transit midpoint is shared by at least three campaign targets"
        ),
    }
    _quarantine_invalid_common_mode([older_row])
    assert older_row["status"] == "survivor"
    assert not any(
        reason in older_row["rejection_reasons"]
        for reason in LEGACY_COMMON_MODE_REASONS
    )


def test_report_reuse_requires_matching_identity_and_complete_plot(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path)
    report_path = tmp_path / "target.json"
    report = {
        "data": {
            "target": "TIC 42",
            "tic_id": 42,
            "requested_sectors": [105],
            "author": "TESScut",
            "requested_cadence_seconds": 158.0,
        },
        "search_configuration": {
            "author": "TESScut",
            "cadence_seconds": 158.0,
            "period_range_days": [0.5, 13.0],
            "mask_width": 1.5,
            "allow_no_known": True,
            "data_pipeline_version": "tesscut-bgsub-commonmode-quarantined-v4",
        },
        "automated_triage": {"passes": True, "rejection_reasons": []},
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")

    assert (
        _load_reusable_report(
            report_path,
            target="TIC 42",
            tic_id=42,
            sectors=[105],
            args=args,
            allow_legacy=False,
        )
        is None
    )

    report["automated_triage"] = {
        "passes": False,
        "rejection_reasons": ["low signal"],
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    assert (
        _load_reusable_report(
            report_path,
            target="TIC 42",
            tic_id=42,
            sectors=[105],
            args=args,
            allow_legacy=False,
        )
        == report
    )

    report["automated_triage"] = {"passes": True, "rejection_reasons": []}
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_path.with_suffix(".png").write_bytes(b"plot")
    assert (
        _load_reusable_report(
            report_path,
            target="TIC 42",
            tic_id=42,
            sectors=[105],
            args=args,
            allow_legacy=False,
        )
        == report
    )
    assert (
        _load_reusable_report(
            report_path,
            target="TIC 43",
            tic_id=43,
            sectors=[105],
            args=args,
            allow_legacy=False,
        )
        is None
    )


def test_batch_hunt_refuses_a_duplicate_campaign_worker(tmp_path: Path) -> None:
    output_dir = tmp_path / "campaign"
    output_dir.mkdir()
    lock = FileLock(str(output_dir / ".batch-hunt.lock"))
    lock.acquire(timeout=0)
    try:
        with pytest.raises(RuntimeError, match="Another batch worker"):
            _batch_hunt(argparse.Namespace(output_dir=str(output_dir)))
    finally:
        lock.release()


def test_target_csv_validation_rejects_duplicate_rows(tmp_path: Path) -> None:
    target_path = tmp_path / "targets.csv"
    target_path.write_text(
        "target,tic_id,sectors\nTIC 42,42,105\nTIC 42,42,105\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicates TIC 42"):
        _read_target_rows(target_path)


def test_transient_search_error_detection_is_conservative() -> None:
    assert _is_transient_search_error(TimeoutError("read timed out"))
    assert _is_transient_search_error(RuntimeError("HTTP 503 from MAST"))
    assert not _is_transient_search_error(ValueError("bad sector"))


def test_lightkurve_download_bypasses_global_stdout_redirect() -> None:
    class FakeSearch:
        def original(self, **kwargs):
            return {"owner": self, **kwargs}

        def decorated(self, **kwargs):
            raise AssertionError("unsafe stdout-redirecting wrapper was called")

    FakeSearch.decorated.__wrapped__ = FakeSearch.original
    search = FakeSearch()

    result = _thread_safe_lightkurve_download(search.decorated, target="TIC 1")

    assert result == {"owner": search, "target": "TIC 1"}


def test_performance_snapshot_reports_average_recent_rate_and_eta() -> None:
    now = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
    results = [{"tic_id": index} for index in range(94)]
    results.extend(
        {
            "tic_id": 94 + index,
            "completed_at_utc": (
                now - timedelta(minutes=10 - index * 2)
            ).isoformat(),
        }
        for index in range(6)
    )

    performance = _performance_snapshot(
        results,
        started_at_utc=(now - timedelta(hours=2)).isoformat(),
        total_targets=200,
        now=now,
    )

    assert performance["average_stars_per_hour"] == 50.0
    assert performance["rolling_stars_per_hour"] == 30.0
    assert performance["rolling_samples"] == 6
    assert performance["eta_hours"] == pytest.approx(3.33, abs=0.01)


def test_parallel_batch_uses_bounded_download_ahead_and_ordered_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_path = tmp_path / "targets.csv"
    target_path.write_text(
        "target,tic_id,sectors\n"
        + "".join(f"TIC {tic},{tic},105\n" for tic in range(1, 9)),
        encoding="utf-8",
    )
    output_dir = tmp_path / "campaign"
    activity_lock = threading.Lock()
    active_downloads = 0
    active_analyses = 0
    maximum_downloads = 0
    maximum_analyses = 0

    def fake_download(spec, args):
        nonlocal active_downloads, maximum_downloads
        with activity_lock:
            active_downloads += 1
            maximum_downloads = max(maximum_downloads, active_downloads)
        try:
            time.sleep(0.015)
            values = np.arange(20, dtype=float)
            return values, np.ones_like(values), {"tic_id": spec["tic_id"]}
        finally:
            with activity_lock:
                active_downloads -= 1

    def fake_analysis(spec, args, downloaded, destination):
        nonlocal active_analyses, maximum_analyses
        with activity_lock:
            active_analyses += 1
            maximum_analyses = max(maximum_analyses, active_analyses)
        try:
            time.sleep(0.05)
            return {
                "target": spec["target"],
                "tic_id": spec["tic_id"],
                "sectors": "105",
                "run_state": "completed",
                "status": "rejected",
                "screening_class": "no_transit_detected",
                "followup_priority": 5,
                "followup_reasons": "deprioritize for this TESS window",
                "planet_free": False,
                "period_days": 3.0,
                "depth_ppm": 500.0,
                "depth_snr": 4.0,
                "observed_transits": 5,
                "transit_time": 1.0,
                "duration_hours": 2.0,
                "rejection_reasons": (
                    "white-noise BLS depth S/N is below 7.1"
                ),
            }
        finally:
            with activity_lock:
                active_analyses -= 1

    monkeypatch.setattr(cli_module, "_download_batch_target", fake_download)
    monkeypatch.setattr(
        cli_module, "_analyze_downloaded_batch_target", fake_analysis
    )
    monkeypatch.setattr(
        cli_module,
        "prune_fits_cache",
        lambda *args, **kwargs: {
            "files_deleted": 0,
            "bytes_deleted": 0,
            "bytes_after": 0,
        },
    )
    monkeypatch.setattr(
        cli_module,
        "record_campaign",
        lambda *args, **kwargs: (None, {"campaign_runs_logged": 1}),
    )
    monkeypatch.chdir(tmp_path)

    args = argparse.Namespace(
        targets=str(target_path),
        output_dir=str(output_dir),
        max_targets=None,
        force=False,
        author="TESScut",
        cadence_seconds=158.0,
        min_period=0.5,
        max_period=13.0,
        mask_width=1.5,
        allow_no_known=True,
        cache_max_gb=10.0,
        retain_rejected_plots=True,
        workers=3,
        prefetch=6,
    )
    assert _run_batch_hunt(args) == 0

    progress = json.loads(
        (output_dir / "batch_progress.json").read_text(encoding="utf-8")
    )
    assert maximum_downloads == 2
    assert maximum_analyses == 3
    assert progress["state"] == "completed"
    assert progress["completed_targets"] == 8
    assert progress["runtime"]["analysis_workers"] == 3
    assert progress["runtime"]["download_workers"] == 2
    assert progress["runtime"]["prefetch_targets"] == 6
    assert [row["tic_id"] for row in progress["results"]] == list(range(1, 9))
    assert all(row["planet_free"] is False for row in progress["results"])
