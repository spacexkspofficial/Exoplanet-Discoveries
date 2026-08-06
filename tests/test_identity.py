"""The identity graph: proper motion, preserved ambiguity, and edge provenance."""

from __future__ import annotations

import pytest

from exohunt import identity, ledger
from exohunt.config import CURRENT_IDENTITY, IdentityConfig, match_radius_arcsec

# Barnard's Star-scale motion: ~10.3"/yr, the extreme of the primary lane.
FAST_MOVER = identity.SkyPosition(
    ra_deg=269.45,
    dec_deg=4.69,
    epoch_jyear=2016.0,
    pmra_mas_yr=-802.8,
    pmdec_mas_yr=10362.5,
)


def test_proper_motion_propagation_moves_a_fast_star_by_the_right_amount() -> None:
    moved, basis = identity.propagate_proper_motion(FAST_MOVER, 1999.3)
    assert basis == "proper_motion_propagated"
    years = 1999.3 - 2016.0
    expected_dec = FAST_MOVER.dec_deg + 10362.5 * years / 3_600_000.0
    assert moved.dec_deg == pytest.approx(expected_dec)
    assert moved.epoch_jyear == 1999.3
    # Over ~17 years this star moves far beyond a TESS pixel, which is exactly
    # why matching an old catalog at the modern position finds the wrong thing.
    from exohunt.snapshots import angular_separation_deg

    drift_arcsec = (
        angular_separation_deg(
            FAST_MOVER.ra_deg, FAST_MOVER.dec_deg, moved.ra_deg, moved.dec_deg
        )
        * 3600.0
    )
    assert drift_arcsec > match_radius_arcsec()


def test_right_ascension_update_divides_out_cos_dec() -> None:
    """mu_alpha* already carries cos(dec); the coordinate step divides it back."""

    import math

    position = identity.SkyPosition(
        ra_deg=100.0, dec_deg=60.0, epoch_jyear=2016.0, pmra_mas_yr=3600.0, pmdec_mas_yr=0.0
    )
    moved, _ = identity.propagate_proper_motion(position, 2017.0)
    expected = 100.0 + (3600.0 / 3_600_000.0) / math.cos(math.radians(60.0))
    assert moved.ra_deg == pytest.approx(expected)
    assert moved.dec_deg == pytest.approx(60.0)


def test_propagation_states_when_it_could_not_propagate() -> None:
    no_motion = identity.SkyPosition(ra_deg=10.0, dec_deg=20.0, epoch_jyear=2016.0)
    moved, basis = identity.propagate_proper_motion(no_motion, 1999.3)
    assert basis == "no_proper_motion_available"
    assert moved is no_motion

    same, basis = identity.propagate_proper_motion(FAST_MOVER, 2016.0)
    assert basis == "same_epoch"
    assert same is FAST_MOVER

    polar = identity.SkyPosition(
        ra_deg=45.0,
        dec_deg=89.9999,
        epoch_jyear=2016.0,
        pmra_mas_yr=500.0,
        pmdec_mas_yr=100.0,
    )
    moved, basis = identity.propagate_proper_motion(polar, 2020.0)
    assert basis == "declination_only_near_pole"
    assert moved.ra_deg == polar.ra_deg


def _target() -> identity.SkyPosition:
    return identity.SkyPosition(ra_deg=100.0, dec_deg=-30.0, epoch_jyear=2016.0)


def _offset(arcsec: float) -> float:
    return -30.0 + arcsec / 3600.0


def test_every_counterpart_inside_the_pixel_is_kept_and_ranked() -> None:
    candidates = [
        {"identifier": "far", "ra_deg": 100.0, "dec_deg": _offset(15.0), "magnitude": 12.0},
        {"identifier": "near", "ra_deg": 100.0, "dec_deg": _offset(2.0), "magnitude": 11.0},
        {"identifier": "outside", "ra_deg": 100.0, "dec_deg": _offset(40.0), "magnitude": 11.0},
    ]
    ranked = identity.rank_counterparts(
        _target(), candidates, target_magnitude=11.0
    )
    assert [item.identifier for item in ranked] == ["near", "far"]
    assert [item.rank for item in ranked] == [1, 2]
    assert ranked[0].separation_arcsec == pytest.approx(2.0, abs=0.01)
    # Ambiguity is preserved: the second candidate is not discarded.
    assert all(item.plausible_host for item in ranked)


