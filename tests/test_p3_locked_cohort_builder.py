from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "build_p3_locked_cohort.py"
SPEC = importlib.util.spec_from_file_location("build_p3_locked_cohort", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_builder_locks_prefix_and_copies_only_matching_mass(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    output = tmp_path / "locked.csv"
    prefix = tmp_path / "prefix.csv"
    rows = [
        {
            "target": f"TIC {tic}",
            "tic_id": tic,
            "sectors": "100",
            "stellar_radius_solar": 0.4,
        }
        for tic in range(1, 5)
    ]
    _write(source, rows)
    _write(
        prefix,
        [
            {**rows[0], "stellar_mass_solar": 0.3},
            {**rows[1], "stellar_mass_solar": ""},
        ],
    )

    manifest = MODULE.build(
        source,
        output_csv=output,
        limit=3,
        enriched_prefix_csv=prefix,
    )
    with output.open(newline="", encoding="utf-8") as handle:
        locked = list(csv.DictReader(handle))
    assert [row["tic_id"] for row in locked] == ["1", "2", "3"]
    assert locked[0]["stellar_mass_solar"] == "0.3"
    assert locked[2]["stellar_mass_solar"] == ""
    assert manifest["status"] == "locked_before_p3_outcome_measurement"
    assert manifest["copied_stellar_mass_rows"] == 1


def test_builder_rejects_identity_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    prefix = tmp_path / "prefix.csv"
    _write(source, [{"target": "TIC 1", "tic_id": 1, "sectors": 100}])
    _write(
        prefix,
        [
            {
                "target": "TIC 2",
                "tic_id": 2,
                "sectors": 100,
                "stellar_mass_solar": 1,
            }
        ],
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        MODULE.build(
            source,
            output_csv=tmp_path / "out.csv",
            limit=1,
            enriched_prefix_csv=prefix,
        )
