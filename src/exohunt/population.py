"""Population-level screens (T4): the absolute-time dip registry.

The shared-ephemeris screen (:mod:`exohunt.commonmode`) catches artifacts
after they alias into periods. This registry catches them earlier, in
absolute time: bin every prepared light curve in a cohort onto one time
axis, and any bin where an improbable fraction of unrelated stars dip
together is a systematic window -- scattered light, a momentum dump, an
edge artifact. Individual transit events falling in registered windows are
discounted before any period is fitted (see
:func:`exohunt.vetoes.dip_window_veto`).

A free by-product is the empirical per-sector map of shared instrumental
events the research review wanted from external documentation: here it is
measured from the cohort itself.

Two structural facts follow from MASTER_PLAN.md section 3.6, and both are
load-bearing rather than stylistic:

* **Cohorts are per sector-camera-CCD.** Only stars read off the same
  detector at the same time can share an observatory event. Pooling stars
  that were never observed together dilutes real windows and manufactures
  fake ones -- the same containment rule the shared-ephemeris screen
  follows.
* **Bins are anchored to absolute time, not to the cohort's own minimum.**
  A campaign cannot hold thousands of light curves in memory to discover a
  common origin first, and an origin that shifts as stars arrive would make
  the result depend on completion order. Anchoring every bin at
  ``floor(t / bin_days)`` makes a star's contribution independent of every
  other star, so :class:`DipRegistryAccumulator` can fold stars in one at a
  time and still produce exactly what the one-shot builder produces.
"""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np

from .config import CURRENT_CONFIG, PopulationConfig
from .vetoes import MEDIAN_STANDARD_ERROR_FACTOR

SCHEMA_VERSION = 1

# A cohort whose detector is unknown is kept separate rather than merged into
# a neighbouring one: an unknown camera is missing information, not a match.
UNKNOWN_COHORT_PART = "unknown"


def cohort_key(
    sector: object = None,
    camera: object = None,
    ccd: object = None,
) -> str:
    """Return the sector-camera-CCD key a registry is scoped to.

    The key is a display and grouping identity, so it stays a stable string
    even when a target list omits the detector columns.
    """

    def _part(value: object) -> str:
        if value is None or value == "":
            return UNKNOWN_COHORT_PART
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return UNKNOWN_COHORT_PART

    return f"s{_part(sector)}-cam{_part(camera)}-ccd{_part(ccd)}"


def _bin_days(cfg: PopulationConfig) -> float:
    return cfg.dip_bin_minutes / (24.0 * 60.0)


def star_bin_dips(
    time: np.ndarray,
    flux: np.ndarray,
    *,
    config: PopulationConfig | None = None,
) -> dict[int, bool]:
    """Return ``{absolute_bin_index: dipped}`` for one prepared light curve.

    A bin is present in the mapping when the star has enough samples there to
    have an opinion at all; the boolean says whether the star dipped. Absence
    is therefore "not observed", which is deliberately different from
    "observed and steady" -- only the latter belongs in the denominator.
    """

    cfg = config or CURRENT_CONFIG.population
    bin_days = _bin_days(cfg)
    t = np.asarray(time, dtype=float)
    y = np.asarray(flux, dtype=float)
    finite = np.isfinite(t) & np.isfinite(y)
    t, y = t[finite], y[finite]
    # Too few samples to estimate a robust scatter; contributing this star
    # would add noise to the denominator without adding information.
    if t.size < 10:
        return {}
    center = float(np.nanmedian(y))
    scatter = float(1.4826 * np.nanmedian(np.abs(y - center)))
    if not np.isfinite(scatter) or scatter <= 0:
        return {}
    indices = np.floor(t / bin_days).astype(np.int64)
    flags: dict[int, bool] = {}
    for index in np.unique(indices):
        values = y[indices == index]
        if values.size < 2:
            continue
        depth = center - float(np.nanmedian(values))
        # The bin depth is a *median*, so its uncertainty is the asymptotic
        # standard error of a median, sqrt(pi/2) * sigma / sqrt(n) -- not the
        # mean's sigma / sqrt(n). Dropping the factor understates the scatter
        # by ~25%, which turns a nominal 3-sigma trip into roughly 2.4 sigma
        # and lets pure noise register systematic windows (measured: 3 of 40
        # pure-noise cohorts before this correction, 0 of 40 after). This is
        # the same estimator error PROGRESS correction 19 fixed in the T3
        # secondary scan; it survived here because this module had no
        # production caller.
        error = MEDIAN_STANDARD_ERROR_FACTOR * scatter / np.sqrt(values.size)
        significance = depth / error
        flags[int(index)] = bool(significance > cfg.dip_star_sigma)
    return flags


