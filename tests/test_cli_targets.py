import argparse
import csv
import json
from pathlib import Path

from exohunt.cli import _make_sector_targets


def test_make_sector_targets_balances_detectors_and_excludes_searched(tmp_path: Path):
    source = tmp_path / "sector.csv"
    source.write_text(
        "\n".join(
            [
                "# official target list",
                "TICID,Camera,CCD,Tmag,RA,Dec",
                "1,1,1,7.1,10,-10",
                "2,1,1,7.2,20,-20",
                "3,1,2,7.3,30,-30",
                "4,2,1,7.4,40,-40",
                "5,2,2,7.5,50,-50",
            ]
        ),
        encoding="utf-8",
    )
    ledger = tmp_path / "events.jsonl"
    ledger.write_text(
        json.dumps({"kind": "campaign_completed", "tic_ids": [1]}) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "overnight.csv"
    args = argparse.Namespace(
        target_list=str(source),
        sector=105,
        output=str(output),
        limit=4,
        min_tmag=7.0,
        max_tmag=12.0,
        exclude_list=[],
        exclude_ledger=str(ledger),
    )

    assert _make_sector_targets(args) == 0
    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert [int(row["tic_id"]) for row in rows] == [2, 3, 4, 5]
    assert len({(row["camera"], row["ccd"]) for row in rows}) == 4
    manifest = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
    assert manifest["selected_count"] == 4
    assert manifest["criteria"]["excluded_completed_campaign_tic_ids"] == 1

