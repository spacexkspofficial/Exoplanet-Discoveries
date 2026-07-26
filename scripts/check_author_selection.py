"""Report which light-curve reduction a campaign's reports actually used.

Run this a short way into a campaign, before letting it continue unattended.
``--author auto`` is supposed to resolve to SPOC, TESS-SPOC, or QLP for almost
every target; a high TESScut fallback count means the archive queries are
failing and the run has silently reverted to the extraction that manufactured
the systematics in the first place.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path


def survey(campaign_dir: Path) -> dict[str, object]:
    authors: collections.Counter[str] = collections.Counter()
    cadences: collections.Counter[float] = collections.Counter()
    fallbacks = 0
    unreadable = 0
    reports = 0

    for path in sorted(campaign_dir.glob("TIC_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8")).get("data")
        except (OSError, json.JSONDecodeError):
            unreadable += 1
            continue
        if not isinstance(data, dict):
            continue
        reports += 1
        authors[str(data.get("author"))] += 1
        cadence = data.get("resolved_cadence_seconds") or data.get(
            "requested_cadence_seconds"
        )
        if cadence is not None:
            cadences[float(cadence)] += 1
        if data.get("author_fallback_to_tesscut"):
            fallbacks += 1

    return {
        "reports": reports,
        "unreadable": unreadable,
        "authors": dict(authors),
        "cadences": dict(cadences),
        "fallbacks": fallbacks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("campaign_dir", type=Path)
    parser.add_argument(
        "--max-tesscut-fraction",
        type=float,
        default=0.10,
        help=(
            "Fail if more than this fraction of reports used a local TESScut "
            "extraction, whether by fallback or by an explicit --author."
        ),
    )
    args = parser.parse_args()

    if not args.campaign_dir.is_dir():
        print(f"error: {args.campaign_dir} is not a directory", file=sys.stderr)
        return 2

    result = survey(args.campaign_dir)
    reports = int(result["reports"])
    if not reports:
        print(f"No per-target reports yet in {args.campaign_dir}.")
        return 0

    print(f"Reports inspected: {reports}")
    if result["unreadable"]:
        print(f"  unreadable files: {result['unreadable']}")
    print("Reduction used:")
    for author, count in sorted(
        result["authors"].items(), key=lambda item: -item[1]
    ):
        print(f"  {author:12s} {count:6d}  ({100 * count / reports:.1f}%)")
    print("Cadence pinned (seconds):")
    for cadence, count in sorted(result["cadences"].items()):
        print(f"  {cadence:8.0f} {count:6d}")

    # What matters is how much of the campaign ran on a local extraction, not
    # how it got there. An explicit --author TESScut sets no fallback flag but
    # carries exactly the same systematics.
    tesscut = int(result["authors"].get("TESScut", 0))
    fraction = tesscut / reports
    fallbacks = int(result["fallbacks"])
    print(f"TESScut reports: {tesscut} ({100 * fraction:.1f}%)")
    print(f"  of which auto-selection fell back: {fallbacks}")

    if fraction > args.max_tesscut_fraction:
        print(
            "\nFAIL: too much of this campaign ran on a local TESScut "
            "extraction. If --author auto was used, the archive queries are "
            "probably failing; if TESScut was requested explicitly, this run "
            "reproduces the spacecraft systematics documented in HANDOFF.md. "
            "Stop the campaign and investigate before letting it continue.",
            file=sys.stderr,
        )
        return 1
    print("\nOK: the campaign is running on processed photometry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
