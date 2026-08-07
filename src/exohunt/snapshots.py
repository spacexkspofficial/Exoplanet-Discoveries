"""Versioned, hashed catalog snapshots -- the substrate every T5 verdict cites.

MASTER_PLAN.md section 4.1, "snapshots, not firehoses". The design rule from
section 4 is that *every catalog claim is (source, version, match-basis,
confidence), and absence is only ever "no match in checked sources at these
versions"*. That is only enforceable if the bytes a verdict was adjudicated
against are identified, so:

* a snapshot is an immutable generation of one source, content-hashed over the
  exact CSV the service returned;
* the manifest records the query, the service, the row count, and -- for
  sample-scoped sources -- the hash of the position list that scoped it, so a
  later reader can tell "this star was not in the extract" from "this star was
  checked and absent";
* generations are pruned to :attr:`IdentityConfig.snapshot_generations_kept`
  **rows only**. Manifests are never pruned: an adjudication that cites a
  content hash must stay interpretable after the bulk data is gone.

Two scope classes exist because the sources genuinely differ. The TOI table
and the confirmed-planet table are small enough to hold whole; VSX, ASAS-SN,
Gaia DR3 and the Gaia NSS orbit tables are not, and the plan asks for
per-sample extracts ("one bulk TAP job per target list, not per star"). A
scoped snapshot therefore carries its scope, and
:func:`covers_position` refuses to answer for a star the extract never
included -- that is the `catalog_coverage_gap` status, kept structurally
distinct from `no_match`.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import requests

from .config import CURRENT_IDENTITY, IdentityConfig, match_radius_arcsec
from .paths import state_root

SNAPSHOT_SCHEMA_VERSION = 1

SERVICES = {
    "nasa_tap": "https://exoplanetarchive.ipac.caltech.edu/TAP/sync",
    "vizier_tap": "https://tapvizier.cds.unistra.fr/TAPVizieR/tap/sync",
}

_USER_AGENT = "exohunt-starter/0.1"
# Degrees per arcsecond, used to express the match radius as an ADQL circle.
_ARCSEC_PER_DEGREE = 3600.0
# Position batching: an ADQL query is transported in a form body, but a union
# of CIRCLE predicates still has to be planned, and a long one defeats the
# service. Measured against VizieR on 2026-08-06: a 200-circle union over
# B/vsx/vsx does not return an error, it drops the connection
# (RemoteDisconnected, no response), so the batch size is a real service limit
# rather than a tuning preference. Batches are re-issued and concatenated; row
# identity is restored by de-duplicating on the full row tuple.
_POSITIONS_PER_QUERY = 25
# Batches are issued concurrently, because the service is latency-bound per
# request rather than throughput-bound -- the same finding `catalogs.py`
# documents for the NASA TAP endpoint. Measured on 2026-08-06: 55 sequential
# batches against B/vsx/vsx took 2,248 s, roughly 41 s each, almost all of it
# waiting. The ceiling is deliberately small and matches the politeness limit
# `catalogs.py` already applies (a bounded semaphore of 2-3): these are shared
# public services, and the point is to stop wasting wall time on latency, not
# to extract maximum throughput from someone else's infrastructure.
_SCOPED_QUERY_CONCURRENCY = 3


class SnapshotError(RuntimeError):
    """A snapshot could not be fetched, written, or read back intact."""


@dataclass(frozen=True, slots=True)
class SnapshotSource:
    """One catalog, with what section 4.2 says it can and cannot settle."""

    name: str
    service: str
    table: str
    scope: str
    settles: str
    cannot_settle: str
    refresh: str
    columns: str = "*"
    identifier_column: str | None = None
    predicate: str | None = None
    # Per-source override of _POSITIONS_PER_QUERY. The binding constraint is
    # the service's own query timeout, and how many cones fit inside it is a
    # property of the table being searched, not of this client. Measured
    # against II/366/catv2021 on 2026-08-06: 1 cone 1.1 s, 5 cones 1.4 s,
    # 25 cones dropped the connection after exactly 61.0 s. A server-side
    # timeout presents as a dead socket rather than an HTTP status, so the
    # symptom looks identical to a network fault.
    positions_per_query: int | None = None

    def batch_size(self) -> int:
        return int(self.positions_per_query or _POSITIONS_PER_QUERY)

    def service_url(self) -> str:
        try:
            return SERVICES[self.service]
        except KeyError as exc:  # pragma: no cover - registry is static
            raise SnapshotError(f"Unknown snapshot service: {self.service}") from exc


_NASA_PS_COLUMNS = (
    "pl_name,hostname,tic_id,gaia_dr2_id,gaia_dr3_id,ra,dec,sy_pmra,sy_pmdec,"
    "pl_orbper,pl_orbpererr1,pl_orbpererr2,pl_tranmid,pl_tranmiderr1,"
    "pl_tranmiderr2,pl_trandur,pl_trandurerr1,pl_trandurerr2,pl_trandep,"
    # TESS magnitude is a *system* column in `ps` (sy_tmag), not a stellar one;
    # `st_tmag` exists in the `toi` table and is the natural thing to type.
    "pl_rade,st_teff,st_rad,sy_tmag,tran_flag,discoverymethod,disc_year,soltype"
)
_NASA_TOI_COLUMNS = (
    "toi,tid,ctoi_alias,tfopwg_disp,ra,dec,st_tmag,st_teff,st_rad,st_dist,"
    "pl_pnum,pl_orbper,pl_orbpererr1,pl_orbpererr2,pl_tranmid,pl_tranmiderr1,"
    "pl_tranmiderr2,pl_trandurh,pl_trandurherr1,pl_trandurherr2,pl_trandep,"
    "rowupdate"
)

SNAPSHOT_SOURCES: dict[str, SnapshotSource] = {
    source.name: source
    for source in (
        SnapshotSource(
            name="nasa_ps",
            service="nasa_tap",
            table="ps",
            columns=_NASA_PS_COLUMNS,
            scope="whole_catalog",
            identifier_column="tic_id",
            # The `ps` table holds every published solution for every planet;
            # `default_flag=1` is the archive's own choice of the current
            # preferred parameter set. Snapshotting all solutions would make
            # "this ephemeris is a known planet" ambiguous between disagreeing
            # publications rather than more complete.
            predicate="default_flag=1",
            settles=(
                "This ephemeris is a known planet, when period and epoch both "
                "match."
            ),
            cannot_settle="Anything about a new signal on the same star.",
            refresh="weekly",
        ),
        SnapshotSource(
            name="nasa_toi",
            service="nasa_tap",
            table="toi",
            columns=_NASA_TOI_COLUMNS,
            scope="whole_catalog",
            identifier_column="tid",
            settles=(
                "Already a community or project candidate; a TFOPWG FP "
                "disposition kills the matching ephemeris."
            ),
            cannot_settle="PC is not a planet, and absence is not novelty.",
            refresh="daily",
        ),
        SnapshotSource(
            name="tess_eb",
            service="vizier_tap",
            table='"J/ApJS/258/16/tess-ebs"',
            scope="whole_catalog",
            settles=(
                "Host is a catalogued eclipsing binary; a period or alias "
                "match kills the signal as an EB rediscovery."
            ),
            cannot_settle=(
                "Host EB-ness does not explain a different residual period."
            ),
            refresh="monthly",
        ),
        SnapshotSource(
            name="vsx",
            service="vizier_tap",
            table='"B/vsx/vsx"',
            scope="position_list",
            settles="Known variable-star classifications, including ground EBs.",
            cannot_settle="Absence -- VSX coverage is heterogeneous.",
            refresh="monthly",
        ),
        SnapshotSource(
            name="asassn_variables",
            service="vizier_tap",
            table='"II/366/catv2021"',
            # 80 columns published; these are the ones a verdict can use.
            # `select *` here was not merely wasteful -- VizieR answered it by
            # dropping the connection (measured 2026-08-06, first batch, so
            # not an accumulated rate limit). Columns whose names need ADQL
            # quoting ("ASASSN-V", "Class?") are deliberately not requested.
            columns="ID, RAJ2000, DEJ2000, Vmag, Amp, Per, Type, HJD, TIC, GaiaDR3",
            scope="position_list",
            positions_per_query=5,
            settles="All-sky bright-variable context and deep EB eclipses.",
            cannot_settle="Anything at millimagnitude depth.",
            refresh="monthly",
        ),
        SnapshotSource(
            name="gaia_dr3",
            service="vizier_tap",
            table='"I/355/gaiadr3"',
            # 225 columns published. This source exists to supply the
            # neighbour scene and astrometric priors, not an ephemeris, so it
            # asks for identity, position, motion, brightness, and the quality
            # flags section 4.2 names -- fifteen columns rather than 225.
            columns=(
                "Source, RA_ICRS, DE_ICRS, Plx, pmRA, pmDE, Gmag, BPmag, "
                "RPmag, RUWE, Teff, Dist, NSS, VarFlag, Dup"
            ),
            scope="position_list",
            # 1.8 billion rows; the same timeout ceiling applies with less
            # headroom than ASAS-SN, not more.
            positions_per_query=5,
            settles=(
                "Counterpart astrometry and photometry: parallax, proper "
                "motion, RUWE, and the neighbour scene inside a TESS pixel."
            ),
            cannot_settle="Binarity on its own; high RUWE never kills.",
            refresh="static",
        ),
        SnapshotSource(
            name="gaia_nss_sb1",
            service="vizier_tap",
            table='"I/357/tbosb1"',
            scope="position_list",
            settles=(
                "A spectroscopic single-lined orbit; a period match at the "
                "candidate ephemeris is a kill."
            ),
            cannot_settle="Absence of an NSS orbit is not absence of binarity.",
            refresh="static",
        ),
        SnapshotSource(
            name="gaia_nss_eb",
            service="vizier_tap",
            table='"I/357/tboeb"',
            scope="position_list",
            settles="An astrometric/photometric eclipsing-binary orbit.",
            cannot_settle="Absence of an NSS orbit is not absence of binarity.",
            refresh="static",
        ),
    )
}


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    """Immutable description of one fetched generation."""

    source: str
    version: str
    content_hash: str
    row_count: int
    columns: tuple[str, ...]
    scope: str
    scope_hash: str | None
    scope_size: int | None
    service_url: str
    query: str
    fetched_at_utc: str
    rows_present: bool
    schema_version: int = SNAPSHOT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["columns"] = list(self.columns)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SnapshotManifest":
        data = dict(payload)
        data["columns"] = tuple(data.get("columns") or ())
        known = {field for field in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{key: value for key, value in data.items() if key in known})


def snapshot_root(root: str | Path | None = None) -> Path:
    """Where snapshot generations live: unsynced local state, not the repo."""

    if root is not None:
        return Path(root)
    return state_root() / "snapshots"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _version_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _generation_dir(source: str, version: str, root: str | Path | None) -> Path:
    return snapshot_root(root) / source / version


def hash_positions(positions: Sequence[tuple[float, float]]) -> str:
    """Stable hash of the scope a sample-scoped extract was taken over."""

    canonical = json.dumps(
        [[round(float(ra), 6), round(float(dec), 6)] for ra, dec in positions],
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _tap_sync(
    url: str,
    query: str,
    *,
    timeout: int = 300,
    attempts: int = 4,
) -> str:
    """POST one ADQL query and return CSV text.

    POST rather than GET for the reason ``catalogs.py`` already documents (the
    IPAC front door redirects broad GET queries), and ``requests`` rather than
    ``urllib`` because the VizieR chain does not verify against this machine's
    system trust store while it does verify against the bundled roots.
    """

    payload = {
        "request": "doQuery",
        "lang": "ADQL",
        "format": "csv",
        "query": query,
    }
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(
                url,
                data=payload,
                headers={"User-Agent": _USER_AGENT},
                timeout=timeout,
            )
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last = exc
            status = getattr(exc.response, "status_code", None)
            retryable = status is None or status == 429 or status >= 500
            if attempt >= attempts or not retryable:
                break
            time.sleep(min(2 ** (attempt - 1), 16))
    # A TAP service reports a bad column name as a 400 whose *body* names the
    # column; without the body the operator sees only "400 Client Error" and
    # has to bisect the column list by hand.
    detail = _service_detail(getattr(last, "response", None))
    raise SnapshotError(
        f"TAP query failed against {url}: {last}{detail}\n  query: {query}"
    ) from last


def _service_detail(response: Any) -> str:
    if response is None:
        return ""
    try:
        body = " ".join(str(response.text).split())
    except Exception:  # noqa: BLE001 - diagnostics must never mask the error
        return ""
    if not body:
        return ""
    return f"\n  service said: {body[:400]}"


def table_columns(source: SnapshotSource, *, timeout: int = 120) -> tuple[str, ...]:
    """Read a table's column names from a one-row probe.

    The registry deliberately does not hard-code column names for the VizieR
    sources: their published column sets change between catalog versions, and
    guessing produces a silent empty extract rather than a loud failure.
    """

    text = _tap_sync(
        source.service_url(),
        f"select top 1 * from {source.table}",
        timeout=timeout,
    )
    reader = csv.reader(io.StringIO(text))
    try:
        return tuple(next(reader))
    except StopIteration as exc:
        raise SnapshotError(f"{source.name}: probe returned no header") from exc


def _choose_position_columns(columns: Sequence[str]) -> tuple[str, str]:
    available = {name.lower(): name for name in columns}
    for ra, dec in (
        ("ra_icrs", "de_icrs"),
        ("raj2000", "dej2000"),
        ("_raj2000", "_dej2000"),
        ("ra", "dec"),
        ("radeg", "dedeg"),
    ):
        if ra in available and dec in available:
            return available[ra], available[dec]
    raise SnapshotError(
        "No recognised position columns in " + ", ".join(sorted(columns))
    )


def _circle_predicate(
    ra_column: str,
    dec_column: str,
    positions: Iterable[tuple[float, float]],
    radius_deg: float,
) -> str:
    terms = [
        "CONTAINS(POINT('ICRS',{ra_col},{dec_col}),"
        "CIRCLE('ICRS',{ra:.6f},{dec:.6f},{radius:.8f}))=1".format(
            ra_col=ra_column,
            dec_col=dec_column,
            ra=float(ra),
            dec=float(dec),
            radius=radius_deg,
        )
        for ra, dec in positions
    ]
    return "(" + " or ".join(terms) + ")"


def fetch(
    name: str,
    *,
    positions: Sequence[tuple[float, float]] | None = None,
    root: str | Path | None = None,
    identity: IdentityConfig | None = None,
    timeout: int = 300,
    conn: Any | None = None,
) -> SnapshotManifest:
    """Fetch one generation of ``name`` and write it as an immutable snapshot.

    ``positions`` is required for ``position_list`` sources and rejected for
    whole-catalog ones -- a scoped source with no scope would silently produce
    an extract nobody can interpret.
    """

    source = SNAPSHOT_SOURCES.get(name)
    if source is None:
        raise SnapshotError(f"Unknown snapshot source: {name}")
    identity = identity or CURRENT_IDENTITY

    if source.scope == "whole_catalog":
        if positions:
            raise SnapshotError(
                f"{name} is a whole-catalog source and takes no position scope."
            )
        query = f"select {source.columns} from {source.table}"
        if source.predicate:
            query += f" where {source.predicate}"
        text = _tap_sync(source.service_url(), query, timeout=timeout)
        rows, columns = _parse_csv(text)
        scope_hash = None
        scope_size = None
    else:
        if not positions:
            raise SnapshotError(
                f"{name} is a sample-scoped source and needs a position list."
            )
        radius_deg = match_radius_arcsec(identity) / _ARCSEC_PER_DEGREE
        available = table_columns(source, timeout=timeout)
        ra_column, dec_column = _choose_position_columns(available)
        batch_size = source.batch_size()
        queries: list[str] = []
        for start in range(0, len(positions), batch_size):
            batch = positions[start : start + batch_size]
            circles = _circle_predicate(ra_column, dec_column, batch, radius_deg)
            predicate = (
                f"{source.predicate} and {circles}" if source.predicate else circles
            )
            queries.append(
                f"select {source.columns} from {source.table} where {predicate}"
            )

        collected: list[dict[str, str]] = []
        columns: tuple[str, ...] = ()
        url = source.service_url()
        if len(queries) == 1 or _SCOPED_QUERY_CONCURRENCY <= 1:
            results = [_tap_sync(url, query, timeout=timeout) for query in queries]
        else:
            with ThreadPoolExecutor(
                max_workers=min(_SCOPED_QUERY_CONCURRENCY, len(queries))
            ) as pool:
                # `map` keeps results in submission order and re-raises the
                # first failure, so one dead batch still fails the whole
                # snapshot rather than silently producing a short extract.
                results = list(
                    pool.map(
                        lambda query: _tap_sync(url, query, timeout=timeout),
                        queries,
                    )
                )
        for text in results:
            batch_rows, batch_columns = _parse_csv(text)
            if batch_columns:
                columns = batch_columns
            collected.extend(batch_rows)
        rows = _deduplicate(collected)
        query = queries[0] if len(queries) == 1 else (
            f"{len(queries)} batched queries; first: {queries[0]}"
        )
        scope_hash = hash_positions(positions)
        scope_size = len(positions)

    return write_snapshot(
        name,
        rows,
        columns=columns,
        query=query,
        service_url=source.service_url(),
        scope=source.scope,
        scope_hash=scope_hash,
        scope_size=scope_size,
        root=root,
        identity=identity,
        conn=conn,
    )


def _parse_csv(text: str) -> tuple[list[dict[str, str]], tuple[str, ...]]:
    reader = csv.DictReader(io.StringIO(text))
    rows = [dict(row) for row in reader]
    columns = tuple(reader.fieldnames or ())
    if not columns:
        raise SnapshotError("TAP response carried no header row")
    return rows, columns


def _deduplicate(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for row in rows:
        key = json.dumps(row, sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _canonical_csv(rows: Sequence[dict[str, str]], columns: Sequence[str]) -> str:
    """Serialize deterministically so the content hash means the content.

    Row order from a TAP service is not guaranteed stable between calls, and an
    unstable hash would report a catalog change on every refresh. Rows are
    sorted by their canonical text.
    """

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=list(columns), lineterminator="\n", extrasaction="ignore"
    )
    writer.writeheader()
    for row in sorted(
        rows, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))
    ):
        writer.writerow({key: row.get(key, "") for key in columns})
    return buffer.getvalue()


def write_snapshot(
    name: str,
    rows: Sequence[dict[str, str]],
    *,
    columns: Sequence[str],
    query: str,
    service_url: str,
    scope: str,
    scope_hash: str | None = None,
    scope_size: int | None = None,
    root: str | Path | None = None,
    identity: IdentityConfig | None = None,
    version: str | None = None,
    conn: Any | None = None,
) -> SnapshotManifest:
    """Write one generation atomically and register it in the ledger."""

    identity = identity or CURRENT_IDENTITY
    if not columns:
        raise SnapshotError(f"{name}: refusing to write a snapshot with no columns")
    body = _canonical_csv(rows, columns)
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    stamp = version or _version_stamp()
    directory = _generation_dir(name, stamp, root)
    directory.mkdir(parents=True, exist_ok=True)

    manifest = SnapshotManifest(
        source=name,
        version=stamp,
        content_hash=content_hash,
        row_count=len(rows),
        columns=tuple(columns),
        scope=scope,
        scope_hash=scope_hash,
        scope_size=scope_size,
        service_url=service_url,
        query=query,
        fetched_at_utc=_utc_now(),
        rows_present=True,
    )

    data_temp = directory / "data.csv.tmp"
    data_temp.write_text(body, encoding="utf-8", newline="")
    data_temp.replace(directory / "data.csv")
    _write_manifest(directory, manifest)

    prune(name, root=root, identity=identity)
    if conn is not None:
        register(conn, manifest)
    return manifest


def _write_manifest(directory: Path, manifest: SnapshotManifest) -> None:
    temp = directory / "manifest.json.tmp"
    temp.write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
    temp.replace(directory / "manifest.json")


def list_snapshots(
    name: str, *, root: str | Path | None = None
) -> list[SnapshotManifest]:
    """Every recorded generation of ``name``, newest first."""

    base = snapshot_root(root) / name
    if not base.is_dir():
        return []
    manifests: list[SnapshotManifest] = []
    for directory in sorted(base.iterdir()):
        path = directory / "manifest.json"
        if not path.is_file():
            continue
        try:
            manifests.append(
                SnapshotManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))
            )
        except (OSError, ValueError, TypeError) as exc:
            raise SnapshotError(f"Unreadable snapshot manifest: {path}") from exc
    manifests.sort(key=lambda item: item.version, reverse=True)
    return manifests


def latest(
    name: str, *, root: str | Path | None = None, with_rows: bool = True
) -> SnapshotManifest | None:
    """Newest generation of ``name``; by default the newest that still has rows."""

    for manifest in list_snapshots(name, root=root):
        if manifest.rows_present or not with_rows:
            return manifest
    return None


def load_rows(
    manifest: SnapshotManifest, *, root: str | Path | None = None
) -> list[dict[str, str]]:
    """Read a generation's rows, verifying the content hash before returning."""

    if not manifest.rows_present:
        raise SnapshotError(
            f"{manifest.source}@{manifest.version} was pruned; its manifest "
            "remains for provenance but the rows are gone."
        )
    path = _generation_dir(manifest.source, manifest.version, root) / "data.csv"
    if not path.is_file():
        raise SnapshotError(f"Snapshot rows are missing: {path}")
    body = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if digest != manifest.content_hash:
        raise SnapshotError(
            f"{manifest.source}@{manifest.version} does not match its content "
            f"hash: {digest} != {manifest.content_hash}"
        )
    rows, _ = _parse_csv(body)
    return rows


