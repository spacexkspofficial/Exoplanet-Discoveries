"""Build a retry target list from a campaign's errored targets.

A long campaign accumulates a handful of targets that error on transient MAST
faults. `campaign.py` already retries those three times inside the run --
"connection aborted" and "remote end closed" are both on its transient list --
so a target that still reports `error` at the end is one where the archive was
unreachable for longer than the retry budget, not one with a code fault. The
2026-08-15 full-pool run collected three inside one minute on adjacent TIC IDs,
which is what a brief archive outage looks like from here.

Those targets are simply missing from the survey until something re-runs them,
and until now that list was assembled by hand. This builds it mechanically.

The output is the *original* target list filtered down to the errored TICs, so
every column and every piece of catalog metadata is preserved exactly. A
generated list with reconstructed columns would be a second source of truth for
target metadata, and the campaign reads more of those columns than the header
suggests.

Usage:

    python scripts/build_retry_targets.py \
        --campaign results/campaign/full_pool_v7_instant_wired \
        --targets targets/full_remaining_pool.csv \
        --output targets/full_pool_v7_retry.csv

Then re-run the campaign against the retry list, writing into the *same*
output directory so the recovered reports land beside the rest:

    python -m exohunt.cli batch-hunt --targets targets/full_pool_v7_retry.csv \
        --output-dir results/campaign/full_pool_v7_instant_wired --force ...

`--force` is required: those TICs already have error reports in that directory,
and without it the resume treats them as done.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


def _errored_tic_ids(campaign_dir: Path) -> set[int]:
    """Collect TIC ids whose most recent result in this campaign is an error.

    Reads the checkpoint rather than walking every per-target report: the
    checkpoint is what the coordinator maintains as the run's view of itself,
    and a full-pool directory holds 65,000 files.
    """

    progress_path = campaign_dir / "batch_progress.json"
    if not progress_path.exists():
        raise SystemExit(f"No batch_progress.json under {campaign_dir}")

    payload = json.loads(progress_path.read_text(encoding="utf-8"))
    errored: set[int] = set()
    for row in payload.get("results", []):
        if row.get("status") != "error":
            continue
        tic_id = row.get("tic_id")
        if tic_id is None:
            continue
        errored.add(int(tic_id))
    return errored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--campaign", required=True, type=Path, help="Campaign output directory."
    )
    parser.add_argument(
        "--targets",
        required=True,
        type=Path,
        help="The target list the campaign ran against.",
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    errored = _errored_tic_ids(args.campaign)
    if not errored:
        print("No errored targets in this campaign; nothing to retry.")
        return 0

    with args.targets.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise SystemExit(f"{args.targets} has no header row.")
        fieldnames = list(reader.fieldnames)
        if "tic_id" not in fieldnames:
            raise SystemExit(f"{args.targets} has no tic_id column.")
        rows = [row for row in reader if int(row["tic_id"]) in errored]

    found = {int(row["tic_id"]) for row in rows}
    # A TIC that errored but is absent from the source list means the campaign
    # and the list have drifted apart, which the caller needs to know about
    # before trusting the retry to be complete.
    missing = errored - found
    if missing:
        print(
            f"WARNING: {len(missing)} errored TIC(s) are not in {args.targets}: "
            + ", ".join(str(t) for t in sorted(missing)[:10]),
            file=sys.stderr,
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"{len(rows)} errored target(s) written to {args.output}")
    print(f"Re-run with --output-dir {args.campaign} --force")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
