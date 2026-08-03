"""Build the cached, safely masked Sector 100 harmonic calibration cohort.

This script is deliberately read-only with respect to remote services. It
selects product-targets only when:

1. an existing Sector 100 report contains a simple harmonic relation;
2. the matching SPOC or TESScut light curve is already cached;
3. the cached catalog response uses the uncertainty-aware schema; and
4. a current masking report says that the referenced catalog signal was
   actually masked.

The resulting target files prepare a later measurement. They do not change
the shipping matcher or execute a campaign.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Iterable


HARMONIC_RELATIONS = {
    "half-period alias",
    "double-period alias",
    "one-third-period alias",
    "triple-period alias",
}
AUTHORS = ("SPOC", "TESScut")


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _report_metadata(report: dict[str, object]) -> tuple[int, str, list[int]]:
    data = report.get("data")
    if not isinstance(data, dict):
        raise ValueError("report has no data object")
    tic_id = int(data["tic_id"])
    author = str(
        data.get("author")
        or data.get("requested_author")
        or (report.get("search_configuration") or {}).get("author")
    )
    raw_sectors = (
        data.get("requested_sectors")
        or data.get("downloaded_sectors")
        or []
    )
    sectors = [int(sector) for sector in raw_sectors]
    return tic_id, author, sectors


def _harmonic_relations(
    report: dict[str, object],
) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for field in (
        "relations_to_known_periods",
        "relations_to_masked_periods",
    ):
        relations = report.get(field) or []
        if isinstance(relations, dict):
            relations = [relations]
        for relation in relations:
            if not isinstance(relation, dict):
                continue
            relation_name = str(relation.get("relation") or "")
            known_signal = str(relation.get("known_signal") or "")
            key = (relation_name, known_signal)
            if relation_name not in HARMONIC_RELATIONS or key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "relation": relation_name,
                    "known_signal": known_signal,
                    "fractional_error_to_relation": relation.get(
                        "fractional_error_to_relation"
                    ),
                    "period_status": relation.get("status"),
                    "historical_mask_status": relation.get("mask_status"),
                }
            )
    return found


def discover_report_paths(results_root: Path) -> list[Path]:
    """Use ripgrep when available, with a portable Python fallback."""

    pattern = "|".join(sorted(HARMONIC_RELATIONS))
    try:
        completed = subprocess.run(
            [
                "rg",
                "-l",
                pattern,
                str(results_root),
                "-g",
                "TIC_*_residual.json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        completed = None
    if completed is not None and completed.returncode in {0, 1}:
        return sorted(
            Path(line)
            for line in completed.stdout.splitlines()
            if line.strip()
        )

    paths: list[Path] = []
    for path in results_root.rglob("TIC_*_residual.json"):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(relation in text for relation in HARMONIC_RELATIONS):
            paths.append(path)
    return sorted(paths)


def collect_historical_evidence(
    report_paths: Iterable[Path],
    *,
    results_root: Path,
) -> tuple[
    dict[tuple[int, str], list[dict[str, object]]],
    dict[str, int],
]:
    paths = list(report_paths)
    evidence: dict[tuple[int, str], list[dict[str, object]]] = {}
    parsed_reports = 0
    sector_100_reports = 0
    for path in paths:
        try:
            report = _load_json(path)
            tic_id, author, sectors = _report_metadata(report)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
        parsed_reports += 1
        relations = _harmonic_relations(report)
        if sectors != [100] or author not in AUTHORS or not relations:
            continue
        sector_100_reports += 1
        signal = report.get("strongest_residual_signal") or {}
        configuration = report.get("search_configuration") or {}
        source = {
            "report": str(path.resolve().relative_to(results_root.resolve())),
            "pipeline_version": configuration.get("data_pipeline_version"),
            "recovered_period_days": signal.get("period_days"),
            "recovered_transit_time_btjd": signal.get("transit_time"),
            "recovered_duration_hours": signal.get("duration_hours"),
            "recovered_depth_snr": signal.get("depth_snr"),
            "relations": relations,
        }
        evidence.setdefault((tic_id, author), []).append(source)

    for sources in evidence.values():
        sources.sort(key=lambda row: str(row["report"]))
    return evidence, {
        "harmonic_report_files_discovered": len(paths),
        "reports_parsed": parsed_reports,
        "sector_100_harmonic_reports": sector_100_reports,
        "sector_100_product_targets": len(evidence),
    }


def _cached_products(
    cache_root: Path,
    *,
    tic_id: int,
    author: str,
) -> list[Path]:
    namespace = cache_root / "batch_targets" / f"TIC_{tic_id}_s100"
    if author == "SPOC":
        return sorted(namespace.rglob("*_lc.fits")) if namespace.exists() else []
    tesscut = namespace / "tesscut"
    return sorted(tesscut.glob("*.fits")) if tesscut.exists() else []


def _catalog_status(catalog_root: Path, tic_id: int) -> str:
    path = catalog_root / f"TIC_{tic_id}.json"
    if not path.exists():
        return "missing"
    try:
        result = _load_json(path).get("result")
    except (OSError, json.JSONDecodeError):
        return "invalid"
    if not isinstance(result, dict):
        return "invalid"
    if result.get("ephemeris_uncertainty_columns_queried") is not True:
        return "refresh_required"
    return "uncertainty_aware"


def load_mask_reports(
    mask_dirs: dict[str, Path],
) -> dict[tuple[int, str], dict[str, object]]:
    reports: dict[tuple[int, str], dict[str, object]] = {}
    for author, directory in mask_dirs.items():
        for path in sorted(directory.glob("TIC_*_residual.json")):
            report = _load_json(path)
            tic_id, report_author, sectors = _report_metadata(report)
            if report_author != author or sectors != [100]:
                raise ValueError(
                    f"mask report metadata does not match {author} Sector 100: "
                    f"{path}"
                )
            reports[(tic_id, author)] = {
                "path": path,
                "report": report,
            }
    return reports


def _unique_historical_relations(
    sources: list[dict[str, object]],
) -> list[dict[str, object]]:
    relations: dict[tuple[str, str], dict[str, object]] = {}
    for source in sources:
        for relation in source["relations"]:
            key = (
                str(relation["relation"]),
                str(relation["known_signal"]),
            )
            relations.setdefault(
                key,
                {
                    **relation,
                    "source_reports": [],
                },
            )
            relations[key]["source_reports"].append(source["report"])
    for relation in relations.values():
        relation["source_reports"] = sorted(set(relation["source_reports"]))
    return sorted(
        relations.values(),
        key=lambda row: (str(row["relation"]), str(row["known_signal"])),
    )


def _mask_records(
    mask_report: dict[str, object] | None,
) -> dict[str, dict[str, object]]:
    if mask_report is None:
        return {}
    report = mask_report["report"]
    return {
        str(record.get("label")): record
        for record in report.get("known_signal_masks", [])
        if isinstance(record, dict)
    }


def build_cohort(
    *,
    evidence: dict[tuple[int, str], list[dict[str, object]]],
    cache_root: Path,
    catalog_root: Path,
    mask_reports: dict[tuple[int, str], dict[str, object]],
    results_root: Path,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    selected: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []
    for (tic_id, author), sources in sorted(
        evidence.items(),
        key=lambda item: (item[0][1], item[0][0]),
    ):
        products = _cached_products(
            cache_root,
            tic_id=tic_id,
            author=author,
        )
        catalog_status = _catalog_status(catalog_root, tic_id)
        mask_report = mask_reports.get((tic_id, author))
        masks = _mask_records(mask_report)
        relations = _unique_historical_relations(sources)
        safe_relations: list[dict[str, object]] = []
        unsafe_relations: list[dict[str, object]] = []
        for relation in relations:
            record = masks.get(str(relation["known_signal"]))
            adjudicated = {
                **relation,
                "current_mask_status": (
                    record.get("mask_status") if record else None
                ),
                "known_period_days": (
                    record.get("period_days") if record else None
                ),
            }
            if record and record.get("mask_status") == "masked":
                safe_relations.append(adjudicated)
            else:
                unsafe_relations.append(adjudicated)

        reasons: list[str] = []
        if not products:
            reasons.append("photometry_not_cached")
        if catalog_status != "uncertainty_aware":
            reasons.append(f"catalog_{catalog_status}")
        if mask_report is None:
            reasons.append("current_mask_report_missing")
        elif not safe_relations:
            reasons.append("no_safely_masked_historical_harmonic")

        row = {
            "target": f"TIC {tic_id}",
            "tic_id": tic_id,
            "sector": 100,
            "sectors": "100",
            "author": author,
            "cadence_seconds": 120 if author == "SPOC" else 158,
            "catalog_cache_status": catalog_status,
            "cached_product_count": len(products),
            "cached_products": [
                str(path.resolve().relative_to(cache_root.resolve()))
                for path in products
            ],
            "current_mask_report": (
                str(
                    mask_report["path"]
                    .resolve()
                    .relative_to(results_root.resolve())
                )
                if mask_report
                else None
            ),
            "safely_masked_relations": safe_relations,
            "excluded_relations": unsafe_relations,
            "historical_sources": sources,
        }
        if reasons:
            excluded.append(
                {
                    "target": row["target"],
                    "tic_id": tic_id,
                    "sector": 100,
                    "author": author,
                    "catalog_cache_status": catalog_status,
                    "cached_product_count": len(products),
                    "current_mask_report": row["current_mask_report"],
                    "historical_relations": [
                        {
                            "relation": relation["relation"],
                            "known_signal": relation["known_signal"],
                            "current_mask_status": relation.get(
                                "current_mask_status"
                            ),
                        }
                        for relation in safe_relations + unsafe_relations
                    ],
                    "exclusion_reasons": reasons,
                }
            )
        else:
            selected.append(row)
    return selected, excluded


def _write_targets(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["target", "tic_id", "sector", "sectors"],
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row[field] for field in writer.fieldnames})


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_outputs(
    *,
    selected: list[dict[str, object]],
    excluded: list[dict[str, object]],
    discovery_counts: dict[str, int],
    results_root: Path,
    historical_dirs: list[Path],
    cache_root: Path,
    catalog_root: Path,
    mask_dirs: dict[str, Path],
    spoc_output: Path,
    tesscut_output: Path,
    manifest_output: Path,
) -> dict[str, object]:
    by_author = {
        author: [row for row in selected if row["author"] == author]
        for author in AUTHORS
    }
    _write_targets(spoc_output, by_author["SPOC"])
    _write_targets(tesscut_output, by_author["TESScut"])

    relation_counts = Counter(
        str(relation["relation"])
        for row in selected
        for relation in row["safely_masked_relations"]
    )
    exclusion_counts = Counter(
        reason
        for row in excluded
        for reason in row["exclusion_reasons"]
    )
    target_files = {
        "SPOC": {
            "path": spoc_output.as_posix(),
            "sha256": _sha256(spoc_output),
            "product_targets": len(by_author["SPOC"]),
        },
        "TESScut": {
            "path": tesscut_output.as_posix(),
            "sha256": _sha256(tesscut_output),
            "product_targets": len(by_author["TESScut"]),
        },
    }
    manifest = {
        "schema_version": 1,
        "scope": (
            "cached Sector 100 product-targets with historical simple-"
            "harmonic evidence and a matching safely masked catalog signal"
        ),
        "selection_rule": [
            "historical report is exactly Sector 100 and uses SPOC or TESScut",
            "historical relation is half, double, one-third, or triple period",
            "matching photometry product is already cached",
            "catalog cache records uncertainty-aware ephemeris fields",
            "uncertainty-aware masking report marked the referenced signal masked",
        ],
        "execution_boundary": (
            "cohort construction only; no network access, campaign execution, "
            "or production matcher change"
        ),
        "inputs": {
            "historical_results_root": results_root.name,
            "historical_report_directories": [
                path.resolve()
                .relative_to(results_root.resolve())
                .as_posix()
                for path in historical_dirs
            ],
            "photometry_cache_root": "/".join(cache_root.parts[-2:]),
            "catalog_cache_root": "/".join(catalog_root.parts[-3:]),
            "mask_report_directories": {
                author: path.resolve()
                .relative_to(results_root.resolve())
                .as_posix()
                for author, path in mask_dirs.items()
            },
        },
        "counts": {
            **discovery_counts,
            "selected_product_targets": len(selected),
            "selected_unique_tics": len(
                {int(row["tic_id"]) for row in selected}
            ),
            "selected_by_author": {
                author: len(rows) for author, rows in by_author.items()
            },
            "safely_masked_harmonic_relations": sum(
                relation_counts.values()
            ),
            "relations_by_type": dict(sorted(relation_counts.items())),
            "excluded_product_targets": len(excluded),
            "exclusion_reasons": dict(sorted(exclusion_counts.items())),
        },
        "target_files": target_files,
        "selected_products": selected,
        "excluded_products": excluded,
    }
    manifest_output.parent.mkdir(parents=True, exist_ok=True)
    manifest_output.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-root", required=True, type=Path)
    parser.add_argument(
        "--historical-dir",
        required=True,
        action="append",
        type=Path,
        help=(
            "Trusted historical shipping-path report directory. Repeat for "
            "multiple product arms."
        ),
    )
    parser.add_argument("--cache-root", required=True, type=Path)
    parser.add_argument("--catalog-root", required=True, type=Path)
    parser.add_argument("--spoc-mask-dir", required=True, type=Path)
    parser.add_argument("--tesscut-mask-dir", required=True, type=Path)
    parser.add_argument("--spoc-output", required=True, type=Path)
    parser.add_argument("--tesscut-output", required=True, type=Path)
    parser.add_argument("--manifest-output", required=True, type=Path)
    args = parser.parse_args()

    report_paths = sorted(
        {
            report_path
            for directory in args.historical_dir
            for report_path in discover_report_paths(directory)
        }
    )
    evidence, discovery_counts = collect_historical_evidence(
        report_paths,
        results_root=args.results_root,
    )
    mask_dirs = {
        "SPOC": args.spoc_mask_dir,
        "TESScut": args.tesscut_mask_dir,
    }
    mask_reports = load_mask_reports(mask_dirs)
    selected, excluded = build_cohort(
        evidence=evidence,
        cache_root=args.cache_root,
        catalog_root=args.catalog_root,
        mask_reports=mask_reports,
        results_root=args.results_root,
    )
    manifest = write_outputs(
        selected=selected,
        excluded=excluded,
        discovery_counts=discovery_counts,
        results_root=args.results_root,
        historical_dirs=args.historical_dir,
        cache_root=args.cache_root,
        catalog_root=args.catalog_root,
        mask_dirs=mask_dirs,
        spoc_output=args.spoc_output,
        tesscut_output=args.tesscut_output,
        manifest_output=args.manifest_output,
    )
    print(json.dumps(manifest["counts"], indent=2))
    print(f"Saved {args.spoc_output}")
    print(f"Saved {args.tesscut_output}")
    print(f"Saved {args.manifest_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
