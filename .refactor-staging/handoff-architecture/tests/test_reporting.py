import csv
import json
from pathlib import Path

import pytest

from exohunt.reporting import create_campaign_report, create_candidate_packet


def _residual_report(passes: bool = True) -> dict[str, object]:
    return {
        "data": {
            "target": "TIC 123",
            "tic_id": 123,
            "downloaded_sectors": [1, 2],
            "author": "SPOC",
            "requested_cadence_seconds": 120.0,
        },
        "known_signal_masks": [
            {
                "label": "TOI-1.01",
                "period_days": 3.0,
                "duration_hours": 2.0,
                "source": "test catalog",
            }
        ],
        "strongest_residual_signal": {
            "period_days": 5.25,
            "transit_time": 2000.5,
            "duration_hours": 1.75,
            "depth_ppm": 500.0,
            "depth_snr": 10.0,
            "radius_ratio": 0.02236,
            "observed_transits": 5,
        },
        "automated_triage": {
            "passes": passes,
            "rejection_reasons": [] if passes else ["test rejection"],
        },
    }


def test_candidate_packet_writes_pdf_and_bjd_worksheet(tmp_path: Path):
    report_path = tmp_path / "signal.json"
    report_path.write_text(json.dumps(_residual_report()), encoding="utf-8")
    outputs = create_candidate_packet(
        report_path,
        output_dir=tmp_path / "packets",
        pdf_output_dir=tmp_path / "pdf",
    )
    pdf = Path(outputs["pdf"])
    assert pdf.read_bytes().startswith(b"%PDF")
    with Path(outputs["worksheet"]).open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert float(row["transit_midpoint_bjd_tdb"]) == pytest.approx(2459000.5)
    assert row["status"] == "DRAFT_NOT_READY_FOR_UPLOAD"


def test_candidate_packet_refuses_rejected_signal(tmp_path: Path):
    report_path = tmp_path / "rejected.json"
    report_path.write_text(json.dumps(_residual_report(False)), encoding="utf-8")
    with pytest.raises(ValueError, match="failed automated triage"):
        create_candidate_packet(report_path, output_dir=tmp_path / "packets")


def test_campaign_report_writes_markdown_and_pdf(tmp_path: Path):
    summary_path = tmp_path / "campaign" / "batch_summary.json"
    summary_path.parent.mkdir()
    summary_path.write_text(
        json.dumps(
            {
                "settings": {"period_range_days": [0.5, 20], "cadence_seconds": 120},
                "counts": {"survivor": 1, "rejected": 0, "error": 0},
                "results": [
                    {
                        "tic_id": 123,
                        "sectors": "1;2",
                        "status": "survivor",
                        "period_days": 5.25,
                        "depth_ppm": 500,
                        "depth_snr": 10,
                        "rejection_reasons": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    outputs = create_campaign_report(
        summary_path,
        output_dir=tmp_path / "reports",
        pdf_output_dir=tmp_path / "pdf",
    )
    assert Path(outputs["pdf"]).read_bytes().startswith(b"%PDF")
    assert "Automated survivors: 1" in Path(outputs["markdown"]).read_text(encoding="utf-8")
