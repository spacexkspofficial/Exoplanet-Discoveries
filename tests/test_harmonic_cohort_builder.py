"""Tests for the non-shipping P2 harmonic-cohort builder."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.build_p2_harmonic_cohort import (
    _harmonic_relations,
    build_cohort,
    collect_historical_evidence,
)


def _report(
    *,
    tic_id: int = 42,
    author: str = "SPOC",
    sectors: list[int] | None = None,
    relation: str = "half-period alias",
    label: str = "TOI-42.01",
) -> dict[str, object]:
    relation_record = {
        "known_signal": label,
        "mask_status": "masked",
        "status": "harmonic_alias",
        "relation": relation,
        "fractional_error_to_relation": 0.001,
    }
    return {
        "data": {
            "target": f"TIC {tic_id}",
            "tic_id": tic_id,
            "requested_sectors": sectors or [100],
            "author": author,
        },
        "search_configuration": {
            "data_pipeline_version": "test-pipeline",
        },
        "strongest_residual_signal": {
            "period_days": 2.0,
            "transit_time": 100.0,
            "duration_hours": 2.0,
            "depth_snr": 10.0,
        },
        "relations_to_known_periods": [relation_record],
        "relations_to_masked_periods": [relation_record],
    }


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _inputs(
    tmp_path: Path,
    *,
    mask_status: str = "masked",
    catalog_current: bool = True,
) -> tuple[
    dict[tuple[int, str], list[dict[str, object]]],
    Path,
    Path,
    dict[tuple[int, str], dict[str, object]],
    Path,
]:
    results_root = tmp_path / "results"
    report_path = results_root / "historical" / "TIC_42_s100_residual.json"
    _write_json(report_path, _report())
    evidence, _ = collect_historical_evidence(
        [report_path],
        results_root=results_root,
    )

    cache_root = tmp_path / "cache"
    product = (
        cache_root
        / "batch_targets"
        / "TIC_42_s100"
        / "mastDownload"
        / "TESS"
        / "test_lc.fits"
    )
    product.parent.mkdir(parents=True)
    product.write_bytes(b"test")

    catalog_root = tmp_path / "catalogs"
    _write_json(
        catalog_root / "TIC_42.json",
        {
            "result": {
                "ephemeris_uncertainty_columns_queried": catalog_current,
            }
        },
    )
    mask_path = results_root / "mask" / "TIC_42_s100_residual.json"
    mask_report = _report()
    mask_report["known_signal_masks"] = [
        {
            "label": "TOI-42.01",
            "mask_status": mask_status,
            "period_days": 4.0,
        }
    ]
    _write_json(mask_path, mask_report)
    mask_reports = {
        (42, "SPOC"): {
            "path": mask_path,
            "report": mask_report,
        }
    }
    return evidence, cache_root, catalog_root, mask_reports, results_root


def test_relation_extraction_deduplicates_report_fields() -> None:
    relations = _harmonic_relations(_report())

    assert len(relations) == 1
    assert relations[0]["relation"] == "half-period alias"


def test_builder_selects_only_cached_uncertainty_aware_safe_relation(
    tmp_path: Path,
) -> None:
    evidence, cache, catalog, masks, results = _inputs(tmp_path)

    selected, excluded = build_cohort(
        evidence=evidence,
        cache_root=cache,
        catalog_root=catalog,
        mask_reports=masks,
        results_root=results,
    )

    assert len(selected) == 1
    assert excluded == []
    assert selected[0]["safely_masked_relations"][0][
        "known_period_days"
    ] == 4.0


def test_builder_excludes_unmaskable_historical_reference(
    tmp_path: Path,
) -> None:
    evidence, cache, catalog, masks, results = _inputs(
        tmp_path,
        mask_status="unmasked_ephemeris_uncertainty",
    )

    selected, excluded = build_cohort(
        evidence=evidence,
        cache_root=cache,
        catalog_root=catalog,
        mask_reports=masks,
        results_root=results,
    )

    assert selected == []
    assert excluded[0]["exclusion_reasons"] == [
        "no_safely_masked_historical_harmonic"
    ]


def test_builder_marks_stale_catalog_cache_for_refresh(
    tmp_path: Path,
) -> None:
    evidence, cache, catalog, masks, results = _inputs(
        tmp_path,
        catalog_current=False,
    )

    selected, excluded = build_cohort(
        evidence=evidence,
        cache_root=cache,
        catalog_root=catalog,
        mask_reports=masks,
        results_root=results,
    )

    assert selected == []
    assert excluded[0]["exclusion_reasons"] == [
        "catalog_refresh_required"
    ]
