"""Dashboard export coverage for the measured pixel/sector science stage."""

import json
from pathlib import Path

from exohunt.dashboard import export_dashboard_data


def _science_workspace(tmp_path: Path, tic_id: int = 303427297) -> Path:
    """Build a workspace whose single star reached the science-vetting stage."""

    (tmp_path / "dashboard").mkdir()
    (tmp_path / "targets").mkdir()
    (tmp_path / "targets" / "targets.csv").write_text(
        "target,tic_id,sectors,ra_deg,dec_deg,distance_pc\n"
        f"TIC {tic_id},{tic_id},100,164.19,-57.75,106.3\n",
        encoding="utf-8",
    )
    campaign = tmp_path / "results" / "campaign" / "sector100"
    campaign.mkdir(parents=True)
    (campaign / "batch_progress.json").write_text(
        json.dumps(
            {
                "state": "running",
                "total_targets": 1,
                "completed_targets": 1,
                "runtime": {},
                "results": [
                    {
                        "target": f"TIC {tic_id}",
                        "tic_id": tic_id,
                        "sectors": "100",
                        "status": "survivor",
                        "screening_class": "automated_survivor",
                        "followup_priority": 99,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    context_dir = tmp_path / "results" / "vetting" / "all_campaigns" / "context"
    context_dir.mkdir(parents=True)
    (context_dir / f"TIC_{tic_id}_cross_mission_context.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "generated_at_utc": "2026-07-25T00:00:00+00:00",
                "tic": {"tic_id": tic_id},
                "context_classification": {
                    "disposition": "unresolved_transit_like_signal",
                    "followup_lane": "transit_followup",
                    "source_states": {"simbad": "completed"},
                    "reasons": ["No checked catalog explains the signal."],
                },
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _write_science_reports(
    tmp_path: Path,
    tic_id: int,
    *,
    on_target: bool,
    offset_pixels: float,
    supported: int,
    tested_sectors: list[int],
) -> None:
    base = (
        tmp_path
        / "results"
        / "vetting"
        / "all_campaigns"
        / "science"
        / f"TIC_{tic_id}"
    )
    (base / "sector").mkdir(parents=True)
    (base / "pixel").mkdir(parents=True)
    (base / "sector" / f"TIC_{tic_id}_sector_vet.json").write_text(
        json.dumps(
            {
                "tic_id": tic_id,
                "supported_sector_count": supported,
                "minimum_supporting_sectors": 2,
                "passes_distinct_sector_gate": supported >= 2,
                "sectors": [
                    {"sector": sector, "supports_signal": index < supported}
                    for index, sector in enumerate(tested_sectors)
                ],
            }
        ),
        encoding="utf-8",
    )
    (base / "pixel" / f"TIC_{tic_id}_s100_pixel.json").write_text(
        json.dumps(
            {
                "target": f"TIC {tic_id}",
                "sector": 100,
                "centroid_offset_pixels": offset_pixels,
                "centroid_offset_arcsec_approx": offset_pixels * 21.0,
                "on_target_within_one_pixel": on_target,
            }
        ),
        encoding="utf-8",
    )


def _export(tmp_path: Path, events: list[dict] | None = None) -> dict:
    output = export_dashboard_data(tmp_path, events=events or [], stats={})
    return json.loads(output.read_text(encoding="utf-8"))


def test_science_vetting_promotes_an_on_target_multi_sector_lead(
    tmp_path: Path,
) -> None:
    _science_workspace(tmp_path)
    _write_science_reports(
        tmp_path,
        303427297,
        on_target=True,
        offset_pixels=0.4,
        supported=2,
        tested_sectors=[9, 100, 101],
    )

    payload = _export(tmp_path)

    star = payload["stars"][0]
    assert star["status"] == "science_vetted_lead"
    assert star["science_on_target"] is True
    assert star["science_sector_gate_passed"] is True
    assert star["science_supported_sector_count"] == 2
    assert star["science_sectors_tested"] == 3
    assert payload["status_counts"]["science_vetted_lead"] == 1
    assert payload["science_vetting"]["passed_both_gates"] == 1


def test_science_vetting_reports_an_off_target_centroid(tmp_path: Path) -> None:
    _science_workspace(tmp_path)
    _write_science_reports(
        tmp_path,
        303427297,
        on_target=False,
        offset_pixels=2.6,
        supported=2,
        tested_sectors=[9, 100, 101],
    )

    payload = _export(tmp_path)

    star = payload["stars"][0]
    assert star["status"] == "pixel_offset_contamination"
    assert "55 arcsec" in star["notes"]
    assert payload["science_vetting"]["off_target"] == 1
    assert payload["science_vetting"]["passed_both_gates"] == 0


def test_science_vetting_flags_a_single_supporting_sector(tmp_path: Path) -> None:
    _science_workspace(tmp_path)
    _write_science_reports(
        tmp_path,
        303427297,
        on_target=True,
        offset_pixels=0.2,
        supported=1,
        tested_sectors=[9, 100, 101],
    )

    payload = _export(tmp_path)

    star = payload["stars"][0]
    assert star["status"] == "single_sector_unconfirmed"
    assert "1 of 3 tested sectors" in star["notes"]


def test_science_vetting_never_overrides_a_logged_human_outcome(
    tmp_path: Path,
) -> None:
    """A recorded false positive must outrank every automated classification."""

    _science_workspace(tmp_path)
    _write_science_reports(
        tmp_path,
        303427297,
        on_target=True,
        offset_pixels=0.2,
        supported=3,
        tested_sectors=[9, 100, 101],
    )
    events = [
        {
            "event_id": "outcome-1",
            "kind": "false_positive",
            "tic_id": 303427297,
            "label": "Vetted false positive",
            "notes": "Nearby eclipsing binary confirmed by hand.",
        }
    ]

    payload = _export(tmp_path, events)

    assert payload["stars"][0]["status"] == "false_positive"


def test_partial_science_evidence_does_not_reclassify_a_star(
    tmp_path: Path,
) -> None:
    """One gate alone cannot promote or demote a context classification."""

    _science_workspace(tmp_path)
    base = (
        tmp_path
        / "results"
        / "vetting"
        / "all_campaigns"
        / "science"
        / "TIC_303427297"
        / "sector"
    )
    base.mkdir(parents=True)
    (base / "TIC_303427297_sector_vet.json").write_text(
        json.dumps(
            {
                "tic_id": 303427297,
                "supported_sector_count": 2,
                "passes_distinct_sector_gate": True,
                "sectors": [{"sector": 100, "supports_signal": True}],
            }
        ),
        encoding="utf-8",
    )

    payload = _export(tmp_path)

    star = payload["stars"][0]
    assert star["status"] == "unresolved_transit_like_signal"
    assert star["science_sector_gate_passed"] is True
    assert star["science_on_target"] is None
    assert payload["science_vetting"]["vetted_targets"] == 0


def test_science_vet_checkpoint_reports_flat_product_count(tmp_path: Path) -> None:
    """The science runner records totals flat, not under a runtime block."""

    (tmp_path / "dashboard").mkdir()
    progress = (
        tmp_path
        / "results"
        / "vetting"
        / "all_campaigns"
        / "science"
        / "science_vet_progress.json"
    )
    progress.parent.mkdir(parents=True)
    progress.write_text(
        json.dumps(
            {
                "state": "running",
                "queue": "queue.json",
                "started_at_utc": "2026-07-26T06:38:32+00:00",
                "updated_at_utc": "2026-07-26T10:13:23+00:00",
                "total_targets": 90,
                "completed_targets": 40,
                "error_targets": 2,
                "remaining_targets": 48,
                "science_products_downloaded": 359,
                "results": [],
            }
        ),
        encoding="utf-8",
    )

    payload = _export(tmp_path)

    campaign = payload["active_campaigns"][0]
    assert campaign["workflow"] == "science_vet"
    assert campaign["runtime"]["science_products_downloaded"] == 359
    assert campaign["runtime"]["targets_remaining"] == 48
    assert campaign["counts"] == {"completed": 40, "error": 2, "remaining": 48}


def test_completed_science_run_leaves_no_stale_live_campaign(
    tmp_path: Path,
) -> None:
    """A finished vetter must not keep occupying the live progress panel."""

    (tmp_path / "dashboard").mkdir()
    progress = (
        tmp_path
        / "results"
        / "vetting"
        / "all_campaigns"
        / "science"
        / "science_vet_progress.json"
    )
    progress.parent.mkdir(parents=True)
    progress.write_text(
        json.dumps(
            {
                "state": "completed",
                "total_targets": 90,
                "completed_targets": 90,
                "error_targets": 0,
                "remaining_targets": 0,
                "results": [],
            }
        ),
        encoding="utf-8",
    )

    payload = _export(tmp_path)

    assert payload["active_campaigns"] == []


def test_newest_science_report_wins_when_a_target_is_vetted_twice(
    tmp_path: Path,
) -> None:
    """A rerun into a different directory must supersede the older verdict."""

    import os

    _science_workspace(tmp_path)
    _write_science_reports(
        tmp_path,
        303427297,
        on_target=False,
        offset_pixels=2.6,
        supported=1,
        tested_sectors=[9, 100, 101],
    )
    stale = (
        tmp_path
        / "results"
        / "vetting"
        / "all_campaigns"
        / "science"
        / "TIC_303427297"
        / "pixel"
        / "TIC_303427297_s100_pixel.json"
    )
    os.utime(stale, (100, 100))

    # An earlier-sorting path holding a newer, corrected measurement.
    rerun = tmp_path / "results" / "pixel" / "TIC_303427297"
    rerun.mkdir(parents=True)
    corrected = rerun / "TIC_303427297_s100_pixel.json"
    corrected.write_text(
        json.dumps(
            {
                "target": "TIC 303427297",
                "sector": 100,
                "centroid_offset_pixels": 0.3,
                "centroid_offset_arcsec_approx": 6.3,
                "on_target_within_one_pixel": True,
            }
        ),
        encoding="utf-8",
    )
    os.utime(corrected, (900, 900))

    payload = _export(tmp_path)

    assert payload["stars"][0]["science_on_target"] is True
