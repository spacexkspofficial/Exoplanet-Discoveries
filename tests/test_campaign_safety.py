import argparse
import json
from pathlib import Path

import pytest
from filelock import FileLock

from exohunt.cli import (
    LEGACY_COMMON_MODE_REASON,
    LEGACY_COMMON_MODE_REASONS,
    _batch_hunt,
    _is_transient_search_error,
    _load_reusable_report,
    _quarantine_invalid_common_mode,
    _read_target_rows,
)


def _args(output_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        output_dir=str(output_dir),
        author="TESScut",
        cadence_seconds=158.0,
        min_period=0.5,
        max_period=13.0,
        mask_width=1.5,
        allow_no_known=True,
    )


def test_common_mode_midpoint_density_never_rejects_targets() -> None:
    rows = [
        {
            "tic_id": index,
            "status": "rejected",
            "rejection_reasons": LEGACY_COMMON_MODE_REASON,
            "common_mode_peer_count": 100,
        }
        for index in range(100)
    ]

    report = _quarantine_invalid_common_mode(rows)

    assert report["automatic_rejection_applied"] is False
    assert report["legacy_rows_repaired"] == 100
    assert all(row["status"] == "survivor" for row in rows)
    assert all(row["rejection_reasons"] == "" for row in rows)
    assert all("common_mode_peer_count" not in row for row in rows)

    older_row = {
        "tic_id": 999,
        "status": "rejected",
        "rejection_reasons": (
            "transit midpoint is shared by at least three campaign targets"
        ),
    }
    _quarantine_invalid_common_mode([older_row])
    assert older_row["status"] == "survivor"
    assert not any(
        reason in older_row["rejection_reasons"]
        for reason in LEGACY_COMMON_MODE_REASONS
    )


def test_report_reuse_requires_matching_identity_and_complete_plot(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path)
    report_path = tmp_path / "target.json"
    report = {
        "data": {
            "target": "TIC 42",
            "tic_id": 42,
            "requested_sectors": [105],
            "author": "TESScut",
            "requested_cadence_seconds": 158.0,
        },
        "search_configuration": {
            "author": "TESScut",
            "cadence_seconds": 158.0,
            "period_range_days": [0.5, 13.0],
            "mask_width": 1.5,
            "allow_no_known": True,
            "data_pipeline_version": "tesscut-bgsub-commonmode-quarantined-v4",
        },
        "automated_triage": {"passes": True, "rejection_reasons": []},
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")

    assert (
        _load_reusable_report(
            report_path,
            target="TIC 42",
            tic_id=42,
            sectors=[105],
            args=args,
            allow_legacy=False,
        )
        is None
    )

    report["automated_triage"] = {
        "passes": False,
        "rejection_reasons": ["low signal"],
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    assert (
        _load_reusable_report(
            report_path,
            target="TIC 42",
            tic_id=42,
            sectors=[105],
            args=args,
            allow_legacy=False,
        )
        == report
    )

    report["automated_triage"] = {"passes": True, "rejection_reasons": []}
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_path.with_suffix(".png").write_bytes(b"plot")
    assert (
        _load_reusable_report(
            report_path,
            target="TIC 42",
            tic_id=42,
            sectors=[105],
            args=args,
            allow_legacy=False,
        )
        == report
    )
    assert (
        _load_reusable_report(
            report_path,
            target="TIC 43",
            tic_id=43,
            sectors=[105],
            args=args,
            allow_legacy=False,
        )
        is None
    )


def test_batch_hunt_refuses_a_duplicate_campaign_worker(tmp_path: Path) -> None:
    output_dir = tmp_path / "campaign"
    output_dir.mkdir()
    lock = FileLock(str(output_dir / ".batch-hunt.lock"))
    lock.acquire(timeout=0)
    try:
        with pytest.raises(RuntimeError, match="Another batch worker"):
            _batch_hunt(argparse.Namespace(output_dir=str(output_dir)))
    finally:
        lock.release()


def test_target_csv_validation_rejects_duplicate_rows(tmp_path: Path) -> None:
    target_path = tmp_path / "targets.csv"
    target_path.write_text(
        "target,tic_id,sectors\nTIC 42,42,105\nTIC 42,42,105\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicates TIC 42"):
        _read_target_rows(target_path)


def test_transient_search_error_detection_is_conservative() -> None:
    assert _is_transient_search_error(TimeoutError("read timed out"))
    assert _is_transient_search_error(RuntimeError("HTTP 503 from MAST"))
    assert not _is_transient_search_error(ValueError("bad sector"))
