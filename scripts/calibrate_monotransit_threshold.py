"""Measure the monotransit detector's false-event rate on inverted data.

MASTER_PLAN section 3.4 specifies single-transit detection at "peak significance
>= 8 sigma", and then immediately qualifies it: "calibrate on inverted data,
target <= 0.3 false events/star at first pass". Section 6.2 is blunter --
"false-alarm control is the whole game" -- because a monotransit has no repeat
to confirm it. Until this runs, ``DEFAULT_SIGNIFICANCE_THRESHOLD`` is a number
someone wrote down, not a number anyone measured, and
:func:`exohunt.monotransit.search_monotransits` says so in every result it
returns.

What this measures
------------------
Inverting the prepared flux about its median turns every real dip into a bump
and every noise excursion into its mirror image. The detector only reports
positive depths, so *every* event it finds in inverted flux is a false event by
construction. Counting them per star, as a function of the significance
threshold, is the calibration.

The inversion is :func:`exohunt.calibration.invert_prepared_flux`, the same one
the periodic search's inverted gate uses -- ``2 * median - flux``, applied after
detrending. Using a different convention here would make the two false-alarm
budgets incomparable.

Why the threshold sweep is done after the search, not by re-searching
--------------------------------------------------------------------
The detector deduplicates overlapping detections in descending significance
order, so the set of surviving events at or above any threshold is identical
whether lower-significance events were in the list or not. One pass at a low
floor therefore yields an exact curve, and ``--max-events`` is set high enough
that truncation cannot bind.

What this does not establish
----------------------------
A false-event rate is half of an operating point. It says what the threshold
costs in noise, not what it buys in real single transits -- that needs an
injection-recovery pass with single-transit signals, which section 3.4 also
requires and which this script does not do. A threshold that meets the 0.3
budget is necessary, not sufficient, and the lane stays down until the
recovery side exists too.

The direct (uninverted) pass is recorded alongside for scale only. Its events
are not candidates and this script does not vet them; a real event and a
systematic look identical to a matched filter until the pixel data is checked.

Resume, and why the default worker count is modest
--------------------------------------------------
Every star is appended to ``stars.jsonl`` as it finishes, and a re-run skips
what is already there. That is not only crash insurance. The first full pass
lost 25 stars whose light curves were sitting in the cache: resolving
``--author auto`` can still reach MAST, and twelve workers doing that in
parallel drew empty results that surface as "No processed TESS light curve
... is available" -- a throttled query and a genuinely absent light curve are
indistinguishable in that message. Re-running those 25 at two workers recovered
all 25. So the default here is deliberately lower than the machine's core count,
and a resumed re-run is the cheap way to tell the two apart: whatever fails
twice, alone, is really missing.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from exohunt.calibration import invert_prepared_flux  # noqa: E402
from exohunt.detrend import prepare_fluxes  # noqa: E402
from exohunt.monotransit import (  # noqa: E402
    DEFAULT_SIGNIFICANCE_THRESHOLD,
    METHOD_TAG,
    search_monotransits,
)

# Section 3.4's budget.
FALSE_EVENT_BUDGET_PER_STAR = 0.3


def _worker_setup() -> None:
    """One BLAS thread per process; the parallelism is across stars."""

    for variable in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ[variable] = "1"


def _sectors(raw: str) -> list[int]:
    return [int(value) for value in str(raw).split(";") if str(value).strip()]


def measure_star(spec: dict[str, object]) -> dict[str, object]:
    """Search one star's inverted (and direct) long-window flux."""

    _worker_setup()
    from exohunt.cli import _download_light_curve

    tic_id = int(spec["tic_id"])
    target = str(spec["target"])
    sectors = list(spec["sectors"])  # type: ignore[arg-type]
    namespace = f"TIC_{tic_id}_s" + "-".join(str(value) for value in sectors)
    try:
        time_values, flux_values, _ = _download_light_curve(
            target,
            sectors,
            str(spec["author"]),
            float(spec["cadence_seconds"]),  # type: ignore[arg-type]
            cache_namespace=namespace,
            flatten=False,
        )
        prepared = prepare_fluxes(time_values, flux_values)["long"]
        inverted = invert_prepared_flux(prepared.flux)

        def run(flux: np.ndarray) -> dict[str, object]:
            return search_monotransits(
                prepared.time,
                flux,
                uncertainty_scale=prepared.uncertainty_scale,
                significance_threshold=float(spec["floor"]),  # type: ignore[arg-type]
                max_events=int(spec["max_events"]),  # type: ignore[arg-type]
            )

        inverted_result = run(inverted)
        direct_result = run(prepared.flux)
    except Exception as exc:  # noqa: BLE001 - every failure mode is reportable
        return {
            "tic_id": tic_id,
            "target": target,
            "error": f"{type(exc).__name__}: {exc}",
        }

    def events(result: dict[str, object]) -> list[dict[str, object]]:
        return [
            {
                "significance": float(event["significance"]),
                "duration_hours": float(event["duration_hours"]),
                "depth": float(event["depth"]),
                "time_btjd": float(event["time_btjd"]),
                "passes": bool(event["passes"]),
                "vetoes": ";".join(event["vetoes"]),  # type: ignore[arg-type]
            }
            for event in result["events"]  # type: ignore[union-attr]
        ]

    return {
        "tic_id": tic_id,
        "target": target,
        "baseline_days": float(prepared.time[-1] - prepared.time[0]),
        "cadences": int(prepared.time.size),
        "segment_count": int(prepared.segment_count),
        "inverted_events": events(inverted_result),
        "direct_events": events(direct_result),
    }


