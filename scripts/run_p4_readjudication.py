"""Re-adjudicate the standing backlog under the calibrated pipeline (P4).

MASTER_PLAN.md section 9, P4: "re-adjudicate the existing backlog under the
calibrated pipeline". Two independent passes, kept separate because they can
disagree and the disagreement is informative:

**T3 re-gate.** Most of the backlog was screened before P3 turned the
red-noise diagnostic into an enforced verdict (commit 13d129e) and before the
calibrated TLS promotion gate landed (36c935b). Those reports already carry
their red-noise-adjusted S/N, so re-applying the calibrated floor needs no
photometry -- it is a re-reading of evidence that was always there.

**T5 catalog adjudication.** Every backlog ephemeris is matched against the
snapshot generations using the section 4.3 rule (period *and* epoch), and the
relation is recorded with the snapshot hash it was adjudicated against.

Evidence rows are written **non-voting** by default. The projection policy for
stars carrying conclusions from more than one campaign is an open owner
decision (PROGRESS.md correction 38), and writing voting verdicts before that
is settled would bake one answer into 83,555 stars. ``--promote`` exists for
when it is settled.

    python scripts/run_p4_readjudication.py --out results/p4/readjudication_v1
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from exohunt import adjudicate, identity, ledger, snapshots  # noqa: E402
from exohunt.config import (  # noqa: E402
    CURRENT_CONFIG,
    CURRENT_EPHEMERIS_MATCH,
    CURRENT_IDENTITY,
    match_radius_arcsec,
    module_digest,
    vetting_signature,
)
from exohunt.paths import default_db_path  # noqa: E402

BACKLOG_LANES = (
    "automated_survivor",
    "single_event_lead",
    "known_eb_host_residual_review",
    "catalog_coverage_gap",
)

VETTING_MODULES = ("adjudicate.py", "identity.py", "snapshots.py")

# Evidence is idempotent on (tic_id, kind, source), which is what stops a
# re-import from duplicating history -- and would equally stop an *improved*
# run from being recorded at all, because the vetting signature digests the
# kernel modules and not this runner's own input policy. Bump this whenever
# the runner changes what it feeds the adjudicator, so a better answer lands
# as a new row beside the old one instead of being silently dropped.
#   v1: ephemeris read from the deciding evidence row only.
#   v2: falls back to any screening row, recovering 262 of 288 stars that v1
#       reported as having no ephemeris at all.
#   v3: counts a source as consulted when its snapshot exists, not when it
#       contributes ephemerides. gaia_dr3 supplies the neighbour scene and no
#       periods, so v2 filed it as an unfetched coverage gap and reported 995
#       stars as uncheckable against catalogs that had in fact been checked.
READJUDICATION_POLICY = "p4-readjudication-v3-consulted-is-fetched"


def _f(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result


def _tic(value: Any) -> int | None:
    text = str(value or "").replace("TIC", "").strip()
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _ephemeris_from(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Pull a usable ephemeris out of one evidence payload, or None."""

    result = payload.get("result")
    result = result if isinstance(result, dict) else {}
    period = _f(result.get("period_days"))
    duration = _f(result.get("duration_hours"))
    if not (period and duration):
        return None
    return {
        "period_days": period,
        "epoch_btjd": _f(result.get("transit_time")),
        "duration_hours": duration,
        "red_noise_adjusted_snr": _f(result.get("red_noise_adjusted_snr")),
        "depth_ppm": _f(result.get("depth_ppm")),
        "observed_transits": result.get("observed_transits"),
        "sectors": result.get("sectors"),
    }


