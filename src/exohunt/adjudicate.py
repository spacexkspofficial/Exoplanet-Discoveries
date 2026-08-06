"""T5 catalog adjudication: does a catalog already explain *this* signal?

MASTER_PLAN.md section 4.3. Three rules, and the reason each exists:

* **Ephemeris matching requires period AND epoch.** The rule this replaces
  (``evidence.py`` ``_period_relation``) accepts a 1% period-ratio match with
  no epoch test. Eclipsing binaries pile up at 0.5-3 day periods, where
  unrelated signals agree on period constantly, so a period-only rule
  manufactures "this is already known" verdicts. A false *known* discards a
  genuine new signal, and unlike a false *novel* nobody ever looks at it again.
* **Host match is not signal match.** A catalogued planet host with a *new*
  period is a new-signal lane, not a rediscovery; a catalogued eclipsing-binary
  host with an unrelated residual period is the residual lane that already
  exists. Only a signal-level match kills.
* **Absence has two meanings.** ``no_match`` (the source covers this star and
  has nothing) and ``catalog_coverage_gap`` (the source was never consulted, or
  its extract never included this star) are different claims, and the snapshot
  layer is what makes the difference knowable.

This module decides *relations*. It deliberately does not write status rows:
several of the natural conclusions here ("this is a known planet") live at the
``human_outcome`` stage in the status registry, and section 4.7 forbids an
automated writer from emitting those. Where no automated status exists for a
conclusion, the adjudication says so in ``blocked_reason`` rather than
inventing one or silently downgrading to a status that means something else.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .config import (
    CURRENT_CONFIG,
    CURRENT_EPHEMERIS_MATCH,
    EphemerisMatchConfig,
    ScienceConfig,
)
from .statuses import STATUS_REGISTRY

# TESS times are Barycentric TESS Julian Date: BJD_TDB - 2457000. Catalog
# ephemerides are published in full BJD, and adding a transit epoch in the
# wrong time base produces a phase error of tens of thousands of periods that
# still lands somewhere in [0, P) -- an answer that looks perfectly reasonable.
BTJD_OFFSET_DAYS = 2_457_000.0
# Any epoch larger than this is a full Julian Date rather than a BTJD.
_JULIAN_DATE_FLOOR = 2_000_000.0

RELATION_EXACT = "exact"
RELATION_PERIOD_ONLY = "period_only"
RELATION_NONE = "none"

MATCH_SIGNAL = "signal_match"
MATCH_PERIOD_ONLY = "period_only_match"
MATCH_HOST = "host_match"
MATCH_NONE = "no_match"
MATCH_COVERAGE_GAP = "catalog_coverage_gap"

EPOCH_AGREES = "agrees"
EPOCH_DISAGREES = "disagrees"
EPOCH_NO_CATALOG_EPOCH = "not_evaluable_no_catalog_epoch"
EPOCH_NO_CANDIDATE_EPOCH = "not_evaluable_no_candidate_epoch"
EPOCH_UNCERTAINTY_TOO_LARGE = "not_evaluable_phase_uncertainty_exceeds_window"

_ALIAS_NAMES = {
    1.0: RELATION_EXACT,
    0.5: "half_period_alias",
    2.0: "double_period_alias",
    1.0 / 3.0: "one_third_period_alias",
    3.0: "triple_period_alias",
    2.0 / 3.0: "two_thirds_period_alias",
    1.5: "three_halves_period_alias",
}


def _alias_name(factor: float) -> str:
    for known, name in _ALIAS_NAMES.items():
        if abs(factor - known) < 1e-9:
            return name
    return f"period_ratio_{factor:g}"


def to_btjd(epoch: float | None) -> float | None:
    """Normalize a catalog epoch into the light curve's time base."""

    if epoch is None:
        return None
    value = float(epoch)
    return value - BTJD_OFFSET_DAYS if value > _JULIAN_DATE_FLOOR else value


@dataclass(frozen=True, slots=True)
class Candidate:
    """The signal being adjudicated, in the light curve's own time base."""

    tic_id: int
    period_days: float
    duration_hours: float
    epoch_btjd: float | None = None

    def duration_days(self) -> float:
        return float(self.duration_hours) / 24.0


@dataclass(frozen=True, slots=True)
class CatalogEphemeris:
    """One catalogued signal, with the snapshot generation it came from."""

    source: str
    identifier: str
    object_class: str
    snapshot_hash: str
    period_days: float | None = None
    epoch_bjd: float | None = None
    duration_hours: float | None = None
    disposition: str | None = None
    period_uncertainty_days: float | None = None
    epoch_uncertainty_days: float | None = None
    host_only: bool = False


