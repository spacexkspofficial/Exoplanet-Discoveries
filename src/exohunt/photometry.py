"""TESS photometry source selection, download, and extraction.

This module is the structural home for the historical acquisition path that
previously lived in :mod:`exohunt.cli`.  Its behavior is intentionally
unchanged while the P2 kernel is wired into the campaign path.
"""

from __future__ import annotations

import os
import re
import warnings
from pathlib import Path

import numpy as np

from .detrending import flatten_edge_safe
from .paths import resolve_cache_dir


def _sector_values(sector: int | list[int] | None) -> list[int]:
    if sector is None:
        return []
    if isinstance(sector, int):
        return [sector]
    return sorted(set(int(value) for value in sector))


def _safe_cache_namespace(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "target"


def _configured_lightkurve():
    # High-churn, re-downloadable FITS data defaults to the unsynced local
    # state root; OneDrive locking the cache mid-write has broken campaigns.
    cache_dir = resolve_cache_dir(
        os.environ.get("EXOHUNT_CACHE_DIR"), workspace_root=Path.cwd()
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    warnings.filterwarnings(
        "ignore",
        message="Warning: the tpfmodel submodule is not available",
        category=UserWarning,
    )
    import lightkurve as lk

    lk.conf.cache_dir = str(cache_dir)
    return lk, cache_dir


def _thread_safe_lightkurve_download(method, **kwargs):
    """Call a Lightkurve download without its process-global stdout redirect.

    Lightkurve decorates SearchResult downloads by replacing ``sys.stdout`` for
    the entire network request. Two concurrent calls can restore and close each
    other's stream, terminating a parallel batch with "I/O operation on closed
    file." ``functools.wraps`` exposes the original method, which is safe to
    call concurrently (its progress text may interleave, but no stream closes).
    """

    original = getattr(method, "__wrapped__", None)
    owner = getattr(method, "__self__", None)
    if original is not None and owner is not None:
        return original(owner, **kwargs)
    return method(**kwargs)


# Mission- and community-processed light curves in descending order of trust.
# Each applies a background and systematics treatment that a bare aperture sum
# over a TESScut cutout does not, which is what a blind transit search needs:
# uncorrected scattered light around perigee imprints the 13.7-day spacecraft
# orbit on the photometry and dominates the resulting detections.
AUTHOR_PREFERENCE = ("SPOC", "TESS-SPOC", "QLP")


def _available_products(lk, target: str, sectors: list[int], author: str):
    """Search one author without pinning a cadence, tolerating MAST gaps."""

    kwargs: dict[str, object] = {"mission": "TESS", "author": author}
    if sectors:
        kwargs["sector"] = sectors
    try:
        return lk.search_lightcurve(target, **kwargs)
    except Exception:
        # A single author being unavailable must not end the search; the next
        # one in the chain may still cover this target.
        return None


# Below roughly two minutes, finer sampling stops adding transit-detection
# power: the shortest event this pipeline fits is fifteen minutes, which a
# 120-second cadence already covers several times over. Twenty-second data
# would multiply the download and search cost for no gain, so it is used only
# when nothing coarser exists.
MIN_USEFUL_CADENCE_SECONDS = 100.0


def _preferred_exposure(search) -> float | None:
    """Pick one exposure time: the finest that still earns its data volume.

    Mixing cadences in one stitched light curve distorts the duration fit, so
    exactly one exposure time is selected rather than downloading everything.
    """

    table = getattr(search, "table", None)
    if table is None or "exptime" not in getattr(table, "colnames", []):
        return None
    values: list[float] = []
    for raw in table["exptime"]:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            values.append(value)
    if not values:
        return None
    useful = [value for value in values if value >= MIN_USEFUL_CADENCE_SECONDS]
    return min(useful) if useful else min(values)


def resolve_light_curve_source(
    lk,
    target: str,
    sectors: list[int],
    *,
    preference: tuple[str, ...] = AUTHOR_PREFERENCE,
    allow_tesscut: bool = True,
) -> dict[str, object]:
    """Choose the best available reduction for one target.

    Returns the first author in ``preference`` that actually has data, together
    with the exposure time to pin. TESScut is reported only as a last resort,
    because extracting it locally reintroduces exactly the systematics the
    processed products already remove.
    """

    considered: list[dict[str, object]] = []
    for author in preference:
        search = _available_products(lk, target, sectors, author)
        count = 0 if search is None else len(search)
        considered.append({"author": author, "products": int(count)})
        if count:
            return {
                "author": author,
                "cadence_seconds": _preferred_exposure(search),
                "fallback": False,
                "considered": considered,
            }
    if not allow_tesscut:
        raise RuntimeError(
            f"No processed TESS light curve (tried {', '.join(preference)}) "
            f"is available for {target!r}."
        )
    return {
        "author": "TESScut",
        "cadence_seconds": None,
        "fallback": True,
        "considered": considered,
    }


def prepare_search_arrays(
    time_values: np.ndarray,
    flux_values: np.ndarray,
    metadata: dict[str, object],
):
    """Run the edge-safe detrend that the download stage would otherwise run.

    Preparation is real CPU work -- a Savitzky-Golay filter over roughly
    eighteen thousand cadences, plus iterative outlier clipping. Doing it in
    the campaign's download threads pinned the coordinator process at 150% of
    one core while eight analysis workers sat at 2% each, starved. Splitting it
    out lets the workers do it in parallel.

    Takes and returns plain arrays so it can run either side of a process
    boundary, and adds exactly the metadata keys the flattening step would have
    contributed, so a report cannot tell which stage produced them.
    """

    import lightkurve as lk

    # Do NOT coerce dtype. Mission flux arrives as float32, and upcasting to
    # float64 changes the Savitzky-Golay arithmetic by about 6e-7 in relative
    # flux -- invisible on its own, but measured across sixteen targets it
    # moved every fitted depth and flipped one target's period from 5.987 d to
    # 5.965 d. Preserving the incoming dtype reproduces the in-place result
    # exactly.
    curve = lk.LightCurve(time=time_values, flux=flux_values)
    flattened, detrending = flatten_edge_safe(curve)
    enriched = dict(metadata)
    enriched["detrending"] = detrending
    enriched["cadence_minutes"] = float(detrending["cadence_days"]) * 24 * 60
    enriched["flatten_window_cadences"] = int(detrending["window_cadences"])
    return flattened.time.value, flattened.flux.value, enriched


def _cached_products(download_dir: Path) -> list[Path]:
    """Light-curve files already present in a target's cache namespace."""

    if not download_dir.is_dir():
        return []
    return sorted(download_dir.rglob("*_lc.fits"))


def _read_cached_collection(lk, paths: list[Path]):
    """Read cached products the way ``download_all`` would return them.

    ``quality_bitmask`` must match what the download path passes, because it
    decides which cadences exist at all -- reading with a different mask would
    silently change the searched data.
    """

    curves = [lk.read(str(path), quality_bitmask="default") for path in paths]
    return lk.LightCurveCollection(curves)


def _cached_identity(curves) -> tuple[int | None, list[int], str | None]:
    """TIC, sectors and author taken from the product headers.

    The search table normally supplies these. Headers are the same facts from
    the file itself, which is what makes skipping the search safe rather than
    a guess.
    """

    tic_id: int | None = None
    author: str | None = None
    sectors: set[int] = set()
    for curve in curves:
        meta = getattr(curve, "meta", {}) or {}
        for key in ("TICID", "TICVER", "OBJECT"):
            raw = meta.get(key)
            if tic_id is None and raw is not None:
                match = re.search(r"(\d+)", str(raw))
                if match:
                    tic_id = int(match.group(1))
                    break
        if author is None and meta.get("AUTHOR"):
            author = str(meta["AUTHOR"]).strip()
        if meta.get("SECTOR") is not None:
            try:
                sectors.add(int(meta["SECTOR"]))
            except (TypeError, ValueError):
                continue
    return tic_id, sorted(sectors), author


def _download_light_curve(
    target: str,
    sector: int | list[int] | None,
    author: str,
    cadence_seconds: float | None = 120.0,
    *,
    cache_namespace: str | None = None,
    flatten: bool = True,
):
    """Fetch one target's photometry, optionally leaving it unprepared.

    ``flatten=False`` returns the normalized-but-undetrended light curve and
    omits the metadata keys that detrending produces, so a caller can run
    :func:`prepare_search_arrays` elsewhere -- in a campaign, on an analysis
    worker rather than on the coordinator. Every other caller keeps the
    prepared result it has always received.
    """

    lk, cache_dir = _configured_lightkurve()
    # Astroquery's TESScut client names its temporary ZIP with only
    # second-level precision. Concurrent downloads into one directory can
    # therefore overwrite each other, producing CRC, bad-magic, and
    # cross-target filename errors. Give each batch target a stable isolated
    # namespace while retaining every file beneath the rolling cache root.
    download_dir = (
        cache_dir / "batch_targets" / _safe_cache_namespace(cache_namespace)
        if cache_namespace
        else cache_dir
    )
    download_dir.mkdir(parents=True, exist_ok=True)
    sectors = _sector_values(sector)
    # Preserved so a resumed campaign can recognise its own earlier reports:
    # reuse must compare what was *asked for*, not what auto-selection resolved
    # to, otherwise every resume would re-download the whole target list.
    requested_author = author
    requested_cadence_seconds = cadence_seconds
    selection: dict[str, object] | None = None

    # Serve an already-cached product without asking the archive about it.
    #
    # `search_lightcurve` runs before every download, including when the file
    # is already on disk, and it is a network round trip. Measured on this
    # pipeline it costs about 2.2 s alone but roughly 37 s under eight-way
    # concurrency, against 0.14 s to read the local file -- enough to starve
    # the analysis pool and cap a fully cached campaign near 430 stars/hour.
    #
    # Only taken when the cache can be *proved* to answer the request: an
    # explicit author (auto-selection needs the archive to compare authors),
    # not TESScut (that path extracts from a cutout rather than reading a
    # product), and headers whose author and sectors match what was asked for.
    # Anything unproven falls through to the search, so this can substitute a
    # cache hit but never a different reduction.
    if (
        cache_namespace is not None
        and author not in {"auto", "TESScut"}
        and sectors
    ):
        cached_paths = _cached_products(download_dir)
        if cached_paths:
            try:
                collection = _read_cached_collection(lk, cached_paths)
                cached_tic, cached_sectors, cached_author = _cached_identity(
                    collection
                )
                matches = (
                    cached_sectors == sorted(sectors)
                    and cached_author is not None
                    and cached_author.upper() == author.upper()
                )
            except Exception:
                # A truncated or unreadable cache entry is not fatal; the
                # search path below will re-fetch it.
                matches = False
            if matches:
                normalized = collection.stitch(
                    corrector_func=lambda lc: lc.remove_nans()
                    .normalize()
                    .remove_outliers(sigma_upper=4.0, sigma_lower=20.0)
                )
                metadata = {
                    "target": target,
                    "tic_id": cached_tic,
                    "requested_sectors": sectors,
                    "downloaded_sectors": cached_sectors,
                    "author": author,
                    "requested_cadence_seconds": requested_cadence_seconds,
                    "downloaded_products": len(collection),
                    "requested_author": requested_author,
                    "resolved_cadence_seconds": cadence_seconds,
                    "author_selection": "explicit",
                    "author_fallback_to_tesscut": False,
                    "authors_considered": [],
                    "served_from_cache": True,
                }
                if not flatten:
                    # The analysis stage checks this rather than inferring from a missing
                    # detrending block, so a caller that supplies prepared arrays without
                    # one is never detrended twice.
                    metadata["requires_preparation"] = True
                    return (
                        normalized.time.value,
                        normalized.flux.value,
                        metadata,
                    )
                flattened, detrending = flatten_edge_safe(normalized)
                metadata["detrending"] = detrending
                metadata["cadence_minutes"] = (
                    float(detrending["cadence_days"]) * 24 * 60
                )
                metadata["flatten_window_cadences"] = int(
                    detrending["window_cadences"]
                )
                return flattened.time.value, flattened.flux.value, metadata

    if author == "auto":
        # TESScut needs exactly one sector, so it can only be the fallback when
        # the request is already scoped to one.
        selection = resolve_light_curve_source(
            lk, target, sectors, allow_tesscut=len(sectors) == 1
        )
        author = str(selection["author"])
        resolved_cadence = selection.get("cadence_seconds")
        if author == "TESScut":
            # The local extraction still needs an explicit FFI cadence; keep the
            # caller's value when it supplied one.
            cadence_seconds = cadence_seconds or 158.0
        else:
            cadence_seconds = (
                float(resolved_cadence) if resolved_cadence else cadence_seconds
            )
    if author == "TESScut":
        if len(sectors) != 1:
            raise ValueError("TESScut searches require exactly one TESS sector.")
        search = lk.search_tesscut(target, sector=sectors[0])
        if len(search) == 0:
            raise RuntimeError(
                f"No public TESScut data found for {target!r} in Sector {sectors[0]}."
            )
        tpf = _thread_safe_lightkurve_download(
            search.download,
            cutout_size=11,
            quality_bitmask="default",
            download_dir=str(download_dir),
        )
        if tpf is None:
            raise RuntimeError(
                "MAST returned no downloadable TESScut target-pixel file."
            )
        aperture_mask = tpf.create_threshold_mask(
            threshold=3, reference_pixel="center"
        )
        if int(np.count_nonzero(aperture_mask)) == 0:
            aperture_mask = np.zeros(tpf.flux.shape[1:], dtype=bool)
            center_row = aperture_mask.shape[0] // 2
            center_column = aperture_mask.shape[1] // 2
            aperture_mask[
                max(0, center_row - 1) : center_row + 2,
                max(0, center_column - 1) : center_column + 2,
            ] = True
        aperture_pixels = int(np.count_nonzero(aperture_mask))
        background_mask = ~aperture_mask
        background_pixels = int(np.count_nonzero(background_mask))
        raw_lc = tpf.to_lightcurve(aperture_mask=aperture_mask)
        background_per_pixel = np.nanmedian(tpf.flux[:, background_mask], axis=1)
        corrected_lc = raw_lc.copy()
        corrected_lc.flux = raw_lc.flux - background_per_pixel * aperture_pixels
        corrected_flux = np.asarray(corrected_lc.flux.value, dtype=float)
        median_flux = float(np.nanmedian(corrected_flux))
        relative_scatter = float(np.nanstd(corrected_flux) / median_flux)
        if not np.isfinite(median_flux) or median_flux <= 0:
            raise RuntimeError(
                "TESScut background subtraction left non-positive target flux."
            )
        if not np.isfinite(relative_scatter) or relative_scatter > 0.5:
            raise RuntimeError(
                "TESScut extraction remains background-dominated after subtraction "
                f"(relative scatter {relative_scatter:.3f})."
            )
        normalized = (
            corrected_lc.remove_nans()
            .normalize()
            .remove_outliers(sigma_upper=4.0, sigma_lower=20.0)
        )
        tic_match = re.search(r"\b(\d+)\b", target)
        metadata = {
            "target": target,
            "tic_id": int(tic_match.group(1)) if tic_match else None,
            "requested_sectors": sectors,
            "downloaded_sectors": sectors,
            "author": author,
            "requested_cadence_seconds": requested_cadence_seconds,
            "downloaded_products": 1,
            "tesscut_size_pixels": 11,
            "aperture_pixels": aperture_pixels,
            "background_pixels": background_pixels,
            "background_subtracted": True,
            "pre_normalization_relative_scatter": relative_scatter,
            "extraction_version": "tesscut-bgsub-v1",
            "requested_author": requested_author,
            "resolved_cadence_seconds": cadence_seconds,
            "author_selection": "auto" if selection else "explicit",
            "author_fallback_to_tesscut": bool(
                selection and selection["fallback"]
            ),
            "authors_considered": (selection or {}).get("considered", []),
        }
        if not flatten:
            # The analysis stage checks this rather than inferring from a missing
            # detrending block, so a caller that supplies prepared arrays without
            # one is never detrended twice.
            metadata["requires_preparation"] = True
            return normalized.time.value, normalized.flux.value, metadata
        flattened, detrending = flatten_edge_safe(normalized)
        metadata["detrending"] = detrending
        metadata["cadence_minutes"] = (
            float(detrending["cadence_days"]) * 24 * 60
        )
        metadata["flatten_window_cadences"] = int(detrending["window_cadences"])
        return flattened.time.value, flattened.flux.value, metadata

    kwargs: dict[str, object] = {"mission": "TESS", "author": author}
    if sectors:
        kwargs["sector"] = sectors
    if cadence_seconds is not None:
        kwargs["exptime"] = cadence_seconds
    search = lk.search_lightcurve(target, **kwargs)
    if len(search) == 0:
        raise RuntimeError(
            f"No {author} TESS light curve found for {target!r}"
            + (f" in sectors {sectors}." if sectors else ".")
            + " Try --author TESS-SPOC or --author QLP."
        )
    collection = _thread_safe_lightkurve_download(
        search.download_all,
        quality_bitmask="default",
        download_dir=str(download_dir),
    )
    if collection is None or len(collection) == 0:
        raise RuntimeError("MAST returned no downloadable light curves.")
    normalized = collection.stitch(
        corrector_func=lambda lc: lc.remove_nans()
        .normalize()
        .remove_outliers(sigma_upper=4.0, sigma_lower=20.0)
    )
    target_name = str(search.table["target_name"][0]).strip()
    tic_id = int(target_name) if target_name.isdigit() else None
    downloaded_sectors = sorted(
        {
            int(match.group(1))
            for mission in search.table["mission"]
            if (match := re.search(r"Sector\s+(\d+)", str(mission)))
        }
    )
    metadata = {
        "target": target,
        "tic_id": tic_id,
        "requested_sectors": sectors,
        "downloaded_sectors": downloaded_sectors,
        "author": author,
        "requested_cadence_seconds": requested_cadence_seconds,
        "downloaded_products": len(collection),
        "requested_author": requested_author,
        "resolved_cadence_seconds": cadence_seconds,
        "author_selection": "auto" if selection else "explicit",
        "author_fallback_to_tesscut": bool(selection and selection["fallback"]),
        "authors_considered": (selection or {}).get("considered", []),
    }
    if not flatten:
        # The analysis stage checks this rather than inferring from a missing
        # detrending block, so a caller that supplies prepared arrays without
        # one is never detrended twice.
        metadata["requires_preparation"] = True
        return normalized.time.value, normalized.flux.value, metadata
    flattened, detrending = flatten_edge_safe(normalized)
    metadata["detrending"] = detrending
    metadata["cadence_minutes"] = float(detrending["cadence_days"]) * 24 * 60
    metadata["flatten_window_cadences"] = int(detrending["window_cadences"])
    return flattened.time.value, flattened.flux.value, metadata
