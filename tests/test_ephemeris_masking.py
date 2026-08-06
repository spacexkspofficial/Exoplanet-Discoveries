import argparse
import json
from pathlib import Path

import numpy as np
import pytest

import exohunt.cli as cli_module
from exohunt.cli import _hunt_from_light_curve
from exohunt.config import CURRENT_CONFIG
from exohunt.detection import DetectionResult
from exohunt.vetoes import DEPTH_EB_LANE_REASON


def _stale_catalog() -> dict[str, object]:
    return {
        "tic_id": 42,
        "tois": [
            {
                "toi": "42.01",
                "tfopwg_disp": "KP",
                "pl_orbper": "2.0",
                "pl_orbpererr1": "0.1",
                "pl_orbpererr2": "-0.1",
                "pl_tranmid": "2457000.0",
                "pl_tranmiderr1": "0.01",
                "pl_tranmiderr2": "-0.01",
                "pl_trandurh": "2.4",
                "pl_trandurherr1": "0.1",
                "pl_trandurherr2": "-0.1",
            }
        ],
        "confirmed_planets": [],
    }


def _args(output_dir: Path, *, allow_no_known: bool) -> argparse.Namespace:
    return argparse.Namespace(
        target="TIC 42",
        tic=42,
        sector=100,
        author="SPOC",
        cadence_seconds=120.0,
        min_period=0.5,
        max_period=13.0,
        mask_width=1.5,
        allow_no_known=allow_no_known,
        output_dir=str(output_dir),
        quiet=True,
    )