def test_a_neighbour_too_faint_to_dilute_is_scene_only() -> None:
    candidates = [
        {"identifier": "bright", "ra_deg": 100.0, "dec_deg": _offset(3.0), "magnitude": 11.2},
        {"identifier": "faint", "ra_deg": 100.0, "dec_deg": _offset(1.0), "magnitude": 25.0},
    ]
    ranked = identity.rank_counterparts(_target(), candidates, target_magnitude=11.0)
    by_id = {item.identifier: item for item in ranked}
    assert by_id["faint"].plausible_host is False
    assert by_id["bright"].plausible_host is True
    # It is still recorded -- the pixel stage may want the full scene -- but it
    # ranks below every plausible host.
    assert by_id["faint"].rank > by_id["bright"].rank


def test_counterparts_are_propagated_before_they_are_matched() -> None:
    """An old-catalog position for a fast mover only matches after propagation."""

    target = identity.SkyPosition(
        ra_deg=269.45, dec_deg=4.69, epoch_jyear=2016.0
    )
    old_epoch_position = {
        "identifier": "2MASS-era",
        "ra_deg": 269.45,
        "dec_deg": 4.69 - 10362.5 * (2016.0 - 1999.3) / 3_600_000.0,
        "epoch_jyear": 1999.3,
        "pmra_mas_yr": 0.0,
        "pmdec_mas_yr": 10362.5,
    }
    matched = identity.rank_counterparts(target, [old_epoch_position])
    assert [item.identifier for item in matched] == ["2MASS-era"]
    assert matched[0].match_basis == identity.MATCH_BASIS_POSITION

    stationary = {**old_epoch_position, "pmra_mas_yr": None, "pmdec_mas_yr": None}
    assert identity.rank_counterparts(target, [stationary]) == []


def test_malformed_candidates_are_skipped_not_guessed() -> None:
    candidates = [
        {"identifier": "ok", "ra_deg": 100.0, "dec_deg": -30.0},
        {"identifier": "no position"},
        {"identifier": "bad", "ra_deg": "north", "dec_deg": -30.0},
    ]
    ranked = identity.rank_counterparts(_target(), candidates)
    assert [item.identifier for item in ranked] == ["ok"]


def test_resolution_reports_unique_ambiguous_and_unresolved() -> None:
    target = _target()
    one = identity.rank_counterparts(
        target,
        [{"identifier": "4242", "ra_deg": 100.0, "dec_deg": _offset(1.0), "magnitude": 11.0}],
        target_magnitude=11.0,
    )
    node = identity.resolve_node(1, target, one)
    assert node.resolution == identity.RESOLUTION_UNIQUE
    assert node.gaia_source_id == 4242

    two = identity.rank_counterparts(
        target,
        [
            {"identifier": "4242", "ra_deg": 100.0, "dec_deg": _offset(1.0), "magnitude": 11.0},
            {"identifier": "4243", "ra_deg": 100.0, "dec_deg": _offset(8.0), "magnitude": 12.0},
        ],
        target_magnitude=11.0,
    )
    node = identity.resolve_node(1, target, two)
    assert node.resolution == identity.RESOLUTION_AMBIGUOUS
    # The working identity is the nearest, but the alternative is carried.
    assert node.gaia_source_id == 4242
    assert [item["identifier"] for item in node.provenance["alternatives"]] == ["4243"]

    faint_only = identity.rank_counterparts(
        target,
        [{"identifier": "9", "ra_deg": 100.0, "dec_deg": _offset(1.0), "magnitude": 30.0}],
        target_magnitude=11.0,
    )
    node = identity.resolve_node(1, target, faint_only)
    assert node.resolution == identity.RESOLUTION_UNRESOLVED
    assert node.gaia_source_id is None
    assert "reason" in node.provenance


