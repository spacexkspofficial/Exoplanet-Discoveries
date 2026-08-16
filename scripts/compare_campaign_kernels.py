"""Transition matrix between two campaigns over the same targets.

Correction 88 named this as the work owed before the v5-v7 kernel changes can
be imported: "per-campaign runs and an old->new transition matrix per
campaign". Correction 71 is the reason it has to be per-campaign -- the pooled
`--campaign-root` invocation under-reports against the established ledger, so
two numbers produced that way are not comparable and 5,301 must not be read as
a drop from 5,615.

This compares only the targets **both** campaigns have completed, so it is
meaningful against a run still in progress: the intersection grows as the new
campaign advances and the matrix stays honest at every point.

The interesting question is never the counts alone but *why* a star moved, so
this separates two very different causes:

  - the search returned something different (period, depth or S/N moved), or
  - the search returned the identical numbers and the verdict changed.

The second is what a veto-set change looks like, and it is invisible in a
count of survivors. Measured at 20% of the 2026-08-15 full-pool re-run, all 87
lost survivors were of the second kind, bit-identical in period, depth and
red-noise S/N.

Usage:

    python scripts/compare_campaign_kernels.py \
        --old results/campaign/full_remaining_pool \
        --new results/campaign/full_pool_v7_instant_wired
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
from typing import Any

# Fields that describe what the search found, as opposed to what was decided
# about it. A change here means detection moved; no change here plus a status
# change means only the verdict moved.
DETECTION_FIELDS = ("period_days", "depth_ppm", "red_noise_adjusted_snr")


def _load(campaign_dir: Path) -> dict[int, dict[str, Any]]:
    path = campaign_dir / "batch_progress.json"
    if not path.exists():
        raise SystemExit(f"No batch_progress.json under {campaign_dir}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: dict[int, dict[str, Any]] = {}
    for row in payload.get("results", []):
        tic_id = row.get("tic_id")
        if tic_id is not None:
            rows[int(tic_id)] = row
    return rows


def _detection_matches(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """True when the search returned the same numbers in both runs.

    Compared exactly rather than with a tolerance: these are the same
    computation over the same FITS input, so they either agree to the last bit
    or something really did change. A tolerance here would quietly absorb the
    small drift that is worth noticing.
    """

    for field in DETECTION_FIELDS:
        if old.get(field) != new.get(field):
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", required=True, type=Path)
    parser.add_argument("--new", required=True, type=Path)
    parser.add_argument(
        "--examples", type=int, default=5, help="Sample rows to print per flow."
    )
    args = parser.parse_args(argv)

    old_rows = _load(args.old)
    new_rows = _load(args.new)
    shared = sorted(set(old_rows) & set(new_rows))

    print(f"old campaign : {args.old.name}  ({len(old_rows)} completed)")
    print(f"new campaign : {args.new.name}  ({len(new_rows)} completed)")
    print(f"comparable   : {len(shared)} targets present in both")
    if not shared:
        return 0

    matrix = collections.Counter(
        (old_rows[t].get("status"), new_rows[t].get("status")) for t in shared
    )
    print("\n--- transition matrix (old -> new) ---")
    for (before, after), count in matrix.most_common():
        flag = "" if before == after else "   <-- changed"
        print(f"  {str(before):>12} -> {str(after):<12} {count:>8}{flag}")

    changed = [t for t in shared if old_rows[t].get("status") != new_rows[t].get("status")]
    if not changed:
        print("\nNo status changed.")
        return 0

    # The distinction the counts cannot show.
    verdict_only = [t for t in changed if _detection_matches(old_rows[t], new_rows[t])]
    detection_moved = [t for t in changed if t not in set(verdict_only)]
    print(f"\n--- why {len(changed)} stars moved ---")
    print(
        f"  verdict only (period/depth/SNR identical) : {len(verdict_only)}"
        "   <- a veto-set change"
    )
    print(f"  detection moved                           : {len(detection_moved)}")

    if verdict_only:
        print("\n--- vetoes now firing on unchanged detections ---")
        reasons: collections.Counter[str] = collections.Counter()
        for tic_id in verdict_only:
            before = (old_rows[tic_id].get("rejection_reasons") or "").strip()
            after = (new_rows[tic_id].get("rejection_reasons") or "").strip()
            if after and after != before:
                for reason in after.split(";"):
                    reason = reason.strip()
                    if reason:
                        reasons[reason] += 1
        for reason, count in reasons.most_common(12):
            print(f"  {count:>6}  {reason[:88]}")

    for label, group in (("verdict only", verdict_only), ("detection moved", detection_moved)):
        if not group:
            continue
        print(f"\n--- {label}: {min(args.examples, len(group))} example(s) ---")
        for tic_id in group[: args.examples]:
            before, after = old_rows[tic_id], new_rows[tic_id]
            print(
                f"  TIC {tic_id}: {before.get('status')} -> {after.get('status')}"
                f" | P {before.get('period_days')} -> {after.get('period_days')}"
                f" | SNR {before.get('red_noise_adjusted_snr')} -> {after.get('red_noise_adjusted_snr')}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
