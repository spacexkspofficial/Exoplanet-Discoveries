"""Compact authoritative catalog evidence for automated transit leads.

The initial TESS search answers only whether a signal survived local numerical
screens.  This module asks independent public metadata services whether the
same target or period is already associated with a planet, threshold-crossing
event, eclipsing binary, variable star, or non-single-star solution.  It never
downloads telescope science products.
"""

from __future__ import annotations

import html
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Iterable, Mapping

import requests

from .tce import check_tces


TESS_EB_SEARCH_URL = "https://tessebs.villanova.edu/search_results"
SIMBAD_URL = "https://simbad.cds.unistra.fr/simbad/"
GAIA_ARCHIVE_URL = "https://gea.esac.esa.int/archive/"


def _plain(value: object) -> object | None:
    if value is None:
        return None
    try:
        if bool(getattr(value, "mask", False)):
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
    return None if text in {"", "--", "nan", "None"} else value


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


def _row_value(row: object, name: str) -> object | None:
    try:
        return _plain(row[name])  # type: ignore[index]
    except (KeyError, TypeError, ValueError):
        return None


def _read_text(url: str, *, timeout: int = 60, attempts: int = 3) -> str:
    headers = {
        "User-Agent": "EXOHUNT/0.1 (+metadata-only catalog vetting)"
    }
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as error:
            status = getattr(error.response, "status_code", None)
            if attempt >= attempts or (
                status is not None and status != 429 and status < 500
            ):
                raise
        except (TimeoutError, ConnectionError):
            if attempt >= attempts:
                raise
        time.sleep(2 ** (attempt - 1))
    raise RuntimeError("Catalog request failed without an exception.")


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._cell: str | None = None
        self._parts: list[str] = []
        self.headers: list[str] = []
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"th", "td"}:
            self._cell = tag
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._cell:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"th", "td"} and self._cell == tag:
            value = " ".join(
                html.unescape("".join(self._parts)).split()
            ).strip()
            if tag == "th":
                self.headers.append(value)
            elif self._row is not None:
                self._row.append(value)
            self._cell = None
            self._parts = []
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def query_tess_eb_catalog(tic_id: int) -> dict[str, object]:
    """Query the live Villanova TESS Eclipsing Binary Catalog by exact TIC."""

    if tic_id <= 0:
        raise ValueError("TIC ID must be positive.")
    columns = [
        "in_catalog",
        "tic__tess_id",
        "sectors",
        "bjd0",
        "period",
        "period_uncert",
        "morph_coeff",
        "source",
        "flags",
    ]
    parameters: list[tuple[str, str]] = [("tic", str(tic_id))]
    parameters.extend(("c", value) for value in columns)
    url = TESS_EB_SEARCH_URL + "?" + urllib.parse.urlencode(parameters)
    parser = _TableParser()
    parser.feed(_read_text(url))
    rows: list[dict[str, object]] = []
    for values in parser.rows:
        if len(values) != len(parser.headers):
            continue
        raw = dict(zip(parser.headers, values))
        returned_tic = _optional_int(raw.get("TESS ID"))
        if returned_tic != tic_id:
            continue
        sectors = [
            int(value)
            for value in str(raw.get("Sectors") or "").replace(";", ",").split(",")
            if value.strip().isdigit()
        ]
        rows.append(
            {
                "tic_id": tic_id,
                "in_catalog": str(raw.get("In catalog") or "").lower()
                in {"true", "1", "yes"},
                "sectors": sorted(set(sectors)),
                "epoch_btjd": _optional_float(raw.get("t0 [BJD]")),
                "period_days": _optional_float(raw.get("P0 [days]")),
                "period_uncertainty_days": _optional_float(
                    raw.get("σ(P0) [days]")
                ),
                "morphology": _optional_float(raw.get("Morphology")),
                "source": raw.get("Source") or None,
                "flags": raw.get("Flags") or None,
            }
        )
    return {
        "source": "Villanova live TESS Eclipsing Binary Catalog",
        "source_url": TESS_EB_SEARCH_URL,
        "status": "completed",
        "tic_id": tic_id,
        "match": bool(rows),
        "rows": rows,
    }


