"""Tests for the locked search-grid cohort builder."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from scripts.build_p2_search_grid_cohort import build


def test_builder_preserves_identity_and_adds_saved_mass(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text(
        "target,tic_id,sectors,stellar_radius_solar\n"
        "TIC 42,42,100,0.4\n"
        "TIC 43,43,100,0.5\n",
        encoding="utf-8",
    )
    context = tmp_path / "context"
    context.mkdir()
    (context / "TIC_42_cross_mission_context.json").write_text(
        json.dumps(
            {
                "tic": {
                    "stellar_radius_solar": 0.41,
                    "stellar_mass_solar": 0.35,
                }
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "cohort.csv"

    manifest = build(
        source,
        context_dir=context,
        output_csv=output,
        limit=2,
        context_label="results/context",
    )
    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert [(row["tic_id"], row["sectors"]) for row in rows] == [
        ("42", "100"),
        ("43", "100"),
    ]
    assert rows[0]["stellar_radius_solar"] == "0.41"
    assert rows[0]["stellar_mass_solar"] == "0.35"
    assert rows[1]["stellar_mass_solar"] == ""
    assert manifest["complete_mass_and_radius"] == 1
    assert manifest["solar_density_fallback"] == 1
    assert manifest["context_dir"] == "results/context"
    identity = b"TIC 42|42|100\nTIC 43|43|100"
    assert manifest["cohort_identity_sha256"] == hashlib.sha256(
        identity
    ).hexdigest()
