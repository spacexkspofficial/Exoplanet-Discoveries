"""The identity graph: which star a signal is about, and how sure we are.

MASTER_PLAN.md section 4.1. Three commitments, each of which exists because
the alternative silently corrupts a verdict:

* **The canonical node is a (TIC, Gaia DR3) pair**, resolved once, stored with
  provenance. Everything else -- 2MASS, TOI, CTOI, common names, EB catalog
  entries -- is an *edge* carrying its own source, confidence, and retrieval
  time, so "this is TOI-xxxx" is a claim with an owner rather than a fact.
* **Positional matching propagates proper motion to the catalog's epoch.** At
  TESS magnitudes the PM error is negligible, but a 1"/yr M dwarf has moved
  more than a TESS pixel between the 2MASS mean epoch and Gaia DR3's J2016.0.
  Matching a high-PM star against an old catalog at its modern position finds
  the wrong object, or nothing, and both answers look confident. High-PM M
  dwarfs are the survey's primary lane, so this is not a corner case.
* **Ambiguity is preserved, never resolved away.** When more than one
  plausible counterpart sits inside a TESS pixel, all of them are stored,
  ranked, and handed to the pixel-vetting stage. A forced unique identity is
  how a neighbour's eclipse becomes "a planet on the target".

Nothing here decides science. It answers "which objects could this flux belong
to, and on what basis", and records that answer so a later re-vetting against
a newer catalog generation produces a *new* edge instead of quietly changing
the meaning of an old one.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .config import CURRENT_IDENTITY, IdentityConfig, match_radius_arcsec
from .snapshots import angular_separation_deg

# Milliarcseconds per degree: proper motions are catalogued in mas/yr and
# positions in degrees.
_MAS_PER_DEGREE = 3_600_000.0
# Declinations this close to a pole make the cos(dec) division in the
# right-ascension update numerically meaningless; the position is carried
# through unchanged in right ascension and the caller is told why.
_POLE_GUARD_DEG = 89.999

RESOLUTION_UNIQUE = "unique"
RESOLUTION_AMBIGUOUS = "ambiguous"
RESOLUTION_UNRESOLVED = "unresolved"

MATCH_BASIS_CATALOG = "catalog_crossmatch"
MATCH_BASIS_POSITION = "position_pm_propagated"
MATCH_BASIS_IDENTIFIER = "identifier_alias"


class IdentityError(RuntimeError):
    """An identity could not be resolved without inventing a fact."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True, slots=True)
class SkyPosition:
    """A position with the epoch it is quoted at, and optional motion."""

    ra_deg: float
    dec_deg: float
    epoch_jyear: float
    pmra_mas_yr: float | None = None
    pmdec_mas_yr: float | None = None

    def has_motion(self) -> bool:
        return self.pmra_mas_yr is not None and self.pmdec_mas_yr is not None


def propagate_proper_motion(
    position: SkyPosition, to_epoch_jyear: float
) -> tuple[SkyPosition, str]:
    """Move a position to another epoch, reporting what was actually done.

    Returns the propagated position and a basis string, because "we could not
    propagate" must be visible in the evidence rather than being indistinguish-
    able from "no motion was needed". ``pmra`` is the catalogued mu_alpha*,
    which already carries the cos(dec) factor, so recovering the coordinate
    increment divides it back out.
    """

    delta_years = float(to_epoch_jyear) - float(position.epoch_jyear)
    if not position.has_motion():
        return position, "no_proper_motion_available"
    if delta_years == 0.0:
        return position, "same_epoch"
    dec = float(position.dec_deg)
    if abs(dec) >= _POLE_GUARD_DEG:
        moved = SkyPosition(
            ra_deg=position.ra_deg,
            dec_deg=dec + float(position.pmdec_mas_yr) * delta_years / _MAS_PER_DEGREE,
            epoch_jyear=float(to_epoch_jyear),
            pmra_mas_yr=position.pmra_mas_yr,
            pmdec_mas_yr=position.pmdec_mas_yr,
        )
        return moved, "declination_only_near_pole"

    import math

    dec_new = dec + float(position.pmdec_mas_yr) * delta_years / _MAS_PER_DEGREE
    ra_new = float(position.ra_deg) + (
        float(position.pmra_mas_yr)
        * delta_years
        / _MAS_PER_DEGREE
        / math.cos(math.radians(dec))
    )
    moved = SkyPosition(
        ra_deg=ra_new % 360.0,
        dec_deg=dec_new,
        epoch_jyear=float(to_epoch_jyear),
        pmra_mas_yr=position.pmra_mas_yr,
        pmdec_mas_yr=position.pmdec_mas_yr,
    )
    return moved, "proper_motion_propagated"


