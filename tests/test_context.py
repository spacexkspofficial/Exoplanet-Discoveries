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
