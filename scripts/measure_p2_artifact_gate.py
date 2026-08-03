"""Measure the P2 artifact/retention gate from shipping-path reports.

The empirical null draws two control epochs uniformly over the observation
span and applies each fitted ephemeris with the same duration-scaled phase
tolerance used for the two named artifact epochs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


ARTIFACT_EPOCHS = (4074.4, 4080.8)
DETECTION_SNR = 7.1
MINIMUM_PHASE_TOLERANCE_DAYS = 0.02


def _folded_offset(epoch: float, period: np.ndarray, transit_time: np.ndarray) -> np.ndarray:
    return np.abs((epoch - transit_time + period / 2) % period - period / 2)


def _alignment_flags(
    epochs: tuple[float, float],
    period: np.ndarray,
    transit_time: np.ndarray,
    tolerance: np.ndarray,
) -> np.ndarray:
    return np.minimum(
        _folded_offset(epochs[0], period, transit_time),
        _folded_offset(epochs[1], period, transit_time),
    ) <= tolerance


def measure(
    results_dir: Path,
    *,
    draws: int,
    seed: int,
    label: str,
) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    for report_path in sorted(results_dir.glob("TIC_*_residual.json")):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        signal = report["strongest_residual_signal"]
        data = report["data"]
        detrending = data["detrending"]
        observation = report["observation_window"]
        edge_support = report.get("edge_event_support")
        rows.append(
            {
                "tic_id": int(data["tic_id"]),
                "period_days": float(signal["period_days"]),
                "transit_time": float(signal["transit_time"]),
                "duration_hours": float(signal["duration_hours"]),
                "depth_snr": float(signal["depth_snr"]),
                "passes_triage": bool(report["automated_triage"]["passes"]),
                "observation_start_btjd": float(observation["start_btjd"]),
                "observation_end_btjd": float(observation["end_btjd"]),
                "detrend_method": str(detrending["method"]),
                "retention": (
                    float(detrending["retained_cadences"])
                    / float(detrending["input_cadences"])
                ),
                "edge_dependent": bool(
                    isinstance(edge_support, dict)
                    and edge_support.get("edge_dependent")
                ),
                "edge_lane": (
                    edge_support.get("lane")
                    if isinstance(edge_support, dict)
                    else None
                ),
                "vetting_tier": report.get("followup_classification", {}).get(
                    "vetting_tier"
                ),
            }
        )
    if not rows:
        raise RuntimeError(f"No per-target reports found in {results_dir}")

    period = np.asarray([row["period_days"] for row in rows], dtype=float)
    transit_time = np.asarray([row["transit_time"] for row in rows], dtype=float)
    tolerance = np.maximum(
        np.asarray([row["duration_hours"] for row in rows], dtype=float) / 48.0,
        MINIMUM_PHASE_TOLERANCE_DAYS,
    )
    observed_flags = _alignment_flags(
        ARTIFACT_EPOCHS, period, transit_time, tolerance
    )
    snr = np.asarray([row["depth_snr"] for row in rows], dtype=float)
    passes = np.asarray([row["passes_triage"] for row in rows], dtype=bool)
    retention = np.asarray([row["retention"] for row in rows], dtype=float)

    span_start = min(float(row["observation_start_btjd"]) for row in rows)
    span_end = max(float(row["observation_end_btjd"]) for row in rows)
    rng = np.random.default_rng(seed)
    null_counts = np.empty(draws, dtype=int)
    for index in range(draws):
        controls = tuple(float(value) for value in rng.uniform(span_start, span_end, 2))
        null_counts[index] = int(
            np.count_nonzero(
                _alignment_flags(controls, period, transit_time, tolerance)
            )
        )

    observed_count = int(np.count_nonzero(observed_flags))
    null_mean = float(np.mean(null_counts))
    for index, row in enumerate(rows):
        offsets = [
            float(_folded_offset(epoch, period[index:index + 1], transit_time[index:index + 1])[0])
            for epoch in ARTIFACT_EPOCHS
        ]
        nearest_index = int(np.argmin(offsets))
        row.update(
            {
                "phase_tolerance_days": round(float(tolerance[index]), 5),
                "nearest_artifact_epoch": ARTIFACT_EPOCHS[nearest_index],
                "folded_offset_days": round(offsets[nearest_index], 5),
                "aligns_with_artifact_epoch": bool(observed_flags[index]),
                "above_detection_gate": bool(snr[index] >= DETECTION_SNR),
                "detects_artifact": bool(
                    observed_flags[index] and snr[index] >= DETECTION_SNR
                ),
            }
        )

    return {
        "label": label,
        "results_dir": str(results_dir.resolve()),
        "targets": len(rows),
        "detrend_methods": sorted({str(row["detrend_method"]) for row in rows}),
        "gate_1_artifact_regression": {
            "definition": (
                "fitted ephemeris predicts a transit within phase tolerance of "
                "BTJD 4074.4 or 4080.8 (folded, not raw epoch difference)"
            ),
            "aligns_regardless_of_snr": observed_count,
            "detects_at_artifact_epoch": int(
                np.count_nonzero(observed_flags & (snr >= DETECTION_SNR))
            ),
            "detects_and_passes_triage": int(
                np.count_nonzero(observed_flags & passes)
            ),
            "empirical_null": {
                "draws": draws,
                "seed": seed,
                "control_epoch_range_btjd": [span_start, span_end],
                "mean_alignments": round(null_mean, 5),
                "enrichment": round(observed_count / null_mean, 5),
                "one_sided_p": round(
                    (int(np.count_nonzero(null_counts >= observed_count)) + 1)
                    / (draws + 1),
                    6,
                ),
            },
        },
        "gate_2_retention": {
            "measured_targets": len(rows),
            "median": round(float(np.median(retention)), 5),
            "mean": round(float(np.mean(retention)), 5),
            "min": round(float(np.min(retention)), 5),
            "max": round(float(np.max(retention)), 5),
            "requirement": ">= 0.85",
            "passed": bool(float(np.median(retention)) >= 0.85),
        },
        "edge_diagnostic_lane": {
            "edge_dependent_signals": sum(
                bool(row["edge_dependent"]) for row in rows
            ),
            "edge_only_diagnostic_tier": sum(
                row["vetting_tier"] == "edge_only_diagnostic" for row in rows
            ),
            "edge_dependent_passes_triage": sum(
                bool(row["edge_dependent"]) and bool(row["passes_triage"])
                for row in rows
            ),
        },
        "survivors": int(np.count_nonzero(passes)),
        "per_target": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results_dir", type=Path)
    parser.add_argument("--draws", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=20_260_727)
    parser.add_argument("--label", default="P2 artifact gate")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    measured = measure(
        args.results_dir,
        draws=args.draws,
        seed=args.seed,
        label=args.label,
    )
    output = args.output or args.results_dir / "p2_gate_measurement.json"
    output.write_text(json.dumps(measured, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in measured.items() if key != "per_target"}, indent=2))
    print(f"Saved {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
