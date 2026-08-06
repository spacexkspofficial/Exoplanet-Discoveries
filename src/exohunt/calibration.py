"""P3 injection/recovery and null-test primitives.

Injections are made into normalized, stitched, pre-detrending flux.  The
caller must then run :func:`exohunt.photometry.prepare_search_arrays` and the
shipping hunt function; this module deliberately does not contain a second
search or veto implementation.
"""

from __future__ import annotations

import csv
import argparse
import hashlib
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from .config import CURRENT_CONFIG, CalibrationConfig
from .detrend import segment_boundaries

_G_SI = 6.67430e-11
_SOLAR_MASS_KG = 1.98847e30
_SOLAR_RADIUS_M = 6.957e8
_DAY_SECONDS = 86400.0


@dataclass(frozen=True, slots=True)
class InjectionTrial:
    index: int
    period_bin: int
    period_days: float
    depth_multiplier: float
    depth_ppm: float
    impact_parameter: float
    transit_time_btjd: float
    phase_class: str
    seed: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _stable_seed(seed: int, *values: object) -> int:
    payload = "|".join([str(seed), *(str(value) for value in values)])
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _optional_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def read_target_rows(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("Calibration target list is empty.")
    required = {"target", "tic_id", "sectors"}
    if not required.issubset(rows[0]):
        raise ValueError("Calibration target list lacks identity columns.")
    return rows


def select_calibration_sample(
    rows: list[dict[str, str]],
    *,
    seed: int,
    config: CalibrationConfig | None = None,
) -> dict[str, object]:
    """Return a reproducible 5% random sample plus feature-space archetypes."""

    cfg = config or CURRENT_CONFIG.calibration
    if not rows:
        raise ValueError("Cannot sample an empty cohort.")
    ids = np.asarray([int(row["tic_id"]) for row in rows], dtype=np.int64)
    random_count = max(1, int(math.ceil(len(rows) * cfg.random_sample_fraction)))
    keyed = sorted(
        range(len(rows)),
        key=lambda index: (_stable_seed(seed, int(ids[index]), "random"), int(ids[index])),
    )
    random_indexes = keyed[:random_count]

    feature_names = (
        "tmag",
        "teff_k",
        "stellar_radius_solar",
        "distance_pc",
        "camera",
        "ccd",
    )
    matrix = np.asarray(
        [
            [
                _optional_float(row.get(name))
                if _optional_float(row.get(name)) is not None
                else np.nan
                for name in feature_names
            ]
            for row in rows
        ],
        dtype=float,
    )
    for column in range(matrix.shape[1]):
        values = matrix[:, column]
        finite = np.isfinite(values)
        fill = float(np.nanmedian(values)) if np.any(finite) else 0.0
        values[~finite] = fill
        spread = float(np.nanstd(values))
        matrix[:, column] = (values - float(np.nanmean(values))) / (
            spread if spread > 0 else 1.0
        )

    archetype_count = min(cfg.archetype_count, len(rows))
    distance_from_center = np.sum(matrix**2, axis=1)
    selected = [
        min(
            range(len(rows)),
            key=lambda index: (-distance_from_center[index], int(ids[index])),
        )
    ]
    minimum_distance = np.sum((matrix - matrix[selected[0]]) ** 2, axis=1)
    while len(selected) < archetype_count:
        candidate = min(
            (index for index in range(len(rows)) if index not in selected),
            key=lambda index: (-minimum_distance[index], int(ids[index])),
        )
        selected.append(candidate)
        minimum_distance = np.minimum(
            minimum_distance,
            np.sum((matrix - matrix[candidate]) ** 2, axis=1),
        )

    sample_indexes = sorted(set(random_indexes) | set(selected))
    return {
        "seed": seed,
        "cohort_rows": len(rows),
        "random_fraction": cfg.random_sample_fraction,
        "random_count": len(random_indexes),
        "archetype_count": len(selected),
        "sample_count": len(sample_indexes),
        "random_tic_ids": [int(ids[index]) for index in random_indexes],
        "archetype_tic_ids": [int(ids[index]) for index in selected],
        "sample_tic_ids": [int(ids[index]) for index in sample_indexes],
        "archetype_features": list(feature_names),
    }


def three_hour_noise_ppm(
    time: np.ndarray,
    flux: np.ndarray,
    *,
    hours: float | None = None,
) -> float:
    """Robust white-noise depth for the configured integration time."""

    integration_hours = hours or CURRENT_CONFIG.calibration.photon_noise_hours
    t = np.asarray(time, dtype=float)
    y = np.asarray(flux, dtype=float)
    finite = np.isfinite(t) & np.isfinite(y)
    t, y = t[finite], y[finite]
    order = np.argsort(t)
    t, y = t[order], y[order]
    positive = np.diff(t)
    positive = positive[np.isfinite(positive) & (positive > 0)]
    if positive.size == 0 or y.size < 3:
        raise ValueError("A sampled finite light curve is required.")
    cadence_hours = float(np.nanmedian(positive) * 24.0)
    cadences = max(1.0, integration_hours / cadence_hours)
    differences = np.diff(y)
    center = float(np.nanmedian(differences))
    point_sigma = 1.4826 * float(np.nanmedian(np.abs(differences - center)))
    point_sigma /= math.sqrt(2.0)
    if not np.isfinite(point_sigma) or point_sigma <= 0:
        raise ValueError("Could not estimate positive light-curve noise.")
    return float(point_sigma / math.sqrt(cadences) * 1_000_000.0)


def stellar_mass_for_injection(
    stellar_radius_solar: float,
    stellar_mass_solar: float | None,
) -> tuple[float, str]:
    if stellar_mass_solar is not None and stellar_mass_solar > 0:
        return float(stellar_mass_solar), "catalog_stellar_mass"
    # A bounded low-mass main-sequence approximation is adequate for transit
    # duration generation; every use is labelled so it cannot masquerade as a
    # catalog measurement.
    estimated = float(np.clip(stellar_radius_solar**0.8, 0.08, 2.5))
    return estimated, "radius_power_law_fallback"


def _limb_darkening(teff_k: float | None) -> tuple[float, float]:
    if teff_k is None or teff_k < 4000:
        return 0.45, 0.25
    if teff_k < 6500:
        return 0.35, 0.25
    return 0.25, 0.20


def inject_limb_darkened_transit(
    time: np.ndarray,
    flux: np.ndarray,
    *,
    period_days: float,
    transit_time_btjd: float,
    depth_ppm: float,
    impact_parameter: float,
    stellar_radius_solar: float,
    stellar_mass_solar: float | None = None,
    teff_k: float | None = None,
) -> tuple[np.ndarray, dict[str, object]]:
    """Multiplicatively inject a quadratic-limb-darkened batman model."""

    try:
        import batman
    except ImportError as exc:  # pragma: no cover - dependency error is explicit
        raise RuntimeError(
            "P3 calibration requires batman-package (install the 'fits' extra)."
        ) from exc

    if period_days <= 0 or depth_ppm <= 0 or stellar_radius_solar <= 0:
        raise ValueError("Period, depth, and stellar radius must be positive.")
    if not 0 <= impact_parameter < 1:
        raise ValueError("Impact parameter must be in [0, 1).")
    t = np.asarray(time, dtype=float)
    y = np.asarray(flux)
    mass, mass_source = stellar_mass_for_injection(
        stellar_radius_solar, stellar_mass_solar
    )
    semi_major_m = (
        _G_SI
        * mass
        * _SOLAR_MASS_KG
        * (period_days * _DAY_SECONDS) ** 2
        / (4.0 * math.pi**2)
    ) ** (1.0 / 3.0)
    a_over_r = semi_major_m / (stellar_radius_solar * _SOLAR_RADIUS_M)
    if a_over_r <= impact_parameter:
        raise ValueError("Requested impact parameter is not physical for this orbit.")
    inclination = math.degrees(math.acos(impact_parameter / a_over_r))
    u = _limb_darkening(teff_k)
    cadence_days = float(np.nanmedian(np.diff(np.sort(t))))

    params = batman.TransitParams()
    params.t0 = transit_time_btjd
    params.per = period_days
    params.a = a_over_r
    params.inc = inclination
    params.ecc = 0.0
    params.w = 90.0
    params.u = list(u)
    params.limb_dark = "quadratic"

    desired_depth = depth_ppm / 1_000_000.0

    def model_for(radius_ratio: float) -> np.ndarray:
        params.rp = radius_ratio
        model = batman.TransitModel(
            params,
            t,
            supersample_factor=5,
            exp_time=max(cadence_days, 1e-6),
        )
        return np.asarray(model.light_curve(params), dtype=float)

    lower, upper = 1e-5, min(0.5, math.sqrt(desired_depth) * 2.5 + 0.01)
    model = model_for(upper)
    while 1.0 - float(np.nanmin(model)) < desired_depth and upper < 0.9:
        upper = min(0.9, upper * 1.5)
        model = model_for(upper)
    for _ in range(24):
        radius_ratio = (lower + upper) / 2.0
        model = model_for(radius_ratio)
        if 1.0 - float(np.nanmin(model)) < desired_depth:
            lower = radius_ratio
        else:
            upper = radius_ratio
    radius_ratio = (lower + upper) / 2.0
    model = model_for(radius_ratio)
    realized_depth_ppm = (1.0 - float(np.nanmin(model))) * 1_000_000.0
    injected = np.asarray(y * model, dtype=y.dtype)
    duration_hours = (
        period_days
        / math.pi
        * math.asin(
            min(
                1.0,
                math.sqrt(max(0.0, (1.0 + radius_ratio) ** 2 - impact_parameter**2))
                / a_over_r
                / math.sin(math.radians(inclination)),
            )
        )
        * 24.0
    )
    return injected, {
        "model": "batman_quadratic_limb_darkening",
        "period_days": period_days,
        "transit_time_btjd": transit_time_btjd,
        "requested_depth_ppm": depth_ppm,
        "realized_depth_ppm": realized_depth_ppm,
        "radius_ratio": radius_ratio,
        "impact_parameter": impact_parameter,
        "duration_hours": duration_hours,
        "a_over_rstar": a_over_r,
        "inclination_degrees": inclination,
        "stellar_mass_solar": mass,
        "stellar_mass_source": mass_source,
        "limb_darkening": {"law": "quadratic", "coefficients": list(u)},
        "pixel_level_injection": False,
    }


def paired_fixed_ephemeris_depth_transfer(
    raw_time: np.ndarray,
    raw_flux: np.ndarray,
    injected_raw_flux: np.ndarray,
    prepared_time: np.ndarray,
    prepared_flux: np.ndarray,
    prepared_injected_time: np.ndarray,
    prepared_injected_flux: np.ndarray,
    *,
    period_days: float,
    transit_time_btjd: float,
    duration_hours: float,
) -> dict[str, object]:
    """Measure the injection's depth transfer through preparation.

    The same fixed-ephemeris box estimator is evaluated before and after the
    shipping detrending path.  Subtracting the uninjected measurement in each
    state removes the star's real variability and any coincident astrophysical
    signal.  This paired quantity isolates the depth erosion that P2 section
    2.3 assigns to the P3 injection gate; the blind BLS fit remains a separate
    search-performance diagnostic.
    """

    # Imported lazily to keep the calibration primitives independent of the
    # detection module at import time.
    from .detection import evaluate_ephemeris

    arguments = {
        "period_days": period_days,
        "transit_time": transit_time_btjd,
        "duration_hours": duration_hours,
    }
    raw_baseline = evaluate_ephemeris(raw_time, raw_flux, **arguments)
    raw_injected = evaluate_ephemeris(raw_time, injected_raw_flux, **arguments)
    prepared_baseline = evaluate_ephemeris(
        prepared_time, prepared_flux, **arguments
    )
    prepared_injected = evaluate_ephemeris(
        prepared_injected_time, prepared_injected_flux, **arguments
    )
    sampled = all(
        bool(measurement["sampled"])
        for measurement in (
            raw_baseline,
            raw_injected,
            prepared_baseline,
            prepared_injected,
        )
    )
    if not sampled:
        return {
            "depth_transfer_status": "insufficient_fixed_ephemeris_samples",
            "input_fixed_ephemeris_depth_ppm": None,
            "prepared_fixed_ephemeris_depth_ppm": None,
            "detrending_depth_bias_fraction": None,
        }

    input_depth = float(raw_injected["depth_ppm"]) - float(
        raw_baseline["depth_ppm"]
    )
    prepared_depth = float(prepared_injected["depth_ppm"]) - float(
        prepared_baseline["depth_ppm"]
    )
    if not np.isfinite(input_depth) or input_depth <= 0:
        return {
            "depth_transfer_status": "nonpositive_input_depth",
            "input_fixed_ephemeris_depth_ppm": input_depth,
            "prepared_fixed_ephemeris_depth_ppm": prepared_depth,
            "detrending_depth_bias_fraction": None,
        }
    return {
        "depth_transfer_status": "measured",
        "input_fixed_ephemeris_depth_ppm": input_depth,
        "prepared_fixed_ephemeris_depth_ppm": prepared_depth,
        "detrending_depth_bias_fraction": abs(prepared_depth - input_depth)
        / input_depth,
    }


def invert_prepared_flux(flux: np.ndarray) -> np.ndarray:
    """Flip residual sign about the median after preparation."""

    values = np.asarray(flux)
    median = float(np.nanmedian(values))
    return np.asarray(2.0 * median - values, dtype=values.dtype)


def segment_shift_scramble(
    time: np.ndarray,
    flux: np.ndarray,
    *,
    seed: int,
    gap_days: float | None = None,
) -> tuple[np.ndarray, dict[str, object]]:
    """Independently roll contiguous observing segments.

    A single whole-sector circular shift is exactly BLS invariant.  When only
    one contiguous segment exists it is therefore split at its midpoint, so
    the null preserves local red noise while breaking global coherence.
    """

    gap = gap_days or CURRENT_CONFIG.detrend.segment_gap_days
    t = np.asarray(time, dtype=float)
    y = np.asarray(flux)
    segments = segment_boundaries(t, gap)
    if len(segments) == 1 and segments[0][1] - segments[0][0] >= 8:
        start, stop = segments[0]
        midpoint = start + (stop - start) // 2
        segments = [(start, midpoint), (midpoint, stop)]
    rng = np.random.default_rng(seed)
    scrambled = y.copy()
    shifts: list[dict[str, int]] = []
    for start, stop in segments:
        size = stop - start
        if size < 2:
            shift = 0
        else:
            minimum = max(1, size // 7)
            maximum = max(minimum + 1, size - minimum)
            shift = int(rng.integers(minimum, maximum))
            scrambled[start:stop] = np.roll(y[start:stop], shift)
        shifts.append({"start": start, "stop": stop, "shift_cadences": shift})
    return scrambled, {
        "method": "independent_contiguous_segment_circular_shift",
        "seed": seed,
        "gap_days": gap,
        "segments": shifts,
        "whole_series_shift_forbidden": True,
    }


def injection_trials(
    time: np.ndarray,
    *,
    tic_id: int,
    noise_ppm: float,
    max_period_days: float,
    seed: int,
    config: CalibrationConfig | None = None,
) -> list[InjectionTrial]:
    """Build the frozen per-star random-phase plus edge trial design."""

    cfg = config or CURRENT_CONFIG.calibration
    t = np.sort(np.asarray(time, dtype=float))
    baseline = float(np.nanmax(t) - np.nanmin(t))
    pmax = min(max_period_days, baseline / CURRENT_CONFIG.search.min_transits_single_sector)
    if pmax <= CURRENT_CONFIG.search.min_period_days:
        raise ValueError("Light-curve baseline is too short for calibration periods.")
    periods = np.geomspace(
        CURRENT_CONFIG.search.min_period_days,
        pmax,
        cfg.period_grid_points,
    )
    combinations = [
        (depth, impact)
        for depth in cfg.depth_noise_multipliers
        for impact in cfg.impact_parameters
    ]
    count = cfg.random_phase_injections_per_star + cfg.edge_injections_per_star
    edges = [t[0], t[-1]]
    for start, stop in segment_boundaries(t, CURRENT_CONFIG.detrend.segment_gap_days):
        edges.extend([t[start], t[stop - 1]])
    edges = sorted(set(float(value) for value in edges))
    trials: list[InjectionTrial] = []
    for index in range(count):
        trial_seed = _stable_seed(seed, tic_id, index, "injection")
        rng = np.random.default_rng(trial_seed)
        edge_trial = index >= cfg.random_phase_injections_per_star
        local_index = (
            index - cfg.random_phase_injections_per_star if edge_trial else index
        )
        period_bin = local_index % len(periods)
        period = float(periods[period_bin])
        permutation_rng = np.random.default_rng(_stable_seed(seed, tic_id, "design"))
        design = list(permutation_rng.permutation(len(combinations)))
        design.extend(permutation_rng.permutation(len(combinations)).tolist())
        depth_multiplier, impact = combinations[design[local_index % len(design)]]
        if not edge_trial:
            transit_time = float(t[0] + rng.uniform(0.0, period))
            phase_class = "random"
        else:
            edge = edges[(index - cfg.random_phase_injections_per_star) % len(edges)]
            transit_time = float(edge + rng.uniform(-0.02, 0.02))
            phase_class = "segment_edge"
        trials.append(
            InjectionTrial(
                index=index,
                period_bin=period_bin,
                period_days=period,
                depth_multiplier=float(depth_multiplier),
                depth_ppm=float(noise_ppm * depth_multiplier),
                impact_parameter=float(impact),
                transit_time_btjd=transit_time,
                phase_class=phase_class,
                seed=trial_seed,
            )
        )
    return trials


def recovery_status(
    report: dict[str, object],
    *,
    injected_period_days: float,
) -> dict[str, object]:
    from .benchmarks import compare_period

    signal = report["strongest_residual_signal"]
    assert isinstance(signal, dict)
    comparison = compare_period(
        float(signal["period_days"]), injected_period_days
    )
    triage = report["automated_triage"]
    flags = report["screening_flags"]
    assert isinstance(triage, dict) and isinstance(flags, dict)
    period_recovered = comparison["status"] in {"exact", "harmonic_alias"}
    passes = bool(triage["passes"])
    detection_gate_passes = not bool(
        flags.get("white_noise_depth_snr_below_7_1")
        or flags.get("fewer_than_two_observed_transits")
    )
    return {
        # Completeness asks whether T2 recovered the injection above its
        # detection gate. Promotion-grade recovery additionally asks every T3
        # and rail rule to pass. Keeping both prevents a boundary injection
        # correctly found at P=0.5 d from being misreported as undetected just
        # because the production rail policy refuses to promote it.
        "recovered": period_recovered and detection_gate_passes,
        "promotion_recovered": period_recovered and passes,
        "period_recovered": period_recovered,
        "detection_gate_passes": detection_gate_passes,
        "production_triage_passes": passes,
        "period_status": comparison["status"],
        "period_relation": comparison["relation"],
        "recovered_period_days": float(signal["period_days"]),
        "recovered_depth_ppm": float(signal["depth_ppm"]),
        "recovered_depth_snr": float(signal["depth_snr"]),
        "rejection_reasons": list(triage.get("rejection_reasons") or []),
    }


def catalog_without_expected_signal(
    catalog: dict[str, object],
    *,
    expected_period_days: float,
    tolerance_fraction: float | None = None,
) -> dict[str, object]:
    """Leave one regression truth exposed while retaining sibling masks."""

    tolerance = (
        tolerance_fraction
        if tolerance_fraction is not None
        else CURRENT_CONFIG.calibration.known_period_tolerance_fraction
    )

    def other_period(row: object) -> bool:
        if not isinstance(row, dict):
            return False
        period = _optional_float(row.get("pl_orbper"))
        return (
            period is not None
            and abs(period - expected_period_days) / expected_period_days > tolerance
        )

    return {
        **catalog,
        "tois": [row for row in catalog.get("tois", []) if other_period(row)],
        "confirmed_planets": [
            row for row in catalog.get("confirmed_planets", []) if other_period(row)
        ],
        "known_recovery_exposed_period_days": expected_period_days,
    }


def _calibration_hunt_args(
    spec: dict[str, object],
    settings: dict[str, object],
    catalog: dict[str, object],
) -> argparse.Namespace:
    return argparse.Namespace(
        target=str(spec["target"]),
        tic=int(spec["tic_id"]),
        sector=list(spec["sectors"]),
        author=str(settings["author"]),
        cadence_seconds=float(settings["cadence_seconds"]),
        min_period=float(settings["min_period"]),
        max_period=float(settings["max_period"]),
        mask_width=float(settings["mask_width"]),
        allow_no_known=True,
        output_dir=str(settings["output_dir"]),
        quiet=True,
        calibration_only=True,
        catalog_override=catalog,
        scientific_signature=str(settings["scientific_signature"]),
        dip_registry=settings.get("dip_registry"),
    )


def _run_production_trial(
    spec: dict[str, object],
    settings: dict[str, object],
    catalog: dict[str, object],
    time: np.ndarray,
    flux: np.ndarray,
    metadata: dict[str, object],
) -> dict[str, object]:
    # Imported lazily to avoid a cli -> calibration -> cli cycle.
    from .cli import _hunt_from_light_curve

    args = _calibration_hunt_args(spec, settings, catalog)
    _hunt_from_light_curve(args, time, flux, metadata)
    return args.generated_report_payload


def calibrate_downloaded_target(
    spec: dict[str, object],
    settings: dict[str, object],
    downloaded: tuple[np.ndarray, np.ndarray, dict[str, object]],
) -> dict[str, object]:
    """Run baseline, both nulls, and optional injections for one star."""

    from .catalogs import check_tic
    from .photometry import prepare_search_arrays

    raw_time, raw_flux, downloaded_metadata = downloaded
    raw_metadata = dict(downloaded_metadata)
    raw_metadata.pop("requires_preparation", None)
    for key in ("stellar_radius_solar", "stellar_mass_solar", "camera", "ccd"):
        if spec.get(key) is not None:
            raw_metadata[key] = spec[key]
    catalog = check_tic(int(spec["tic_id"]))
    prepared_time, prepared_flux, prepared_metadata = prepare_search_arrays(
        raw_time, raw_flux, raw_metadata
    )
    baseline_report = _run_production_trial(
        spec,
        settings,
        catalog,
        prepared_time,
        prepared_flux,
        prepared_metadata,
    )

    def null_row(kind: str, report: dict[str, object], transform: object) -> dict[str, object]:
        signal = report["strongest_residual_signal"]
        triage = report["automated_triage"]
        vetting = report["deeper_vetting"]
        grid = report["search_grid"]
        assert all(
            isinstance(value, dict)
            for value in (signal, triage, vetting, grid)
        )
        return {
            "tic_id": int(spec["tic_id"]),
            "kind": kind,
            "survivor": bool(triage["passes"]),
            "period_days": float(signal["period_days"]),
            "depth_ppm": float(signal["depth_ppm"]),
            "depth_snr": float(signal["depth_snr"]),
            "bls_sde_like": float(grid["bls_sde_like"]),
            "red_noise_factor": float(vetting["red_noise_factor"]),
            "red_noise_adjusted_snr": float(vetting["red_noise_adjusted_snr"]),
            "vetting_flags": list(vetting.get("flags") or []),
            "rejection_reasons": list(triage.get("rejection_reasons") or []),
            "transform": transform,
        }

    baseline_triage = baseline_report["automated_triage"]
    baseline_signal = baseline_report["strongest_residual_signal"]
    baseline_vetting = baseline_report["deeper_vetting"]
    baseline_grid = baseline_report["search_grid"]
    assert all(
        isinstance(value, dict)
        for value in (
            baseline_triage,
            baseline_signal,
            baseline_vetting,
            baseline_grid,
        )
    )
    baseline_row = {
        "tic_id": int(spec["tic_id"]),
        "survivor": bool(baseline_triage["passes"]),
        "period_days": float(baseline_signal["period_days"]),
        "transit_time": float(baseline_signal["transit_time"]),
        "duration_hours": float(baseline_signal["duration_hours"]),
        "depth_ppm": float(baseline_signal["depth_ppm"]),
        "depth_snr": float(baseline_signal["depth_snr"]),
        "bls_sde_like": float(baseline_grid["bls_sde_like"]),
        "red_noise_factor": float(baseline_vetting["red_noise_factor"]),
        "red_noise_adjusted_snr": float(
            baseline_vetting["red_noise_adjusted_snr"]
        ),
        "vetting_flags": list(baseline_vetting.get("flags") or []),
        "rejection_reasons": list(baseline_triage.get("rejection_reasons") or []),
    }

    inverted_flux = invert_prepared_flux(prepared_flux)
    inverted_report = _run_production_trial(
        spec,
        settings,
        catalog,
        prepared_time,
        inverted_flux,
        prepared_metadata,
    )
    inverted_row = null_row(
        "inverted",
        inverted_report,
        {"method": "sign_flip_after_preparation"},
    )

    scramble_seed = _stable_seed(
        int(settings["seed"]), int(spec["tic_id"]), "scramble"
    )
    scrambled_flux, scramble_metadata = segment_shift_scramble(
        prepared_time,
        prepared_flux,
        seed=scramble_seed,
    )
    scrambled_report = _run_production_trial(
        spec,
        settings,
        catalog,
        prepared_time,
        scrambled_flux,
        prepared_metadata,
    )
    scrambled_row = null_row(
        "segment_shift_scramble", scrambled_report, scramble_metadata
    )

    injection_rows: list[dict[str, object]] = []
    if bool(spec.get("run_injections")):
        stellar_radius = _optional_float(spec.get("stellar_radius_solar"))
        if stellar_radius is None or stellar_radius <= 0:
            raise ValueError("Injection target lacks a positive stellar radius.")
        stellar_mass = _optional_float(spec.get("stellar_mass_solar"))
        teff_k = _optional_float(spec.get("teff_k"))
        noise_ppm = three_hour_noise_ppm(raw_time, raw_flux)
        trials = injection_trials(
            raw_time,
            tic_id=int(spec["tic_id"]),
            noise_ppm=noise_ppm,
            max_period_days=float(settings["max_period"]),
            seed=int(settings["seed"]),
        )
        for trial in trials:
            injected_flux, model = inject_limb_darkened_transit(
                raw_time,
                raw_flux,
                period_days=trial.period_days,
                transit_time_btjd=trial.transit_time_btjd,
                depth_ppm=trial.depth_ppm,
                impact_parameter=trial.impact_parameter,
                stellar_radius_solar=stellar_radius,
                stellar_mass_solar=stellar_mass,
                teff_k=teff_k,
            )
            injected_time, prepared_injected_flux, injected_metadata = (
                prepare_search_arrays(raw_time, injected_flux, raw_metadata)
            )
            depth_transfer = paired_fixed_ephemeris_depth_transfer(
                raw_time,
                raw_flux,
                injected_flux,
                prepared_time,
                prepared_flux,
                injected_time,
                prepared_injected_flux,
                period_days=trial.period_days,
                transit_time_btjd=trial.transit_time_btjd,
                duration_hours=float(model["duration_hours"]),
            )
            report = _run_production_trial(
                spec,
                settings,
                catalog,
                injected_time,
                prepared_injected_flux,
                injected_metadata,
            )
            outcome = recovery_status(
                report, injected_period_days=trial.period_days
            )
            injection_rows.append(
                {
                    "tic_id": int(spec["tic_id"]),
                    **trial.to_dict(),
                    "period_grid_value_days": trial.period_days,
                    "noise_3h_ppm": noise_ppm,
                    **model,
                    **depth_transfer,
                    **outcome,
                }
            )

    return {
        "schema_version": 1,
        "scientific_signature": settings["scientific_signature"],
        "calibration_signature": settings["calibration_signature"],
        "target": spec["target"],
        "tic_id": int(spec["tic_id"]),
        "sectors": list(spec["sectors"]),
        "run_injections": bool(spec.get("run_injections")),
        "baseline": baseline_row,
        "inverted": inverted_row,
        "scrambled": scrambled_row,
        "injections": injection_rows,
        "baseline_report": baseline_report,
    }


def recover_downloaded_known_planet(
    spec: dict[str, object],
    settings: dict[str, object],
    downloaded: tuple[np.ndarray, np.ndarray, dict[str, object]],
) -> dict[str, object]:
    """Run one known planet through the shipping preparation/search/veto path."""

    from .benchmarks import compare_period
    from .catalogs import check_tic
    from .photometry import prepare_search_arrays

    raw_time, raw_flux, downloaded_metadata = downloaded
    metadata = dict(downloaded_metadata)
    metadata.pop("requires_preparation", None)
    prepared_time, prepared_flux, metadata = prepare_search_arrays(
        raw_time, raw_flux, metadata
    )
    # Expose only the frozen truth signal. Sibling planets remain in the
    # catalog so the shipping mask removes them exactly as it would before a
    # residual search; otherwise a multi-planet host asks one BLS peak to
    # recover several truths simultaneously and tests an undefined mixture.
    catalog = catalog_without_expected_signal(
        check_tic(int(spec["tic_id"])),
        expected_period_days=float(spec["expected_period_days"]),
    )
    report = _run_production_trial(
        spec,
        settings,
        catalog,
        prepared_time,
        prepared_flux,
        metadata,
    )
    signal = report["strongest_residual_signal"]
    triage = report["automated_triage"]
    assert isinstance(signal, dict) and isinstance(triage, dict)
    comparison = compare_period(
        float(signal["period_days"]),
        float(spec["expected_period_days"]),
        tolerance_fraction=CURRENT_CONFIG.calibration.known_period_tolerance_fraction,
    )
    expected_depth = float(spec["expected_depth_ppm"])
    recovered_depth = float(signal["depth_ppm"])
    depth_error = abs(recovered_depth - expected_depth) / expected_depth
    correct_alias = comparison["status"] in {"exact", "harmonic_alias"}
    depth_passes = (
        depth_error <= CURRENT_CONFIG.calibration.known_depth_tolerance_fraction
    )
    production_passes = bool(triage["passes"])
    return {
        "schema_version": 1,
        "scientific_signature": settings["scientific_signature"],
        "target": spec["target"],
        "tic_id": int(spec["tic_id"]),
        "planet": spec["planet"],
        "sectors": list(spec["sectors"]),
        "expected_period_days": float(spec["expected_period_days"]),
        "expected_depth_ppm": expected_depth,
        "recovered_period_days": float(signal["period_days"]),
        "recovered_depth_ppm": recovered_depth,
        "recovered_depth_snr": float(signal["depth_snr"]),
        "period_status": comparison["status"],
        "period_relation": comparison["relation"],
        "period_fractional_error": comparison["fractional_error_to_relation"],
        "depth_fractional_error": depth_error,
        "correct_alias": correct_alias,
        "depth_within_tolerance": depth_passes,
        "production_triage_passes": production_passes,
        "rejection_reasons": list(triage.get("rejection_reasons") or []),
        # The regression contract is correct alias + depth scale. A known hot
        # Jupiter can have a real secondary eclipse, so requiring it to survive
        # every discovery-triage veto would turn correct T3 behaviour into a
        # failed recovery. The complete T3 verdict remains recorded above.
        "passes": correct_alias and depth_passes,
        "report": report,
    }


def summarize_calibration(
    rows: list[dict[str, object]],
    *,
    baseline_rows: list[dict[str, object]],
    inverted_rows: list[dict[str, object]],
    scrambled_rows: list[dict[str, object]],
    maximum_epoch_enrichment: float | None = None,
    config: CalibrationConfig | None = None,
) -> dict[str, object]:
    """Calculate completeness, false-alarm estimates, and release gates."""

    cfg = config or CURRENT_CONFIG.calibration

    def rate(items: list[dict[str, object]], key: str) -> float | None:
        return (
            sum(bool(item.get(key)) for item in items) / len(items)
            if items
            else None
        )

    recovered = [row for row in rows if bool(row.get("recovered"))]
    blind_search_depth_errors = [
        abs(float(row["recovered_depth_ppm"]) - float(row["realized_depth_ppm"]))
        / float(row["realized_depth_ppm"])
        for row in recovered
        if float(row.get("realized_depth_ppm") or 0) > 0
    ]
    depth_biases = [
        float(row["detrending_depth_bias_fraction"])
        for row in recovered
        if row.get("detrending_depth_bias_fraction") is not None
        and np.isfinite(float(row["detrending_depth_bias_fraction"]))
    ]
    random_rows = [row for row in rows if row.get("phase_class") == "random"]
    edge_rows = [row for row in rows if row.get("phase_class") == "segment_edge"]
    random_rate = rate(random_rows, "recovered")
    edge_rate = rate(edge_rows, "recovered")
    random_promotion_rate = rate(random_rows, "promotion_recovered")
    edge_promotion_rate = rate(edge_rows, "promotion_recovered")
    edge_gap = (
        random_rate - edge_rate
        if random_rate is not None and edge_rate is not None
        else None
    )
    inverted_rate = rate(inverted_rows, "survivor")
    scrambled_rate = rate(scrambled_rows, "survivor")
    baseline_rate = rate(baseline_rows, "survivor")
    median_depth_bias = float(np.nanmedian(depth_biases)) if depth_biases else None
    median_blind_search_depth_error = (
        float(np.nanmedian(blind_search_depth_errors))
        if blind_search_depth_errors
        else None
    )

    surface: list[dict[str, object]] = []
    keys = sorted(
        {
            (int(row["period_bin"]), float(row["depth_multiplier"]))
            for row in random_rows
        }
    )
    for period_bin, multiplier in keys:
        cell = [
            row
            for row in random_rows
            if int(row["period_bin"]) == period_bin
            and float(row["depth_multiplier"]) == multiplier
        ]
        surface.append(
            {
                "period_bin": period_bin,
                "median_period_days": float(
                    np.nanmedian(
                        [float(row["period_grid_value_days"]) for row in cell]
                    )
                ),
                "depth_multiplier": multiplier,
                "trials": len(cell),
                "recovered": sum(bool(row.get("recovered")) for row in cell),
                "completeness": rate(cell, "recovered"),
            }
        )

    gates = {
        "inverted_survivor_rate": {
            "value": inverted_rate,
            "maximum": cfg.inverted_survivor_budget,
            "passes": (
                inverted_rate is not None
                and inverted_rate <= cfg.inverted_survivor_budget
            ),
        },
        "scrambled_survivor_rate": {
            "value": scrambled_rate,
            "maximum": cfg.scrambled_survivor_budget,
            "passes": (
                scrambled_rate is not None
                and scrambled_rate <= cfg.scrambled_survivor_budget
            ),
        },
        "t3_pass_rate": {
            "value": baseline_rate,
            "minimum": cfg.t3_pass_rate_min,
            "maximum": cfg.t3_pass_rate_max,
            "passes": (
                baseline_rate is not None
                and cfg.t3_pass_rate_min <= baseline_rate <= cfg.t3_pass_rate_max
            ),
        },
        "median_recovered_depth_bias": {
            "value": median_depth_bias,
            "maximum": cfg.maximum_median_depth_bias_fraction,
            "measurement": "paired_fixed_ephemeris_detrending_depth_transfer",
            "measured_recoveries": len(depth_biases),
            "passes": (
                median_depth_bias is not None
                and median_depth_bias <= cfg.maximum_median_depth_bias_fraction
            ),
        },
        "edge_recovery_gap": {
            "value": edge_gap,
            "maximum": cfg.maximum_edge_recovery_gap,
            "passes": (
                edge_gap is not None and edge_gap <= cfg.maximum_edge_recovery_gap
            ),
        },
        "epoch_enrichment": {
            "value": maximum_epoch_enrichment,
            "maximum": cfg.maximum_epoch_enrichment,
            "passes": (
                maximum_epoch_enrichment is not None
                and maximum_epoch_enrichment < cfg.maximum_epoch_enrichment
            ),
        },
    }
    return {
        "schema_version": 1,
        "calibration_config": asdict(cfg),
        "counts": {
            "injections": len(rows),
            "recovered": len(recovered),
            "baseline": len(baseline_rows),
            "inverted": len(inverted_rows),
            "scrambled": len(scrambled_rows),
        },
        "random_phase_completeness": random_rate,
        "edge_completeness": edge_rate,
        "random_phase_promotion_completeness": random_promotion_rate,
        "edge_promotion_completeness": edge_promotion_rate,
        "median_recovered_depth_bias_fraction": median_depth_bias,
        "median_blind_search_depth_error_fraction": median_blind_search_depth_error,
        "completeness_surface": surface,
        "gates": gates,
        "release_gate_passes": all(bool(gate["passes"]) for gate in gates.values()),
    }
