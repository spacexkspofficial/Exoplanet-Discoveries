"""Compare pre-T3 and T3-wired reports on one frozen shipping cohort."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


PRE_T3_SCIENCE_FIELDS = (
    "warning",
    "data",
    "observation_window",
    "search_mode",
    "catalog_checked",
    "known_signal_masks",
    "known_signal_mask_limitations",
    "mask_summary",
    "phase_curve",
    "strongest_residual_signal",
    "search_grid",
    "top_period_peaks",
    "harmonic_checks",
    "relations_to_known_periods",
    "catalog_epoch_agreement",
    "relations_to_masked_periods",
    "sensitivity_probe",
    "deeper_vetting",
)


def _load_reports(path: Path) -> dict[int, dict[str, object]]:
    reports: dict[int, dict[str, object]] = {}
    for report_path in sorted(path.glob("TIC_*_residual.json")):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        tic_id = int(report["data"]["tic_id"])
        if tic_id in reports:
            raise ValueError(f"Duplicate TIC {tic_id} in {path}")
        reports[tic_id] = report
    return reports


def _passes(report: dict[str, object]) -> bool:
    triage = report.get("automated_triage")
    return bool(triage.get("passes")) if isinstance(triage, dict) else False


def _reasons(report: dict[str, object]) -> list[str]:
    triage = report.get("automated_triage")
    values = triage.get("rejection_reasons", []) if isinstance(triage, dict) else []
    return [str(value) for value in values] if isinstance(values, list) else []


def _t3(report: dict[str, object]) -> dict[str, object]:
    value = report.get("t3_vetoes")
    return value if isinstance(value, dict) else {}


def _checks(report: dict[str, object]) -> dict[str, object]:
    value = _t3(report).get("checks")
    return value if isinstance(value, dict) else {}


def _verdict(
    report: dict[str, object],
    check_name: str,
) -> str:
    check = _checks(report).get(check_name)
    if not isinstance(check, dict):
        return "missing"
    return str(check.get("verdict", "missing"))


def _pre_t3_payload(report: dict[str, object]) -> dict[str, object]:
    return {field: report.get(field) for field in PRE_T3_SCIENCE_FIELDS}


def measure(
    baseline_dir: Path,
    *,
    t3_dir: Path,
) -> dict[str, object]:
    baseline = _load_reports(baseline_dir)
    t3_reports = _load_reports(t3_dir)
    common = sorted(set(baseline) & set(t3_reports))

    transitions: Counter[str] = Counter()
    lost: list[int] = []
    gained: list[int] = []
    added_reasons: Counter[str] = Counter()
    removed_reasons: Counter[str] = Counter()
    for tic_id in common:
        left_passes = _passes(baseline[tic_id])
        right_passes = _passes(t3_reports[tic_id])
        transitions[
            ("survivor" if left_passes else "rejected")
            + "_to_"
            + ("survivor" if right_passes else "rejected")
        ] += 1
        if left_passes and not right_passes:
            lost.append(tic_id)
        if not left_passes and right_passes:
            gained.append(tic_id)
        left_reasons = set(_reasons(baseline[tic_id]))
        right_reasons = set(_reasons(t3_reports[tic_id]))
        added_reasons.update(right_reasons - left_reasons)
        removed_reasons.update(left_reasons - right_reasons)

    verdict_counts = {
        name: dict(
            sorted(
                Counter(
                    _verdict(report, name)
                    for report in t3_reports.values()
                ).items()
            )
        )
        for name in (
            "duration_density",
            "depth_physicality",
            "odd_even",
            "full_phase_secondary",
        )
    }
    insufficient_support = 0
    supported_event_counts: Counter[int] = Counter()
    for report in t3_reports.values():
        t3 = _t3(report)
        support = _checks(report).get("event_support")
        support = support if isinstance(support, dict) else {}
        supported = int(support.get("supported_events", 0))
        minimum = int(t3.get("minimum_supported_events", 0))
        supported_event_counts[supported] += 1
        insufficient_support += supported < minimum

    exact = {
        "observation_window": sum(
            baseline[tic_id].get("observation_window")
            == t3_reports[tic_id].get("observation_window")
            for tic_id in common
        ),
        "strongest_residual_signal": sum(
            baseline[tic_id].get("strongest_residual_signal")
            == t3_reports[tic_id].get("strongest_residual_signal")
            for tic_id in common
        ),
        "search_grid": sum(
            baseline[tic_id].get("search_grid")
            == t3_reports[tic_id].get("search_grid")
            for tic_id in common
        ),
        "complete_pre_t3_science_payload": sum(
            _pre_t3_payload(baseline[tic_id])
            == _pre_t3_payload(t3_reports[tic_id])
            for tic_id in common
        ),
    }
    t3_rejection_reasons = Counter(
        reason
        for report in t3_reports.values()
        for reason in (
            _t3(report).get("rejection_reasons", [])
            if isinstance(_t3(report).get("rejection_reasons"), list)
            else []
        )
    )
    review_flags = Counter(
        flag
        for report in t3_reports.values()
        for flag in (
            _t3(report).get("review_flags", [])
            if isinstance(_t3(report).get("review_flags"), list)
            else []
        )
    )
    result = {
        "schema_version": 1,
        "scope": (
            "shipping-path T3 A/B over one frozen 150-target cohort; search "
            "outputs must remain exact while post-search veto evidence changes"
        ),
        "identity": {
            "baseline_reports": len(baseline),
            "t3_reports": len(t3_reports),
            "common_reports": len(common),
            "exact_tic_set": set(baseline) == set(t3_reports),
            "missing_from_baseline": sorted(set(t3_reports) - set(baseline)),
            "missing_from_t3": sorted(set(baseline) - set(t3_reports)),
        },
        "arms": {
            "baseline": {
                "passes": sum(_passes(report) for report in baseline.values()),
                "rejected": sum(
                    not _passes(report) for report in baseline.values()
                ),
            },
            "t3": {
                "passes": sum(_passes(report) for report in t3_reports.values()),
                "rejected": sum(
                    not _passes(report) for report in t3_reports.values()
                ),
                "routes_to_eb_lane": sum(
                    bool(_t3(report).get("routes_to_eb_lane"))
                    for report in t3_reports.values()
                ),
                "insufficient_event_support": insufficient_support,
                "check_verdict_counts": verdict_counts,
                "supported_event_counts": dict(
                    sorted(supported_event_counts.items())
                ),
                "t3_rejection_reason_counts": dict(
                    sorted(t3_rejection_reasons.items())
                ),
                "t3_review_flag_counts": dict(sorted(review_flags.items())),
            },
        },
        "comparison": {
            "transition_counts": dict(sorted(transitions.items())),
            "lost_survivor_tic_ids": lost,
            "gained_survivor_tic_ids": gained,
            "added_rejection_reason_counts": dict(sorted(added_reasons.items())),
            "removed_rejection_reason_counts": dict(
                sorted(removed_reasons.items())
            ),
            "exact_fields": exact,
        },
    }
    result["acceptance_checks"] = {
        "exact_cohort": bool(result["identity"]["exact_tic_set"]),
        "observation_windows_exact": exact["observation_window"] == len(common),
        "strongest_signals_exact": (
            exact["strongest_residual_signal"] == len(common)
        ),
        "search_grids_exact": exact["search_grid"] == len(common),
        "complete_pre_t3_science_payload_exact": (
            exact["complete_pre_t3_science_payload"] == len(common)
        ),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-dir", required=True, type=Path)
    parser.add_argument("--t3-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = measure(args.baseline_dir, t3_dir=args.t3_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2))
    print(f"Saved {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
