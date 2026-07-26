import json
import os
from pathlib import Path

from exohunt.dashboard import survey_source_mtime_ns
from exohunt.dashboard_server import (
    _needs_survey_refresh,
    _phase_curve_for_tic,
    _prefer_live_campaign_last,
    _survey_header,
    _survey_sources_are_newer,
)


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    dashboard = tmp_path / "dashboard"
    (dashboard / "dist").mkdir(parents=True)
    (dashboard / "dist" / "index.html").write_text("dashboard", encoding="utf-8")
    run_dir = tmp_path / "results" / "campaign" / "test"
    run_dir.mkdir(parents=True)
    return dashboard, run_dir


def test_live_campaign_is_last_for_older_dashboard_bundles() -> None:
    payload = {
        "active_campaigns": [
            {
                "name": "running",
                "state": "running",
                "updated_at_utc": "2026-07-24T17:20:00+00:00",
            },
            {
                "name": "retry",
                "state": "retry_pending",
                "updated_at_utc": "2026-07-24T16:58:24+00:00",
            },
        ]
    }

    _prefer_live_campaign_last(payload)

    assert [campaign["name"] for campaign in payload["active_campaigns"]] == [
        "retry",
        "running",
    ]


def test_old_campaign_snapshot_requires_schema_refresh() -> None:
    assert _needs_survey_refresh({"schema_version": 1})
    assert _needs_survey_refresh({"schema_version": 2})
    assert not _needs_survey_refresh({"schema_version": 2, "sector_coverage": []})


def _write_checkpoint(tmp_path: Path, mtime: int) -> Path:
    progress = (
        tmp_path
        / "results"
        / "vetting"
        / "all_campaigns"
        / "context"
        / "context_vet_progress.json"
    )
    progress.parent.mkdir(parents=True, exist_ok=True)
    progress.write_text("{}", encoding="utf-8")
    os.utime(progress, (mtime, mtime))
    return progress


def test_live_context_checkpoint_invalidates_dashboard_snapshot(
    tmp_path: Path,
) -> None:
    progress = _write_checkpoint(tmp_path, 200)
    stale = progress.stat().st_mtime_ns - 1

    assert _survey_sources_are_newer(tmp_path, {"source_mtime_ns": stale})
    assert not _survey_sources_are_newer(
        tmp_path, {"source_mtime_ns": progress.stat().st_mtime_ns}
    )


def test_snapshot_without_fingerprint_is_always_refreshed(tmp_path: Path) -> None:
    _write_checkpoint(tmp_path, 200)

    assert _survey_sources_are_newer(tmp_path, {})


def test_checkpoint_written_during_export_still_invalidates_snapshot(
    tmp_path: Path,
) -> None:
    """A snapshot must not look fresh merely because it was written last.

    The exporter samples its fingerprint before reading, so a checkpoint saved
    while the export was in flight is newer than the fingerprint even though
    the resulting file is newer than the checkpoint.
    """

    _write_checkpoint(tmp_path, 200)
    fingerprint = survey_source_mtime_ns(tmp_path)

    # The vetter finishes and rewrites its checkpoint mid-export.
    progress = _write_checkpoint(tmp_path, 400)
    output = tmp_path / "dashboard" / "public" / "data" / "survey.json"
    output.parent.mkdir(parents=True)
    output.write_text("{}", encoding="utf-8")
    os.utime(output, (900, 900))
    assert output.stat().st_mtime_ns > progress.stat().st_mtime_ns

    assert _survey_sources_are_newer(tmp_path, {"source_mtime_ns": fingerprint})


def test_per_target_vetting_reports_invalidate_snapshot(tmp_path: Path) -> None:
    """A manually rerun single-target vet updates no checkpoint at all."""

    _write_checkpoint(tmp_path, 200)
    fingerprint = survey_source_mtime_ns(tmp_path)
    science = tmp_path / "results" / "vetting" / "all_campaigns" / "science" / "TIC_7"
    science.mkdir(parents=True)
    report = science / "TIC_7_sector_vet.json"
    report.write_text("{}", encoding="utf-8")
    os.utime(report, (500, 500))

    assert _survey_sources_are_newer(tmp_path, {"source_mtime_ns": fingerprint})


