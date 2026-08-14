"""The epoch-enrichment gate (corrections 80 and 82).

The gate read 5.03 against a ceiling of 2.0 because it counted every fitted
ephemeris, including folds whose transits land in the data gaps -- one exemplar
reported 215,028 ppm at S/N 113.6 with `observed_transits: 0`. Stars share a gap
structure rather than a star, so those fits pile onto shared instants.

Two things had to change together. Counting only signals the pipeline would
actually report fixes the population; but that shrinks it ~13x, and a raw ratio
does not survive the shrink -- at a bin expectation of ~50 a ratio of 2.0 is a
7-sigma excess, at ~2.8 it is a 1.5-sigma fluctuation that 230 of 3,731 bins
show by chance. So the gate also scores each bin against its own expectation and
only lets trials-corrected detections set the value.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from run_p3_calibration import _epoch_histogram, _survives_triage  # noqa: E402


def _signal(**overrides) -> dict:
    signal = {
        "period_days": 3.0,
        "transit_time": 1000.5,
        "duration_hours": 2.0,
        "depth_ppm": 3000.0,
        "depth_snr": 12.0,
        "observed_transits": 6,
        "odd_even_depth_difference_sigma": 0.2,
        "secondary_snr": 0.3,
    }
    signal.update(overrides)
    return signal


def _report(signal: dict, *, start: float = 1000.0, end: float = 1030.0) -> dict:
    return {
        "observation_window": {"start_btjd": start, "end_btjd": end},
        "strongest_residual_signal": signal,
    }


def test_a_zero_transit_fold_does_not_survive_triage() -> None:
    """The exemplar: S/N 113 measured against no events at all."""

    assert _survives_triage(_signal(observed_transits=0, depth_snr=113.6)) is False


def test_an_impossibly_deep_signal_does_not_survive_triage() -> None:
    assert _survives_triage(_signal(depth_ppm=215_028.0)) is False


def test_an_ordinary_signal_survives_triage() -> None:
    assert _survives_triage(_signal()) is True


def test_a_malformed_signal_is_not_counted_as_having_passed() -> None:
    """Unreadable is not survivable. Failing open here would restore the bug."""

    assert _survives_triage({"period_days": 3.0}) is False


def test_the_gate_counts_only_signals_the_pipeline_would_report() -> None:
    """A pile-up made entirely of rejected folds must not set the gate."""

    # 40 zero-transit folds sharing one epoch exactly, plus 40 ordinary signals
    # spread in phase.
    reports = [
        _report(_signal(observed_transits=0, transit_time=1000.5, period=3.0))
        for _ in range(40)
    ]
    reports += [
        _report(_signal(transit_time=1000.0 + 0.37 * index, period_days=3.0))
        for index in range(40)
    ]

    histogram = _epoch_histogram(reports, triage_surviving_only=True)

    assert histogram["signals_total"] == 80
    assert histogram["signals_triage_surviving"] == 40
    # The rejected pile-up is still visible in the retired number.
    assert histogram["maximum_enrichment_uncorrected"] is not None


def test_an_undetectable_excess_reports_one_not_the_raw_ratio() -> None:
    """No trials-corrected detection means no measured excess.

    On v4 the triage-surviving population's uncorrected maximum is 4.08 with
    zero Bonferroni-significant bins: 0.22 such bins are expected by chance.
    Reporting 4.08 there would gate on noise.
    """

    reports = [
        _report(_signal(transit_time=1000.0 + 0.31 * index, period_days=5.0))
        for index in range(40)
    ]

    histogram = _epoch_histogram(reports, triage_surviving_only=True)

    if histogram["significant_bins"] == 0:
        assert histogram["maximum_enrichment"] == 1.0


def test_the_retired_number_is_kept_rather_than_hidden() -> None:
    """Narrowing a gate must read as a recorded change, not an improvement."""

    reports = [
        _report(_signal(transit_time=1000.0 + 0.31 * index)) for index in range(40)
    ]

    histogram = _epoch_histogram(reports, triage_surviving_only=True)

    assert "maximum_enrichment_uncorrected" in histogram
    assert "maximum_enrichment_all_signals" in histogram
    assert histogram["gated_population"] == "triage-surviving signals only"
    assert "Bonferroni" in histogram["trials_correction"]
