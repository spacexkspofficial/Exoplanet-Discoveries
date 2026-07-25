import argparse
import json
from pathlib import Path

import exohunt.cli as cli_module
from exohunt.cli import build_history_context_queue, _run_context_vet_queue
from exohunt.context import (
    build_followup_actions,
    summarize_mast_observations,
    summarize_tic_neighbors,
)


def test_mast_summary_separates_tess_sectors_reductions_and_other_missions():
    rows = [
        {
            "obs_collection": "TESS",
            "provenance_name": "SPOC",
            "dataproduct_type": "image",
            "sequence_number": 105,
            "t_min": 100.0,
            "t_max": 127.0,
        },
        {
            "obs_collection": "TESS",
            "provenance_name": "SPOC",
            "dataproduct_type": "timeseries",
            "sequence_number": 28,
            "t_min": 20.0,
            "t_max": 47.0,
        },
        {
            "obs_collection": "HLSP",
            "provenance_name": "QLP",
            "dataproduct_type": "timeseries",
            "sequence_number": 28,
            "t_min": 20.0,
            "t_max": 47.0,
        },
        {
            "obs_collection": "K2",
            "provenance_name": "K2",
            "dataproduct_type": "timeseries",
            "sequence_number": 7,
        },
    ]

    summary = summarize_mast_observations(rows)

    assert summary["observation_records"] == 4
    assert summary["collection_counts"] == {"HLSP": 1, "K2": 1, "TESS": 2}
    assert summary["tess"]["all_sectors"] == [28, 105]
    assert summary["tess"]["timeseries_sectors"] == [28]
    assert summary["tess"]["image_only_sectors"] == [105]
    assert summary["tess"]["alternate_reductions"] == ["QLP"]
    assert summary["tess"]["calendar_span_days"] == 107.0
    assert "K2" in summary["mission_roles"]


def test_neighbor_summary_reports_one_pixel_crowding_without_claiming_dilution():
    rows = [
        {"ID": 10, "dstArcSec": 0.0, "Tmag": 10.0, "GAIA": 100},
        {
            "ID": 11,
            "dstArcSec": 8.0,
            "Tmag": 12.0,
            "GAIA": "4919125829084987520",
        },
        {"ID": 12, "dstArcSec": 30.0, "Tmag": 9.0, "GAIA": 102},
    ]

    summary = summarize_tic_neighbors(
        rows, target_tic_id=10, target_tmag=10.0
    )

    assert summary["neighbors_in_query_radius"] == 2
    assert summary["neighbors_within_one_tess_pixel"] == 1
    assert summary["crowding_risk"] == "high"
    assert summary["neighbors"][0]["delta_tmag_vs_target"] == 2.0
    assert summary["neighbors"][0]["gaia_source_id"] == 4919125829084987520
    assert summary["rough_neighbor_to_target_flux_ratio_upper_bound"] > 1.0


def test_followup_actions_put_giant_and_multisector_checks_first():
    actions = build_followup_actions(
        tic={
            "stellar_radius_solar": 5.2,
            "luminosity_class": "GIANT",
            "gaia_source_id": 123,
        },
        catalog={"tois": [], "confirmed_planets": []},
        mast={
            "tess": {
                "all_sectors": [1, 2, 28],
                "timeseries_sectors": [1, 2],
                "alternate_reductions": ["QLP", "TGLC"],
            },
            "collection_counts": {"HST": 1},
        },
        neighbors={"neighbors_within_one_tess_pixel": 0},
    )

    assert actions[0]["priority"] == "critical"
    assert "giant" in actions[0]["action"].lower()
    assert any("additional TESS sectors" in row["action"] for row in actions)
    assert any("independently extracted" in row["action"] for row in actions)
    assert any("HST" in row["action"] for row in actions)


