"""Measure the absolute-time dip registry against a finished campaign.

MASTER_PLAN.md section 3.6 specifies cohorts per sector-camera-CCD, but the
partition that a given cohort can actually support is an empirical question,
not a design choice: too fine and each detector falls under the minimum-star
floor, too coarse and a detector-local artifact is diluted below the fraction
floor. This measures all three granularities from the same reports so the
answer is measured rather than argued.

Nothing here re-runs a search or downloads anything. T4 is defined as pure
post-processing (section 1.3 tier table), and every input is already in the
durable reports: each carries its own `population_bins`, and each carries the
fitted event centres the veto consumes. That is what makes the registry
re-derivable, and re-thresholdable, forever.

The projected triage effect is a *replay*, in the same sense as
`measure_p2_catalog_matching.py`: it applies the production veto helper to
frozen outputs and reports what would change. It is not a claim that any
signal is or is not astrophysical.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from exohunt.config import CURRENT_CONFIG
from exohunt.population import (
    cohort_key,
    decode_star_bins,
    registries_from_reports,
    registry_windows,
)
from exohunt.vetoes import dip_window_veto

# The epochs the cohort was selected around. They are coarse bin labels, not
# transit times (PROGRESS correction 9), so they are reported as context for
# the registry's windows rather than used as a pass/fail tolerance.
ARTIFACT_EPOCHS_BTJD = (4074.4, 4080.8)

GRANULARITIES = ("ccd", "camera", "sector")


def _read_detector_map(path: Path) -> dict[int, tuple[int, int]]:
    """Recover camera/CCD per TIC from an official sector target list."""

    with path.open(encoding="utf-8") as handle:
        rows = [line for line in handle if not line.startswith("#")]
    mapping: dict[int, tuple[int, int]] = {}
    for row in csv.DictReader(rows):
        try:
            mapping[int(row["TICID"])] = (int(row["Camera"]), int(row["CCD"]))
        except (KeyError, TypeError, ValueError):
            continue
    return mapping


def _load_reports(directory: Path) -> list[dict[str, Any]]:
    reports = []
    for path in sorted(directory.glob("*_residual.json")):
        try:
            reports.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return reports


def _tic_of(report: dict[str, Any]) -> int | None:
    data = report.get("data") or {}
    for key in ("tic_id", "tic"):
        value = data.get(key) if isinstance(data, dict) else None
        if value is None:
            value = report.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _key_for(
    granularity: str,
    sector: object,
    detector: tuple[int, int] | None,
) -> str:
    if granularity == "sector" or detector is None:
        return cohort_key(sector, None, None)
    camera, ccd = detector
    if granularity == "camera":
        return cohort_key(sector, camera, None)
    return cohort_key(sector, camera, ccd)


def _window_covers(spans: Iterable[tuple[float, float]], epoch: float) -> bool:
    return any(start <= epoch <= stop for start, stop in spans)


def measure(
    reports: list[dict[str, Any]],
    detectors: dict[int, tuple[int, int]],
    sector: object,
) -> dict[str, Any]:
    cfg = CURRENT_CONFIG.population
    out: dict[str, Any] = {
        "reports": len(reports),
        "settings": {
            "dip_bin_minutes": cfg.dip_bin_minutes,
            "dip_star_sigma": cfg.dip_star_sigma,
            "dip_min_fraction": cfg.dip_min_fraction,
            "dip_min_stars": cfg.dip_min_stars,
        },
        "artifact_epochs_btjd": list(ARTIFACT_EPOCHS_BTJD),
        "granularities": {},
    }
    with_bins = sum(1 for r in reports if r.get("population_bins"))
    out["reports_with_population_bins"] = with_bins
    resolved = sum(1 for r in reports if (_tic_of(r) or -1) in detectors)
    out["reports_with_detector"] = resolved

    for granularity in GRANULARITIES:
        keyed: list[tuple[str, Any]] = []
        sizes: Counter[str] = Counter()
        for report in reports:
            tic = _tic_of(report)
            key = _key_for(granularity, sector, detectors.get(tic or -1))
            bins = report.get("population_bins")
            if bins and decode_star_bins(bins):
                keyed.append((key, bins))
                sizes[key] += 1
        registries = registries_from_reports(keyed)

        windows_by_key = {
            key: registry_windows(registry)
            for key, registry in registries.items()
        }
        total_windows = sum(len(v) for v in windows_by_key.values())
        usable = {k: n for k, n in sizes.items() if n >= cfg.dip_min_stars}

        # Replay the production veto over the frozen event centres.
        vetoed_reports = 0
        vetoed_events = 0
        dropped_below_minimum = 0
        passes_before = 0
        passes_after = 0
        for report in reports:
            tic = _tic_of(report)
            key = _key_for(granularity, sector, detectors.get(tic or -1))
            t3 = report.get("t3_vetoes") or {}
            checks = t3.get("checks") or {}
            support = checks.get("event_support") or {}
            events = support.get("events") or []
            centres = [
                float(e["center"])
                for e in events
                if isinstance(e, dict) and e.get("supported")
            ]
            minimum = int(t3.get("minimum_supported_events") or 0)
            triage = report.get("automated_triage") or {}
            passed = bool(triage.get("passes"))
            passes_before += int(passed)
            spans = windows_by_key.get(key, [])
            if not spans or not centres:
                passes_after += int(passed)
                continue
            veto = dip_window_veto(centres, spans)
            flagged = int(veto["events_in_systematic_windows"])
            if flagged:
                vetoed_reports += 1
                vetoed_events += flagged
            clean = int(veto["events_clean"])
            still = passed and clean >= minimum
            if passed and not still:
                dropped_below_minimum += 1
            passes_after += int(still)

        out["granularities"][granularity] = {
            "cohorts": len(sizes),
            "cohorts_at_or_above_star_floor": len(usable),
            "stars_in_usable_cohorts": sum(usable.values()),
            "stars_below_star_floor": sum(sizes.values()) - sum(usable.values()),
            "cohort_sizes": dict(sorted(sizes.items())),
            "registered_windows_total": total_windows,
            "cohorts_with_windows": sum(
                1 for v in windows_by_key.values() if v
            ),
            "windows": {k: v for k, v in sorted(windows_by_key.items()) if v},
            "artifact_epoch_coverage": {
                f"{epoch}": sorted(
                    k
                    for k, spans in windows_by_key.items()
                    if _window_covers(spans, epoch)
                )
                for epoch in ARTIFACT_EPOCHS_BTJD
            },
            "replay": {
                "reports_with_a_vetoed_event": vetoed_reports,
                "events_vetoed": vetoed_events,
                "passes_before": passes_before,
                "passes_after": passes_after,
                "passes_lost_to_registry": dropped_below_minimum,
            },
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_dir", type=Path)
    parser.add_argument("--target-list", type=Path, required=True)
    parser.add_argument("--sector", default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    reports = _load_reports(args.campaign_dir)
    if not reports:
        parser.error(f"no *_residual.json reports under {args.campaign_dir}")
    detectors = _read_detector_map(args.target_list)
    result = measure(reports, detectors, args.sector)

    print(f"reports                     : {result['reports']}")
    print(f"  with population_bins      : {result['reports_with_population_bins']}")
    print(f"  with camera/CCD resolved  : {result['reports_with_detector']}")
    for granularity in GRANULARITIES:
        block = result["granularities"][granularity]
        print(f"\n--- {granularity} ---")
        print(
            f"  cohorts {block['cohorts']}, "
            f"at/above {result['settings']['dip_min_stars']}-star floor "
            f"{block['cohorts_at_or_above_star_floor']}, "
            f"stars stranded {block['stars_below_star_floor']}"
        )
        print(
            f"  registered windows: {block['registered_windows_total']} "
            f"across {block['cohorts_with_windows']} cohort(s)"
        )
        for epoch, hits in block["artifact_epoch_coverage"].items():
            print(f"  BTJD {epoch}: {'covered by ' + ', '.join(hits) if hits else 'no window'}")
        replay = block["replay"]
        print(
            f"  replay: {replay['events_vetoed']} events discounted across "
            f"{replay['reports_with_a_vetoed_event']} report(s); "
            f"triage passes {replay['passes_before']} -> {replay['passes_after']}"
        )

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(f"\nwrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