@dataclass(frozen=True, slots=True)
class Relation:
    """What one catalog row says about one candidate, and how strongly."""

    source: str
    identifier: str
    object_class: str
    snapshot_hash: str
    relation: str
    alias_factor: float | None
    fractional_period_error: float | None
    epoch_verdict: str
    phase_offset_days: float | None
    phase_tolerance_days: float | None
    match_level: str
    disposition: str | None
    kills: bool
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Adjudication:
    """Every relation found, plus the conservative routing they imply."""

    tic_id: int
    relations: list[Relation] = field(default_factory=list)
    consulted_sources: list[str] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)
    recommended_status: str | None = None
    blocked_reason: str | None = None
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tic_id": self.tic_id,
            "relations": [item.to_dict() for item in self.relations],
            "consulted_sources": list(self.consulted_sources),
            "coverage_gaps": list(self.coverage_gaps),
            "recommended_status": self.recommended_status,
            "blocked_reason": self.blocked_reason,
            "conflicts": list(self.conflicts),
        }

    def killing_relations(self) -> list[Relation]:
        return [item for item in self.relations if item.kills]


def period_relation(
    candidate_period_days: float,
    catalog_period_days: float,
    *,
    matching: EphemerisMatchConfig | None = None,
    science: ScienceConfig | None = None,
) -> tuple[str, float | None, float | None]:
    """Best alias relation between two periods, over the configured ladder.

    The ladder is the *search* config's alias ratios, not a second list: a
    relation this pipeline cannot produce in its own search is not a relation
    it should claim to recognise in a catalog.
    """

    matching = matching or CURRENT_EPHEMERIS_MATCH
    science = science or CURRENT_CONFIG
    if not candidate_period_days or not catalog_period_days:
        return RELATION_NONE, None, None
    if candidate_period_days <= 0 or catalog_period_days <= 0:
        return RELATION_NONE, None, None

    best_factor: float | None = None
    best_error: float | None = None
    for factor in science.search.alias_ratios:
        expected = catalog_period_days * factor
        error = abs(candidate_period_days - expected) / expected
        if best_error is None or error < best_error:
            best_error = error
            best_factor = factor
    if best_error is None or best_error > matching.period_tolerance_fraction:
        return RELATION_NONE, best_factor, best_error
    return _alias_name(float(best_factor)), best_factor, best_error


def epoch_agreement(
    candidate: Candidate,
    entry: CatalogEphemeris,
    *,
    alias_factor: float,
    matching: EphemerisMatchConfig | None = None,
) -> tuple[str, float | None, float | None]:
    """Do the two ephemerides put transits at the same phase?

    The catalog epoch is projected forward to the candidate's epoch through
    however many cycles separate them, and its quoted period uncertainty is
    propagated over those same cycles. When that propagated uncertainty grows
    past a quarter of a period the catalog no longer predicts *where* the
    transit is, and the honest answer is that the phase test could not be run
    -- not that it failed.
    """

    matching = matching or CURRENT_EPHEMERIS_MATCH
    if candidate.epoch_btjd is None:
        return EPOCH_NO_CANDIDATE_EPOCH, None, None
    catalog_epoch = to_btjd(entry.epoch_bjd)
    if catalog_epoch is None or not entry.period_days:
        return EPOCH_NO_CATALOG_EPOCH, None, None

    # Compare in the catalog's own period: an alias relation means the
    # candidate's events are a subset or superset of the catalog's, and every
    # candidate event should still land on a catalogued transit.
    period = float(entry.period_days)
    elapsed = float(candidate.epoch_btjd) - catalog_epoch
    cycles = elapsed / period
    offset = abs((elapsed + period / 2.0) % period - period / 2.0)

    period_error = entry.period_uncertainty_days
    if period_error is None:
        period_error = period * matching.assumed_period_uncertainty_fraction
    epoch_error = entry.epoch_uncertainty_days or 0.0
    propagated = abs(float(period_error) * cycles) + abs(float(epoch_error))
    if propagated > matching.max_phase_uncertainty_periods * period:
        return EPOCH_UNCERTAINTY_TOO_LARGE, offset, None

    tolerance = max(
        candidate.duration_days() * matching.phase_tolerance_duration_fraction,
        propagated,
    )
    if entry.duration_hours:
        tolerance = max(
            tolerance,
            (float(entry.duration_hours) / 24.0)
            * matching.phase_tolerance_duration_fraction,
        )
    verdict = EPOCH_AGREES if offset <= tolerance else EPOCH_DISAGREES
    return verdict, offset, tolerance


