import json
from pathlib import Path

from exohunt.dashboard_server import _phase_curve_for_tic


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    dashboard = tmp_path / "dashboard"
    (dashboard / "dist").mkdir(parents=True)
    (dashboard / "dist" / "index.html").write_text("dashboard", encoding="utf-8")
    run_dir = tmp_path / "results" / "campaign" / "test"
    run_dir.mkdir(parents=True)
    return dashboard, run_dir


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