class DipRegistryAccumulator:
    """Fold stars into one cohort's dip counts without holding light curves.

    Memory is two integers per occupied time bin, not one array per star, so
    a campaign can accumulate across thousands of targets and still build the
    registry at finalization.
    """

    def __init__(self, config: PopulationConfig | None = None) -> None:
        self._cfg = config or CURRENT_CONFIG.population
        self._stars_in_bin: dict[int, int] = {}
        self._dips_in_bin: dict[int, int] = {}
        self._stars = 0

    @property
    def stars(self) -> int:
        return self._stars

    def add_flags(self, flags: dict[int, bool]) -> None:
        """Fold one star's precomputed bin flags into the cohort."""

        self._stars += 1
        for index, dipped in flags.items():
            self._stars_in_bin[index] = self._stars_in_bin.get(index, 0) + 1
            if dipped:
                self._dips_in_bin[index] = self._dips_in_bin.get(index, 0) + 1

    def add_curve(self, time: np.ndarray, flux: np.ndarray) -> None:
        """Fold one prepared light curve into the cohort."""

        self.add_flags(star_bin_dips(time, flux, config=self._cfg))

    def build(self, *, scope: str | None = None) -> dict[str, Any]:
        """Return the registry for everything folded in so far."""

        cfg = self._cfg
        bin_days = _bin_days(cfg)
        settings = _settings(cfg)
        if not self._stars_in_bin:
            registry: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "windows": [],
                "stars": self._stars,
                "bins": 0,
                "settings": settings,
            }
            if scope is not None:
                registry["cohort"] = scope
            return registry

        first = min(self._stars_in_bin)
        last = max(self._stars_in_bin)
        windows: list[dict[str, Any]] = []
        index = first
        while index <= last:
            if not self._is_flagged(index):
                index += 1
                continue
            start = index
            while index <= last and self._is_flagged(index):
                index += 1
            stop = index - 1
            span = range(start, stop + 1)
            windows.append(
                {
                    "start": round(start * bin_days, 5),
                    "stop": round((stop + 1) * bin_days, 5),
                    "peak_fraction": round(
                        max(self._fraction(i) for i in span), 4
                    ),
                    "stars_evaluated": max(
                        self._stars_in_bin.get(i, 0) for i in span
                    ),
                }
            )
        registry = {
            "schema_version": SCHEMA_VERSION,
            "windows": windows,
            "window_spans": [
                (window["start"], window["stop"]) for window in windows
            ],
            "stars": self._stars,
            "bins": last - first + 1,
            "time_range": [
                round(first * bin_days, 5),
                round((last + 1) * bin_days, 5),
            ],
            "settings": settings,
            "warning": (
                "A registered window says many unrelated stars dimmed together "
                "at this absolute time; individual events there are discounted. "
                "It does not certify events outside the windows as astrophysical."
            ),
        }
        if scope is not None:
            registry["cohort"] = scope
        return registry

    def _fraction(self, index: int) -> float:
        stars = self._stars_in_bin.get(index, 0)
        if stars <= 0:
            return 0.0
        return self._dips_in_bin.get(index, 0) / stars

    def _is_flagged(self, index: int) -> bool:
        # Both floors matter. The star floor stops a handful of targets from
        # manufacturing a window by chance; the fraction floor is what makes
        # the window a *shared* event rather than one star's transit.
        return (
            self._stars_in_bin.get(index, 0) >= self._cfg.dip_min_stars
            and self._fraction(index) >= self._cfg.dip_min_fraction
        )


class CohortDipRegistries:
    """Accumulate one registry per sector-camera-CCD cohort."""

    def __init__(self, config: PopulationConfig | None = None) -> None:
        self._cfg = config or CURRENT_CONFIG.population
        self._cohorts: dict[str, DipRegistryAccumulator] = {}

    def add_flags(self, key: str, flags: dict[int, bool]) -> None:
        accumulator = self._cohorts.get(key)
        if accumulator is None:
            accumulator = DipRegistryAccumulator(self._cfg)
            self._cohorts[key] = accumulator
        accumulator.add_flags(flags)

    def add_curve(
        self,
        time: np.ndarray,
        flux: np.ndarray,
        *,
        sector: object = None,
        camera: object = None,
        ccd: object = None,
    ) -> str:
        key = cohort_key(sector, camera, ccd)
        self.add_flags(key, star_bin_dips(time, flux, config=self._cfg))
        return key

    def build(self) -> dict[str, dict[str, Any]]:
        return {
            key: accumulator.build(scope=key)
            for key, accumulator in sorted(self._cohorts.items())
        }

    def windows_for(self, key: str) -> list[tuple[float, float]]:
        """Return the registered spans for one cohort, or none when absent."""

        accumulator = self._cohorts.get(key)
        if accumulator is None:
            return []
        return [
            (window["start"], window["stop"])
            for window in accumulator.build(scope=key)["windows"]
        ]