def load_backlog(conn) -> list[dict[str, Any]]:
    """Pull every backlog star, with the best ephemeris its evidence holds.

    A star's *deciding* row is whichever conclusion currently wins the status
    projection, and for the catalog-context lanes that is a ``context`` record
    with no search result attached. The ephemeris is still in the ledger --
    it is in the ``screening`` row the same star already has -- so falling
    back to it recovers 262 of the 288 stars that a deciding-row-only read
    reports as unadjudicable. Nothing new is fetched; this is a different
    query over evidence that was always there.
    """

    placeholders = ",".join("?" for _ in BACKLOG_LANES)
    rows = conn.execute(
        "SELECT s.tic_id, s.status, e.payload, e.signature "
        "FROM star_state s JOIN evidence e "
        "  ON e.evidence_id = s.decided_by_evidence_id "
        f"WHERE s.status IN ({placeholders})",
        BACKLOG_LANES,
    ).fetchall()

    backlog: list[dict[str, Any]] = []
    for row in rows:
        ephemeris = _ephemeris_from(json.loads(row["payload"]))
        source = "deciding_evidence"
        if ephemeris is None:
            for other in conn.execute(
                "SELECT payload FROM evidence WHERE tic_id = ? "
                "ORDER BY evidence_id DESC",
                (row["tic_id"],),
            ):
                ephemeris = _ephemeris_from(json.loads(other["payload"]))
                if ephemeris is not None:
                    source = "recovered_from_screening_evidence"
                    break
        star = {
            "tic_id": row["tic_id"],
            "status": row["status"],
            "signature": row["signature"],
            "ephemeris_source": source if ephemeris else "none_in_ledger",
            "period_days": None,
            "epoch_btjd": None,
            "duration_hours": None,
            "red_noise_adjusted_snr": None,
            "depth_ppm": None,
            "observed_transits": None,
            "sectors": None,
        }
        if ephemeris is not None:
            star.update(ephemeris)
        star["has_ephemeris"] = bool(
            star["period_days"] and star["duration_hours"]
        )
        backlog.append(star)
    return backlog


