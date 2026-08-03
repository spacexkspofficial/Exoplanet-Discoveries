import argparse
import json
from pathlib import Path

import numpy as np
import pytest

import exohunt.cli as cli_module
from exohunt.cli import _hunt_from_light_curve
from exohunt.detection import DetectionResult


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
