import json
from pathlib import Path

from exohunt.metrics import invalidate_event, record_campaign, record_outcome


def test_campaign_metrics_are_idempotent_and_outcomes_accumulate(tmp_path: Path):
    summary_path = tmp_path / "batch_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "target_list": "targets.csv",
                "settings": {"period_range_days": [0.5, 20]},
                "counts": {"survivor": 1, "rejected": 1, "error": 0},
                "results": [
                    {
                        "tic_id": 1,
                        "sectors": "1;2",
                        "status": "survivor",
                        "rejection_reasons": "",
                    },
                    {
                        "tic_id": 2,
                        "sectors": "3;4",
                        "status": "rejected",
                        "rejection_reasons": "low S/N; harmonic",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    ledger = tmp_path / "events.jsonl"
    snapshot = tmp_path / "stats.json"
    first, stats = record_campaign(summary_path, ledger_path=ledger, snapshot_path=snapshot)
    second, stats = record_campaign(summary_path, ledger_path=ledger, snapshot_path=snapshot)
    assert first is True
    assert second is False
    assert stats["campaign_runs_logged"] == 1
    assert stats["unique_targets_searched"] == 2
    assert stats["rejection_reasons"] == {"harmonic": 1, "low S/N": 1}

    record_outcome(
        "vetted_candidate",
        tic_id=1,
        label="TIC 1 signal",
        ledger_path=ledger,
        snapshot_path=snapshot,
    )
    stats = json.loads(snapshot.read_text(encoding="utf-8"))
    assert stats["vetted_new_candidates"] == 1


def test_record_outcome_is_idempotent(tmp_path: Path):
    ledger = tmp_path / "events.jsonl"
    snapshot = tmp_path / "stats.json"
    first, _ = record_outcome(
        "known_tce_rediscovery",
        tic_id=260708537,
        label="00260708537-01",
        source="check.json",
        ledger_path=ledger,
        snapshot_path=snapshot,
    )
    second, stats = record_outcome(
        "known_tce_rediscovery",
        tic_id=260708537,
        label="00260708537-01",
        source="check.json",
        ledger_path=ledger,
        snapshot_path=snapshot,
    )
    assert first is True
    assert second is False
    assert stats["known_tce_rediscoveries"] == 1


def test_campaign_invalidation_removes_it_from_active_totals(tmp_path: Path):
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "target_list": "targets.csv",
                "settings": {"pipeline": "bad-v1"},
                "counts": {"survivor": 1, "rejected": 0, "error": 0},
                "results": [{"tic_id": 9, "sectors": "105", "status": "survivor"}],
            }
        ),
        encoding="utf-8",
    )
    ledger = tmp_path / "events.jsonl"
    snapshot = tmp_path / "stats.json"
    _, _ = record_campaign(summary_path, ledger_path=ledger, snapshot_path=snapshot)
    event_id = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])["event_id"]
    _, stats = invalidate_event(
        event_id,
        reason="bad extraction",
        ledger_path=ledger,
        snapshot_path=snapshot,
    )
    assert stats["campaign_runs_logged"] == 0
    assert stats["target_search_runs"] == 0
    assert stats["invalidated_events"] == 1


def test_corrected_campaign_summary_supersedes_prior_outcomes(tmp_path: Path):
    summary_path = tmp_path / "summary.json"
    summary = {
        "target_list": "targets.csv",
        "settings": {
            "period_range_days": [0.5, 13],
            "storage_retention": {"fits_cache_max_gb": 2},
        },
        "counts": {"survivor": 0, "rejected": 0, "error": 1},
        "results": [{"tic_id": 9, "sectors": "105", "status": "error"}],
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    ledger = tmp_path / "events.jsonl"
    snapshot = tmp_path / "stats.json"
    first, _ = record_campaign(
        summary_path, ledger_path=ledger, snapshot_path=snapshot
    )

    summary["counts"] = {"survivor": 1, "rejected": 0, "error": 0}
    summary["results"][0]["status"] = "survivor"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    second, stats = record_campaign(
        summary_path, ledger_path=ledger, snapshot_path=snapshot
    )

    assert first is True
    assert second is True
    assert stats["campaign_runs_logged"] == 1
    assert stats["target_search_runs"] == 1
    assert stats["automated_survivors"] == 1
    assert stats["search_errors"] == 0
    assert stats["invalidated_events"] == 1


def test_execution_settings_do_not_change_scientific_campaign_identity(
    tmp_path: Path,
) -> None:
    summary_path = tmp_path / "summary.json"
    summary = {
        "target_list": "targets.csv",
        "settings": {
            "period_range_days": [0.5, 13],
            "execution": {"analysis_workers": 1, "prefetch_targets": 2},
        },
        "counts": {"survivor": 1, "rejected": 0, "error": 0},
        "results": [{"tic_id": 9, "sectors": "105", "status": "survivor"}],
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    ledger = tmp_path / "events.jsonl"
    snapshot = tmp_path / "stats.json"
    first, _ = record_campaign(
        summary_path, ledger_path=ledger, snapshot_path=snapshot
    )

    summary["settings"]["execution"] = {
        "analysis_workers": 3,
        "prefetch_targets": 6,
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    second, stats = record_campaign(
        summary_path, ledger_path=ledger, snapshot_path=snapshot
    )

    assert first is True
    assert second is False
    assert stats["campaign_runs_logged"] == 1


def test_campaign_can_be_relogged_after_its_exact_revision_was_invalidated(
    tmp_path: Path,
) -> None:
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "target_list": "targets.csv",
                "settings": {"period_range_days": [0.5, 13]},
                "counts": {"survivor": 1, "rejected": 0, "error": 0},
                "results": [{"tic_id": 7, "sectors": "105", "status": "survivor"}],
            }
        ),
        encoding="utf-8",
    )
    ledger = tmp_path / "events.jsonl"
    snapshot = tmp_path / "stats.json"
    record_campaign(summary_path, ledger_path=ledger, snapshot_path=snapshot)
    original = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    invalidate_event(
        original["event_id"],
        reason="audit retry",
        ledger_path=ledger,
        snapshot_path=snapshot,
    )

    added, stats = record_campaign(
        summary_path, ledger_path=ledger, snapshot_path=snapshot
    )

    events = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
    ]
    replacements = [
        event
        for event in events
        if event.get("kind") == "campaign_completed"
        and event.get("event_id") != original["event_id"]
    ]
    assert added is True
    assert len(replacements) == 1
    assert replacements[0]["event_id"].endswith("-rev-1")
    assert stats["campaign_runs_logged"] == 1