def build_dip_registry(
    curves: Iterable[tuple[int, np.ndarray, np.ndarray]],
    *,
    config: PopulationConfig | None = None,
) -> dict[str, Any]:
    """Build the shared-dip window registry for one cohort.

    ``curves`` yields ``(tic_id, time, normalized_flux)`` for every prepared
    star observed together (one sector-camera cohort; pooling stars never
    observed together would dilute real windows and manufacture fake ones,
    the same rule the shared-ephemeris screen follows).
    """

    cfg = config or CURRENT_CONFIG.population
    accumulator = DipRegistryAccumulator(cfg)
    for _tic_id, time, flux in curves:
        accumulator.add_curve(time, flux)
    return accumulator.build()


def encode_star_bins(
    flags: dict[int, bool],
    *,
    config: PopulationConfig | None = None,
) -> dict[str, Any]:
    """Compress one star's bin flags for storage inside its report.

    Reports are the project's durable truth, so carrying each star's own
    contribution there makes the registry rebuildable from evidence alone --
    no cached photometry, no re-download. The encoding matters because the
    alternative is thousands of integers per report: observed bins are
    run-length encoded (a sector is a few contiguous stretches split by
    downlink gaps) and dipped bins are listed explicitly because at a correct
    3-sigma gate they are rare.
    """

    cfg = config or CURRENT_CONFIG.population
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "bin_minutes": cfg.dip_bin_minutes,
        "observed_spans": [],
        "dipped": sorted(index for index, dipped in flags.items() if dipped),
    }
    if not flags:
        return payload
    spans: list[list[int]] = []
    for index in sorted(flags):
        if spans and index == spans[-1][1] + 1:
            spans[-1][1] = index
        else:
            spans.append([index, index])
    payload["observed_spans"] = spans
    return payload


def decode_star_bins(payload: object) -> dict[int, bool]:
    """Rebuild bin flags from :func:`encode_star_bins`, tolerantly."""

    if not isinstance(payload, dict):
        return {}
    dipped = {
        int(index)
        for index in payload.get("dipped", [])
        if isinstance(index, (int, float))
    }
    flags: dict[int, bool] = {}
    for span in payload.get("observed_spans", []):
        if not isinstance(span, (list, tuple)) or len(span) != 2:
            continue
        start, stop = span
        if not isinstance(start, (int, float)) or not isinstance(
            stop, (int, float)
        ):
            continue
        start, stop = int(start), int(stop)
        if stop < start:
            continue
        for index in range(start, stop + 1):
            flags[index] = index in dipped
    return flags


def registries_from_reports(
    reports: Iterable[tuple[str, object]],
    *,
    config: PopulationConfig | None = None,
) -> dict[str, dict[str, Any]]:
    """Build per-cohort registries from ``(cohort_key, encoded_bins)`` pairs.

    This is the T4 entry point the plan describes as pure post-processing: it
    reads only what reports already record, so a registry can be rebuilt or
    re-thresholded at any time without re-running a search.
    """

    cfg = config or CURRENT_CONFIG.population
    registries = CohortDipRegistries(cfg)
    for key, payload in reports:
        flags = decode_star_bins(payload)
        if not flags:
            continue
        registries.add_flags(key, flags)
    return registries.build()


def registry_windows(registry: dict[str, Any] | None) -> list[tuple[float, float]]:
    """Return ``(start, stop)`` spans from a registry payload, tolerantly.

    Reports and ledger evidence are read back long after they are written, so
    a missing or malformed registry degrades to "no registered windows"
    rather than raising: an absent screen must never look like a clean one to
    the caller, and every caller here treats an empty list as "not applied".
    """

    if not isinstance(registry, dict):
        return []
    spans = registry.get("window_spans")
    if isinstance(spans, list):
        pairs = spans
    else:
        pairs = [
            (window.get("start"), window.get("stop"))
            for window in registry.get("windows", [])
            if isinstance(window, dict)
        ]
    result: list[tuple[float, float]] = []
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        start, stop = pair
        if not isinstance(start, (int, float)) or not isinstance(
            stop, (int, float)
        ):
            continue
        if not np.isfinite(start) or not np.isfinite(stop) or stop < start:
            continue
        result.append((float(start), float(stop)))
    return result


def _settings(cfg: PopulationConfig) -> dict[str, float | int]:
    return {
        "dip_bin_minutes": cfg.dip_bin_minutes,
        "dip_star_sigma": cfg.dip_star_sigma,
        "dip_min_fraction": cfg.dip_min_fraction,
        "dip_min_stars": cfg.dip_min_stars,
    }
