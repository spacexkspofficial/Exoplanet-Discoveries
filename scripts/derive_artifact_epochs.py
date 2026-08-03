"""Derive artifact epochs from the reduction they will be measured against.

PROGRESS correction 22: section 2.3's epochs (BTJD 4074.4, 4080.8) were taken
from historical TESScut evidence and then used to judge a SPOC cohort, where
SPOC's own quality masking has removed the entire interval -- zero of 371
stars have a cadence at the most-shared epoch in the ledger. Folding fitted
ephemerides against an epoch that was never searched measures phase
coincidence, not artifact contamination, so the gate cannot discriminate
between detrending arms on that reduction.

This finds epochs where stars in *this* campaign actually dipped together,
using the per-report `population_bins` the T4 registry already records. No
downloads and no re-analysis: it reads durable evidence only.

The selection is deliberately conservative. A bin qualifies only when its
shared-dip fraction is improbable against the cohort's own background dip
rate, so an ordinary bin where a handful of unrelated stars happen to dim is
not promoted into a gate threshold. Adjacent qualifying bins are merged and
reported by their peak, because one observatory event spans several bins.

The output is a candidate list for review, not an automatic gate change.
Whether an epoch reflects an instrument event or a genuine astrophysical
coincidence is not decidable from dip counts alone.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

from exohunt.config import CURRENT_CONFIG
from exohunt.population import cohort_key, decode_star_bins


def _binomial_tail(observed: int, trials: int, probability: float) -> float:
    """P(X >= observed) for X ~ Binomial(trials, probability)."""

    if observed <= 0:
        return 1.0
    if probability <= 0.0:
        return 0.0
    if probability >= 1.0:
        return 1.0
    total = 0.0
    for k in range(observed, trials + 1):
        total += math.exp(
            math.lgamma(trials + 1)
            - math.lgamma(k + 1)
            - math.lgamma(trials - k + 1)
            + k * math.log(probability)
            + (trials - k) * math.log1p(-probability)
        )
        if total >= 1.0:
            return 1.0
    return total


def derive(
    campaign_dir: Path,
    *,
    per_cohort: bool,
    max_epochs: int,
    significance: float,
) -> dict[str, Any]:
    cfg = CURRENT_CONFIG.population
    bin_days = cfg.dip_bin_minutes / (24.0 * 60.0)

    # cohort key -> bin index -> counts
    observed: dict[str, Counter] = {}
    dipped: dict[str, Counter] = {}
    stars: Counter = Counter()

    for path in sorted(campaign_dir.glob("*_residual.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        payload = report.get("population_bins")
        flags = decode_star_bins(payload)
        if not flags:
            continue
        key = (
            str((payload or {}).get("cohort") or cohort_key())
            if per_cohort
            else "all"
        )
        stars[key] += 1
        obs = observed.setdefault(key, Counter())
        dip = dipped.setdefault(key, Counter())
        for index, is_dip in flags.items():
            obs[index] += 1
            if is_dip:
                dip[index] += 1

    cohorts: dict[str, Any] = {}
    for key, obs in sorted(observed.items()):
        dip = dipped[key]
        total_observed = sum(obs.values())
        total_dipped = sum(dip.values())
        # The cohort's own background rate: how often any star-bin dips at
        # all. Using this rather than a theoretical 3-sigma rate keeps the
        # test honest about the data's real noise properties.
        background = (total_dipped / total_observed) if total_observed else 0.0

        candidates = []
        for index, count in dip.items():
            trials = obs.get(index, 0)
            if trials < cfg.dip_min_stars:
                continue
            p = _binomial_tail(count, trials, background)
            if p <= significance:
                candidates.append(
                    {
                        "bin": index,
                        "btjd": round(index * bin_days, 4),
                        "stars_observing": trials,
                        "stars_dipping": count,
                        "fraction": round(count / trials, 4),
                        "p_value": p,
                    }
                )
        candidates.sort(key=lambda row: row["bin"])

        # Merge adjacent qualifying bins: one observatory event is wider than
        # a single 30-minute bin, and reporting each bin separately would
        # multiply-count it in the gate's null.
        events: list[dict[str, Any]] = []
        for row in candidates:
            if events and row["bin"] == events[-1]["_last_bin"] + 1:
                event = events[-1]
                event["_last_bin"] = row["bin"]
                event["stop_btjd"] = round((row["bin"] + 1) * bin_days, 4)
                event["bins"] += 1
                if row["fraction"] > event["peak_fraction"]:
                    event["peak_fraction"] = row["fraction"]
                    event["epoch_btjd"] = row["btjd"]
                    event["stars_dipping"] = row["stars_dipping"]
                    event["stars_observing"] = row["stars_observing"]
                    event["p_value"] = row["p_value"]
            else:
                events.append(
                    {
                        "_last_bin": row["bin"],
                        "start_btjd": row["btjd"],
                        "stop_btjd": round((row["bin"] + 1) * bin_days, 4),
                        "bins": 1,
                        "epoch_btjd": row["btjd"],
                        "peak_fraction": row["fraction"],
                        "stars_observing": row["stars_observing"],
                        "stars_dipping": row["stars_dipping"],
                        "p_value": row["p_value"],
                    }
                )
        for event in events:
            event.pop("_last_bin", None)
        events.sort(key=lambda e: -e["peak_fraction"])

        cohorts[key] = {
            "stars": stars[key],
            "background_dip_rate": round(background, 6),
            "qualifying_bins": len(candidates),
            "events": events[:max_epochs],
        }

    return {
        "campaign_dir": str(campaign_dir.resolve()),
        "settings": {
            "dip_bin_minutes": cfg.dip_bin_minutes,
            "dip_star_sigma": cfg.dip_star_sigma,
            "dip_min_stars": cfg.dip_min_stars,
            "significance": significance,
            "scope": "per sector-camera-CCD" if per_cohort else "whole campaign",
        },
        "cohorts": cohorts,
        "warning": (
            "Candidate epochs only. A shared dip is evidence that stars dimmed "
            "together, not proof of an instrument event, and these have not "
            "been adopted as a gate threshold."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_dir", type=Path)
    parser.add_argument(
        "--per-cohort",
        action="store_true",
        help="Derive per sector-camera-CCD instead of across the campaign.",
    )
    parser.add_argument("--max-epochs", type=int, default=5)
    parser.add_argument("--significance", type=float, default=1e-4)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    result = derive(
        args.campaign_dir,
        per_cohort=args.per_cohort,
        max_epochs=args.max_epochs,
        significance=args.significance,
    )

    for key, block in result["cohorts"].items():
        print(
            f"\n--- {key} --- {block['stars']} stars, background dip rate "
            f"{block['background_dip_rate']:.4%}, "
            f"{block['qualifying_bins']} qualifying bins"
        )
        if not block["events"]:
            print("   no epoch clears the significance threshold")
        for event in block["events"]:
            print(
                f"   BTJD {event['epoch_btjd']:>9.4f}  "
                f"{event['stars_dipping']:>4}/{event['stars_observing']:<4} "
                f"({event['peak_fraction']:>6.2%})  "
                f"spans {event['bins']} bin(s)  p={event['p_value']:.2e}"
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