class SourceSpec:
    """How to read one snapshot as adjudicable ephemerides.

    Column names are given as preference lists rather than single strings:
    the VizieR catalogues spell their period and epoch columns differently
    from each other and from the NASA tables, and a wrong guess produces a
    silently empty extract instead of a loud failure.
    """

    def __init__(
        self,
        name: str,
        object_class: str,
        *,
        tic_keys: tuple[str, ...] = (),
        id_keys: tuple[str, ...] = (),
        period_keys: tuple[str, ...] = (),
        epoch_keys: tuple[str, ...] = (),
        duration_keys: tuple[str, ...] = (),
        duration_is_phase_fraction: bool = False,
        disposition_keys: tuple[str, ...] = (),
        period_error_keys: tuple[str, ...] = (),
        epoch_error_keys: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.object_class = object_class
        self.tic_keys = tic_keys
        self.id_keys = id_keys
        self.period_keys = period_keys
        self.epoch_keys = epoch_keys
        self.duration_keys = duration_keys
        self.duration_is_phase_fraction = duration_is_phase_fraction
        self.disposition_keys = disposition_keys
        self.period_error_keys = period_error_keys
        self.epoch_error_keys = epoch_error_keys


SOURCE_SPECS = (
    SourceSpec(
        "nasa_ps",
        "confirmed_planet",
        tic_keys=("tic_id",),
        id_keys=("pl_name",),
        period_keys=("pl_orbper",),
        epoch_keys=("pl_tranmid",),
        duration_keys=("pl_trandur",),
        period_error_keys=("pl_orbpererr1", "pl_orbpererr2"),
        epoch_error_keys=("pl_tranmiderr1", "pl_tranmiderr2"),
    ),
    SourceSpec(
        "nasa_toi",
        "toi",
        tic_keys=("tid",),
        id_keys=("toi",),
        period_keys=("pl_orbper",),
        epoch_keys=("pl_tranmid",),
        duration_keys=("pl_trandurh",),
        disposition_keys=("tfopwg_disp",),
        period_error_keys=("pl_orbpererr1", "pl_orbpererr2"),
        epoch_error_keys=("pl_tranmiderr1", "pl_tranmiderr2"),
    ),
    SourceSpec(
        "tess_eb",
        "eclipsing_binary",
        tic_keys=("TIC",),
        id_keys=("TIC",),
        period_keys=("Per",),
        epoch_keys=("BJD0",),
        duration_keys=("Wp-pf",),
        duration_is_phase_fraction=True,
        disposition_keys=("Morph",),
        period_error_keys=("e_Per",),
        epoch_error_keys=("e_BJD0",),
    ),
    SourceSpec(
        "vsx",
        "variable_star",
        id_keys=("Name", "OID"),
        period_keys=("Period",),
        epoch_keys=("Epoch",),
        disposition_keys=("Type",),
    ),
    SourceSpec(
        "asassn_variables",
        "variable_star",
        id_keys=("ASASSN-V", "ID", "Name"),
        period_keys=("Per", "Period"),
        # ASAS-SN publishes HJD, not HJD0. It is an epoch of extremum rather
        # than a transit centre, so it can only ever support the period-only
        # relation this catalogue is limited to anyway.
        epoch_keys=("HJD0", "HJD", "Epoch"),
        disposition_keys=("Type",),
    ),
    SourceSpec(
        "gaia_nss_sb1",
        "spectroscopic_binary",
        id_keys=("Source",),
        period_keys=("Per", "Period"),
        epoch_keys=("T0", "Tperi"),
        period_error_keys=("e_Per",),
    ),
    SourceSpec(
        "gaia_nss_eb",
        "eclipsing_binary",
        id_keys=("Source",),
        period_keys=("Per", "Period"),
        epoch_keys=("T0", "Tperi"),
        period_error_keys=("e_Per",),
    ),
)


def _pick(row: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if key in row:
            return key
    return None


def _entry_from_row(
    row: dict[str, Any], spec: SourceSpec, snapshot_hash: str
) -> adjudicate.CatalogEphemeris | None:
    period_key = _pick(row, spec.period_keys)
    period = _f(row.get(period_key)) if period_key else None
    id_key = _pick(row, spec.id_keys)
    epoch_key = _pick(row, spec.epoch_keys)
    duration_key = _pick(row, spec.duration_keys)
    duration = _f(row.get(duration_key)) if duration_key else None
    if duration is not None and spec.duration_is_phase_fraction:
        duration = duration * period * 24.0 if period else None
    disposition_key = _pick(row, spec.disposition_keys)
    return adjudicate.CatalogEphemeris(
        source=spec.name,
        identifier=str(row.get(id_key, "")).strip() if id_key else "",
        object_class=spec.object_class,
        snapshot_hash=snapshot_hash,
        period_days=period,
        epoch_bjd=_f(row.get(epoch_key)) if epoch_key else None,
        duration_hours=duration,
        disposition=(
            (str(row.get(disposition_key)).strip() or None)
            if disposition_key
            else None
        ),
        period_uncertainty_days=_worst(row, spec.period_error_keys),
        epoch_uncertainty_days=_worst(row, spec.epoch_error_keys),
        host_only=period is None,
    )


def _worst(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    values = [abs(v) for v in (_f(row.get(key)) for key in keys) if v is not None]
    return max(values) if values else None


def _position_index(
    positions: dict[int, tuple[float, float]]
) -> dict[int, list[int]]:
    """Bin stars by whole degree of declination, so a row checks ~3 bins.

    Brute force over 1,363 stars x a few thousand catalog rows is tolerable;
    over a P5-scale sample it is not, and the binning costs three lines.
    """

    bands: dict[int, list[int]] = {}
    for tic, (_, dec) in positions.items():
        bands.setdefault(int(dec // 1), []).append(tic)
    return bands


def index_snapshots(
    positions: dict[int, tuple[float, float]]
) -> tuple[dict[int, list[adjudicate.CatalogEphemeris]], dict[str, str], list[str], dict[str, Any]]:
    """Attach every snapshot row to the backlog stars it belongs to.

    TIC-keyed sources join on the identifier. Sample-scoped sources have no
    TIC column at all -- they were extracted by position -- so they are joined
    positionally at the same match radius the extract was taken with.
    """

    by_tic: dict[int, list[adjudicate.CatalogEphemeris]] = {}
    hashes: dict[str, str] = {}
    consulted: list[str] = []
    diagnostics: dict[str, Any] = {}
    bands = _position_index(positions)
    radius_deg = match_radius_arcsec() / 3600.0

    for spec in SOURCE_SPECS:
        manifest = snapshots.latest(spec.name)
        if manifest is None:
            diagnostics[spec.name] = {"available": False}
            continue
        hashes[spec.name] = manifest.content_hash
        consulted.append(spec.name)
        rows = snapshots.load_rows(manifest)
        attached = 0

        if spec.tic_keys:
            for row in rows:
                tic_key = _pick(row, spec.tic_keys)
                tic = _tic(row.get(tic_key)) if tic_key else None
                if tic is None or tic not in positions:
                    continue
                entry = _entry_from_row(row, spec, manifest.content_hash)
                if entry is not None:
                    by_tic.setdefault(tic, []).append(entry)
                    attached += 1
        else:
            ra_key, dec_key = snapshots._choose_position_columns(manifest.columns)
            for row in rows:
                ra, dec = _f(row.get(ra_key)), _f(row.get(dec_key))
                if ra is None or dec is None:
                    continue
                candidates: list[int] = []
                for band in (int(dec // 1) - 1, int(dec // 1), int(dec // 1) + 1):
                    candidates.extend(bands.get(band, ()))
                for tic in candidates:
                    star_ra, star_dec = positions[tic]
                    if (
                        snapshots.angular_separation_deg(star_ra, star_dec, ra, dec)
                        <= radius_deg
                    ):
                        entry = _entry_from_row(row, spec, manifest.content_hash)
                        if entry is not None:
                            by_tic.setdefault(tic, []).append(entry)
                            attached += 1

        diagnostics[spec.name] = {
            "available": True,
            "rows": len(rows),
            "attached_to_backlog": attached,
            "join": "tic" if spec.tic_keys else "position",
            "period_column": _pick(rows[0], spec.period_keys) if rows else None,
            "epoch_column": _pick(rows[0], spec.epoch_keys) if rows else None,
        }

    # A source is consulted when its snapshot exists, not when it happens to
    # publish ephemerides. gaia_dr3 supplies the neighbour scene and no
    # periods; treating that as an unfetched gap told 995 stars they could not
    # be checked against catalogs that had in fact been checked.
    for name in snapshots.SNAPSHOT_SOURCES:
        manifest = snapshots.latest(name)
        if manifest is None:
            diagnostics.setdefault(name, {"available": False})
            continue
        hashes.setdefault(name, manifest.content_hash)
        consulted.append(name)
        diagnostics.setdefault(
            name,
            {
                "available": True,
                "rows": manifest.row_count,
                "attached_to_backlog": None,
                "join": "scene_only",
                "period_column": None,
                "epoch_column": None,
            },
        )
    return by_tic, hashes, sorted(set(consulted)), diagnostics


def resolve_scene(
    conn, stars: dict[int, tuple[float, float, float | None]]
) -> dict[str, Any]:
    """Rank every Gaia counterpart inside each backlog star's TESS pixel.

    Section 4.1's third commitment: ambiguity is preserved, never resolved
    away. A star with more than one plausible counterpart in its pixel is not
    a star with a fainter neighbour that can be ignored -- it is a star where
    a neighbour's eclipse can masquerade as a transit on the target, and the
    pixel stage needs the ranked list to test that.
    """

    manifest = snapshots.latest("gaia_dr3")
    if manifest is None:
        return {"available": False}

    rows = snapshots.load_rows(manifest)
    ra_key, dec_key = snapshots._choose_position_columns(manifest.columns)
    bands: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        dec = _f(row.get(dec_key))
        if dec is None:
            continue
        bands.setdefault(int(dec // 1), []).append(row)

    ambiguous = unique = unresolved = 0
    edges_written = 0
    for tic, (ra, dec, tmag) in sorted(stars.items()):
        nearby: list[dict[str, Any]] = []
        for band in (int(dec // 1) - 1, int(dec // 1), int(dec // 1) + 1):
            for row in bands.get(band, ()):
                nearby.append(
                    {
                        "identifier": str(row.get("Source", "")).strip(),
                        "ra_deg": _f(row.get(ra_key)),
                        "dec_deg": _f(row.get(dec_key)),
                        "magnitude": _f(row.get("Gmag")),
                        "pmra_mas_yr": _f(row.get("pmRA")),
                        "pmdec_mas_yr": _f(row.get("pmDE")),
                        "epoch_jyear": CURRENT_IDENTITY.gaia_reference_epoch_jyear,
                    }
                )
        target = identity.SkyPosition(
            ra_deg=ra,
            dec_deg=dec,
            epoch_jyear=CURRENT_IDENTITY.gaia_reference_epoch_jyear,
        )
        ranked = identity.rank_counterparts(
            target, nearby, target_magnitude=tmag
        )
        node = identity.resolve_node(tic, target, ranked, source="gaia_dr3")
        # The TIC's own cross-match already wrote a node; this refines it with
        # the measured scene, which is a stronger claim about ambiguity than a
        # single catalogued pairing can make.
        identity.upsert_node(conn, node)
        edges_written += identity.record_counterparts(
            conn, tic, ranked, source="gaia_dr3", snapshot_hash=manifest.content_hash
        )
        if node.resolution == identity.RESOLUTION_AMBIGUOUS:
            ambiguous += 1
        elif node.resolution == identity.RESOLUTION_UNIQUE:
            unique += 1
        else:
            unresolved += 1

    return {
        "available": True,
        "snapshot_hash": manifest.content_hash,
        "gaia_rows": len(rows),
        "stars_scened": len(stars),
        "unique": unique,
        "ambiguous": ambiguous,
        "unresolved": unresolved,
        "edges_written": edges_written,
    }


def t3_regate(star: dict[str, Any]) -> dict[str, Any]:
    """Re-apply the calibrated red-noise floor to an older screening result."""

    floor = CURRENT_CONFIG.search.red_noise_snr_min
    measured = star["red_noise_adjusted_snr"]
    if measured is None:
        return {
            "verdict": "not_evaluable",
            "reason": "report carries no red-noise-adjusted S/N",
            "floor": floor,
        }
    passes = measured >= floor
    return {
        "verdict": "passes" if passes else "fails_calibrated_red_noise_floor",
        "measured_red_noise_snr": measured,
        "floor": floor,
        "margin": round(measured - floor, 3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("results/p4/readjudication_v1"))
    parser.add_argument(
        "--promote",
        action="store_true",
        help="Write voting evidence (blocked on PROGRESS.md correction 38).",
    )
    args = parser.parse_args(argv)

    conn = ledger.connect(default_db_path())
    try:
        backlog = load_backlog(conn)
        print(f"backlog stars: {len(backlog)}")
        positions = {
            int(row["tic_id"]): (float(row["ra_deg"]), float(row["dec_deg"]))
            for row in conn.execute(
                "SELECT tic_id, ra_deg, dec_deg FROM star "
                "WHERE ra_deg IS NOT NULL AND dec_deg IS NOT NULL"
            )
        }
        positions = {
            star["tic_id"]: positions[star["tic_id"]]
            for star in backlog
            if star["tic_id"] in positions
        }
        print(f"backlog stars with coordinates: {len(positions)}")
        by_tic, hashes, consulted, diagnostics = index_snapshots(positions)
        print(f"snapshot sources consulted: {', '.join(consulted)}")
        for name, info in sorted(diagnostics.items()):
            if info.get("available"):
                attached = info["attached_to_backlog"]
                attached = "     -" if attached is None else f"{attached:>6}"
                print(
                    f"    {name:<18} {info['rows']:>7} rows, "
                    f"{attached} attached "
                    f"(join={info['join']}, period={info['period_column']}, "
                    f"epoch={info['epoch_column']})"
                )
            else:
                print(f"    {name:<18} not fetched")
        print(f"backlog stars with any catalogued row: {len(by_tic)}")

        scene_stars = {
            int(row["tic_id"]): (
                float(row["ra_deg"]),
                float(row["dec_deg"]),
                _f(row["tmag"]),
            )
            for row in conn.execute(
                "SELECT tic_id, ra_deg, dec_deg, tmag FROM star "
                "WHERE ra_deg IS NOT NULL AND dec_deg IS NOT NULL"
            )
            if int(row["tic_id"]) in positions
        }
        scene = resolve_scene(conn, scene_stars)
        if scene.get("available"):
            print(
                f"gaia scene: {scene['unique']} unique, "
                f"{scene['ambiguous']} ambiguous, "
                f"{scene['unresolved']} unresolved "
                f"({scene['edges_written']} edges)"
            )
        else:
            print("gaia scene: gaia_dr3 snapshot not fetched")

        signature = vetting_signature(
            code="modules:" + module_digest(*VETTING_MODULES),
            identity=CURRENT_IDENTITY,
            matching=CURRENT_EPHEMERIS_MATCH,
            snapshots=hashes,
        )
        print(f"vetting signature: {signature}")

        records: list[dict[str, Any]] = []
        t3_counter: Counter[str] = Counter()
        t5_counter: Counter[str] = Counter()
        lane_counter: Counter[str] = Counter()

        for star in backlog:
            t3 = t3_regate(star)
            t3_counter[t3["verdict"]] += 1

            entries = by_tic.get(star["tic_id"], [])
            if star["has_ephemeris"]:
                result = adjudicate.adjudicate(
                    adjudicate.Candidate(
                        tic_id=star["tic_id"],
                        period_days=star["period_days"],
                        duration_hours=star["duration_hours"],
                        epoch_btjd=star["epoch_btjd"],
                    ),
                    entries,
                    consulted_sources=consulted,
                    coverage_gaps=[
                        name
                        for name in snapshots.SNAPSHOT_SOURCES
                        if name not in consulted
                    ],
                )
                t5 = result.to_dict()
                outcome = (
                    result.recommended_status
                    or ("blocked:" + (result.blocked_reason or "")[:40])
                )
            else:
                # Deliberately not `blocked_reason`: that key means "adjudicated
                # to a conclusion no automated status may express", which is a
                # resolution awaiting review. Having no ephemeris to match on
                # is the opposite, and sharing the key silently counted 288
                # unadjudicable stars as resolved.
                t5 = {
                    "relations": [],
                    "recommended_status": None,
                    "unadjudicable_reason": "no ephemeris in the deciding evidence",
                }
                outcome = "no_ephemeris"
            t5_counter[outcome] += 1

            # P4's exit asks how many "resolve into a terminal or review lane
            # with full evidence chains". `unresolved_transit_like_signal` is
            # a review lane -- it means every checked source was checked and
            # none explains the signal -- so it counts. What does not count is
            # a star nobody could adjudicate: an uncovered catalog, or a
            # deciding record with no ephemeris to match on.
            unresolvable = {"catalog_coverage_gap", None}
            t5_status = t5.get("recommended_status")
            resolved = (
                t3["verdict"] == "fails_calibrated_red_noise_floor"
                or (t5_status not in unresolvable)
                or bool(t5.get("blocked_reason"))
            )
            lane_counter["resolved" if resolved else "still_open"] += 1
            if not resolved:
                reason = (
                    "no_ephemeris"
                    if not star["has_ephemeris"]
                    else "catalog_coverage_gap"
                )
                lane_counter[f"still_open:{reason}"] += 1

            record = {
                "tic_id": star["tic_id"],
                "prior_status": star["status"],
                "prior_signature": star["signature"],
                "ephemeris_source": star["ephemeris_source"],
                "ephemeris": {
                    "period_days": star["period_days"],
                    "epoch_btjd": star["epoch_btjd"],
                    "duration_hours": star["duration_hours"],
                },
                "t3_regate": t3,
                "t5_adjudication": t5,
                "resolved": resolved,
            }
            records.append(record)

            ledger.append_evidence(
                conn,
                tic_id=star["tic_id"],
                kind="t5_readjudication",
                source=f"p4_readjudication:{READJUDICATION_POLICY}:{signature}",
                payload=record,
                verdict=None,
                affects_state=bool(args.promote),
                signature=signature,
            )
        conn.commit()
    finally:
        conn.close()

    args.out.mkdir(parents=True, exist_ok=True)
    report = {
        "vetting_signature": signature,
        "readjudication_policy": READJUDICATION_POLICY,
        "snapshot_hashes": hashes,
        "consulted_sources": consulted,
        "backlog_size": len(backlog),
        "source_diagnostics": diagnostics,
        "gaia_scene": scene,
        "ephemeris_sources": dict(
            sorted(Counter(star["ephemeris_source"] for star in backlog).items())
        ),
        "evidence_voting": bool(args.promote),
        "t3_regate": dict(sorted(t3_counter.items())),
        "t5_outcomes": dict(sorted(t5_counter.items())),
        "resolution": dict(sorted(lane_counter.items())),
        "resolved_fraction": (
            round(lane_counter["resolved"] / len(backlog), 4) if backlog else None
        ),
    }
    (args.out / "readjudication_summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    (args.out / "readjudication_records.json").write_text(
        json.dumps(records, indent=2, sort_keys=True), encoding="utf-8"
    )

    print()
    print("=== T3 re-gate under the calibrated red-noise floor")
    for key, count in sorted(t3_counter.items()):
        print(f"  {count:>6}  {key}")
    print("=== T5 catalog adjudication outcomes")
    for key, count in sorted(t5_counter.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>6}  {key}")
    print("=== resolution")
    for key, count in sorted(lane_counter.items()):
        print(f"  {count:>6}  {key}")
    print(f"resolved fraction: {report['resolved_fraction']}")
    print(f"[written] {args.out / 'readjudication_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
