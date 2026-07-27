"""End-to-end parity: files -> exporter versus files -> ledger -> projection.

Builds a miniature workspace exercising every evidence stage (screening from
summaries and an active checkpoint, context vetting, measured science, the
population screen, human outcomes, and an invalidated event), then requires
the ledger projection to reproduce the dashboard exporter's status counts
exactly. This is the hermetic form of the Phase 1 parity gate.
"""

from __future__ import annotations

import json
from pathlib import Path

from exohunt import ledger
from exohunt.importer import import_workspace, parity_check


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _summary_row(tic_id: int, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "target": f"TIC {tic_id}",
        "tic_id": tic_id,
        "sectors": "100",
        "status": "survivor",
        "period_days": 3.21,
        "depth_ppm": 1200.0,
        "depth_snr": 9.5,
        "duration_hours": 2.0,
        "observed_transits": 4,
        "transit_time": 4070.0,
        "rejection_reasons": "",
        "followup_priority": 80,
        "followup_reasons": "localize the signal in target pixels",
        "vetting_tier": "high_priority_followup",
        "data_pipeline_version": "processed-lc-v2",
    }
    row.update(overrides)
    return row


def _build_workspace(root: Path) -> None:
    (root / "dashboard").mkdir(parents=True)

    summary_rows = [
        _summary_row(1),
        _summary_row(
            2,
            status="rejected",
            rejection_reasons="the fitted transit depth exceeds 5 percent",
        ),
        _summary_row(3),
        _summary_row(
            4,
            status="rejected",
            depth_snr=4.0,
            rejection_reasons="white-noise BLS depth S/N is below 7.1",
        ),
        _summary_row(5),
        _summary_row(6, status="error"),
    ]
    _write_json(
        root / "results" / "campaign" / "camp1" / "batch_summary.json",
        {
            "target_list": "targets/camp1.csv",
            "settings": {"data_pipeline_version": "processed-lc-v2"},
            "counts": {"survivor": 3, "rejected": 2, "error": 1},
            "results": summary_rows,
        },
    )
    # An active checkpoint overrides the completed row for star 2.
    _write_json(
        root / "results" / "campaign" / "camp2" / "batch_progress.json",
        {
            "schema_version": 1,
            "state": "running",
            "started_at_utc": "2026-07-27T00:00:00+00:00",
            "updated_at_utc": "2026-07-27T00:05:00+00:00",
            "target_list": "targets/camp2.csv",
            "total_targets": 1,
            "completed_targets": 1,
            "settings": {"data_pipeline_version": "processed-lc-v3-edge-safe"},
            "counts": {"survivor": 1, "rejected": 0, "error": 0},
            "results": [
                _summary_row(
                    2, data_pipeline_version="processed-lc-v3-edge-safe"
                )
            ],
        },
    )

    events = [
        {
            "event_id": "e1",
            "kind": "campaign_completed",
            "summary_path": "results/campaign/camp1/batch_summary.json",
            "targets": 6,
            "automated_survivors": 3,
            "rejected": 2,
            "errors": 1,
            "tic_ids": [1, 2, 3, 4, 5, 6],
            "timestamp_utc": "2026-07-26T00:00:00+00:00",
        },
        {
            "event_id": "e2",
            "kind": "vetted_candidate",
            "tic_id": 1,
            "label": "mistaken promotion",
            "notes": "logged in error",
            "source": "manual",
            "timestamp_utc": "2026-07-26T01:00:00+00:00",
        },
        {
            "event_id": "e3",
            "kind": "event_invalidated",
            "invalidates_event_id": "e2",
            "timestamp_utc": "2026-07-26T02:00:00+00:00",
        },
        {
            "event_id": "e4",
            "kind": "false_positive",
            "tic_id": 3,
            "label": "eclipsing binary",
            "notes": "secondary eclipse at 5.9 sigma in the stacked fold",
            "source": "manual",
            "timestamp_utc": "2026-07-26T03:00:00+00:00",
        },
    ]
    events_path = root / "metrics" / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )

    _write_json(
        root
        / "results"
        / "vetting"
        / "context"
        / "TIC_5_cross_mission_context.json",
        {
            "tic": {"tic_id": 5},
            "generated_at_utc": "2026-07-26T04:00:00+00:00",
            "context_classification": {
                "disposition": "unresolved_transit_like_signal",
                "reasons": ["no checked catalog explains the signal"],
            },
        },
    )
    _write_json(
        root / "results" / "vetting" / "science" / "TIC_5_s100_pixel.json",
        {
            "tic_id": 5,
            "sector": 100,
            "on_target_within_one_pixel": True,
            "centroid_offset_pixels": 0.3,
            "centroid_offset_arcsec_approx": 6.3,
        },
    )
    _write_json(
        root / "results" / "vetting" / "science" / "TIC_5_sector_vet.json",
        {
            "tic_id": 5,
            "passes_distinct_sector_gate": True,
            "supported_sector_count": 2,
            "minimum_supporting_sectors": 2,
            "sectors": [
                {"sector": 100, "supports_signal": True},
                {"sector": 101, "supports_signal": True},
            ],
        },
    )
    # A measured pixel result without the second science gate is display
    # evidence, but it must not vote on star 1's current status.
    _write_json(
        root / "results" / "vetting" / "science" / "TIC_1_s100_pixel.json",
        {
            "tic_id": 1,
            "sector": 100,
            "on_target_within_one_pixel": True,
            "centroid_offset_pixels": 0.2,
            "centroid_offset_arcsec_approx": 4.2,
        },
    )
    # The population screen outranks the measured science for star 5.
    _write_json(
        root / "results" / "vetting" / "common_mode_screen.json",
        {
            "schema_version": 1,
            "verdicts": {
                "5": {
                    "verdict": "common_mode_systematic",
                    "shared_targets": 240,
                    "expected_shared_targets": 3.2,
                    "enrichment": 75.0,
                    "cameras_spanned": 4,
                    "sky_spread_deg": 12.0,
                    "shared_epoch_btjd": 4080.8,
                },
                "1": {
                    "verdict": "independent_timing",
                    "shared_targets": 0,
                    "expected_shared_targets": 1.1,
                    "enrichment": 0.0,
                    "cameras_spanned": 0,
                    "sky_spread_deg": None,
                    "shared_epoch_btjd": 4074.0,
                },
            },
        },
    )


