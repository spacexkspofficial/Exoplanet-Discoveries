"""Known planets used to test the complete download-and-search workflow."""

from __future__ import annotations


# Expected values are from the NASA Exoplanet Archive default solutions queried
# on 2026-07-22. TOI-700 c is intentionally a single-sector alias stress test.
BENCHMARKS: tuple[dict[str, object], ...] = (
    {
        "target": "WASP-18",
        "tic_id": 100100827,
        "planet": "WASP-18 b",
        "sector": 2,
        "expected_period_days": 0.94145223000,
        "expected_depth_ppm": 11445.0,
        "min_period_days": 0.2,
        "max_period_days": 2.0,
        "purpose": "deep hot Jupiter",
    },
    {
        "target": "LHS 3844",
        "tic_id": 410153553,
        "planet": "LHS 3844 b",
        "sector": 1,
        "expected_period_days": 0.46292970900,
        "expected_depth_ppm": 4507.3268775,
        "min_period_days": 0.2,
        "max_period_days": 1.5,
        "purpose": "small ultra-short-period planet",
    },
    {
        "target": "Pi Mensae",
        "tic_id": 261136679,
        "planet": "pi Men c",
        "sector": 1,
        "expected_period_days": 6.26783990000,
        "expected_depth_ppm": 321.0,
        "min_period_days": 1.0,
        "max_period_days": 10.0,
        "purpose": "shallow sub-Neptune transit",
    },
    {
        "target": "HD 209458",
        "tic_id": 420814525,
        "planet": "HD 209458 b",
        "sector": 56,
        "expected_period_days": 3.52474859000,
        "expected_depth_ppm": 17803.0,
        "min_period_days": 1.0,
        "max_period_days": 6.0,
        "purpose": "classic hot Jupiter in a recent dual-cadence sector",
    },
    {
        "target": "TOI-700",
        "tic_id": 150428135,
        "planet": "TOI-700 c",
        "sector": 3,
        "expected_period_days": 16.05113700000,
        "expected_depth_ppm": 2768.5212418,
        "min_period_days": 5.0,
        "max_period_days": 18.0,
        "purpose": "single-sector harmonic-alias stress test",
    },
)


def compare_period(
    recovered_days: float, expected_days: float, tolerance_fraction: float = 0.01
) -> dict[str, object]:
    """Classify a recovered period as exact, a simple harmonic, or a miss."""

    relations = (
        ("exact", 1.0),
        ("half-period alias", 0.5),
        ("double-period alias", 2.0),
        ("one-third-period alias", 1.0 / 3.0),
        ("triple-period alias", 3.0),
    )
    best_name = "miss"
    best_error = float("inf")
    for name, factor in relations:
        reference = expected_days * factor
        error = abs(recovered_days - reference) / reference
        if error < best_error:
            best_name = name
            best_error = error
    if best_error > tolerance_fraction:
        status = "miss"
    elif best_name == "exact":
        status = "exact"
    else:
        status = "harmonic_alias"
    return {
        "status": status,
        "relation": best_name,
        "fractional_error_to_relation": best_error,
        "period_error_seconds_if_exact": abs(recovered_days - expected_days) * 86400.0,
    }

