"""Execute the frozen known-planet cohort through production T1-T3."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from exohunt.calibration import recover_downloaded_known_planet  # noqa: E402
from exohunt.campaign import _batch_target_spec, _download_batch_target  # noqa: E402
from exohunt.cli import _scientific_settings  # noqa: E402
from exohunt.config import (  # noqa: E402
    CURRENT_CONFIG,
    code_version,
    hash_target_list,
    require_clean_repository,
    settings_signature,
)
from exohunt.progress import TRACKER  # noqa: E402


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _spec(index: int, row: dict[str, str], output_dir: Path) -> dict[str, object]:
    spec = _batch_target_spec(index, row, output_dir)
    spec.update(
        {
            "planet": row["planet"],
            "expected_period_days": float(row["expected_period_days"]),
            "expected_depth_ppm": float(row["expected_depth_ppm"]),
        }
    )
    return spec


def run(args: argparse.Namespace) -> int:
    if not args.allow_dirty:
        require_clean_repository(ROOT)
    targets = Path(args.targets)
    output = Path(args.output_dir)
    with targets.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    full_cohort = len(rows) == 20
    if not full_cohort:
        raise ValueError("The locked known-planet gate requires exactly 20 rows.")
    if args.max_targets is not None:
        rows = rows[: args.max_targets]
    run_total = len(rows)
    science_args = argparse.Namespace(
        author=args.author,
        cadence_seconds=args.cadence_seconds,
        min_period=args.min_period,
        max_period=args.max_period,
        mask_width=args.mask_width,
        allow_no_known=True,
    )
    settings_payload = {
        **_scientific_settings(science_args),
        "calibration": asdict(CURRENT_CONFIG.calibration),
        "known_recovery_catalog_masking": (
            "frozen truth period exposed; all sibling transiting ephemerides "
            "retained for shipping-path masking"
        ),
    }
    running_code_version = code_version(ROOT)
    signature = settings_signature(
        code=running_code_version,
        settings=settings_payload,
        product_family=f"{args.author}-{args.cadence_seconds:g}s",
        target_list_hash=hash_target_list(targets),
    )
    settings: dict[str, object] = {
        "author": args.author,
        "cadence_seconds": args.cadence_seconds,
        "min_period": args.min_period,
        "max_period": args.max_period,
        "mask_width": args.mask_width,
        "output_dir": str(output / "transient"),
        "scientific_signature": signature,
        "code_version": running_code_version,
        "dip_registry": None,
    }
    specs = [_spec(index, row, output) for index, row in enumerate(rows, start=1)]
    pending: list[dict[str, object]] = []
    results: list[dict[str, object]] = []
    for spec in specs:
        path = output / "stars" / f"TIC_{int(spec['tic_id'])}.json"
        if path.exists() and not args.force:
            prior = json.loads(path.read_text(encoding="utf-8"))
            if prior.get("scientific_signature") == signature and not prior.get("error"):
                results.append(prior)
                continue
        pending.append(spec)

    for variable in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ.setdefault(variable, "1")
    errors: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=args.download_workers) as download_pool:
        download_futures = {
            download_pool.submit(_download_batch_target, spec, args): spec
            for spec in pending
        }
        downloaded: list[tuple[dict[str, object], object]] = []
        for future in as_completed(download_futures):
            spec = download_futures[future]
            try:
                downloaded.append((spec, future.result()))
            except Exception as exc:
                TRACKER.finish(int(spec["tic_id"]))
                errors.append(
                    {
                        "tic_id": int(spec["tic_id"]),
                        "planet": spec["planet"],
                        "error": f"download: {type(exc).__name__}: {exc}",
                    }
                )
    with ProcessPoolExecutor(max_workers=args.workers) as analysis_pool:
        analysis_futures = {
            analysis_pool.submit(
                recover_downloaded_known_planet, spec, settings, payload
            ): spec
            for spec, payload in downloaded
        }
        for future in as_completed(analysis_futures):
            spec = analysis_futures[future]
            try:
                result = future.result()
                _atomic_json(
                    output / "stars" / f"TIC_{int(spec['tic_id'])}.json", result
                )
                _atomic_json(
                    output
                    / "reports"
                    / f"TIC_{int(spec['tic_id'])}_{str(spec['planet']).replace(' ', '_')}.json",
                    result["report"],
                )
                results.append(result)
                print(
                    f"{len(results) + len(errors)}/{run_total} {spec['planet']}: "
                    f"{'PASS' if result['passes'] else 'FAIL'} "
                    f"({result['period_relation']}, depth error "
                    f"{100 * float(result['depth_fractional_error']):.1f}%)",
                    flush=True,
                )
            except Exception as exc:
                errors.append(
                    {
                        "tic_id": int(spec["tic_id"]),
                        "planet": spec["planet"],
                        "error": f"analysis: {type(exc).__name__}: {exc}",
                    }
                )
            finally:
                TRACKER.finish(int(spec["tic_id"]))
    results.sort(key=lambda row: str(row["planet"]))
    failed = [row for row in results if not bool(row["passes"])]
    summary = {
        "schema_version": 1,
        "scientific_signature": signature,
        "target_list": str(targets),
        "target_list_sha256": hash_target_list(targets),
        "completed_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "period_tolerance_fraction": CURRENT_CONFIG.calibration.known_period_tolerance_fraction,
        "depth_tolerance_fraction": CURRENT_CONFIG.calibration.known_depth_tolerance_fraction,
        "counts": {
            "total": run_total,
            "passed": len(results) - len(failed),
            "failed": len(failed),
            "errors": len(errors),
        },
        "passes": (
            full_cohort
            and args.max_targets is None
            and len(results) == 20
            and not failed
            and not errors
        ),
        "errors": errors,
        "failed_planets": [str(row["planet"]) for row in failed],
        "results": results,
    }
    _atomic_json(output / "known_planet_summary.json", summary)
    print(json.dumps({key: value for key, value in summary.items() if key != "results"}, indent=2))
    return 0 if summary["passes"] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", required=True)
    parser.add_argument("--output-dir", default="results/p3/known_planets")
    parser.add_argument("--author", default="SPOC")
    parser.add_argument("--cadence-seconds", type=float, default=120.0)
    parser.add_argument("--min-period", type=float, default=0.5)
    parser.add_argument("--max-period", type=float, default=20.0)
    parser.add_argument("--mask-width", type=float, default=1.5)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--download-workers", type=int, default=3)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--max-targets", type=int)
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Diagnostic smoke runs only; release evidence requires a clean worktree.",
    )
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