def query_simbad_context(tic_id: int) -> dict[str, object]:
    """Return exact-identifier SIMBAD object types and aliases."""

    from astroquery.simbad import Simbad

    client = Simbad()
    client.TIMEOUT = 60
    client.add_votable_fields("otype", "otypes", "ids")
    table = client.query_object(f"TIC {tic_id}")
    if table is None or len(table) == 0:
        return {
            "source": "SIMBAD",
            "source_url": SIMBAD_URL,
            "status": "completed",
            "match": False,
            "tic_id": tic_id,
            "main_id": None,
            "object_types": [],
            "identifiers": [],
        }
    main_ids: set[str] = set()
    types: set[str] = set()
    identifiers: set[str] = set()
    for row in table:
        main_id = _row_value(row, "main_id")
        if main_id:
            main_ids.add(str(main_id))
        for column in ("otype", "otypes.otype", "otypes.otype_txt"):
            value = _row_value(row, column)
            if value:
                types.add(str(value))
        raw_ids = _row_value(row, "ids")
        if raw_ids:
            identifiers.update(
                value.strip()
                for value in str(raw_ids).split("|")
                if value.strip()
            )
    return {
        "source": "SIMBAD",
        "source_url": SIMBAD_URL,
        "status": "completed",
        "match": True,
        "tic_id": tic_id,
        "main_id": sorted(main_ids)[0] if main_ids else None,
        "object_types": sorted(types),
        "identifiers": sorted(identifiers),
    }


def query_gaia_dr3_context(source_id: int | None) -> dict[str, object]:
    """Query Gaia DR3 variability and non-single-star tables by source ID."""

    if not source_id:
        return {
            "source": "Gaia DR3",
            "source_url": GAIA_ARCHIVE_URL,
            "status": "not_applicable",
            "reason": "The TIC row has no Gaia DR3 source identifier.",
            "source_id": None,
            "rows": [],
        }
    from astroquery.gaia import Gaia

    query = (
        "select gs.source_id, vs.in_vari_eclipsing_binary, "
        "vs.in_vari_planetary_transit, vs.in_vari_classification_result, "
        "vc.best_class_name, vc.best_class_score, "
        "veb.frequency, veb.frequency_error, "
        "nss.nss_solution_type, nss.period as nss_period_days "
        "from gaiadr3.gaia_source as gs "
        "left outer join gaiadr3.vari_summary as vs "
        "on gs.source_id=vs.source_id "
        "left outer join gaiadr3.vari_classifier_result as vc "
        "on gs.source_id=vc.source_id "
        "left outer join gaiadr3.vari_eclipsing_binary as veb "
        "on gs.source_id=veb.source_id "
        "left outer join gaiadr3.nss_two_body_orbit as nss "
        "on gs.source_id=nss.source_id "
        f"where gs.source_id={int(source_id)}"
    )
    table = Gaia.launch_job(query).get_results()
    rows: list[dict[str, object]] = []
    for row in table:
        frequency = _optional_float(_row_value(row, "frequency"))
        rows.append(
            {
                "source_id": int(source_id),
                "in_vari_eclipsing_binary": bool(
                    _row_value(row, "in_vari_eclipsing_binary") or False
                ),
                "in_vari_planetary_transit": bool(
                    _row_value(row, "in_vari_planetary_transit") or False
                ),
                "in_vari_classification_result": bool(
                    _row_value(row, "in_vari_classification_result") or False
                ),
                "best_class_name": _row_value(row, "best_class_name"),
                "best_class_score": _optional_float(
                    _row_value(row, "best_class_score")
                ),
                "eclipsing_binary_period_days": (
                    1.0 / frequency if frequency and frequency > 0 else None
                ),
                "nss_solution_type": _row_value(row, "nss_solution_type"),
                "nss_period_days": _optional_float(
                    _row_value(row, "nss_period_days")
                ),
            }
        )
    return {
        "source": "Gaia DR3 variability and non-single-star tables",
        "source_url": GAIA_ARCHIVE_URL,
        "status": "completed",
        "source_id": int(source_id),
        "match": bool(rows),
        "rows": rows,
    }


def _safe_source_query(name: str, query) -> dict[str, object]:
    try:
        return query()
    except Exception as error:
        return {
            "source": name,
            "status": "error",
            "error": str(error),
        }


