import argparse
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest
from filelock import FileLock

import exohunt.campaign as campaign_module
import exohunt.cli as cli_module
from exohunt.lease import acquire_machine_lock
from exohunt.cli import (
    LEGACY_COMMON_MODE_REASON,
    LEGACY_COMMON_MODE_REASONS,
    _analyze_downloaded_batch_target,
    _batch_hunt,
    _batch_target_spec,
    _campaign_settings,
    _download_batch_target,
    _is_transient_search_error,
    _load_reusable_report,
    _scientific_settings,
    _performance_snapshot,
    _quarantine_invalid_common_mode,
    _read_target_rows,
    _reusable_checkpoint_rows,
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


def test_batch_target_spec_preserves_optional_stellar_parameters(
    tmp_path: Path,
) -> None:
    spec = _batch_target_spec(
        1,
        {
            "target": "TIC 42",
            "tic_id": "42",
            "sectors": "100",
            "stellar_radius_solar": "0.3",
            "stellar_mass_solar": "0.25",
        },
        tmp_path,
    )

    assert spec["stellar_radius_solar"] == 0.3
    assert spec["stellar_mass_solar"] == 0.25


def test_batch_analysis_passes_stellar_parameters_into_hunt_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_hunt(args, _time, _flux, metadata):
        captured.update(metadata)
        report_path = tmp_path / "report.json"
        report_path.write_text("{}", encoding="utf-8")
        args.generated_report_path = str(report_path)
        return 0

    monkeypatch.setattr(cli_module, "_hunt_from_light_curve", fake_hunt)
    monkeypatch.setattr(
        campaign_module,
        "_result_row_from_report",
        lambda *_args, **_kwargs: {"status": "rejected"},
    )
    spec = {
        "target": "TIC 42",
        "tic_id": 42,
        "sectors": [100],
        "expected_report": tmp_path / "expected.json",
        "stellar_radius_solar": 0.3,
        "stellar_mass_solar": 0.25,
    }

    _analyze_downloaded_batch_target(
        spec,
        _args(tmp_path),
        (np.arange(100, dtype=float), np.ones(100), {"tic_id": 42}),
        tmp_path,
    )

    assert captured["stellar_radius_solar"] == 0.3
    assert captured["stellar_mass_solar"] == 0.25


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
        # Derived from the function under test rather than hand-copied, so that
        # adding a field to scientific identity cannot silently turn this into a
        # test of a configuration nothing produces.
        "search_configuration": _scientific_settings(args),
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


def test_final_checkpoint_fast_path_reuses_successes_and_requeues_errors(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "campaign"
    output_dir.mkdir()
    target_path = tmp_path / "targets.csv"
    target_path.write_text(
        "target,tic_id,sectors\nTIC 42,42,105\nTIC 43,43,105\n",
        encoding="utf-8",
    )
    args = _args(output_dir)
    args.workers = 2
    args.download_workers = 2
    args.prefetch = 4
    args.cache_max_gb = 10.0
    args.workspace_max_gb = None
    args.retain_rejected_plots = False
    specs = [
        _batch_target_spec(
            index,
            row,
            output_dir,
        )
        for index, row in enumerate(_read_target_rows(target_path), start=1)
    ]
    Path(specs[0]["expected_report"]).write_text("{}", encoding="utf-8")
    progress = {
        "state": "retry_pending",
        "target_list": str(target_path),
        "total_targets": 2,
        "settings": _campaign_settings(args),
        "results": [
            {
                "target": "TIC 42",
                "tic_id": 42,
                "sectors": "105",
                "status": "rejected",
                "report": str(specs[0]["expected_report"]),
                "scientific_configuration_verified": True,
            },
            {
                "target": "TIC 43",
                "tic_id": 43,
                "sectors": "105",
                "status": "error",
                "report": "",
                "scientific_configuration_verified": False,
            },
        ],
    }

    reused = _reusable_checkpoint_rows(
        progress,
        specs=specs,
        args=args,
        target_path=target_path,
        output_dir=output_dir,
    )

    assert list(reused) == [1]
    assert reused[1]["run_state"] == "resumed"


def test_final_checkpoint_fast_path_requires_exact_settings_and_artifacts(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "campaign"
    output_dir.mkdir()
    target_path = tmp_path / "targets.csv"
    target_path.write_text(
        "target,tic_id,sectors\nTIC 42,42,105\n",
        encoding="utf-8",
    )
    args = _args(output_dir)
    args.workers = 1
    args.download_workers = 1
    args.prefetch = 1
    args.cache_max_gb = 10.0
    args.workspace_max_gb = None
    args.retain_rejected_plots = False
    spec = _batch_target_spec(1, _read_target_rows(target_path)[0], output_dir)
    row = {
        "target": "TIC 42",
        "tic_id": 42,
        "sectors": "105",
        "status": "survivor",
        "report": str(spec["expected_report"]),
        "scientific_configuration_verified": True,
    }
    progress = {
        "state": "completed",
        "target_list": str(target_path),
        "total_targets": 1,
        "settings": _campaign_settings(args),
        "results": [row],
    }
    Path(spec["expected_report"]).write_text("{}", encoding="utf-8")

    # Survivor reuse requires both its report and durable plot.
    assert not _reusable_checkpoint_rows(
        progress,
        specs=[spec],
        args=args,
        target_path=target_path,
        output_dir=output_dir,
    )
    Path(spec["expected_report"]).with_suffix(".png").write_bytes(b"plot")
    assert list(
        _reusable_checkpoint_rows(
            progress,
            specs=[spec],
            args=args,
            target_path=target_path,
            output_dir=output_dir,
        )
    ) == [1]

    changed = argparse.Namespace(**vars(args))
    changed.max_period = 12.0
    assert not _reusable_checkpoint_rows(
        progress,
        specs=[spec],
        args=changed,
        target_path=target_path,
        output_dir=output_dir,
    )


def test_partial_checkpoint_maps_rows_by_identity_and_requires_storage_headroom(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "campaign"
    output_dir.mkdir()
    target_path = tmp_path / "targets.csv"
    target_path.write_text(
        "target,tic_id,sectors\nTIC 41,41,104\nTIC 42,42,105\n",
        encoding="utf-8",
    )
    args = _args(output_dir)
    args.workers = 1
    args.download_workers = 1
    args.prefetch = 1
    args.cache_max_gb = 10.0
    args.workspace_max_gb = 20.0
    args.retain_rejected_plots = False
    specs = [
        _batch_target_spec(index, row, output_dir)
        for index, row in enumerate(_read_target_rows(target_path), start=1)
    ]
    Path(specs[1]["expected_report"]).write_text("{}", encoding="utf-8")
    progress = {
        "state": "running",
        "target_list": str(target_path),
        "total_targets": 2,
        "settings": _campaign_settings(args),
        # The first target is absent, exactly as in an atomic checkpoint taken
        # between two out-of-order worker completions.
        "results": [
            {
                "target": "TIC 42",
                "tic_id": 42,
                "sectors": "105",
                "status": "rejected",
                "report": str(specs[1]["expected_report"]),
                "scientific_configuration_verified": True,
            }
        ],
        "runtime": {
            "storage": {
                "workspace_bytes": 1_000_000_000,
                "workspace_max_bytes": 20_000_000_000,
                "download_cache_bytes": 8_000_000_000,
                "download_cache_effective_max_bytes": 10_000_000_000,
            }
        },
    }

    reused = _reusable_checkpoint_rows(
        progress,
        specs=specs,
        args=args,
        target_path=target_path,
        output_dir=output_dir,
    )
    assert list(reused) == [2]
    assert (
        campaign_module._terminal_resume_storage_snapshot(
            progress, pending_targets=1
        )
        == progress["runtime"]["storage"]
    )
    progress["runtime"]["storage"]["download_cache_bytes"] = 9_900_000_001
    assert (
        campaign_module._terminal_resume_storage_snapshot(
            progress, pending_targets=1
        )
        is None
    )


def test_batch_hunt_refuses_a_duplicate_campaign_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "campaign"
    output_dir.mkdir()

    # `_batch_hunt` takes the machine-wide coordinator lease *before* it looks
    # at the per-directory lock, and a second coordinator returns 0 by design so
    # restart automations do nothing. Without a private lease this test asserts
    # on that branch instead: on a machine with a campaign genuinely running it
    # fails with "DID NOT RAISE", having never reached the lock it is about.
    # That made the suite's result depend on whether the server was busy.
    lease = acquire_machine_lock(
        f"exohunt-test-{uuid4().hex}", directory=tmp_path, force_file_lock=True
    )
    assert lease is not None
    monkeypatch.setattr(campaign_module, "acquire_machine_lock", lambda: lease)

    lock = FileLock(str(output_dir / ".batch-hunt.lock"))
    lock.acquire(timeout=0)
    try:
        with pytest.raises(RuntimeError, match="Another batch worker"):
            _batch_hunt(argparse.Namespace(output_dir=str(output_dir)))
    finally:
        lock.release()
        # Idempotent; `_batch_hunt` releases the lease it was handed on its way
        # out of the error path.
        lease.release()


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


def test_cache_prune_does_not_drain_the_download_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending prune must not empty the read-ahead buffer.

    The prune flag is raised every ten completions. It used to suppress every
    new download until the pipeline had drained to zero in-flight work, so with
    more than ten targets the buffer in front of the analysers collapsed to
    empty on a fixed cycle and they then waited on a cold queue. This asserts
    the observable consequence: downloads keep being submitted across the prune
    boundary, and at no point after start-up does the pipeline go idle.

    The prune interval is forced to zero here because a real run throttles
    prunes to one every two minutes: a prune walks the whole cache and sizes
    the entire workspace, which is far too expensive to run once per ten
    completions on a workspace of any size.
    """

    # raising=False so this guard still runs against a build predating the
    # throttle, where it must fail on the assertion rather than error on setup.
    monkeypatch.setattr(
        campaign_module, "MINIMUM_PRUNE_INTERVAL_SECONDS", 0.0, raising=False
    )

    target_count = 60
    target_path = tmp_path / "targets.csv"
    target_path.write_text(
        "target,tic_id,sectors\n"
        + "".join(f"TIC {tic},{tic},105\n" for tic in range(1, target_count + 1)),
        encoding="utf-8",
    )
    output_dir = tmp_path / "campaign"
    activity_lock = threading.Lock()
    downloads_started = 0
    completed = 0
    prune_calls = 0
    # How many downloads were submitted while a prune was in progress. Counting
    # progress across a fixed prune window is deterministic; sampling in-flight
    # depth at the instant a prune fires is not, and was flaky under load.
    started_during_prune: list[int] = []

    def fake_download(spec, args):
        nonlocal downloads_started
        with activity_lock:
            downloads_started += 1
        time.sleep(0.01)
        values = np.arange(20, dtype=float)
        return values, np.ones_like(values), {"tic_id": spec["tic_id"]}

    def fake_analysis(spec, args, downloaded, destination):
        nonlocal completed
        time.sleep(0.02)
        with activity_lock:
            completed += 1
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
            "rejection_reasons": "white-noise BLS depth S/N is below 7.1",
        }

    def fake_prune(*args, **kwargs):
        nonlocal prune_calls
        with activity_lock:
            prune_calls += 1
            before = downloads_started
        # A real prune walks the cache and sizes the workspace, which takes
        # seconds. Holding here for a fixed window is what makes the assertion
        # deterministic: either downloads were submitted during it or they were
        # not.
        time.sleep(0.3)
        with activity_lock:
            started_during_prune.append(downloads_started - before)
        return {
            "files_deleted": 0,
            "bytes_deleted": 0,
            "bytes_after": 0,
            "files_protected": 0,
            "bytes_protected": 0,
        }

    monkeypatch.setattr(cli_module, "_download_batch_target", fake_download)
    monkeypatch.setattr(
        cli_module, "_analyze_downloaded_batch_target", fake_analysis
    )
    monkeypatch.setattr(cli_module, "prune_fits_cache", fake_prune)
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
        workers=4,
        download_workers=4,
        prefetch=24,
    )
    assert _run_batch_hunt(args) == 0

    progress = json.loads(
        (output_dir / "batch_progress.json").read_text(encoding="utf-8")
    )
    assert progress["completed_targets"] == target_count
    # Every ten completions raises the flag, so the run must have pruned more
    # than once rather than only at the end.
    assert prune_calls >= 2
    # The point of the change: downloads kept being submitted while a prune was
    # running. Under the drain-first behaviour every entry here would be zero,
    # because quiescence was a precondition for pruning at all.
    assert started_during_prune
    assert max(started_during_prune) > 0


def test_buffered_targets_report_staged_rather_than_downloading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A downloaded target waiting for a worker must not read as downloading.

    `_download_batch_target` registers a target as "downloading" and only
    clears it on failure, so on success the stage persisted while the target
    sat in the read-ahead buffer. At a buffer depth of forty that left dozens
    of targets apparently downloading for many minutes, which reads as a
    download bottleneck when the downloads have in fact already finished.
    """

    from exohunt.progress import STAGES, TRACKER

    target_count = 12
    target_path = tmp_path / "targets.csv"
    target_path.write_text(
        "target,tic_id,sectors\n"
        + "".join(f"TIC {tic},{tic},105\n" for tic in range(1, target_count + 1)),
        encoding="utf-8",
    )
    output_dir = tmp_path / "campaign"
    seen_stages: set[str] = set()

    def fake_download(spec, args):
        TRACKER.begin(
            int(spec["tic_id"]), target=str(spec["target"]), stage="downloading"
        )
        values = np.arange(20, dtype=float)
        return values, np.ones_like(values), {"tic_id": spec["tic_id"]}

    def fake_analysis(spec, args, downloaded, destination):
        # Sample what the panel would show while this one is being analysed.
        for row in TRACKER.snapshot():
            seen_stages.add(str(row["stage"]))
        time.sleep(0.03)
        TRACKER.finish(int(spec["tic_id"]))
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
            "rejection_reasons": "white-noise BLS depth S/N is below 7.1",
        }

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
            "files_protected": 0,
            "bytes_protected": 0,
        },
    )
    monkeypatch.setattr(
        cli_module,
        "record_campaign",
        lambda *args, **kwargs: (None, {"campaign_runs_logged": 1}),
    )
    monkeypatch.chdir(tmp_path)
    TRACKER.clear()

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
        workers=1,
        download_workers=2,
        prefetch=12,
    )
    assert _run_batch_hunt(args) == 0

    assert "staged" in STAGES
    # With one analysis worker and a prefetch of twelve, targets certainly sat
    # in the buffer; every one of them must have reported "staged".
    assert "staged" in seen_stages
    TRACKER.clear()


