"""Coverage for the shared-ephemeris (observatory systematic) screen."""

import json
from pathlib import Path

from exohunt.commonmode import screen_campaign, screen_campaign_root
from exohunt.dashboard import export_dashboard_data


def _row(
    tic_id: int,
    period: float,
    epoch: float,
    *,
    duration: float = 2.0,
    camera: int = 1,
    ccd: int = 1,
    ra: float = 160.0,
    dec: float = -30.0,
) -> dict:
    return {
        "tic_id": tic_id,
        "period_days": period,
        "transit_time": epoch,
        "duration_hours": duration,
        "camera": camera,
        "ccd": ccd,
        "ra_deg": ra,
        "dec_deg": dec,
        "status": "survivor",
    }


def test_shared_ephemeris_across_cameras_is_called_a_systematic() -> None:
    """Many unrelated stars dimming on one ephemeris is the observatory."""

    rows = [
        _row(
            1000 + index,
            6.85,
            4080.2,
            camera=1 + index % 4,
            ra=100.0 + index,
            dec=-40.0 + index % 30,
        )
        for index in range(200)
    ]
    # A genuinely independent signal mixed into the same campaign.
    rows.append(_row(9999, 3.14159, 4076.5, camera=2, ra=170.0, dec=-25.0))

    verdicts = screen_campaign(rows)

    assert verdicts[1000]["verdict"] == "common_mode_systematic"
    assert verdicts[1000]["shared_targets"] == 199
    assert verdicts[1000]["cameras_spanned"] == 4
    assert verdicts[1000]["enrichment"] > 10
    assert verdicts[9999]["verdict"] == "independent_timing"
    assert verdicts[9999]["shared_targets"] == 0


def test_independent_periods_are_not_flagged() -> None:
    """A spread of unrelated ephemerides must survive the screen."""

    rows = [
        _row(2000 + index, 1.0 + index * 0.37, 4070.0 + index * 0.61)
        for index in range(120)
    ]

    verdicts = screen_campaign(rows)

    assert all(
        verdict["verdict"] == "independent_timing" for verdict in verdicts.values()
    )


def test_matching_period_but_scattered_phase_is_not_flagged() -> None:
    """Period clustering alone is not evidence; the epochs must line up too.

    This is the case the screen must not over-call: an instrumental period
    distribution can pile many targets at one period, but real signals there
    still transit at unrelated times.
    """

    rows = [
        _row(3000 + index, 6.85, 4074.0 + index * 6.85 / 150)
        for index in range(150)
    ]

    verdicts = screen_campaign(rows)

    flagged = [v for v in verdicts.values() if v["verdict"] != "independent_timing"]
    assert flagged == []


def test_tight_sky_cluster_is_reported_as_localized() -> None:
    """Neighbours sharing an ephemeris point at one contaminating source."""

    rows = [
        _row(
            4000 + index,
            6.85,
            4080.2,
            camera=1,
            ccd=1,
            ra=160.0 + index * 0.001,
            dec=-30.0 + index * 0.001,
        )
        for index in range(60)
    ]

    verdicts = screen_campaign(rows)

    assert verdicts[4000]["verdict"] == "localized_coincidence"
    assert verdicts[4000]["cameras_spanned"] == 1
    assert verdicts[4000]["sky_spread_deg"] < 1.0


def test_targets_without_an_ephemeris_receive_no_verdict() -> None:
    """Absence of a measurement is not evidence of anything."""

    verdicts = screen_campaign(
        [
            {"tic_id": 1, "period_days": None, "transit_time": None},
            {"tic_id": 2, "period_days": 5.0, "transit_time": 4070.0},
            {"tic_id": 3, "period_days": 5.0, "transit_time": 4071.0},
        ]
    )

    assert 1 not in verdicts
    assert {2, 3} <= set(verdicts)


def _campaign(tmp_path: Path, name: str, rows: list[dict]) -> Path:
    directory = tmp_path / "results" / "campaign" / name
    directory.mkdir(parents=True)
    (directory / "batch_summary.json").write_text(
        json.dumps({"target_list": "", "results": rows}), encoding="utf-8"
    )
    return directory


def test_campaigns_are_screened_separately(tmp_path: Path) -> None:
    """Stars never observed together cannot share an observatory event."""

    shared = [
        _row(5000 + index, 6.85, 4080.2, camera=1 + index % 4, ra=100.0 + index)
        for index in range(30)
    ]
    other = [
        _row(6000 + index, 6.85, 4080.2, camera=1 + index % 4, ra=100.0 + index)
        for index in range(30)
    ]
    _campaign(tmp_path, "alpha", shared)
    _campaign(tmp_path, "beta", other)

    payload = screen_campaign_root(
        tmp_path / "results" / "campaign", workspace=tmp_path
    )

    assert payload["screened_targets"] == 60
    # Each campaign has 29 sharers, not 59: the two were never pooled.
    assert payload["verdicts"]["5000"]["shared_targets"] == 29
    assert payload["verdicts"]["6000"]["shared_targets"] == 29
    assert payload["verdicts"]["5000"]["campaign"] == "alpha"


def test_dashboard_demotes_a_science_lead_that_shares_its_ephemeris(
    tmp_path: Path,
) -> None:
    """A shared ephemeris must outrank the per-star science gates.

    Multi-sector coherence passes an observatory systematic by construction, so
    the screen has to win when the two disagree.
    """

    (tmp_path / "dashboard").mkdir()
    (tmp_path / "targets").mkdir()
    (tmp_path / "targets" / "targets.csv").write_text(
        "target,tic_id,sectors,ra_deg,dec_deg,distance_pc\n"
        "TIC 777,777,100,164.19,-57.75,106.3\n",
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
                        "target": "TIC 777",
                        "tic_id": 777,
                        "sectors": "100",
                        "status": "survivor",
                        "screening_class": "automated_survivor",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    science = tmp_path / "results" / "vetting" / "science" / "TIC_777"
    (science / "sector").mkdir(parents=True)
    (science / "pixel").mkdir(parents=True)
    (science / "sector" / "TIC_777_sector_vet.json").write_text(
        json.dumps(
            {
                "tic_id": 777,
                "supported_sector_count": 3,
                "passes_distinct_sector_gate": True,
                "sectors": [{"sector": 100, "supports_signal": True}],
            }
        ),
        encoding="utf-8",
    )
    (science / "pixel" / "TIC_777_s100_pixel.json").write_text(
        json.dumps(
            {
                "target": "TIC 777",
                "sector": 100,
                "centroid_offset_pixels": 0.2,
                "centroid_offset_arcsec_approx": 4.2,
                "on_target_within_one_pixel": True,
            }
        ),
        encoding="utf-8",
    )
    screen = tmp_path / "results" / "vetting" / "common_mode"
    screen.mkdir(parents=True)
    (screen / "common_mode_screen.json").write_text(
        json.dumps(
            {
                "verdicts": {
                    "777": {
                        "verdict": "common_mode_systematic",
                        "shared_targets": 331,
                        "expected_shared_targets": 6.4,
                        "enrichment": 51.9,
                        "cameras_spanned": 4,
                        "sky_spread_deg": 77.4,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    payload = json.loads(
        export_dashboard_data(tmp_path, events=[], stats={}).read_text(
            encoding="utf-8"
        )
    )

    star = payload["stars"][0]
    assert star["status"] == "common_mode_systematic"
    assert star["science_disposition"] == "science_vetted_lead"
    assert "331 unrelated targets" in star["notes"]
    assert payload["common_mode_screen"]["observatory_systematic"] == 1