def query_authoritative_evidence(
    tic_id: int,
    *,
    gaia_source_id: int | None,
    sectors: Iterable[int],
    candidate_period_days: float | None,
) -> dict[str, object]:
    """Collect independent, metadata-only known-object evidence."""

    sector_values = sorted({int(value) for value in sectors if int(value) > 0})
    tess_eb = _safe_source_query(
        "Villanova live TESS Eclipsing Binary Catalog",
        lambda: query_tess_eb_catalog(tic_id),
    )
    simbad = _safe_source_query(
        "SIMBAD",
        lambda: query_simbad_context(tic_id),
    )
    gaia = _safe_source_query(
        "Gaia DR3 variability and non-single-star tables",
        lambda: query_gaia_dr3_context(gaia_source_id),
    )
    if sector_values and candidate_period_days and candidate_period_days > 0:
        tce = _safe_source_query(
            "MAST TESS threshold-crossing-event statistics",
            lambda: check_tces(tic_id, sector_values, candidate_period_days),
        )
        if tce.get("status") != "error":
            tce = {
                "source": "MAST TESS threshold-crossing-event statistics",
                "status": (
                    "completed"
                    if int(tce.get("catalogs_checked", 0)) > 0
                    else "not_available"
                ),
                **tce,
            }
    else:
        tce = {
            "source": "MAST TESS threshold-crossing-event statistics",
            "status": "not_applicable",
            "reason": "A candidate period and searched sector are required.",
        }
    return {
        "tess_eclipsing_binary_catalog": tess_eb,
        "simbad": simbad,
        "gaia_dr3": gaia,
        "tess_tce": tce,
    }


def _period_relation(
    found: float | None,
    reference: float | None,
    tolerance: float = 0.01,
) -> dict[str, object] | None:
    if not found or not reference or found <= 0 or reference <= 0:
        return None
    relations = (
        ("exact", 1.0),
        ("half-period alias", 0.5),
        ("double-period alias", 2.0),
        ("one-third-period alias", 1.0 / 3.0),
        ("triple-period alias", 3.0),
    )
    label, error = min(
        (
            (
                label,
                abs(found - reference * factor) / (reference * factor),
            )
            for label, factor in relations
        ),
        key=lambda item: item[1],
    )
    return {
        "relation": label if error <= tolerance else "none",
        "fractional_error": error,
        "matches": error <= tolerance,
    }


