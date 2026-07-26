"""Characterization tests for the dashboard's evidence precedence.

These tests intentionally pin the observable behavior that existed before the
status-registry refactor.  They should not be weakened to accommodate a
structural change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exohunt.dashboard import export_dashboard_data


HUMAN_OUTCOMES = (
    "false_positive",
    "rediscovery",
    "known_tce_rediscovery",
    "vetted_candidate",
    "confirmed_planet",
)


def _workspace_with_population_verdict(tmp_path: Path) -> None:
    (tmp_path / "dashboard").mkdir()
    targets = tmp_path / "targets"
    targets.mkdir()
    (targets / "targets.csv").write_text(
        "target,tic_id,sectors,ra_deg,dec_deg,distance_pc\n"
        "TIC 42,42,100,10.0,-20.0,50.0\n",
        encoding="utf-8",
    )

    campaign = tmp_path / "results" / "campaign" / "sector100"
    campaign.mkdir(parents=True)
    (campaign / "batch_progress.json").write_text(
        json.dumps(
            {
                "state": "running",
                "target_list": "targets/targets.csv",
                "total_targets": 1,
                "completed_targets": 1,
                "results": [
                    {
                        "target": "TIC 42",
                        "tic_id": 42,
                        "sectors": "100",
                        "status": "survivor",
                        "screening_class": "automated_survivor",
                    }
                ],
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
                    "42": {
                        "verdict": "common_mode_systematic",
                        "shared_targets": 40,
                        "expected_targets": 2.0,
                        "enrichment": 20.0,
                        "cameras_spanned": 4,
                        "sky_spread_deg": 80.0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("human_outcome", HUMAN_OUTCOMES)
def test_every_human_outcome_overrides_the_highest_automated_stage(
    tmp_path: Path,
    human_outcome: str,
) -> None:
    _workspace_with_population_verdict(tmp_path)

    output = export_dashboard_data(
        tmp_path,
        events=[
            {
                "event_id": f"outcome-{human_outcome}",
                "kind": human_outcome,
                "tic_id": 42,
                "label": f"Human: {human_outcome}",
                "notes": "Reviewed by a person.",
            }
        ],
        stats={},
    )
    star = json.loads(output.read_text(encoding="utf-8"))["stars"][0]

    assert star["common_mode_verdict"] == "common_mode_systematic"
    assert star["status"] == human_outcome
    assert star["status_label"] == f"Human: {human_outcome}"
    assert star["notes"] == "Reviewed by a person."


def test_later_evidence_wins_when_human_outcomes_have_equal_precedence(
    tmp_path: Path,
) -> None:
    _workspace_with_population_verdict(tmp_path)

    output = export_dashboard_data(
        tmp_path,
        events=[
            {
                "event_id": "outcome-1",
                "kind": "false_positive",
                "tic_id": 42,
                "label": "First review",
                "notes": "First",
            },
            {
                "event_id": "outcome-2",
                "kind": "rediscovery",
                "tic_id": 42,
                "label": "Second review",
                "notes": "Second",
            },
        ],
        stats={},
    )
    star = json.loads(output.read_text(encoding="utf-8"))["stars"][0]

    assert star["status"] == "rediscovery"
    assert star["status_label"] == "Second review"
    assert star["notes"] == "Second"