def prune(
    name: str,
    *,
    root: str | Path | None = None,
    identity: IdentityConfig | None = None,
) -> list[str]:
    """Drop the bulk rows of older generations, keeping every manifest."""

    identity = identity or CURRENT_IDENTITY
    keep = max(1, int(identity.snapshot_generations_kept))
    pruned: list[str] = []
    for manifest in list_snapshots(name, root=root)[keep:]:
        if not manifest.rows_present:
            continue
        directory = _generation_dir(name, manifest.version, root)
        data = directory / "data.csv"
        if data.is_file():
            data.unlink()
        _write_manifest(directory, replace(manifest, rows_present=False))
        pruned.append(manifest.version)
    return pruned


def register(conn: Any, manifest: SnapshotManifest) -> None:
    """Record a generation in the ledger's snapshot table."""

    from . import ledger

    ledger.record_snapshot(
        conn,
        source=manifest.source,
        version=manifest.version,
        content_hash=manifest.content_hash,
        path=str(_generation_dir(manifest.source, manifest.version, None)),
    )


def coverage(*, root: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """What each declared source currently offers an adjudication.

    A source with no generation is not "no match" -- it is
    ``catalog_coverage_gap``, and this is where that distinction is sourced.
    """

    report: dict[str, dict[str, Any]] = {}
    for name, source in SNAPSHOT_SOURCES.items():
        manifest = latest(name, root=root)
        report[name] = {
            "available": manifest is not None,
            "scope": source.scope,
            "refresh": source.refresh,
            "settles": source.settles,
            "cannot_settle": source.cannot_settle,
            "version": manifest.version if manifest else None,
            "content_hash": manifest.content_hash if manifest else None,
            "row_count": manifest.row_count if manifest else None,
            "scope_hash": manifest.scope_hash if manifest else None,
        }
    return report


def snapshot_hashes(*, root: str | Path | None = None) -> dict[str, str]:
    """The content hashes a vetting signature should name."""

    hashes: dict[str, str] = {}
    for name in SNAPSHOT_SOURCES:
        manifest = latest(name, root=root)
        if manifest is not None:
            hashes[name] = manifest.content_hash
    return hashes


def covers_position(
    manifest: SnapshotManifest,
    positions: Sequence[tuple[float, float]],
    ra_deg: float,
    dec_deg: float,
    *,
    identity: IdentityConfig | None = None,
) -> bool:
    """Whether a scoped extract actually looked at this sky position.

    Whole-catalog snapshots cover everything. A scoped extract covers only the
    positions it was taken over, and answering "absent" outside that set would
    manufacture a negative result -- exactly the failure the coverage-gap
    status exists to prevent.
    """

    if manifest.scope == "whole_catalog":
        return True
    identity = identity or CURRENT_IDENTITY
    if hash_positions(positions) != manifest.scope_hash:
        raise SnapshotError(
            f"{manifest.source}@{manifest.version} was scoped over a different "
            "position list than the one supplied."
        )
    radius_deg = match_radius_arcsec(identity) / _ARCSEC_PER_DEGREE
    for scope_ra, scope_dec in positions:
        if angular_separation_deg(ra_deg, dec_deg, scope_ra, scope_dec) <= radius_deg:
            return True
    return False


def angular_separation_deg(
    ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float
) -> float:
    """Great-circle separation, via the haversine form.

    The small-angle planar approximation is fine at one TESS pixel but wrong
    near the poles, and the TESS continuous viewing zones sit at the ecliptic
    poles -- precisely where the primary lane's multi-sector coverage lives.
    """

    ra1, dec1, ra2, dec2 = (
        math.radians(float(value))
        for value in (ra1_deg, dec1_deg, ra2_deg, dec2_deg)
    )
    sin_dec = math.sin((dec2 - dec1) / 2.0) ** 2
    sin_ra = math.sin((ra2 - ra1) / 2.0) ** 2
    haversine = sin_dec + math.cos(dec1) * math.cos(dec2) * sin_ra
    return math.degrees(2.0 * math.asin(math.sqrt(min(1.0, haversine))))