def test_phase_curve_endpoint_returns_only_compact_curve(tmp_path: Path):
    _, run_dir = _workspace(tmp_path)
    report_path = run_dir / "tic_42.json"
    curve = {
        "schema_version": 1,
        "source": "actual normalized residual TESS photometry",
        "phase_min": -0.12,
        "phase_max": 0.12,
        "bin_count": 2,
        "phase": [-0.06, 0.06],
        "median_residual_flux_ppm": [-500.0, 12.0],
        "scatter_ppm": [30.0, 25.0],
        "count": [8, 9],
        "measurements_total": 100,
        "measurements_in_range": 17,
    }
    report_path.write_text(json.dumps({"phase_curve": curve}), encoding="utf-8")
    (run_dir / "batch_progress.json").write_text(
        json.dumps(
            {
                "state": "running",
                "results": [
                    {
                        "tic_id": 42,
                        "report": str(report_path.relative_to(tmp_path)),
                        "phase_curve_available": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = _phase_curve_for_tic(tmp_path, 42)

    assert loaded == curve


def test_phase_curve_endpoint_explains_legacy_target(tmp_path: Path):
    _, run_dir = _workspace(tmp_path)
    legacy_report = run_dir / "tic_7.json"
    legacy_report.write_text(json.dumps({"strongest_residual_signal": {}}), encoding="utf-8")
    (run_dir / "batch_progress.json").write_text(
        json.dumps(
            {
                "state": "running",
                "results": [
                    {
                        "tic_id": 7,
                        "report": str(legacy_report.relative_to(tmp_path)),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert _phase_curve_for_tic(tmp_path, 7) is None


def test_phase_curve_endpoint_rejects_report_outside_results(tmp_path: Path):
    _, run_dir = _workspace(tmp_path)
    outside_report = tmp_path / "outside.json"
    outside_report.write_text(json.dumps({"phase_curve": {"phase": [0]}}), encoding="utf-8")
    (run_dir / "batch_progress.json").write_text(
        json.dumps(
            {
                "state": "running",
                "results": [{"tic_id": 9, "report": str(outside_report)}],
            }
        ),
        encoding="utf-8",
    )

    assert _phase_curve_for_tic(tmp_path, 9) is None


def test_survey_header_is_parsed_once_per_written_file(tmp_path: Path) -> None:
    """The browser polls constantly; the snapshot is only parsed when it changes."""

    from exohunt.dashboard_server import _SURVEY_HEADER_CACHE, _survey_header

    snapshot = tmp_path / "survey.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "source_mtime_ns": 4242,
                "sector_coverage": [{"sector": 1}],
                "stars": [{"tic_id": 1}],
            }
        ),
        encoding="utf-8",
    )
    _SURVEY_HEADER_CACHE.pop(str(snapshot), None)

    first = _survey_header(snapshot)
    assert first["schema_version"] == 2
    assert first["source_mtime_ns"] == 4242
    assert first["sector_coverage"] == []
    assert "stars" not in first

    # A second call must reuse the cache rather than re-read the file.
    snapshot.write_text("this is not json", encoding="utf-8")
    os.utime(snapshot, (500, 500))
    cached_key, cached_header = _SURVEY_HEADER_CACHE[str(snapshot)]
    _SURVEY_HEADER_CACHE[str(snapshot)] = (
        (snapshot.stat().st_mtime_ns, snapshot.stat().st_size),
        cached_header,
    )
    assert _survey_header(snapshot) is cached_header


def test_fresh_snapshot_does_not_trigger_another_export(tmp_path: Path) -> None:
    """The poll path must not rewrite the snapshot when nothing changed.

    Re-exporting on every poll was the expensive failure mode: it walked the
    results tree and rewrote tens of megabytes several times a minute.
    """

    from exohunt.dashboard import export_dashboard_data

    dashboard, _ = _workspace(tmp_path)
    export_dashboard_data(tmp_path, events=[], stats={})
    snapshot = dashboard / "public" / "data" / "survey.json"
    assert snapshot.exists()

    header = _survey_header(snapshot)
    assert not _needs_survey_refresh(header)
    assert not _survey_sources_are_newer(tmp_path, header)

    # A new checkpoint must still invalidate it.
    _write_checkpoint(tmp_path, 10_000_000)
    assert _survey_sources_are_newer(tmp_path, header)