def test_context_vet_queue_is_compact_and_idempotent(
    tmp_path: Path, monkeypatch
) -> None:
    queue_path = tmp_path / "deep_followup_queue.json"
    queue_path.write_text(
        json.dumps(
            {
                "targets": [
                    {
                        "tic_id": 42,
                        "target": "TIC 42",
                        "followup_priority": 99,
                        "vetting_tier": "high_priority_followup",
                        "period_days": 3.5,
                    },
                    {
                        "tic_id": 43,
                        "target": "TIC 43",
                        "followup_priority": 80,
                        "vetting_tier": "needs_manual_review",
                        "period_days": 7.0,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    calls: list[int] = []

    def fake_query(tic_id: int, **_kwargs):
        calls.append(tic_id)
        return {
            "schema_version": 2,
            "tic": {"tic_id": tic_id},
            "context_classification": {
                "disposition": "unresolved_transit_like_signal",
                "followup_lane": "independent_photometry_and_pixel_vetting",
                "followup_priority": 90,
                "known_binary_host": False,
                "source_states": {"tess_tce": "completed"},
                "exact_period_matches": [],
            },
            "mast_holdings": {
                "observation_records": 2,
                "collection_counts": {"TESS": 2},
                "tess": {
                    "all_sectors": [100],
                    "alternate_reductions": ["QLP"],
                },
            },
            "neighbor_context": {"crowding_risk": "low"},
            "recommended_actions": [{"priority": "high", "action": "check"}],
        }

    monkeypatch.setattr(cli_module, "query_cross_mission_context", fake_query)
    output_dir = tmp_path / "context"
    args = argparse.Namespace(
        queue=str(queue_path),
        output_dir=str(output_dir),
        max_targets=None,
        workers=2,
        force=False,
        mast_radius_arcsec=3.0,
        neighbor_radius_arcsec=42.0,
    )

    assert _run_context_vet_queue(args) == 0
    assert sorted(calls) == [42, 43]
    summary = json.loads(
        (output_dir / "context_vet_summary.json").read_text(encoding="utf-8")
    )
    assert summary["state"] == "completed"
    assert summary["counts"] == {"completed": 2, "error": 0, "remaining": 0}
    assert summary["runtime"]["science_products_downloaded"] == 0

    calls.clear()
    assert _run_context_vet_queue(args) == 0
    assert calls == []
    reused = json.loads(
        (output_dir / "context_vet_summary.json").read_text(encoding="utf-8")
    )
    assert all(row["run_state"] == "reused" for row in reused["results"])


def test_history_queue_preserves_initial_checks_without_tess_redownload(
    tmp_path: Path,
) -> None:
    campaign = tmp_path / "results" / "campaign" / "one"
    campaign.mkdir(parents=True)
    report_path = campaign / "TIC_42_s100_residual.json"
    report_path.write_text(
        json.dumps(
            {
                "data": {
                    "target": "TIC 42",
                    "tic_id": 42,
                    "requested_sectors": [100],
                },
                "search_configuration": {
                    "data_pipeline_version": "test-v1"
                },
                "observation_window": {"measurements": 1000},
                "strongest_residual_signal": {
                    "period_days": 3.5,
                    "depth_ppm": 900.0,
                    "depth_snr": 12.0,
                    "observed_transits": 4,
                    "secondary_snr": 0.2,
                },
                "automated_triage": {
                    "passes": True,
                    "rejection_reasons": [],
                },
                "screening_flags": {
                    "secondary_eclipse_over_3_sigma": False
                },
                "deeper_vetting": {
                    "red_noise_adjusted_snr": 9.0,
                    "event_coverage_fraction": 1.0,
                    "positive_depth_event_fraction": 1.0,
                },
                "sensitivity_probe": {"periods": []},
                "catalog_checked": {
                    "tois": [],
                    "confirmed_planets": [],
                },
            }
        ),
        encoding="utf-8",
    )
    (campaign / "batch_progress.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "tic_id": 42,
                        "target": "TIC 42",
                        "sectors": "100",
                        "status": "survivor",
                        "screening_class": "automated_survivor",
                        "followup_priority": 90,
                        "vetting_tier": "high_priority_followup",
                        "period_days": 3.5,
                        "depth_ppm": 900.0,
                        "depth_snr": 12.0,
                        "observed_transits": 4,
                        "report": str(report_path),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "results" / "vetting" / "context_queue.json"

    payload = build_history_context_queue(
        tmp_path / "results" / "campaign",
        output,
    )

    assert len(payload["targets"]) == 1
    target = payload["targets"][0]
    assert target["prior_scan_count"] == 1
    initial = target["initial_scan_evidence"][0]
    assert initial["searched_sectors"] == [100]
    assert initial["screening_flags"][
        "secondary_eclipse_over_3_sigma"
    ] is False
    assert initial["deeper_vetting"]["red_noise_adjusted_snr"] == 9.0
    assert output.exists()
