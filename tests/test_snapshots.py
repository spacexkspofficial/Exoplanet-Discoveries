"""Catalog snapshots: immutable generations that a verdict can cite.

Every test here is offline. The network fetchers are exercised separately by
``scripts/fetch_p4_snapshots.py`` against the live services, and the artifacts
that produced are recorded in PROGRESS.md; what must be *tested* is that a
snapshot means the same thing tomorrow as it did when a verdict cited it.
"""

from __future__ import annotations

import dataclasses

import pytest

from exohunt import ledger, snapshots
from exohunt.config import IdentityConfig

ROWS = [
    {"toi": "101.01", "tid": "1", "tfopwg_disp": "PC"},
    {"toi": "102.01", "tid": "2", "tfopwg_disp": "FP"},
    {"toi": "103.01", "tid": "3", "tfopwg_disp": "CP"},
]
COLUMNS = ("toi", "tid", "tfopwg_disp")


def _write(root, version: str, rows=None, **overrides):
    return snapshots.write_snapshot(
        "nasa_toi",
        ROWS if rows is None else rows,
        columns=COLUMNS,
        query="select * from toi",
        service_url=snapshots.SERVICES["nasa_tap"],
        scope="whole_catalog",
        root=root,
        version=version,
        **overrides,
    )


def test_round_trip_preserves_rows_and_hash(tmp_path) -> None:
    manifest = _write(tmp_path, "20260101T000000Z")
    assert manifest.row_count == 3
    assert manifest.rows_present is True
    # Rows come back in the canonical order the content hash is taken over,
    # not in service order, so the comparison is on content.
    stored = snapshots.load_rows(manifest, root=tmp_path)
    assert sorted(stored, key=lambda row: row["toi"]) == ROWS
    assert snapshots.latest("nasa_toi", root=tmp_path).content_hash == (
        manifest.content_hash
    )


def test_content_hash_ignores_row_order_but_not_content(tmp_path) -> None:
    """A TAP service does not promise stable row order.

    Hashing the raw response would report a catalog change on every refresh,
    which trains the operator to ignore the one signal that says a catalog
    actually moved.
    """

    first = _write(tmp_path, "20260101T000000Z")
    reordered = _write(tmp_path, "20260102T000000Z", rows=list(reversed(ROWS)))
    assert reordered.content_hash == first.content_hash

    edited = list(ROWS)
    edited[1] = {**edited[1], "tfopwg_disp": "PC"}
    changed = _write(tmp_path, "20260103T000000Z", rows=edited)
    assert changed.content_hash != first.content_hash


def test_pruning_drops_rows_but_never_provenance(tmp_path) -> None:
    identity = IdentityConfig(snapshot_generations_kept=2)
    versions = [f"2026010{n}T000000Z" for n in range(1, 5)]
    for index, version in enumerate(versions):
        _write(
            tmp_path,
            version,
            rows=ROWS[: index + 1] if index < len(ROWS) else ROWS,
            identity=identity,
        )

    generations = snapshots.list_snapshots("nasa_toi", root=tmp_path)
    assert [item.version for item in generations] == sorted(versions, reverse=True)
    assert [item.rows_present for item in generations] == [True, True, False, False]

    pruned = [item for item in generations if not item.rows_present]
    for manifest in pruned:
        # The hash, row count and query survive, so an adjudication that cited
        # this generation is still interpretable.
        assert manifest.content_hash
        assert manifest.query
        with pytest.raises(snapshots.SnapshotError, match="pruned"):
            snapshots.load_rows(manifest, root=tmp_path)

    assert snapshots.latest("nasa_toi", root=tmp_path).version == versions[-1]


def test_load_rows_refuses_altered_bytes(tmp_path) -> None:
    manifest = _write(tmp_path, "20260101T000000Z")
    data = tmp_path / "nasa_toi" / manifest.version / "data.csv"
    data.write_text(
        data.read_text(encoding="utf-8").replace("PC", "FP"), encoding="utf-8"
    )
    with pytest.raises(snapshots.SnapshotError, match="content hash"):
        snapshots.load_rows(manifest, root=tmp_path)


def test_coverage_separates_absent_sources_from_empty_ones(tmp_path) -> None:
    _write(tmp_path, "20260101T000000Z")
    report = snapshots.coverage(root=tmp_path)
    assert report["nasa_toi"]["available"] is True
    assert report["nasa_toi"]["row_count"] == 3
    # Never fetched is a coverage gap, not an absence of matches.
    assert report["vsx"]["available"] is False
    assert report["vsx"]["version"] is None
    # Every declared source states its limits, so a reader cannot mistake a
    # TCE for an astrophysical classification.
    for entry in report.values():
        assert entry["settles"] and entry["cannot_settle"]


def test_snapshot_hashes_only_report_what_exists(tmp_path) -> None:
    assert snapshots.snapshot_hashes(root=tmp_path) == {}
    manifest = _write(tmp_path, "20260101T000000Z")
    assert snapshots.snapshot_hashes(root=tmp_path) == {
        "nasa_toi": manifest.content_hash
    }


