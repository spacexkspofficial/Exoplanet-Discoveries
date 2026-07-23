import csv
import json
from pathlib import Path

import pytest

from exohunt.dashboard import _cartesian, export_dashboard_data


def test_cartesian_uses_the_galactic_plane_and_pole() -> None:
    galactic_center = _cartesian(266.4051, -28.936175, 10.0)
    assert galactic_center["x"] == pytest.approx(10.0, abs=0.001)
    assert galactic_center["y"] == pytest.approx(0.0, abs=0.001)
    assert galactic_center["z"] == pytest.approx(0.0, abs=0.001)

    north_galactic_pole = _cartesian(192.85948, 27.12825, 10.0)
    assert north_galactic_pole["x"] == pytest.approx(0.0, abs=0.001)
    assert north_galactic_pole["y"] == pytest.approx(0.0, abs=0.001)
    assert north_galactic_pole["z"] == pytest.approx(10.0, abs=0.001)


def test_dashboard_includes_active_campaign_checkpoint(tmp_path: Path):
    (tmp_path / "dashboard").mkdir()
    (tmp_path / "targets").mkdir()
    target_path = tmp_path / "targets" / "overnight.csv"
    with target_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "target",
                "tic_id",
                "sectors",
                "distance_pc",
                "ra_deg",
                "dec_deg",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "target": "TIC 42",
                "tic_id": 42,
                "sectors": "105",
                "distance_pc": 12.5,
                "ra_deg": 120,
                "dec_deg": -30,
            }
        )

    progress_path = tmp_path / "results" / "campaign" / "overnight" / "batch_progress.json"
    progress_path.parent.mkdir(parents=True)
    progress_path.write_text(
        json.dumps(
            {
                "state": "running",
                "target_list": str(target_path),
                "total_targets": 1000,
                "completed_targets": 1,
                "started_at_utc": "2026-07-23T07:00:00+00:00",
                "updated_at_utc": "2026-07-23T07:01:00+00:00",
                "counts": {"survivor": 0, "rejected": 1, "error": 0},
                "results": [
                    {
                        "target": "TIC 42",
                        "tic_id": 42,
                        "sectors": "105",
                        "status": "rejected",
                        "period_days": 3.5,
                        "depth_ppm": 700,
                        "depth_snr": 9.2,
                        "duration_hours": 2,
                        "observed_transits": 4,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    output = export_dashboard_data(tmp_path, events=[], stats={"campaign_runs_logged": 0})
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["active_campaigns"][0]["completed_targets"] == 1
    assert payload["active_campaigns"][0]["total_targets"] == 1000
    assert payload["active_campaigns"][0]["sectors"] == [105]
    assert payload["stars"][0]["tic_id"] == 42
    assert payload["stars"][0]["screening_status"] == "rejected"
    assert payload["stars"][0]["distance_pc"] == 12.5