def _catalog_periods(catalog: Mapping[str, object]) -> list[float]:
    periods: list[float] = []
    for key in ("tois", "confirmed_planets"):
        rows = catalog.get(key, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            value = _optional_float(row.get("pl_orbper"))
            if value and value > 0:
                periods.append(value)
    return periods


def classify_context_evidence(
    *,
    candidate_period_days: float | None,
    nasa_catalog: Mapping[str, object],
    evidence: Mapping[str, object],
    neighbors: Mapping[str, object],
) -> dict[str, object]:
    """Route a lead conservatively using exact host and period evidence."""

    reasons: list[str] = []
    matches: list[dict[str, object]] = []
    source_states: dict[str, str] = {}
    for key, value in evidence.items():
        if isinstance(value, Mapping):
            source_states[key] = str(value.get("status") or "completed")

    for period in _catalog_periods(nasa_catalog):
        relation = _period_relation(candidate_period_days, period)
        if relation and relation["matches"]:
            matches.append(
                {
                    "source": "NASA Exoplanet Archive",
                    "object_class": "known_planet_or_toi",
                    "reference_period_days": period,
                    **relation,
                }
            )

    tce = evidence.get("tess_tce", {})
    tce = tce if isinstance(tce, Mapping) else {}
    tce_matches = tce.get("matching_tces", [])
    if isinstance(tce_matches, list) and tce_matches:
        for row in tce_matches:
            if isinstance(row, Mapping):
                matches.append(
                    {
                        "source": "MAST TESS TCE statistics",
                        "object_class": "known_tce",
                        "reference_period_days": row.get("period_days"),
                        "label": row.get("tce_id"),
                        "relation": row.get("relation_to_candidate"),
                        "fractional_error": row.get(
                            "fractional_period_error"
                        ),
                        "matches": True,
                    }
                )

    eb_host = False
    eb_period_match = False
    tess_eb = evidence.get("tess_eclipsing_binary_catalog", {})
    tess_eb = tess_eb if isinstance(tess_eb, Mapping) else {}
    eb_rows = tess_eb.get("rows", [])
    if isinstance(eb_rows, list) and eb_rows:
        eb_host = True
        for row in eb_rows:
            if not isinstance(row, Mapping):
                continue
            period = _optional_float(row.get("period_days"))
            relation = _period_relation(candidate_period_days, period)
            if relation and relation["matches"]:
                eb_period_match = True
                matches.append(
                    {
                        "source": "Villanova TESS Eclipsing Binary Catalog",
                        "object_class": "known_eclipsing_binary",
                        "reference_period_days": period,
                        **relation,
                    }
                )

    gaia = evidence.get("gaia_dr3", {})
    gaia = gaia if isinstance(gaia, Mapping) else {}
    gaia_rows = gaia.get("rows", [])
    gaia_variable = False
    if isinstance(gaia_rows, list):
        for row in gaia_rows:
            if not isinstance(row, Mapping):
                continue
            class_name = str(row.get("best_class_name") or "").upper()
            gaia_variable = gaia_variable or bool(class_name)
            is_eb = bool(row.get("in_vari_eclipsing_binary")) or any(
                marker in class_name
                for marker in ("ECL", "ECLIPS", "ELL", "BINARY")
            )
            nss_type = str(row.get("nss_solution_type") or "")
            if nss_type:
                gaia_variable = True
            if is_eb or "Eclipsing" in nss_type:
                eb_host = True
                periods = (
                    _optional_float(row.get("eclipsing_binary_period_days")),
                    _optional_float(row.get("nss_period_days")),
                )
                for period in periods:
                    relation = _period_relation(candidate_period_days, period)
                    if relation and relation["matches"]:
                        eb_period_match = True
                        matches.append(
                            {
                                "source": "Gaia DR3",
                                "object_class": "known_eclipsing_binary",
                                "reference_period_days": period,
                                **relation,
                            }
                        )

    simbad = evidence.get("simbad", {})
    simbad = simbad if isinstance(simbad, Mapping) else {}
    simbad_types = {
        str(value).upper()
        for value in simbad.get("object_types", [])
        if str(value).strip()
    }
    eb_markers = {"EB*", "EB?", "AL*", "BL*", "WU*", "SB*", "SB?"}
    if simbad_types.intersection(eb_markers):
        eb_host = True
    variable_host = gaia_variable or any(
        marker in value
        for value in simbad_types
        for marker in ("V*", "VAR", "PULS", "ROT")
    )

    planet_match = next(
        (
            row
            for row in matches
            if row.get("object_class") == "known_planet_or_toi"
        ),
        None,
    )
    tce_match = next(
        (
            row
            for row in matches
            if row.get("object_class") == "known_tce"
        ),
        None,
    )
    if planet_match:
        disposition = "known_planet_rediscovery"
        lane = "known_planet_validation"
        priority = 10
        reasons.append("The recovered period matches a NASA TOI or confirmed planet.")
    elif eb_period_match:
        disposition = "known_eb_rediscovery"
        lane = "stellar_eclipse_or_etv_followup"
        priority = 20
        reasons.append(
            "The target and recovered period match an authoritative eclipsing-binary record."
        )
        if tce_match:
            reasons.append(
                "The same signal is also present in the official TESS TCE statistics."
            )
    elif tce_match:
        disposition = "known_tce_rediscovery"
        lane = "known_signal_validation"
        priority = 15
        reasons.append("The recovered period matches an official TESS TCE.")
    elif eb_host:
        disposition = "known_eb_host_residual_review"
        lane = "circumbinary_or_residual_signal_review"
        priority = 85
        reasons.append(
            "The host is a known binary, but the recovered period is not yet tied to its catalogued eclipse period."
        )
    elif variable_host:
        disposition = "known_variable_star_review"
        lane = "stellar_variability_review"
        priority = 60
        reasons.append(
            "Gaia or SIMBAD identifies stellar variability that can mimic a transit signal."
        )
    elif str(neighbors.get("crowding_risk") or "") in {"high", "moderate"}:
        disposition = "crowding_contamination_review"
        lane = "pixel_localization"
        priority = 75
        reasons.append(
            "Nearby-source metadata requires pixel localization before this signal can advance."
        )
    elif any(value == "error" for value in source_states.values()):
        disposition = "context_incomplete"
        lane = "retry_metadata_sources"
        priority = 95
        reasons.append("At least one authoritative metadata source failed and must be retried.")
    elif any(value == "not_available" for value in source_states.values()):
        disposition = "catalog_coverage_gap"
        lane = "independent_data_review"
        priority = 80
        reasons.append(
            "One or more applicable public catalogs do not yet cover the searched sector."
        )
    else:
        disposition = "unresolved_transit_like_signal"
        lane = "independent_photometry_and_pixel_vetting"
        priority = 90
        reasons.append(
            "No checked catalog explains the signal, but catalog absence is not evidence of a planet."
        )
    return {
        "disposition": disposition,
        "followup_lane": lane,
        "followup_priority": priority,
        "promotion_eligible": False,
        "planet_candidate": False,
        "known_binary_host": eb_host,
        "exact_period_matches": matches,
        "source_states": source_states,
        "reasons": reasons,
        "required_before_promotion": [
            "difference-image or target-pixel localization",
            "independent TESS reduction",
            "repeat-sector or independent-epoch recovery where available",
            "human review of eclipse, variability, and contamination alternatives",
        ],
    }