def test_scope_class_is_enforced_in_both_directions() -> None:
    with pytest.raises(snapshots.SnapshotError, match="position scope"):
        snapshots.fetch("nasa_toi", positions=[(10.0, -20.0)])
    with pytest.raises(snapshots.SnapshotError, match="position list"):
        snapshots.fetch("vsx")
    with pytest.raises(snapshots.SnapshotError, match="Unknown snapshot source"):
        snapshots.fetch("not_a_catalog")


def test_scoped_extract_refuses_to_answer_outside_its_scope(tmp_path) -> None:
    positions = [(10.0, -20.0), (11.0, -21.0)]
    manifest = snapshots.write_snapshot(
        "vsx",
        [{"Name": "V1", "RAJ2000": "10.0", "DEJ2000": "-20.0"}],
        columns=("Name", "RAJ2000", "DEJ2000"),
        query="select * from vsx where ...",
        service_url=snapshots.SERVICES["vizier_tap"],
        scope="position_list",
        scope_hash=snapshots.hash_positions(positions),
        scope_size=len(positions),
        root=tmp_path,
        version="20260101T000000Z",
    )
    assert snapshots.covers_position(manifest, positions, 10.0, -20.0) is True
    # A star the extract never looked at is a coverage gap, not "no match".
    assert snapshots.covers_position(manifest, positions, 200.0, 40.0) is False
    with pytest.raises(snapshots.SnapshotError, match="different"):
        snapshots.covers_position(manifest, [(1.0, 2.0)], 10.0, -20.0)


def test_whole_catalog_snapshots_cover_every_position(tmp_path) -> None:
    manifest = _write(tmp_path, "20260101T000000Z")
    assert snapshots.covers_position(manifest, [], 123.4, -56.7) is True


def test_position_hash_is_stable_and_order_sensitive() -> None:
    first = snapshots.hash_positions([(1.0, 2.0), (3.0, 4.0)])
    assert first == snapshots.hash_positions([(1.0, 2.0), (3.0, 4.0)])
    assert first != snapshots.hash_positions([(3.0, 4.0), (1.0, 2.0)])


def test_angular_separation_is_correct_near_the_pole() -> None:
    """The CVZs sit at the ecliptic poles, where planar approximations fail."""

    # One degree apart in declination is one degree of separation anywhere.
    assert snapshots.angular_separation_deg(0.0, 0.0, 0.0, 1.0) == pytest.approx(1.0)
    # Twelve hours of RA at +89.9 dec is a small physical separation, not 180.
    near_pole = snapshots.angular_separation_deg(0.0, 89.9, 180.0, 89.9)
    assert near_pole == pytest.approx(0.2, abs=1e-6)
    assert snapshots.angular_separation_deg(10.0, -20.0, 10.0, -20.0) == 0.0


def test_registration_records_the_generation_in_the_ledger(tmp_path) -> None:
    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        manifest = _write(tmp_path, "20260101T000000Z", conn=conn)
        conn.commit()
        rows = conn.execute("SELECT source, version, content_hash FROM snapshot").fetchall()
        assert [tuple(row) for row in rows] == [
            ("nasa_toi", manifest.version, manifest.content_hash)
        ]
        # Registration is idempotent on (source, version, hash).
        snapshots.register(conn, manifest)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0] == 1
    finally:
        conn.close()


def test_empty_column_set_is_refused(tmp_path) -> None:
    with pytest.raises(snapshots.SnapshotError, match="no columns"):
        snapshots.write_snapshot(
            "nasa_toi",
            [],
            columns=(),
            query="q",
            service_url="url",
            scope="whole_catalog",
            root=tmp_path,
        )


def test_position_column_discovery_prefers_icrs() -> None:
    assert snapshots._choose_position_columns(["RA_ICRS", "DE_ICRS", "Gmag"]) == (
        "RA_ICRS",
        "DE_ICRS",
    )
    assert snapshots._choose_position_columns(["RAJ2000", "DEJ2000"]) == (
        "RAJ2000",
        "DEJ2000",
    )
    with pytest.raises(snapshots.SnapshotError, match="position columns"):
        snapshots._choose_position_columns(["name", "period"])


def test_manifest_survives_a_dict_round_trip(tmp_path) -> None:
    manifest = _write(tmp_path, "20260101T000000Z")
    assert snapshots.SnapshotManifest.from_dict(manifest.to_dict()) == manifest
    # Unknown keys from a future schema version are ignored, not fatal.
    payload = {**manifest.to_dict(), "some_future_field": 1}
    assert snapshots.SnapshotManifest.from_dict(payload) == manifest


def test_every_source_declares_a_supported_service_and_scope() -> None:
    for name, source in snapshots.SNAPSHOT_SOURCES.items():
        assert source.name == name
        assert source.service in snapshots.SERVICES
        assert source.scope in {"whole_catalog", "position_list"}
        assert source.service_url().startswith("https://")
        assert dataclasses.is_dataclass(source)