def test_shipping_hunt_refuses_to_call_an_uncertain_ephemeris_masked(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(cli_module, "check_tic", lambda *_args, **_kwargs: _stale_catalog())
    time = np.linspace(100.0, 110.0, 1_000)

    with pytest.raises(RuntimeError, match="sufficiently precise"):
        _hunt_from_light_curve(
            _args(tmp_path, allow_no_known=False),
            time,
            np.ones_like(time),
            {"tic_id": 42},
        )


def test_sector_vet_refuses_an_uncertain_catalog_mask(
    tmp_path: Path, monkeypatch
) -> None:
    source_path = tmp_path / "source.json"
    source_path.write_text(
        json.dumps(
            {
                "strongest_residual_signal": {
                    "period_days": 2.0,
                    "transit_time": 101.0,
                    "duration_hours": 2.4,
                },
                "data": {
                    "target": "TIC 42",
                    "tic_id": 42,
                    "downloaded_sectors": [100],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli_module,
        "check_tic",
        lambda *_args, **_kwargs: _stale_catalog(),
    )
    time = np.linspace(100.0, 110.0, 1_000)
    monkeypatch.setattr(
        cli_module,
        "_download_light_curve",
        lambda *_args, **_kwargs: (
            time,
            np.ones_like(time),
            {"downloaded_products": []},
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "evaluate_ephemeris",
        lambda *_args, **_kwargs: pytest.fail(
            "sector vet must not measure flux after an untrustworthy mask"
        ),
    )
    args = argparse.Namespace(
        report=str(source_path),
        sector=None,
        author="SPOC",
        cadence_seconds=120.0,
        mask_width=1.5,
    )

    with pytest.raises(RuntimeError, match="untrustworthy catalog mask"):
        cli_module._sector_vet(args)


def test_recovery_only_report_names_the_uncertain_unmasked_signal(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(cli_module, "check_tic", lambda *_args, **_kwargs: _stale_catalog())
    result = DetectionResult(
        period_days=2.0,
        transit_time=101.0,
        duration_hours=2.4,
        depth_ppm=10_000.0,
        depth_snr=20.0,
        radius_ratio=0.1,
        observed_transits=5,
        odd_even_depth_difference_sigma=0.0,
        secondary_depth_ppm=0.0,
        secondary_snr=0.0,
    )
    arrays = {
        "period_grid": np.array([1.5, 2.0, 2.5]),
        "power": np.array([0.1, 1.0, 0.2]),
        "effective_frequency_factor": 1.0,
        "period_grid_was_capped": False,
        "bls_sde": np.asarray(10.0),
    }
    monkeypatch.setattr(
        cli_module,
        "search_transits",
        lambda *_args, **_kwargs: (result, arrays),
    )
    monkeypatch.setattr(cli_module, "harmonic_diagnostics", lambda *_args: [])
    monkeypatch.setattr(
        cli_module,
        "signal_vetting_diagnostics",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        cli_module,
        "_classify_screening_result",
        lambda *_args, **_kwargs: {"screening_class": "screened_rejected"},
    )
    monkeypatch.setattr(
        cli_module,
        "fixed_ephemeris_injection_sensitivity",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(cli_module, "binned_phase_curve", lambda *_args: {})
    monkeypatch.setattr(cli_module, "independent_period_peaks", lambda *_args: [])

    def fake_plot(_result, _arrays, destination: Path) -> None:
        destination.write_bytes(b"test plot")

    monkeypatch.setattr(cli_module, "_plot_result", fake_plot)
    time = np.linspace(100.0, 110.0, 1_000)
    args = _args(tmp_path, allow_no_known=True)

    assert (
        _hunt_from_light_curve(
            args,
            time,
            np.ones_like(time),
            {"target": "TIC 42", "tic_id": 42},
        )
        == 0
    )

    report = json.loads(Path(args.generated_report_path).read_text(encoding="utf-8"))
    mask = report["known_signal_masks"][0]
    assert mask["mask_status"] == "unmasked_ephemeris_uncertainty"
    assert mask["removed_measurements"] == 0
    assert report["search_mode"] == "unmasked known-signal recovery"
    assert report["known_signal_mask_limitations"]["promotion_allowed"] is False
    assert report["relations_to_known_periods"][0]["mask_status"] == (
        "unmasked_ephemeris_uncertainty"
    )
    assert any(
        "sufficiently precise" in reason
        for reason in report["automated_triage"]["rejection_reasons"]
    )


def _safe_catalog() -> dict[str, object]:
    return {
        "tic_id": 42,
        "tois": [
            {
                "toi": "42.01",
                "tfopwg_disp": "KP",
                "pl_orbper": "2.0",
                "pl_orbpererr1": "0.00001",
                "pl_orbpererr2": "-0.00001",
                "pl_tranmid": "2457101.0",
                "pl_tranmiderr1": "0.00001",
                "pl_tranmiderr2": "-0.00001",
                "pl_trandurh": "2.4",
                "pl_trandurherr1": "0.1",
                "pl_trandurherr2": "-0.1",
            }
        ],
        "confirmed_planets": [],
    }


def _shipping_catalog_report(
    tmp_path: Path,
    monkeypatch,
    *,
    recovered_transit_time: float,
    recovered_period: float = 2.0,
    recovered_duration_hours: float = 2.4,
    effective_duration_grid_hours: np.ndarray | None = None,
    requested_duration_grid_hours: np.ndarray | None = None,
    deeper_flags: list[str] | None = None,
    tls_sde: float = 12.0,
    tls_period_days: float | None = None,
) -> dict[str, object]:
    monkeypatch.setattr(
        cli_module,
        "check_tic",
        lambda *_args, **_kwargs: _safe_catalog(),
    )
    result = DetectionResult(
        period_days=recovered_period,
        transit_time=recovered_transit_time,
        duration_hours=recovered_duration_hours,
        depth_ppm=10_000.0,
        depth_snr=20.0,
        radius_ratio=0.1,
        observed_transits=5,
        odd_even_depth_difference_sigma=0.0,
        secondary_depth_ppm=0.0,
        secondary_snr=0.0,
    )
    arrays = {
        "period_grid": np.array(
            [
                max(0.1, recovered_period * 0.75),
                recovered_period,
                recovered_period * 1.25,
            ]
        ),
        "power": np.array([0.1, 1.0, 0.2]),
        "effective_frequency_factor": 1.0,
        "period_grid_was_capped": False,
        "bls_sde": np.asarray(10.0),
    }
    if effective_duration_grid_hours is not None:
        arrays["duration_grid_hours"] = effective_duration_grid_hours
    if requested_duration_grid_hours is not None:
        arrays["requested_duration_grid_hours"] = requested_duration_grid_hours
    monkeypatch.setattr(
        cli_module,
        "search_transits",
        lambda *_args, **_kwargs: (result, arrays),
    )
    monkeypatch.setattr(cli_module, "harmonic_diagnostics", lambda *_args: [])
    monkeypatch.setattr(
        cli_module,
        "signal_vetting_diagnostics",
        lambda *_args, **_kwargs: {"flags": list(deeper_flags or [])},
    )
    measured_tls_period = tls_period_days or recovered_period
    tls_threshold = CURRENT_CONFIG.search.sde_min_single_sector
    monkeypatch.setattr(
        cli_module,
        "tls_signal_diagnostics",
        lambda *_args, **_kwargs: {
            "schema_version": 1,
            "status": "measured",
            "tls_period_days": measured_tls_period,
            "tls_sde": tls_sde,
            "tls_sde_threshold": tls_threshold,
            "tls_sde_passes": tls_sde >= tls_threshold,
            "period_agreement": {
                "agrees": bool(
                    np.isclose(measured_tls_period, recovered_period)
                ),
            },
            "passes": (
                tls_sde >= tls_threshold
                and bool(np.isclose(measured_tls_period, recovered_period))
            ),
        },
    )
    monkeypatch.setattr(
        cli_module,
        "fixed_ephemeris_injection_sensitivity",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(cli_module, "binned_phase_curve", lambda *_args: {})
    monkeypatch.setattr(
        cli_module,
        "independent_period_peaks",
        lambda *_args: [],
    )

    def fake_plot(_result, _arrays, destination: Path) -> None:
        destination.write_bytes(b"test plot")

    monkeypatch.setattr(cli_module, "_plot_result", fake_plot)
    time = np.linspace(100.0, 110.0, 1_000)
    args = _args(tmp_path, allow_no_known=False)
    assert (
        _hunt_from_light_curve(
            args,
            time,
            np.ones_like(time),
            {"target": "TIC 42", "tic_id": 42},
        )
        == 0
    )
    return json.loads(
        Path(args.generated_report_path).read_text(encoding="utf-8")
    )


def test_shipping_hunt_keeps_exact_mask_overlap_rejected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    report = _shipping_catalog_report(
        tmp_path,
        monkeypatch,
        recovered_transit_time=101.0,
    )

    relation = report["relations_to_known_periods"][0]
    assert relation["epoch_verdict"] == (
        "consistent_with_masked_known_signal"
    )
    assert relation["catalog_match_rejects"] is True
    assert report["automated_triage"]["passes"] is False
    assert any(
        "catalogued transit period" in reason
        for reason in report["automated_triage"]["rejection_reasons"]
    )


def test_shipping_hunt_allows_exact_phase_distinct_signal_to_other_gates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    report = _shipping_catalog_report(
        tmp_path,
        monkeypatch,
        recovered_transit_time=101.5,
    )

    relation = report["relations_to_known_periods"][0]
    assert relation["epoch_verdict"] == (
        "phase_distinct_from_masked_known_signal"
    )
    assert relation["catalog_match_rejects"] is False
    assert report["automated_triage"]["passes"] is True
    assert report["automated_triage"]["rejection_reasons"] == []


def test_shipping_hunt_gates_a_red_noise_vetting_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    reason = "red-noise-adjusted depth S/N is below 7.1"
    report = _shipping_catalog_report(
        tmp_path,
        monkeypatch,
        recovered_transit_time=101.5,
        deeper_flags=[reason],
    )

    assert report["automated_triage"]["passes"] is False
    assert reason in report["automated_triage"]["rejection_reasons"]
    assert report["followup_classification"]["screening_class"] == (
        "screened_rejected"
    )


def test_shipping_hunt_gates_a_tls_sde_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    report = _shipping_catalog_report(
        tmp_path,
        monkeypatch,
        recovered_transit_time=101.5,
        tls_sde=8.5,
    )

    assert report["automated_triage"]["passes"] is False
    assert report["tls_decision"]["status"] == "measured"
    assert report["screening_flags"]["tls_sde_below_threshold"] is True
    assert any(
        "TLS SDE is below" in reason
        for reason in report["automated_triage"]["rejection_reasons"]
    )


def test_shipping_hunt_keeps_aligned_half_period_alias_rejected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    report = _shipping_catalog_report(
        tmp_path,
        monkeypatch,
        recovered_period=1.0,
        recovered_transit_time=101.0,
        recovered_duration_hours=1.0,
    )

    relation = report["relations_to_known_periods"][0]
    assert relation["relation"] == "half-period alias"
    assert relation["epoch_verdict"] == (
        "consistent_with_catalog_harmonic"
    )
    assert relation["catalog_match_rejects"] is True
    assert report["automated_triage"]["passes"] is False


def test_shipping_hunt_allows_phase_distinct_half_period_alias(
    tmp_path: Path,
    monkeypatch,
) -> None:
    report = _shipping_catalog_report(
        tmp_path,
        monkeypatch,
        recovered_period=1.0,
        recovered_transit_time=101.5,
        recovered_duration_hours=1.0,
    )

    relation = report["relations_to_known_periods"][0]
    assert relation["relation"] == "half-period alias"
    assert relation["epoch_verdict"] == (
        "phase_distinct_from_catalog_harmonic"
    )
    assert relation["catalog_match_rejects"] is False
    assert report["automated_triage"]["passes"] is True


def test_shipping_hunt_keeps_one_third_period_alias_rejected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    report = _shipping_catalog_report(
        tmp_path,
        monkeypatch,
        recovered_period=2.0 / 3.0,
        recovered_transit_time=101.5,
        recovered_duration_hours=1.0,
    )

    relation = report["relations_to_known_periods"][0]
    assert relation["relation"] == "one-third-period alias"
    assert relation["epoch_verdict"] == (
        "not_evaluated_undercontrolled_relation"
    )
    assert relation["catalog_match_rejects"] is True
    assert report["automated_triage"]["passes"] is False


def test_shipping_hunt_rejects_best_fit_inside_period_overscan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    report = _shipping_catalog_report(
        tmp_path,
        monkeypatch,
        recovered_period=5.2,
        recovered_transit_time=101.0,
        recovered_duration_hours=1.0,
    )

    grid = report["search_grid"]
    assert grid["maximum_reportable_period_days"] == 5.0
    assert grid["maximum_searched_period_days"] == 5.4
    assert grid["best_period_in_overscan"] is True
    assert report["automated_triage"]["passes"] is False
    assert (
        "the best-fit period is in the search-grid overscan zone"
        in report["automated_triage"]["rejection_reasons"]
    )


def test_shipping_hunt_rejects_duration_grid_rail(
    tmp_path: Path,
    monkeypatch,
) -> None:
    report = _shipping_catalog_report(
        tmp_path,
        monkeypatch,
        recovered_period=2.0,
        recovered_transit_time=101.5,
        recovered_duration_hours=1.95,
        effective_duration_grid_hours=np.array([0.5, 1.0, 1.95]),
        requested_duration_grid_hours=np.array([0.5, 1.0, 1.96877]),
    )

    assert report["search_grid"]["duration_grid_hours"][-1] == 1.95
    assert report["search_grid"]["requested_duration_grid_hours"][-1] == (
        1.96877
    )
    assert report["search_grid"]["duration_at_grid_rail"] is True
    assert report["search_grid"]["grid_rail"] is True
    assert report["automated_triage"]["passes"] is False
    assert (
        "the best-fit period or duration is pinned to a search-grid rail"
        in report["automated_triage"]["rejection_reasons"]
    )


def test_shipping_hunt_records_t3_gate_and_routes_eb_lane(
    tmp_path: Path,
    monkeypatch,
) -> None:
    t3 = {
        "schema_version": 1,
        "passes": False,
        "routes_to_eb_lane": True,
        "minimum_supported_events": 2,
        "rejection_reasons": [DEPTH_EB_LANE_REASON],
        "review_flags": [],
        "checks": {
            "duration_density": {"verdict": "pass"},
            "depth_physicality": {
                "verdict": "eb_lane",
                "implied_radius_rjup": 3.2,
            },
            "odd_even": {"verdict": "pass", "sigma": 0.0},
            "full_phase_secondary": {"verdict": "pass", "snr": 0.0},
            "event_support": {"supported_events": 5},
        },
    }
    monkeypatch.setattr(
        cli_module,
        "evaluate_t3_vetoes",
        lambda *_args, **_kwargs: t3,
    )

    report = _shipping_catalog_report(
        tmp_path,
        monkeypatch,
        recovered_period=2.0,
        recovered_transit_time=101.5,
    )

    assert report["t3_vetoes"] == t3
    assert DEPTH_EB_LANE_REASON in (
        report["automated_triage"]["rejection_reasons"]
    )
    assert report["followup_classification"]["screening_class"] == (
        "eclipsing_binary_signal"
    )
    assert report["followup_classification"]["vetting_tier"] == "eb_lane"
    assert "vetoes" in report["search_configuration"]


def test_shipping_hunt_routes_t3_review_flag_to_manual_review(
    tmp_path: Path,
    monkeypatch,
) -> None:
    review_flag = "duration-density review"
    t3 = {
        "schema_version": 1,
        "passes": True,
        "routes_to_eb_lane": False,
        "minimum_supported_events": 2,
        "rejection_reasons": [],
        "review_flags": [review_flag],
        "checks": {
            "duration_density": {"verdict": "flag"},
            "depth_physicality": {"verdict": "pass"},
            "odd_even": {"verdict": "pass", "sigma": 0.0},
            "full_phase_secondary": {"verdict": "pass", "snr": 0.0},
            "event_support": {"supported_events": 5},
        },
    }
    monkeypatch.setattr(
        cli_module,
        "evaluate_t3_vetoes",
        lambda *_args, **_kwargs: t3,
    )

    report = _shipping_catalog_report(
        tmp_path,
        monkeypatch,
        recovered_period=2.0,
        recovered_transit_time=101.5,
    )

    assert report["automated_triage"]["passes"] is True
    assert report["followup_classification"]["screening_class"] == (
        "automated_survivor"
    )
    assert report["followup_classification"]["vetting_tier"] == (
        "needs_manual_review"
    )
    assert report["followup_classification"]["t3_review_flags"] == [
        review_flag
    ]