def test_ledger_projection_matches_exporter_counts(tmp_path: Path) -> None:
    _build_workspace(tmp_path)
    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        report = import_workspace(conn, tmp_path)
        parity = parity_check(conn, tmp_path)
        assert parity["match"], json.dumps(parity, indent=2)
        assert parity["exporter_total"] == parity["ledger_total"] == 6
        assert parity["star_status_differences"] == {}
        assert parity["star_payload_differences"] == {}

        states = {
            row["tic_id"]: row["status"]
            for row in conn.execute("SELECT tic_id, status FROM star_state")
        }
        assert states[1] == "automated_survivor"  # invalidated event ignored
        assert states[2] == "automated_survivor"  # active checkpoint override
        assert states[3] == "false_positive"  # human outcome outranks all
        assert states[4] == "no_transit_detected"
        assert states[5] == "common_mode_systematic"  # outranks science lead
        assert states[6] == "search_error"

        # The superseded camp1 row for star 2 is preserved as history.
        history = conn.execute(
            "SELECT COUNT(*) FROM evidence "
            "WHERE tic_id = 2 AND kind = 'screening' AND affects_state = 0"
        ).fetchone()[0]
        assert history == 1
        # Only e4 survives: e2 was invalidated and e3 carries no tic_id.
        assert report["human_outcomes"] == 1
        non_voting_science = conn.execute(
            "SELECT verdict, affects_state FROM evidence "
            "WHERE tic_id = 1 AND kind = 'science'"
        ).fetchone()
        assert non_voting_science["verdict"] is None
        assert non_voting_science["affects_state"] == 0
    finally:
        conn.close()


def test_reimport_is_idempotent(tmp_path: Path) -> None:
    _build_workspace(tmp_path)
    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        first = import_workspace(conn, tmp_path)
        assert first["evidence_added"] > 0
        second = import_workspace(conn, tmp_path)
        assert second["evidence_added"] == 0
        assert second["evidence_already_present"] == first["evidence_added"]
        assert second["status_counts"] == first["status_counts"]
    finally:
        conn.close()


def test_orphan_reports_are_preserved_as_history(tmp_path: Path) -> None:
    _build_workspace(tmp_path)
    # A summary-less campaign directory holding mixed-version reports: the
    # sector100_spoc situation in miniature.
    orphan_dir = tmp_path / "results" / "campaign" / "orphan"
    _write_json(
        orphan_dir / "batch_progress.json",
        {
            "state": "interrupted",
            "settings": {"data_pipeline_version": "processed-lc-v3-edge-safe"},
            "results": [],
        },
    )
    _write_json(
        orphan_dir / "TIC_77_s100_residual.json",
        {
            "data": {"tic_id": 77},
            "search_configuration": {
                "data_pipeline_version": "processed-lc-v2"
            },
            "strongest_residual_signal": {"period_days": 6.85},
            "automated_triage": {"passes": True},
        },
    )
    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        import_workspace(conn, tmp_path)
        row = conn.execute(
            "SELECT affects_state, signature FROM evidence "
            "WHERE tic_id = 77 AND kind = 'screening_report'"
        ).fetchone()
        assert row is not None
        assert row["affects_state"] == 0
        assert row["signature"] == "legacy:processed-lc-v2"
        # History rows never vote: star 77 has no current state.
        state = conn.execute(
            "SELECT COUNT(*) FROM star_state WHERE tic_id = 77"
        ).fetchone()[0]
        assert state == 0
        # Parity still holds: the exporter cannot see orphan reports either.
        assert parity_check(conn, tmp_path)["match"]
    finally:
        conn.close()