def relate(
    candidate: Candidate,
    entry: CatalogEphemeris,
    *,
    matching: EphemerisMatchConfig | None = None,
    science: ScienceConfig | None = None,
) -> Relation:
    """Adjudicate one candidate against one catalog row."""

    matching = matching or CURRENT_EPHEMERIS_MATCH
    if entry.host_only or not entry.period_days:
        return Relation(
            source=entry.source,
            identifier=entry.identifier,
            object_class=entry.object_class,
            snapshot_hash=entry.snapshot_hash,
            relation=RELATION_NONE,
            alias_factor=None,
            fractional_period_error=None,
            epoch_verdict=EPOCH_NO_CATALOG_EPOCH,
            phase_offset_days=None,
            phase_tolerance_days=None,
            match_level=MATCH_HOST,
            disposition=entry.disposition,
            kills=False,
            note="catalogued host with no comparable ephemeris",
        )

    name, factor, error = period_relation(
        candidate.period_days,
        float(entry.period_days),
        matching=matching,
        science=science,
    )
    if name == RELATION_NONE:
        return Relation(
            source=entry.source,
            identifier=entry.identifier,
            object_class=entry.object_class,
            snapshot_hash=entry.snapshot_hash,
            relation=RELATION_NONE,
            alias_factor=factor,
            fractional_period_error=error,
            epoch_verdict=EPOCH_NO_CANDIDATE_EPOCH,
            phase_offset_days=None,
            phase_tolerance_days=None,
            match_level=MATCH_HOST,
            disposition=entry.disposition,
            kills=False,
            note="same star, unrelated period",
        )

    verdict, offset, tolerance = epoch_agreement(
        candidate, entry, alias_factor=float(factor or 1.0), matching=matching
    )
    if verdict == EPOCH_AGREES:
        match_level = MATCH_SIGNAL
        kills = True
        note = "period and epoch both agree"
    elif verdict == EPOCH_DISAGREES:
        match_level = MATCH_HOST
        kills = False
        note = (
            "period agrees but the catalogued transits fall at a different "
            "phase; this is a different signal on a catalogued star"
        )
    else:
        match_level = MATCH_PERIOD_ONLY
        kills = False
        note = f"period agrees; epoch test not evaluable ({verdict})"
    return Relation(
        source=entry.source,
        identifier=entry.identifier,
        object_class=entry.object_class,
        snapshot_hash=entry.snapshot_hash,
        relation=name,
        alias_factor=factor,
        fractional_period_error=error,
        epoch_verdict=verdict,
        phase_offset_days=offset,
        phase_tolerance_days=tolerance,
        match_level=match_level,
        disposition=entry.disposition,
        kills=kills,
        note=note,
    )


# Conclusions this module may route to automatically. Everything else -- most
# importantly "this is a known planet" -- lives at the human_outcome stage and
# section 4.7 forbids an automated writer from emitting it.
_AUTOMATED_STATUS = {
    "eclipsing_binary_signal": "known_eb_rediscovery",
    "eclipsing_binary_host": "known_eb_host_residual_review",
    "coverage_gap": "catalog_coverage_gap",
    "unresolved": "unresolved_transit_like_signal",
}


def adjudicate(
    candidate: Candidate,
    entries: Sequence[CatalogEphemeris],
    *,
    consulted_sources: Iterable[str],
    coverage_gaps: Iterable[str] = (),
    matching: EphemerisMatchConfig | None = None,
    science: ScienceConfig | None = None,
) -> Adjudication:
    """Adjudicate a candidate against every catalog row known for its star."""

    relations = [
        relate(candidate, entry, matching=matching, science=science)
        for entry in entries
    ]
    consulted = sorted(set(consulted_sources))
    gaps = sorted(set(coverage_gaps))

    killing = [item for item in relations if item.kills]
    eb_signal = [
        item for item in killing if item.object_class == "eclipsing_binary"
    ]
    planet_signal = [
        item
        for item in killing
        if item.object_class in {"confirmed_planet", "toi"}
    ]
    eb_host = [
        item
        for item in relations
        if item.object_class == "eclipsing_binary"
        and item.match_level in {MATCH_HOST, MATCH_PERIOD_ONLY}
    ]

    conflicts = _conflicts(relations)
    status: str | None = None
    blocked: str | None = None

    if planet_signal:
        # The registry stages every "known planet / TOI / TCE rediscovery"
        # slug at human_outcome, so there is no automated status to write.
        # Saying so is the honest outcome; picking a nearby catalog-context
        # slug would file a planet rediscovery under something else.
        blocked = (
            "signal matches a catalogued planet or TOI, but every rediscovery "
            "status in the registry is human-stage; queued for review instead"
        )
    elif eb_signal:
        status = _AUTOMATED_STATUS["eclipsing_binary_signal"]
    elif eb_host:
        status = _AUTOMATED_STATUS["eclipsing_binary_host"]
    elif gaps and not relations:
        status = _AUTOMATED_STATUS["coverage_gap"]
    elif not relations and consulted:
        status = _AUTOMATED_STATUS["unresolved"]
    elif relations and not killing:
        status = _AUTOMATED_STATUS["unresolved"]

    if status is not None:
        _assert_automated_status(status)
    return Adjudication(
        tic_id=candidate.tic_id,
        relations=relations,
        consulted_sources=consulted,
        coverage_gaps=gaps,
        recommended_status=status,
        blocked_reason=blocked,
        conflicts=conflicts,
    )