def test_nodes_and_edges_round_trip_through_the_ledger(tmp_path) -> None:
    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        target = _target()
        ranked = identity.rank_counterparts(
            target,
            [
                {"identifier": "4242", "ra_deg": 100.0, "dec_deg": _offset(1.0), "magnitude": 11.0},
                {"identifier": "4243", "ra_deg": 100.0, "dec_deg": _offset(9.0), "magnitude": 12.5},
            ],
            target_magnitude=11.0,
        )
        node = identity.resolve_node(55, target, ranked)
        identity.upsert_node(conn, node)
        written = identity.record_counterparts(
            conn, 55, ranked, source="gaia_dr3", snapshot_hash="snap-a"
        )
        conn.commit()
        assert written == 2

        stored = identity.node_for(conn, 55)
        assert stored["resolution"] == identity.RESOLUTION_AMBIGUOUS
        assert stored["candidate_count"] == 2
        assert stored["ra_deg"] == pytest.approx(100.0)

        edges = identity.edges_for(conn, 55, identifier_type="gaia_dr3")
        assert [edge["identifier"] for edge in edges] == ["4242", "4243"]
        # Confidence orders by separation and never leaves [0, 1].
        assert edges[0]["confidence"] > edges[1]["confidence"]
        assert all(0.0 <= edge["confidence"] <= 1.0 for edge in edges)
        assert all(edge["snapshot_hash"] == "snap-a" for edge in edges)

        # Re-running against the same snapshot changes nothing.
        assert identity.record_counterparts(
            conn, 55, ranked, source="gaia_dr3", snapshot_hash="snap-a"
        ) == 0
        assert len(identity.edges_for(conn, 55)) == 2

        assert [row["tic_id"] for row in identity.ambiguous_nodes(conn)] == [55]
    finally:
        conn.close()


def test_edge_confidence_is_bounded(tmp_path) -> None:
    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        with pytest.raises(identity.IdentityError):
            identity.add_edge(
                conn,
                tic_id=1,
                identifier_type="toi",
                identifier="101.01",
                source="nasa_toi",
                confidence=1.5,
                match_basis=identity.MATCH_BASIS_IDENTIFIER,
            )
    finally:
        conn.close()


def test_a_new_snapshot_generation_writes_a_new_edge(tmp_path) -> None:
    """Re-vetting against a newer catalog must not overwrite the old verdict."""

    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        for snapshot_hash in ("snap-a", "snap-b"):
            identity.add_edge(
                conn,
                tic_id=7,
                identifier_type="toi",
                identifier="101.01",
                source=f"nasa_toi@{snapshot_hash}",
                confidence=1.0,
                match_basis=identity.MATCH_BASIS_IDENTIFIER,
                snapshot_hash=snapshot_hash,
            )
        conn.commit()
        edges = identity.edges_for(conn, 7)
        assert {edge["snapshot_hash"] for edge in edges} == {"snap-a", "snap-b"}
    finally:
        conn.close()


def test_match_radius_widens_the_scene(tmp_path) -> None:
    candidates = [
        {"identifier": "edge", "ra_deg": 100.0, "dec_deg": _offset(30.0), "magnitude": 12.0}
    ]
    assert identity.rank_counterparts(_target(), candidates) == []
    wider = identity.rank_counterparts(
        _target(), candidates, identity=IdentityConfig(match_radius_pixels=2.0)
    )
    assert [item.identifier for item in wider] == ["edge"]
    assert CURRENT_IDENTITY.match_radius_pixels == 1.0


def test_schema_migration_is_recorded_and_additive(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    conn = ledger.connect(path)
    conn.close()

    import sqlite3

    raw = sqlite3.connect(path)
    raw.execute("UPDATE schema_meta SET value = '1' WHERE key = 'schema_version'")
    raw.commit()
    raw.close()

    conn = ledger.connect(path)
    try:
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        assert int(version) == ledger.SCHEMA_VERSION
        events = conn.execute(
            "SELECT kind FROM event_log WHERE kind = 'schema_migration'"
        ).fetchall()
        assert len(events) == 1
        # Re-opening does not re-log a migration that already happened.
        conn.close()
        conn = ledger.connect(path)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM event_log WHERE kind = 'schema_migration'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()
