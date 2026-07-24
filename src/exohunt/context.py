"""Compact, metadata-only cross-mission context for follow-up targets.

This module intentionally does not download science products.  It asks MAST
which observations exist, refreshes small catalog records, and turns the result
into an ordered follow-up plan.  Large light curves, images, and spectra remain
separate, explicitly requested steps.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timezone
from typing import Iterable, Mapping


TESS_REDUCTIONS = {
    "CDIPS",
    "ELEANOR",
    "GSFC-ELEANOR-LITE",
    "QLP",
    "T16",
    "TARS",
    "TASOC",
    "TESS-SPOC",
    "TGLC",
}

MISSION_ROLES = {
    "TESS": "transit search and repeat-sector confirmation",
    "Kepler": "long-baseline, high-precision transit photometry when the field overlaps",
    "K2": "roughly 80-day transit and variability photometry when a campaign overlaps",
    "HST": "targeted high-resolution imaging or spectroscopy; not an all-sky transit survey",
    "JWST": "targeted infrared characterization; not an all-sky discovery survey",
    "GALEX": "ultraviolet activity context rather than transit confirmation",
}


def _plain(value: object) -> object | None:
    """Convert masked/numpy scalar values into compact JSON-safe scalars."""

    if value is None:
        return None
    mask = getattr(value, "mask", False)
    try:
        if bool(mask):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    text = str(value).strip()
    if text in {"", "--", "nan", "None"}:
        return None
    return value


def _row_value(row: object, name: str) -> object | None:
    try:
        return _plain(row[name])  # type: ignore[index]
    except (KeyError, TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    value = _plain(value)
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _optional_int(value: object) -> int | None:
    value = _plain(value)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def summarize_mast_observations(
    rows: Iterable[Mapping[str, object] | object],
) -> dict[str, object]:
    """Summarize MAST holdings without listing or downloading data products."""

    combinations: Counter[tuple[str, str, str]] = Counter()
    collection_counts: Counter[str] = Counter()
    tess_image_sectors: set[int] = set()
    tess_timeseries_sectors: set[int] = set()
    tess_reductions: set[str] = set()
    start_mjd: list[float] = []
    end_mjd: list[float] = []

    for row in rows:
        collection = str(_row_value(row, "obs_collection") or "unknown")
        provenance = str(_row_value(row, "provenance_name") or "unknown")
        product_type = str(_row_value(row, "dataproduct_type") or "unknown")
        collection_counts[collection] += 1
        combinations[(collection, provenance, product_type)] += 1

        is_tess = collection == "TESS" or (
            collection == "HLSP" and provenance.upper() in TESS_REDUCTIONS
        )
        if is_tess:
            sector = _optional_int(_row_value(row, "sequence_number"))
            if sector is not None and sector > 0:
                if product_type == "timeseries":
                    tess_timeseries_sectors.add(sector)
                elif product_type == "image":
                    tess_image_sectors.add(sector)
            if collection == "HLSP" and provenance.upper() in TESS_REDUCTIONS:
                tess_reductions.add(provenance.upper())
            start = _optional_float(_row_value(row, "t_min"))
            end = _optional_float(_row_value(row, "t_max"))
            if start is not None:
                start_mjd.append(start)
            if end is not None:
                end_mjd.append(end)

    products = [
        {
            "collection": collection,
            "provenance": provenance,
            "product_type": product_type,
            "observation_records": count,
        }
        for (collection, provenance, product_type), count in sorted(
            combinations.items()
        )
    ]
    all_tess_sectors = tess_image_sectors | tess_timeseries_sectors
    return {
        "observation_records": sum(collection_counts.values()),
        "collection_counts": dict(sorted(collection_counts.items())),
        "products": products,
        "tess": {
            "all_sectors": sorted(all_tess_sectors),
            "timeseries_sectors": sorted(tess_timeseries_sectors),
            "image_only_sectors": sorted(
                tess_image_sectors - tess_timeseries_sectors
            ),
            "alternate_reductions": sorted(tess_reductions),
            "calendar_span_days": (
                round(max(end_mjd) - min(start_mjd), 3)
                if start_mjd and end_mjd
                else None
            ),
        },
        "mission_roles": {
            mission: MISSION_ROLES[mission]
            for mission in sorted(collection_counts)
            if mission in MISSION_ROLES
        },
    }


def summarize_tic_neighbors(
    rows: Iterable[Mapping[str, object] | object],
    *,
    target_tic_id: int,
    target_tmag: float | None,
) -> dict[str, object]:
    """Describe nearby TIC/Gaia-crossmatched sources and rough dilution risk."""

    neighbors: list[dict[str, object]] = []
    flux_ratio_upper_bound = 0.0
    for row in rows:
        tic_id = _optional_int(_row_value(row, "ID"))
        separation = _optional_float(_row_value(row, "dstArcSec"))
        if tic_id == target_tic_id or separation is None or separation < 0.1:
            continue
        tmag = _optional_float(_row_value(row, "Tmag"))
        delta_tmag = (
            round(tmag - target_tmag, 4)
            if tmag is not None and target_tmag is not None
            else None
        )
        if delta_tmag is not None:
            flux_ratio_upper_bound += 10 ** (-0.4 * delta_tmag)
        neighbors.append(
            {
                "tic_id": tic_id,
                "gaia_source_id": _optional_int(_row_value(row, "GAIA")),
                "separation_arcsec": round(separation, 4),
                "tmag": tmag,
                "delta_tmag_vs_target": delta_tmag,
            }
        )

    neighbors.sort(
        key=lambda row: (
            float(row["separation_arcsec"]),
            int(row["tic_id"] or 0),
        )
    )
    within_pixel = [
        row for row in neighbors if float(row["separation_arcsec"]) <= 21.0
    ]
    meaningful = [
        row
        for row in within_pixel
        if row["delta_tmag_vs_target"] is None
        or float(row["delta_tmag_vs_target"]) <= 5.0
    ]
    if meaningful:
        risk = "high"
    elif within_pixel:
        risk = "moderate"
    elif neighbors:
        risk = "low"
    else:
        risk = "none_detected"
    return {
        "query_note": (
            "TIC sources are Gaia-crossmatched where available. This is a fast "
            "crowding screen, not a substitute for a Gaia DR3 quality/NSS query "
            "or a TESS difference image."
        ),
        "tess_pixel_scale_arcsec_approx": 21.0,
        "neighbors_in_query_radius": len(neighbors),
        "neighbors_within_one_tess_pixel": len(within_pixel),
        "crowding_risk": risk,
        "rough_neighbor_to_target_flux_ratio_upper_bound": round(
            flux_ratio_upper_bound, 6
        ),
        "neighbors": neighbors,
    }


def build_followup_actions(
    *,
    tic: Mapping[str, object],
    catalog: Mapping[str, object],
    mast: Mapping[str, object],
    neighbors: Mapping[str, object],
) -> list[dict[str, str]]:
    """Turn compact context into an ordered, conservative follow-up plan."""

    actions: list[dict[str, str]] = []
    tois = list(catalog.get("tois", []))
    confirmed = list(catalog.get("confirmed_planets", []))
    if tois or confirmed:
        actions.append(
            {
                "priority": "critical",
                "action": "Resolve the current NASA catalog match before making any new-candidate claim.",
                "reason": f"Found {len(tois)} TOI row(s) and {len(confirmed)} confirmed-planet row(s).",
            }
        )

    radius = _optional_float(tic.get("stellar_radius_solar"))
    lumclass = str(tic.get("luminosity_class") or "").upper()
    if lumclass == "GIANT" or (radius is not None and radius >= 2.0):
        actions.append(
            {
                "priority": "critical",
                "action": "Downgrade until giant-star and eclipsing-binary interpretations are tested.",
                "reason": (
                    f"TIC reports luminosity class {lumclass or 'unknown'} and "
                    f"radius {radius if radius is not None else 'unknown'} R_sun; "
                    "a given planet produces a shallower transit around a large star."
                ),
            }
        )

    tess = mast.get("tess", {})
    tess = tess if isinstance(tess, Mapping) else {}
    sectors = [int(value) for value in tess.get("all_sectors", [])]
    time_series_sectors = [
        int(value) for value in tess.get("timeseries_sectors", [])
    ]
    if len(sectors) >= 2:
        actions.append(
            {
                "priority": "high",
                "action": "Test the saved ephemeris across the additional TESS sectors before downloading another mission.",
                "reason": (
                    f"MAST reports TESS coverage in sectors {', '.join(map(str, sectors))}; "
                    f"mission/HLSP time series currently exist for {', '.join(map(str, time_series_sectors)) or 'none'}."
                ),
            }
        )
    reductions = [str(value) for value in tess.get("alternate_reductions", [])]
    if reductions:
        actions.append(
            {
                "priority": "high",
                "action": "Compare the signal in an independently extracted TESS light curve.",
                "reason": "Available reductions: " + ", ".join(reductions) + ".",
            }
        )

    if int(neighbors.get("neighbors_within_one_tess_pixel", 0)) > 0:
        actions.append(
            {
                "priority": "high",
                "action": "Run pixel localization and Gaia DR3 neighbor/NSS checks before promotion.",
                "reason": (
                    f"{neighbors.get('neighbors_within_one_tess_pixel')} TIC source(s) "
                    "lie within approximately one TESS pixel."
                ),
            }
        )
    elif tic.get("gaia_source_id"):
        actions.append(
            {
                "priority": "medium",
                "action": "Query the matched Gaia DR3 source for astrometric quality, variability, and non-single-star flags.",
                "reason": f"TIC cross-match: Gaia source {tic['gaia_source_id']}.",
            }
        )

    collections = mast.get("collection_counts", {})
    collections = collections if isinstance(collections, Mapping) else {}
    for mission in ("Kepler", "K2"):
        if int(collections.get(mission, 0)) > 0:
            actions.append(
                {
                    "priority": "high",
                    "action": f"Test the ephemeris and longer-period residuals in public {mission} time-series data.",
                    "reason": MISSION_ROLES[mission] + ".",
                }
            )
    for mission in ("HST", "JWST"):
        if int(collections.get(mission, 0)) > 0:
            actions.append(
                {
                    "priority": "later",
                    "action": f"Inspect the existing {mission} program metadata after the transit and host-star checks pass.",
                    "reason": MISSION_ROLES[mission] + ".",
                }
            )
    if int(collections.get("GALEX", 0)) > 0:
        actions.append(
            {
                "priority": "later",
                "action": "Use GALEX only as stellar-activity context.",
                "reason": MISSION_ROLES["GALEX"] + ".",
            }
        )
    return actions


def query_cross_mission_context(
    tic_id: int,
    *,
    mast_radius_arcsec: float = 3.0,
    neighbor_radius_arcsec: float = 42.0,
) -> dict[str, object]:
    """Query compact public metadata for one TIC target."""

    if tic_id <= 0:
        raise ValueError("TIC ID must be positive.")
    if mast_radius_arcsec <= 0 or neighbor_radius_arcsec <= 0:
        raise ValueError("Query radii must be positive.")

    import astropy.units as u
    from astropy.coordinates import SkyCoord
    from astroquery.mast import Catalogs, Observations

    from .catalogs import check_tic

    tic_rows = Catalogs.query_criteria(catalog="TIC", ID=tic_id)
    if len(tic_rows) != 1:
        raise RuntimeError(
            f"Expected one TIC catalog row for TIC {tic_id}; found {len(tic_rows)}."
        )
    row = tic_rows[0]
    ra = _optional_float(_row_value(row, "ra"))
    dec = _optional_float(_row_value(row, "dec"))
    if ra is None or dec is None:
        raise RuntimeError(f"TIC {tic_id} has no usable sky position.")
    tmag = _optional_float(_row_value(row, "Tmag"))
    tic = {
        "tic_id": tic_id,
        "ra_deg": ra,
        "dec_deg": dec,
        "tmag": tmag,
        "teff_k": _optional_float(_row_value(row, "Teff")),
        "stellar_radius_solar": _optional_float(_row_value(row, "rad")),
        "stellar_mass_solar": _optional_float(_row_value(row, "mass")),
        "distance_pc": _optional_float(_row_value(row, "d")),
        "parallax_mas": _optional_float(_row_value(row, "plx")),
        "luminosity_class": _plain(_row_value(row, "lumclass")),
        "object_type": _plain(_row_value(row, "objType")),
        "gaia_source_id": _optional_int(_row_value(row, "GAIA")),
        "kic_id": _optional_int(_row_value(row, "KIC")),
        "twomass_id": _plain(_row_value(row, "TWOMASS")),
        "contamination_ratio": _optional_float(_row_value(row, "contratio")),
    }
    coordinate = SkyCoord(ra, dec, unit="deg")
    observations = Observations.query_region(
        coordinate, radius=mast_radius_arcsec * u.arcsec
    )
    neighbor_rows = Catalogs.query_region(
        coordinate, radius=neighbor_radius_arcsec * u.arcsec, catalog="TIC"
    )
    catalog = check_tic(tic_id)
    mast = summarize_mast_observations(observations)
    neighbors = summarize_tic_neighbors(
        neighbor_rows, target_tic_id=tic_id, target_tmag=tmag
    )
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "warning": (
            "Metadata coverage and catalog absence do not establish a planet. "
            "No science products were downloaded by this command."
        ),
        "query": {
            "mast_radius_arcsec": mast_radius_arcsec,
            "neighbor_radius_arcsec": neighbor_radius_arcsec,
            "science_products_downloaded": 0,
        },
        "tic": tic,
        "nasa_exoplanet_archive": catalog,
        "mast_holdings": mast,
        "neighbor_context": neighbors,
        "recommended_actions": build_followup_actions(
            tic=tic, catalog=catalog, mast=mast, neighbors=neighbors
        ),
    }