def _conflicts(relations: Sequence[Relation]) -> list[dict[str, Any]]:
    """Disagreements never auto-resolve; they surface for a human.

    Section 4.3: a TOI saying PC while a Gaia NSS orbit says SB1 at the same
    period is exactly the case where an automatic winner would be wrong.
    """

    signal = [item for item in relations if item.match_level == MATCH_SIGNAL]
    classes = {item.object_class for item in signal}
    if len(classes) <= 1:
        return []
    return [
        {
            "kind": "disagreeing_object_classes",
            "object_classes": sorted(classes),
            "sources": sorted({item.source for item in signal}),
            "detail": (
                "more than one class of catalogued object claims this exact "
                "ephemeris; the conservative lane applies and the conflict is "
                "preserved for review"
            ),
        }
    ]


def _assert_automated_status(status: str) -> None:
    definition = STATUS_REGISTRY.get(status)
    if definition is None:
        raise ValueError(f"{status!r} is not in the status registry.")
    stage = getattr(definition, "evidence_stage", None)
    name = getattr(stage, "name", str(stage))
    if name == "HUMAN_OUTCOME":
        raise ValueError(
            f"{status!r} is a human-stage status; MASTER_PLAN section 4.7 "
            "forbids an automated writer from emitting it."
        )


def catalog_entries_from_snapshot_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    source: str,
    object_class: str,
    snapshot_hash: str,
    period_key: str,
    epoch_key: str,
    identifier_key: str,
    duration_key: str | None = None,
    disposition_key: str | None = None,
    period_error_keys: tuple[str, str] | None = None,
    epoch_error_keys: tuple[str, str] | None = None,
    duration_in_hours: bool = True,
) -> list[CatalogEphemeris]:
    """Adapt raw snapshot rows into adjudicable ephemerides.

    Uncertainty columns come in asymmetric pairs (``...err1``/``...err2``); the
    larger magnitude is used, because a mask or a phase tolerance built from
    the smaller one is the optimistic choice and this layer does not get to be
    optimistic.
    """

    entries: list[CatalogEphemeris] = []
    for row in rows:
        period = _as_float(row.get(period_key))
        entries.append(
            CatalogEphemeris(
                source=source,
                identifier=str(row.get(identifier_key, "")).strip(),
                object_class=object_class,
                snapshot_hash=snapshot_hash,
                period_days=period,
                epoch_bjd=_as_float(row.get(epoch_key)),
                duration_hours=(
                    _duration_hours(row, duration_key, duration_in_hours)
                    if duration_key
                    else None
                ),
                disposition=(
                    str(row.get(disposition_key)).strip()
                    if disposition_key and row.get(disposition_key)
                    else None
                ),
                period_uncertainty_days=_worst_uncertainty(row, period_error_keys),
                epoch_uncertainty_days=_worst_uncertainty(row, epoch_error_keys),
                host_only=period is None,
            )
        )
    return entries


def _duration_hours(
    row: Mapping[str, Any], key: str, already_hours: bool
) -> float | None:
    value = _as_float(row.get(key))
    if value is None:
        return None
    return value if already_hours else value * 24.0


def _worst_uncertainty(
    row: Mapping[str, Any], keys: tuple[str, str] | None
) -> float | None:
    if not keys:
        return None
    values = [abs(v) for v in (_as_float(row.get(key)) for key in keys) if v is not None]
    return max(values) if values else None


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result