@dataclass(frozen=True, slots=True)
class Counterpart:
    """One object that could be contributing flux to the target aperture."""

    identifier: str
    identifier_type: str
    ra_deg: float
    dec_deg: float
    separation_arcsec: float
    magnitude: float | None = None
    delta_mag: float | None = None
    plausible_host: bool = True
    rank: int = 1
    # *How* the two objects were associated (a position match, a catalog's own
    # cross-match, an identifier alias) is a different claim from *what was
    # done to the coordinates first*. Keeping them in one field made "matched
    # positionally" and "propagated to a common epoch" indistinguishable, and
    # the second is exactly the step a reader needs to audit for a high-proper-
    # motion star.
    match_basis: str = MATCH_BASIS_POSITION
    propagation: str = "same_epoch"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def rank_counterparts(
    target: SkyPosition,
    candidates: Sequence[dict[str, Any]],
    *,
    target_magnitude: float | None = None,
    catalog_epoch_jyear: float | None = None,
    identity: IdentityConfig | None = None,
    identifier_type: str = "gaia_dr3",
) -> list[Counterpart]:
    """Rank every candidate inside the match radius, keeping all of them.

    ``candidates`` are mappings with ``identifier``, ``ra_deg``, ``dec_deg``
    and optionally ``magnitude``, ``pmra_mas_yr``, ``pmdec_mas_yr`` and
    ``epoch_jyear``. Candidates are propagated to the target's epoch (not the
    other way round) so the comparison happens in one frame.

    A candidate fainter than ``max_neighbour_delta_mag`` is kept -- it is part
    of the scene and the pixel stage may still want it -- but marked as not a
    plausible host, because it cannot dilute to the observed depth.
    """

    identity = identity or CURRENT_IDENTITY
    radius = match_radius_arcsec(identity)
    matched: list[Counterpart] = []
    for candidate in candidates:
        try:
            ra = float(candidate["ra_deg"])
            dec = float(candidate["dec_deg"])
        except (KeyError, TypeError, ValueError):
            continue
        epoch = candidate.get("epoch_jyear", catalog_epoch_jyear)
        position = SkyPosition(
            ra_deg=ra,
            dec_deg=dec,
            epoch_jyear=float(epoch) if epoch is not None else target.epoch_jyear,
            pmra_mas_yr=_optional_float(candidate.get("pmra_mas_yr")),
            pmdec_mas_yr=_optional_float(candidate.get("pmdec_mas_yr")),
        )
        moved, basis = propagate_proper_motion(position, target.epoch_jyear)
        separation = (
            angular_separation_deg(
                target.ra_deg, target.dec_deg, moved.ra_deg, moved.dec_deg
            )
            * 3600.0
        )
        if separation > radius:
            continue
        magnitude = _optional_float(candidate.get("magnitude"))
        delta = (
            magnitude - target_magnitude
            if magnitude is not None and target_magnitude is not None
            else None
        )
        plausible = delta is None or delta <= identity.max_neighbour_delta_mag
        matched.append(
            Counterpart(
                identifier=str(candidate.get("identifier", "")),
                identifier_type=identifier_type,
                ra_deg=moved.ra_deg,
                dec_deg=moved.dec_deg,
                separation_arcsec=separation,
                magnitude=magnitude,
                delta_mag=delta,
                plausible_host=plausible,
                match_basis=MATCH_BASIS_POSITION,
                propagation=basis,
                notes=(
                    ""
                    if plausible
                    else "fainter than the dilution ceiling; scene member only"
                ),
            )
        )
    matched.sort(key=lambda item: (not item.plausible_host, item.separation_arcsec))
    return [
        Counterpart(**{**item.to_dict(), "rank": index})
        for index, item in enumerate(matched, start=1)
    ]


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result