def threshold_curve(
    per_star: list[dict[str, object]],
    *,
    floor: float,
    ceiling: float,
    step: float,
) -> list[dict[str, float]]:
    """False events per star as a function of the significance threshold.

    Only events that clear the detector's own vetoes are counted: a detection
    the detector already rejects is not a false alarm it would report.
    """

    stars = [row for row in per_star if "error" not in row]
    if not stars:
        return []
    significances = [
        float(event["significance"])
        for row in stars
        for event in row["inverted_events"]  # type: ignore[union-attr]
        if event["passes"]
    ]
    direct_significances = [
        float(event["significance"])
        for row in stars
        for event in row["direct_events"]  # type: ignore[union-attr]
        if event["passes"]
    ]
    values = np.asarray(significances, dtype=float)
    direct_values = np.asarray(direct_significances, dtype=float)
    curve: list[dict[str, float]] = []
    threshold = floor
    while threshold <= ceiling + 1e-9:
        false_events = int(np.count_nonzero(values >= threshold))
        direct_events = int(np.count_nonzero(direct_values >= threshold))
        curve.append(
            {
                "threshold": round(threshold, 4),
                "false_events": false_events,
                "false_events_per_star": false_events / len(stars),
                "stars_with_a_false_event": int(
                    sum(
                        any(
                            event["passes"]
                            and float(event["significance"]) >= threshold
                            for event in row["inverted_events"]  # type: ignore[union-attr]
                        )
                        for row in stars
                    )
                ),
                "direct_events": direct_events,
                "direct_events_per_star": direct_events / len(stars),
            }
        )
        threshold += step
    return curve


def _first_passing(curve: list[dict[str, float]]) -> dict[str, float] | None:
    for point in curve:
        if point["false_events_per_star"] <= FALSE_EVENT_BUDGET_PER_STAR:
            return point
    return None