def test_completed_targets_leave_the_in_flight_panel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coordinator must clear its own in-flight entries.

    `_analyze_downloaded_batch_target` calls TRACKER.finish, but under a process
    pool that runs in the child against the child's registry. The coordinator's
    entry then survived forever: a 64,000-target run showed 6,283 targets
    "in flight" against eight analysis workers, every one frozen at SEARCHING
    with an hour of elapsed time, and the registry grew without bound.

    Simulated here by an analysis that never calls finish, which is exactly
    what the coordinator observes when the real one runs out of process.
    """

    from exohunt.progress import TRACKER

    target_count = 12
    target_path = tmp_path / "targets.csv"
    target_path.write_text(
        "target,tic_id,sectors\n"
        + "".join(f"TIC {tic},{tic},105\n" for tic in range(1, target_count + 1)),
        encoding="utf-8",
    )
    output_dir = tmp_path / "campaign"

    def fake_download(spec, args):
        TRACKER.begin(
            int(spec["tic_id"]), target=str(spec["target"]), stage="downloading"
        )
        values = np.arange(20, dtype=float)
        return values, np.ones_like(values), {"tic_id": spec["tic_id"]}

    def fake_analysis(spec, args, downloaded, destination):
        # Deliberately does NOT call TRACKER.finish: a child process cannot
        # reach the coordinator's registry.
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
            "rejection_reasons": "white-noise BLS depth S/N is below 7.1",
        }

    monkeypatch.setattr(cli_module, "_download_batch_target", fake_download)
    monkeypatch.setattr(
        cli_module, "_analyze_downloaded_batch_target", fake_analysis
    )
    monkeypatch.setattr(
        cli_module,
        "prune_fits_cache",
        lambda *args, **kwargs: {
            "files_deleted": 0, "bytes_deleted": 0, "bytes_after": 0,
            "files_protected": 0, "bytes_protected": 0,
        },
    )
    monkeypatch.setattr(
        cli_module,
        "record_campaign",
        lambda *args, **kwargs: (None, {"campaign_runs_logged": 1}),
    )
    monkeypatch.chdir(tmp_path)
    TRACKER.clear()

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
        workers=2,
        download_workers=2,
        prefetch=8,
    )
    assert _run_batch_hunt(args) == 0

    remaining = TRACKER.snapshot()
    assert remaining == [], (
        f"{len(remaining)} targets left in the panel after completion"
    )
    TRACKER.clear()


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
        download_workers=3,
        prefetch=6,
    )
    assert _run_batch_hunt(args) == 0

    progress = json.loads(
        (output_dir / "batch_progress.json").read_text(encoding="utf-8")
    )
    assert maximum_downloads == 3
    assert maximum_analyses == 3
    assert progress["state"] == "completed"
    assert progress["completed_targets"] == 8
    assert progress["runtime"]["analysis_workers"] == 3
    assert progress["runtime"]["download_workers"] == 3
    assert progress["runtime"]["prefetch_targets"] == 6
    assert [row["tic_id"] for row in progress["results"]] == list(range(1, 9))
    assert all(row["planet_free"] is False for row in progress["results"])


def test_campaign_settings_preserve_two_download_default() -> None:
    args = argparse.Namespace(
        author="TESScut",
        cadence_seconds=158.0,
        min_period=0.5,
        max_period=13.0,
        mask_width=1.5,
        allow_no_known=True,
        workers=3,
        prefetch=6,
        cache_max_gb=10.0,
        workspace_max_gb=20.0,
        retain_rejected_plots=False,
    )

    assert _campaign_settings(args)["execution"]["download_workers"] == 2


def test_parallel_tesscut_targets_use_isolated_cache_namespaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespaces: list[str] = []

    def fake_download(*_args, cache_namespace=None, **_kwargs):
        namespaces.append(cache_namespace)
        values = np.arange(5, dtype=float)
        return values, np.ones_like(values), {}

    monkeypatch.setattr(cli_module, "_download_light_curve", fake_download)
    args = argparse.Namespace(author="TESScut", cadence_seconds=158.0)

    _download_batch_target(
        {"target": "TIC 101", "tic_id": 101, "sectors": [100]},
        args,
    )
    _download_batch_target(
        {"target": "TIC 202", "tic_id": 202, "sectors": [100]},
        args,
    )

    assert namespaces == ["TIC_101_s100", "TIC_202_s100"]
    assert len(set(namespaces)) == 2


@pytest.mark.parametrize(
    "message",
    [
        "Bad magic number for file header",
        "Bad CRC-32 for file",
        "File name in directory and header differ",
        "[Errno 22] Invalid argument",
    ],
)
def test_corrupt_parallel_tesscut_archives_are_retryable(message: str) -> None:
    assert _is_transient_search_error(RuntimeError(message))