@dataclass(frozen=True, slots=True)
class IdentityNode:
    """The canonical node, plus how confident its resolution is."""

    tic_id: int
    gaia_source_id: int | None
    position: SkyPosition | None
    resolution: str
    candidate_count: int
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tic_id": self.tic_id,
            "gaia_source_id": self.gaia_source_id,
            "position": asdict(self.position) if self.position else None,
            "resolution": self.resolution,
            "candidate_count": self.candidate_count,
            "provenance": self.provenance,
        }


def resolve_node(
    tic_id: int,
    target: SkyPosition,
    counterparts: Sequence[Counterpart],
    *,
    identity: IdentityConfig | None = None,
    source: str = "gaia_dr3",
) -> IdentityNode:
    """Choose the canonical Gaia counterpart, or refuse to choose one.

    A single plausible counterpart resolves the node. Several plausible
    counterparts inside one pixel leave it ``ambiguous``: the nearest is
    recorded as the working identity so downstream code has something to key
    on, but the resolution string says it is provisional and every alternative
    survives as a ranked edge.
    """

    identity = identity or CURRENT_IDENTITY
    plausible = [item for item in counterparts if item.plausible_host]
    if not plausible:
        return IdentityNode(
            tic_id=int(tic_id),
            gaia_source_id=None,
            position=target,
            resolution=RESOLUTION_UNRESOLVED,
            candidate_count=len(counterparts),
            provenance={
                "source": source,
                "match_radius_arcsec": match_radius_arcsec(identity),
                "reason": (
                    "no counterpart inside the match radius bright enough to "
                    "produce the observed depth"
                ),
            },
        )
    best = plausible[0]
    resolution = (
        RESOLUTION_UNIQUE if len(plausible) == 1 else RESOLUTION_AMBIGUOUS
    )
    identifier: int | None
    try:
        identifier = int(best.identifier)
    except (TypeError, ValueError):
        identifier = None
    return IdentityNode(
        tic_id=int(tic_id),
        gaia_source_id=identifier,
        position=target,
        resolution=resolution,
        candidate_count=len(counterparts),
        provenance={
            "source": source,
            "match_radius_arcsec": match_radius_arcsec(identity),
            "match_basis": best.match_basis,
            "separation_arcsec": best.separation_arcsec,
            "plausible_counterparts": len(plausible),
            "alternatives": [item.to_dict() for item in plausible[1:]],
        },
    )


def upsert_node(conn: sqlite3.Connection, node: IdentityNode) -> None:
    """Store or refresh a canonical node."""

    position = node.position
    conn.execute(
        "INSERT INTO identity_node "
        "(tic_id, gaia_source_id, ra_deg, dec_deg, pmra_mas_yr, pmdec_mas_yr, "
        "epoch_jyear, resolution, candidate_count, provenance, resolved_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(tic_id) DO UPDATE SET "
        "gaia_source_id=excluded.gaia_source_id, ra_deg=excluded.ra_deg, "
        "dec_deg=excluded.dec_deg, pmra_mas_yr=excluded.pmra_mas_yr, "
        "pmdec_mas_yr=excluded.pmdec_mas_yr, epoch_jyear=excluded.epoch_jyear, "
        "resolution=excluded.resolution, "
        "candidate_count=excluded.candidate_count, "
        "provenance=excluded.provenance, resolved_at_utc=excluded.resolved_at_utc",
        (
            int(node.tic_id),
            node.gaia_source_id,
            position.ra_deg if position else None,
            position.dec_deg if position else None,
            position.pmra_mas_yr if position else None,
            position.pmdec_mas_yr if position else None,
            position.epoch_jyear if position else None,
            node.resolution,
            int(node.candidate_count),
            json.dumps(node.provenance, sort_keys=True, separators=(",", ":")),
            _utc_now(),
        ),
    )