def load_completed(path: Path) -> dict[int, dict[str, object]]:
    """Per-star results from a previous pass, keyed by TIC.

    A star that errored is *not* treated as completed: the whole point of
    resuming is to retry it under less parallel load and find out whether the
    failure was the network or the archive.
    """

    completed: dict[int, dict[str, object]] = {}
    if not path.exists():
        return completed
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "error" in row:
            continue
        try:
            completed[int(row["tic_id"])] = row
        except (KeyError, TypeError, ValueError):
            continue
    return completed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--targets", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--author", default="auto")
    parser.add_argument("--cadence-seconds", type=float, default=120.0)
    parser.add_argument(
        "--floor-significance",
        type=float,
        default=4.0,
        help="Search floor. The sweep cannot report below this.",
    )
    parser.add_argument("--ceiling-significance", type=float, default=20.0)
    parser.add_argument("--threshold-step", type=float, default=0.25)
    parser.add_argument("--max-events", type=int, default=2000)
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help=(
            "Deliberately below the core count: resolving --author auto can "
            "reach MAST, and parallel queries get throttled into what looks "
            "like a missing light curve. See the module docstring."
        ),
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore stars.jsonl and re-measure every star.",
    )
    args = parser.parse_args(argv)

    rows = list(csv.DictReader(open(args.targets, encoding="utf-8-sig")))
    if args.limit:
        rows = rows[: args.limit]
    specs = [
        {
            "tic_id": int(row["tic_id"]),
            "target": str(row["target"]),
            "sectors": _sectors(row["sectors"]),
            "author": args.author,
            "cadence_seconds": args.cadence_seconds,
            "floor": args.floor_significance,
            "max_events": args.max_events,
        }
        for row in rows
    ]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    stars_path = output_dir / "stars.jsonl"
    if args.force and stars_path.exists():
        stars_path.unlink()
    already = load_completed(stars_path)
    pending = [spec for spec in specs if int(spec["tic_id"]) not in already]
    per_star: list[dict[str, object]] = list(already.values())
    if already:
        print(
            f"Resuming: {len(already)} star(s) already measured, "
            f"{len(pending)} to go.",
            flush=True,
        )

    completed = 0
    with stars_path.open("a", encoding="utf-8") as journal:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(measure_star, spec): spec for spec in pending}
            for future in as_completed(futures):
                row = future.result()
                per_star.append(row)
                journal.write(json.dumps(row) + "\n")
                journal.flush()
                completed += 1
                if completed % 25 == 0 or completed == len(pending):
                    errors = sum(1 for item in per_star if "error" in item)
                    print(
                        f"[{completed}/{len(pending)}] errors={errors}",
                        flush=True,
                    )

    curve = threshold_curve(
        per_star,
        floor=args.floor_significance,
        ceiling=args.ceiling_significance,
        step=args.threshold_step,
    )
    passing = _first_passing(curve)
    searched = [row for row in per_star if "error" not in row]
    at_written = next(
        (
            point
            for point in curve
            if abs(point["threshold"] - DEFAULT_SIGNIFICANCE_THRESHOLD) < 1e-9
        ),
        None,
    )

    summary = {
        "schema_version": 1,
        "method": METHOD_TAG,
        "generated_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "targets": str(args.targets),
        "stars_requested": len(specs),
        "stars_searched": len(searched),
        "errors": [row for row in per_star if "error" in row],
        "settings": {
            "author": args.author,
            "cadence_seconds": args.cadence_seconds,
            "floor_significance": args.floor_significance,
            "max_events": args.max_events,
            "inversion": "2 * median - flux, after detrending (calibration.invert_prepared_flux)",
            "flux": "long-window detrended (DetrendConfig.long_window_days)",
        },
        "false_event_budget_per_star": FALSE_EVENT_BUDGET_PER_STAR,
        "written_threshold": DEFAULT_SIGNIFICANCE_THRESHOLD,
        "at_written_threshold": at_written,
        "calibrated_threshold": passing["threshold"] if passing else None,
        "calibrated_threshold_point": passing,
        "curve": curve,
        "warning": (
            "A false-event rate is half of an operating point. This measures "
            "what the threshold costs in noise, not what it buys in real single "
            "transits; section 3.4 also requires an injection-recovery pass, "
            "which this does not do. Meeting the budget is necessary, not "
            "sufficient, and the lane stays down until the recovery side exists."
        ),
    }
    summary_path = output_dir / "monotransit_threshold.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    events_path = output_dir / "monotransit_events.csv"
    with events_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "tic_id",
                "kind",
                "significance",
                "duration_hours",
                "depth",
                "time_btjd",
                "passes",
                "vetoes",
            ]
        )
        for row in searched:
            for kind in ("inverted", "direct"):
                for event in row[f"{kind}_events"]:  # type: ignore[union-attr]
                    writer.writerow(
                        [
                            row["tic_id"],
                            kind,
                            f"{event['significance']:.4f}",
                            f"{event['duration_hours']:.4f}",
                            f"{event['depth']:.8f}",
                            f"{event['time_btjd']:.6f}",
                            event["passes"],
                            event["vetoes"],
                        ]
                    )

    print(f"\nSearched {len(searched)} star(s); {len(specs) - len(searched)} error(s).")
    if at_written is not None:
        print(
            f"At the written threshold {DEFAULT_SIGNIFICANCE_THRESHOLD:g} sigma: "
            f"{at_written['false_events_per_star']:.3f} false events/star "
            f"(budget {FALSE_EVENT_BUDGET_PER_STAR})."
        )
    if passing is None:
        print(
            f"No threshold at or below {args.ceiling_significance:g} sigma meets "
            f"the {FALSE_EVENT_BUDGET_PER_STAR} false-events/star budget."
        )
    else:
        print(
            f"Calibrated threshold: {passing['threshold']:g} sigma "
            f"({passing['false_events_per_star']:.3f} false events/star, "
            f"{passing['direct_events_per_star']:.3f} direct events/star)."
        )
    print(f"Wrote {summary_path} and {events_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
