import json
import os
from pathlib import Path

from exohunt.retention import (
    prune_fits_cache,
    prune_historical_rejected_plots,
    prune_rejected_plots,
)


def test_prune_fits_cache_deletes_oldest_and_preserves_other_files(tmp_path: Path) -> None:
    cache = tmp_path / "data" / "lightkurve"
    cache.mkdir(parents=True)
    files = [cache / f"cutout-{index}.fits" for index in range(3)]
    for index, path in enumerate(files):
        path.write_bytes(bytes([index]) * 10)
        os.utime(path, (100 + index, 100 + index))
    note = cache / "catalog.json"
    note.write_text("keep", encoding="utf-8")

    preview = prune_fits_cache(cache, max_bytes=10, dry_run=True)
    assert preview["files_deleted"] == 2
    assert all(path.exists() for path in files)

    report = prune_fits_cache(cache, max_bytes=10)
    assert report["bytes_before"] == 30
    assert report["bytes_after"] == 10
    assert report["files_deleted"] == 2
    assert not files[0].exists()
    assert not files[1].exists()
    assert files[2].exists()
    assert note.exists()


def test_prune_rejected_plots_keeps_survivors_and_outside_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    results = workspace / "results"
    results.mkdir(parents=True)
    rejected = results / "rejected.png"
    survivor = results / "survivor.png"
    outside = workspace / "outside.png"
    for path in (rejected, survivor, outside):
        path.write_bytes(b"plot")

    rows = [
        {"status": "rejected", "plot": "results/rejected.png"},
        {"status": "survivor", "plot": "results/survivor.png"},
        {"status": "rejected", "plot": "outside.png"},
    ]
    report = prune_rejected_plots(
        rows, results_root=results, workspace_root=workspace
    )

    assert report["files_deleted"] == 1
    assert not rejected.exists()
    assert survivor.exists()
    assert outside.exists()


def test_historical_prune_reads_batch_summaries(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    campaign = workspace / "results" / "campaign" / "one"
    campaign.mkdir(parents=True)
    plot = campaign / "target.png"
    plot.write_bytes(b"plot")
    (campaign / "batch_summary.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "status": "rejected",
                        "plot": "results/campaign/one/target.png",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = prune_historical_rejected_plots(
        workspace / "results", workspace_root=workspace
    )

    assert report["summaries_read"] == 1
    assert report["files_deleted"] == 1
    assert not plot.exists()