def add_edge(
    conn: sqlite3.Connection,
    *,
    tic_id: int,
    identifier_type: str,
    identifier: str,
    source: str,
    confidence: float,
    match_basis: str,
    separation_arcsec: float | None = None,
    rank: int = 1,
    snapshot_hash: str | None = None,
) -> int | None:
    """Append one identifier claim. Idempotent on (tic, type, identifier, source).

    Returns the new row id, or ``None`` when the identical claim already
    existed. Re-running a resolution against the same snapshot therefore costs
    nothing and changes nothing; running it against a *new* snapshot writes a
    new row, because the snapshot hash is part of what is stored.
    """

    if not 0.0 <= float(confidence) <= 1.0:
        raise IdentityError("Edge confidence must lie in [0, 1].")
    cursor = conn.execute(
        "INSERT OR IGNORE INTO identity_edge "
        "(tic_id, identifier_type, identifier, source, confidence, match_basis, "
        "separation_arcsec, rank, snapshot_hash, retrieved_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(tic_id),
            identifier_type,
            str(identifier),
            source,
            float(confidence),
            match_basis,
            separation_arcsec,
            int(rank),
            snapshot_hash,
            _utc_now(),
        ),
    )
    return cursor.lastrowid if cursor.rowcount else None


def record_counterparts(
    conn: sqlite3.Connection,
    tic_id: int,
    counterparts: Iterable[Counterpart],
    *,
    source: str,
    snapshot_hash: str | None = None,
) -> int:
    """Store every ranked counterpart as an edge; returns how many were new."""

    written = 0
    for counterpart in counterparts:
        if not counterpart.identifier:
            continue
        # Confidence falls off with separation inside the pixel: a counterpart
        # on top of the target is the identity, one at the pixel edge is a
        # candidate. This is an ordering, not a probability, and is recorded as
        # such so nothing downstream multiplies it into a likelihood.
        radius = match_radius_arcsec()
        confidence = max(0.0, min(1.0, 1.0 - counterpart.separation_arcsec / radius))
        if add_edge(
            conn,
            tic_id=tic_id,
            identifier_type=counterpart.identifier_type,
            identifier=counterpart.identifier,
            source=source,
            confidence=confidence,
            match_basis=counterpart.match_basis,
            separation_arcsec=counterpart.separation_arcsec,
            rank=counterpart.rank,
            snapshot_hash=snapshot_hash,
        ) is not None:
            written += 1
    return written


def node_for(conn: sqlite3.Connection, tic_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM identity_node WHERE tic_id = ?", (int(tic_id),)
    ).fetchone()
    if row is None:
        return None
    node = dict(row)
    node["provenance"] = json.loads(node["provenance"])
    return node


def edges_for(
    conn: sqlite3.Connection, tic_id: int, *, identifier_type: str | None = None
) -> list[dict[str, Any]]:
    if identifier_type is None:
        rows = conn.execute(
            "SELECT * FROM identity_edge WHERE tic_id = ? "
            "ORDER BY identifier_type, rank, edge_id",
            (int(tic_id),),
        )
    else:
        rows = conn.execute(
            "SELECT * FROM identity_edge WHERE tic_id = ? AND identifier_type = ? "
            "ORDER BY rank, edge_id",
            (int(tic_id), identifier_type),
        )
    return [dict(row) for row in rows]


def ambiguous_nodes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every node the graph refused to resolve uniquely."""

    return [
        dict(row)
        for row in conn.execute(
            "SELECT tic_id, gaia_source_id, resolution, candidate_count "
            "FROM identity_node WHERE resolution != ? ORDER BY tic_id",
            (RESOLUTION_UNIQUE,),
        )
    ]
