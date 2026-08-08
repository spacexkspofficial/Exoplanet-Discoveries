"""Measure this pipeline's recovery rate against known transiting planets.

This is NOT `run_p3_known_planets.py`. That script is the locked P3 release
gate: exactly 20 curated stars, pass/fail, guarding the trusted release against
regression. A curated set that passes by construction cannot estimate a rate,
so this is a separate instrument with a separate output and no gate semantics.

It reuses the same primitive, `recover_downloaded_known_planet`, which exposes
the target planet's frozen truth period while leaving sibling transiting
ephemerides masked for the shipping path. The survey normally masks catalogued
signals, which is why a recovery rate cannot be read off ordinary campaign
output (PROGRESS correction 59).

Scope: the cohort must already be restricted to planets that are in principle
recoverable -- period inside the search range, and enough transits fitting the
observed baseline. Scoring planets the search cannot reach measures nothing.
`scripts/build_p5_known_planet_recovery.py` applies that filter and writes the
exclusions to a companion file.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import asdict
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
    hash_target_list,
    settings_signature,
)
from exohunt.progress import TRACKER  # noqa: E402

try:
    from exohunt.config import kernel_version as _code_version
except ImportError:  # pragma: no cover
    from exohunt.config import code_version as _code_version


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


def _bucket(value: float, edges: list[float]) -> str:
    for low, high in zip(edges, edges[1:]):
        if low <= value < high:
            return f"{low:g}-{high:g}"
    return f">={edges[-1]:g}"


def _stratify(results: list[dict[str, object]], rows_by_tic: dict[int, dict[str, str]],
              field: str, edges: list[float]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for result in results:
        row = rows_by_tic.get(int(result["tic_id"]))
        if not row:
            continue
        raw = row.get(field)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        key = _bucket(value, edges)
        cell = out.setdefault(key, {"trials": 0, "recovered": 0})
        cell["trials"] += 1
        cell["recovered"] += int(bool(result.get("passes")))
    for cell in out.values():
        cell["rate"] = round(cell["recovered"] / cell["trials"], 4) if cell["trials"] else None
    return dict(sorted(out.items(), key=lambda kv: float(kv[0].split("-")[0].lstrip(">="))))


def run(args: argparse.Namespace) -> int:
    targets = Path(args.targets)
    output = Path(args.output_dir)
    with targets.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if args.max_targets is not None:
        rows = rows[: args.max_targets]
    if not rows:
        raise ValueError("The recovery cohort is empty.")
    rows_by_tic = {int(float(row["tic_id"])): row for row in rows}
    run_total = len(rows)
    print(f"known-planet recovery cohort: {run_total} planets", flush=True)

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
        "measurement": "survey_wide_known_planet_recovery_rate",
    }
    running_code_version = _code_version()
    signature = settings_signature(
        code=running_code_version,
        settings=settings_payload,
        product_family=f"recovery:{args.author}-{args.cadence_seconds:g}s",
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
    print(f"signature: {signature}", flush=True)

    specs = [_spec(i, row, output) for i, row in enumerate(rows, start=1)]
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
    if results:
        print(f"resumed {len(results)} completed planets", flush=True)

    for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                     "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(variable, "1")

    errors: list[dict[str, object]] = []
    downloaded: list[tuple[dict[str, object], object]] = []
    with ThreadPoolExecutor(max_workers=args.download_workers) as pool:
        futures = {pool.submit(_download_batch_target, spec, args): spec for spec in pending}
        for future in as_completed(futures):
            spec = futures[future]
            try:
                downloaded.append((spec, future.result()))
            except Exception as exc:
                TRACKER.finish(int(spec["tic_id"]))
                errors.append({"tic_id": int(spec["tic_id"]), "planet": spec["planet"],
                               "error": f"download: {type(exc).__name__}: {exc}"})
                print(f"  download failed TIC {spec['tic_id']}: {exc}", flush=True)

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(recover_downloaded_known_planet, spec, settings, payload): spec
            for spec, payload in downloaded
        }
        for future in as_completed(futures):
            spec = futures[future]
            try:
                result = future.result()
                _atomic_json(output / "stars" / f"TIC_{int(spec['tic_id'])}.json", result)
                results.append(result)
                done = len(results) + len(errors)
                print(
                    f"[{done}/{run_total}] {spec['planet']}: "
                    f"{'RECOVERED' if result['passes'] else 'missed'} "
                    f"({result.get('period_relation')}, depth error "
                    f"{100 * float(result.get('depth_fractional_error') or 0):.1f}%)",
                    flush=True,
                )
            except Exception as exc:
                errors.append({"tic_id": int(spec["tic_id"]), "planet": spec["planet"],
                               "error": f"analysis: {type(exc).__name__}: {exc}"})
                print(f"  analysis failed TIC {spec['tic_id']}: {exc}", flush=True)

    scored = len(results)
    recovered = sum(1 for r in results if r.get("passes"))

    # Two rates, deliberately separated. `passes` requires the right period AND
    # a depth within the calibrated tolerance. But 234 of this cohort's 371
    # depths are derived from pl_rade/st_rad, which ignores limb darkening and
    # impact parameter and is easily wrong by tens of percent -- so a depth
    # miss can be an artifact of the expected value rather than a pipeline
    # failure. Period recovery does not depend on that estimate at all.
    def _period_ok(r: dict[str, object]) -> bool:
        rel = str(r.get("period_relation") or "").strip().lower()
        return bool(rel) and rel not in {"none", "no_match", "unmatched", "mismatch"}

    period_recovered = sum(1 for r in results if _period_ok(r))
    by_provenance: dict[str, dict[str, object]] = {}
    for r in results:
        row = rows_by_tic.get(int(r["tic_id"]), {})
        key = row.get("source_rowupdate") or "unknown"
        cell = by_provenance.setdefault(
            key, {"trials": 0, "recovered": 0, "period_recovered": 0}
        )
        cell["trials"] += 1
        cell["recovered"] += int(bool(r.get("passes")))
        cell["period_recovered"] += int(_period_ok(r))
    for cell in by_provenance.values():
        n = cell["trials"]
        cell["rate"] = round(cell["recovered"] / n, 4) if n else None
        cell["period_rate"] = round(cell["period_recovered"] / n, 4) if n else None

    summary = {
        "schema_version": 1,
        "measurement": "survey_wide_known_planet_recovery_rate",
        "not_a_release_gate": (
            "This is a rate, not a pass/fail. The locked 20-star gate is "
            "run_p3_known_planets.py and is unaffected by this measurement."
        ),
        "scientific_signature": signature,
        "code_version": running_code_version,
        "target_list": str(targets),
        "target_list_sha256": hash_target_list(targets),
        "author": args.author,
        "period_range_days": [args.min_period, args.max_period],
        "counts": {"cohort": run_total, "scored": scored,
                   "recovered": recovered,
                   "period_recovered": period_recovered, "errors": len(errors)},
        "recovery_rate": round(recovered / scored, 4) if scored else None,
        "period_recovery_rate": round(period_recovered / scored, 4) if scored else None,
        "rate_definitions": {
            "recovery_rate": (
                "right period AND depth within the calibrated tolerance "
                f"({CURRENT_CONFIG.calibration.known_depth_tolerance_fraction})"
            ),
            "period_recovery_rate": (
                "right period, ignoring depth. Prefer this where the expected "
                "depth was derived rather than catalogued."
            ),
        },
        "by_depth_provenance": by_provenance,
        "by_period_days": _stratify(results, rows_by_tic, "expected_period_days",
                                    [0.5, 1, 2, 4, 8, 16, 20]),
        "by_depth_ppm": _stratify(results, rows_by_tic, "expected_depth_ppm",
                                  [0, 250, 500, 1000, 2500, 5000, 10000, 100000]),
        "by_tmag": _stratify(results, rows_by_tic, "tmag", [5, 8, 9, 10, 11, 12, 13, 16]),
        "errors": errors[:50],
    }
    _atomic_json(output / "known_planet_recovery_summary.json", summary)

    with (output / "known_planet_recovery.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["tic_id", "planet", "expected_period_days", "expected_depth_ppm",
                         "tmag", "recovered", "period_relation", "depth_fractional_error"])
        for r in sorted(results, key=lambda x: int(x["tic_id"])):
            row = rows_by_tic.get(int(r["tic_id"]), {})
            writer.writerow([r["tic_id"], r.get("planet"), row.get("expected_period_days"),
                             row.get("expected_depth_ppm"), row.get("tmag"),
                             int(bool(r.get("passes"))), r.get("period_relation"),
                             r.get("depth_fractional_error")])

    print(f"\nscored {scored}, recovered {recovered}, errors {len(errors)}")
    if scored:
        print(f"RECOVERY RATE: {100 * recovered / scored:.1f}%")
    print(f"wrote {output / 'known_planet_recovery_summary.json'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", required=True)
    parser.add_argument("--output-dir", default="results/p5/known_recovery")
    parser.add_argument("--author", default="SPOC")
    parser.add_argument("--cadence-seconds", type=float, default=120.0)
    parser.add_argument("--min-period", type=float, default=0.5)
    parser.add_argument("--max-period", type=float, default=20.0)
    parser.add_argument("--mask-width", type=float, default=1.5)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--download-workers", type=int, default=2)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--max-targets", type=int)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
