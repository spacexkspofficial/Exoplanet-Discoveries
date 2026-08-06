from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_p3_calibration.py"
SPEC = importlib.util.spec_from_file_location("run_p3_calibration", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _report(period: float, epoch: float) -> dict[str, object]:
    return {
        "observation_window": {"start_btjd": 100.0, "end_btjd": 110.0},
        "strongest_residual_signal": {
            "period_days": period,
            "transit_time": epoch,
            "duration_hours": 2.0,
        },
    }


def test_epoch_histogram_detects_shared_epoch() -> None:
    reports = [_report(2.0, 101.0) for _ in range(50)]
    result = MODULE._epoch_histogram(reports)
    assert result["maximum_enrichment"] > 2.0
    assert result["maximum_bin"]["aligned_signals"] == 50
    # Release evidence must remain plain-JSON serializable rather than leaking
    # NumPy scalar types from the histogram grid.
    json.dumps(result)
