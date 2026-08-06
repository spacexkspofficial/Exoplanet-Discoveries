"""Small read-only checks against NASA Exoplanet Archive TAP tables."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from filelock import FileLock


TAP_URL = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync"
CATALOG_CACHE_MAX_AGE = timedelta(days=7)
_CATALOG_QUERY_LIMIT = threading.BoundedSemaphore(2)


def _tap_csv(
    query: str,
    timeout: int = 45,
    *,
    attempts: int = 5,
) -> list[dict[str, str]]:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    parameters = urllib.parse.urlencode({"query": query, "format": "csv"})
    url = TAP_URL + "?" + parameters
    request = urllib.request.Request(url, headers={"User-Agent": "exohunt-starter/0.1"})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8")
            return list(csv.DictReader(io.StringIO(text)))
        except urllib.error.HTTPError as exc:
            # IPAC's front door sometimes redirects broad GET queries to a
            # blocked page (observed as a terminal 404 after redirect) while
            # accepting the same standards-compliant TAP sync request as
            # form-encoded POST. This also avoids URI-length rejection.
            if exc.code in {302, 404}:
                post = urllib.request.Request(
                    TAP_URL,
                    data=parameters.encode("utf-8"),
                    headers={
                        "User-Agent": "exohunt-starter/0.1",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(post, timeout=timeout) as response:
                        text = response.read().decode("utf-8")
                    return list(csv.DictReader(io.StringIO(text)))
                except (urllib.error.URLError, TimeoutError, ConnectionError):
                    if attempt >= attempts:
                        raise
                    time.sleep(min(2 ** (attempt - 1), 16))
                    continue
            if attempt >= attempts or (exc.code != 429 and exc.code < 500):
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt >= attempts:
                raise
        time.sleep(min(2 ** (attempt - 1), 16))
    raise RuntimeError("NASA Exoplanet Archive request failed without an exception")


def _catalog_cache_root(cache_dir: str | Path | None = None) -> Path:
    raw = (
        Path(cache_dir)
        if cache_dir is not None
        else Path(
            os.environ.get(
                "EXOHUNT_CATALOG_CACHE_DIR",
                "data/catalogs/nasa_exoplanet_archive",
            )
        )
    )
    root = raw.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _has_complete_transit_ephemeris(result: dict[str, object]) -> bool:
    """Return whether a cached catalog contains any potentially maskable row."""

    for row in result.get("tois", []):
        if isinstance(row, dict) and all(
            row.get(key) not in (None, "")
            for key in ("pl_orbper", "pl_tranmid", "pl_trandurh")
        ):
            return True
    for row in result.get("confirmed_planets", []):
        if (
            isinstance(row, dict)
            and str(row.get("tran_flag")) == "1"
            and all(
                row.get(key) not in (None, "")
                for key in ("pl_orbper", "pl_tranmid", "pl_trandur")
            )
        ):
            return True
    return False


def _read_fresh_cache(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        generated = datetime.fromisoformat(str(payload["generated_at_utc"]))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - generated > CATALOG_CACHE_MAX_AGE:
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    # Older cache rows did not request period/epoch uncertainties. Refresh
    # only known hosts that could otherwise be silently masked; zero-known
    # targets retain their existing rate-safe cache entries.
    if (
        _has_complete_transit_ephemeris(result)
        and result.get("ephemeris_uncertainty_columns_queried") is not True
    ):
        return None
    return result


def _write_cache(path: Path, result: dict[str, object]) -> None:
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "source": TAP_URL,
        "result": result,
    }
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


_TOI_COLUMNS = (
    "toi,tid,ctoi_alias,tfopwg_disp,pl_orbper,pl_tranmid,"
    "pl_trandurh,pl_trandep,pl_orbpererr1,pl_orbpererr2,"
    "pl_tranmiderr1,pl_tranmiderr2,pl_trandurherr1,"
    "pl_trandurherr2,rowupdate"
)
_PS_COLUMNS = (
    "pl_name,hostname,pl_orbper,pl_tranmid,pl_trandur,pl_rade,"
    "pl_orbpererr1,pl_orbpererr2,pl_tranmiderr1,pl_tranmiderr2,"
    "pl_trandurerr1,pl_trandurerr2,tran_flag,discoverymethod,"
    "disc_year,tic_id,gaia_dr2_id,gaia_dr3_id"
)


def _tap_batch(tic_ids: list[int]) -> tuple[list[dict[str, str]], list[dict[str, str]], int]:
    """Query one batch, halving it if the archive rejects the URL length.

    The TAP service takes its query in the URL, so a batch that is too large
    comes back as HTTP 414 rather than as a smaller result. Batch sizes are
    bounded by total TIC digits rather than by count, so the safe size is not
    a constant; halving on rejection finds it without guessing.
    """

    joined = ",".join(str(value) for value in tic_ids)
    quoted = ",".join(f"'TIC {value}'" for value in tic_ids)
    try:
        tois = _tap_csv(f"select {_TOI_COLUMNS} from toi where tid in ({joined})")
        confirmed = _tap_csv(
            f"select {_PS_COLUMNS} from ps where default_flag=1 "
            f"and tic_id in ({quoted})"
        )
        return tois, confirmed, 2
    except urllib.error.HTTPError as error:
        if error.code != 414 or len(tic_ids) <= 1:
            raise
    middle = len(tic_ids) // 2
    left_tois, left_ps, left_queries = _tap_batch(tic_ids[:middle])
    right_tois, right_ps, right_queries = _tap_batch(tic_ids[middle:])
    return (
        left_tois + right_tois,
        left_ps + right_ps,
        left_queries + right_queries,
    )


def warm_cache_bulk(
    tic_ids: list[int],
    *,
    batch_size: int = 150,
    cache_dir: str | Path | None = None,
    progress=None,
) -> dict[str, int]:
    """Populate the per-TIC cache using batched queries instead of one each.

    :func:`check_tic` asks the archive about a single TIC at a time, which is
    correct but pays a network round trip per target. Measured against the TAP
    service, that runs near 3,250 targets/hour no matter how many threads are
    used -- the service is latency-bound per request, not throughput-bound --
    so a four-thousand-target campaign spends over an hour of analysis time
    waiting on it.

    The same information comes back from ``where tid in (...)``, so this asks
    once per few hundred targets and writes the identical cache entries.

    Entries are written in exactly the shape :func:`check_tic` writes, with
    ``gaia_source_id`` left unset, because every caller in this codebase asks
    without one and a mismatch there would make ``check_tic`` refetch and undo
    the benefit. TICs with no rows get empty lists rather than no file, which
    is the whole point: the common case is a target with nothing catalogued,
    and it must still be a cache hit.
    """

    values = sorted({int(value) for value in tic_ids if int(value) > 0})
    root = _catalog_cache_root(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    written = 0
    queried = 0
    for start in range(0, len(values), batch_size):
        batch = values[start : start + batch_size]
        tois, confirmed, used = _tap_batch(batch)
        queried += used

        by_toi: dict[int, list[dict[str, str]]] = {}
        for row in tois:
            try:
                by_toi.setdefault(int(float(row["tid"])), []).append(row)
            except (KeyError, TypeError, ValueError):
                continue
        by_ps: dict[int, list[dict[str, str]]] = {}
        for row in confirmed:
            raw = str(row.get("tic_id", "")).replace("TIC", "").strip()
            try:
                by_ps.setdefault(int(float(raw)), []).append(row)
            except (TypeError, ValueError):
                continue

        for tic_id in batch:
            _write_cache(
                root / f"TIC_{tic_id}.json",
                {
                    "tic_id": tic_id,
                    "query_identifiers": {
                        "tic_id": tic_id,
                        "gaia_source_id": None,
                    },
                    "ephemeris_uncertainty_columns_queried": True,
                    "tois": by_toi.get(tic_id, []),
                    "confirmed_planets": by_ps.get(tic_id, []),
                },
            )
            written += 1
        if progress is not None:
            progress(written, len(values))
    return {"written": written, "queries": queried, "targets": len(values)}


def check_tic(
    tic_id: int,
    *,
    gaia_source_id: int | None = None,
    force_refresh: bool = False,
    cache_dir: str | Path | None = None,
) -> dict[str, object]:
    """Find TOIs and confirmed planets associated with a TIC/Gaia identity."""

    if tic_id <= 0:
        raise ValueError("TIC ID must be a positive integer.")
    if gaia_source_id is not None and gaia_source_id <= 0:
        raise ValueError("Gaia source ID must be a positive integer.")
    cache_path = _catalog_cache_root(cache_dir) / f"TIC_{tic_id}.json"
    lock = FileLock(str(cache_path.with_suffix(".lock")))
    with lock.acquire(timeout=180):
        if not force_refresh:
            cached = _read_fresh_cache(cache_path)
            if cached is not None and int(cached.get("tic_id", 0)) == tic_id:
                identifiers = cached.get("query_identifiers")
                identifiers = (
                    identifiers if isinstance(identifiers, dict) else {}
                )
                cached_gaia = identifiers.get("gaia_source_id")
                if gaia_source_id is None or (
                    cached_gaia is not None
                    and int(cached_gaia) == gaia_source_id
                ):
                    return cached
        # Limit concurrent TAP traffic across analysis threads. The previous
        # unbounded per-target queries caused hundreds of exhausted timeouts
        # during parallel campaigns even while TESScut itself was healthy.
        with _CATALOG_QUERY_LIMIT:
            tois = _tap_csv(
                "select toi,tid,ctoi_alias,tfopwg_disp,pl_orbper,pl_tranmid,"
                "pl_trandurh,pl_trandep,pl_orbpererr1,pl_orbpererr2,"
                "pl_tranmiderr1,pl_tranmiderr2,pl_trandurherr1,"
                "pl_trandurherr2,rowupdate "
                f"from toi where tid={tic_id}"
            )
            confirmed_identities = [f"tic_id='TIC {tic_id}'"]
            if gaia_source_id is not None:
                confirmed_identities.extend(
                    [
                        f"gaia_dr2_id='Gaia DR2 {gaia_source_id}'",
                        f"gaia_dr3_id='Gaia DR3 {gaia_source_id}'",
                    ]
                )
            confirmed = _tap_csv(
                "select pl_name,hostname,pl_orbper,pl_tranmid,pl_trandur,pl_rade,"
                "pl_orbpererr1,pl_orbpererr2,pl_tranmiderr1,pl_tranmiderr2,"
                "pl_trandurerr1,pl_trandurerr2,tran_flag,discoverymethod,"
                "disc_year,tic_id,gaia_dr2_id,gaia_dr3_id "
                "from ps where default_flag=1 and ("
                + " or ".join(confirmed_identities)
                + ")"
            )
        result: dict[str, object] = {
            "tic_id": tic_id,
            "query_identifiers": {
                "tic_id": tic_id,
                "gaia_source_id": gaia_source_id,
            },
            "ephemeris_uncertainty_columns_queried": True,
            "tois": tois,
            "confirmed_planets": confirmed,
        }
        _write_cache(cache_path, result)
        return result


def known_planet_host_tic_ids(tic_ids: list[int]) -> set[int]:
    """Return TIC IDs already present in the TOI or confirmed-planet tables."""

    values = sorted({int(value) for value in tic_ids if int(value) > 0})
    if not values:
        return set()
    joined = ",".join(str(value) for value in values)
    known = {
        int(float(row["tid"]))
        for row in _tap_csv(f"select distinct tid from toi where tid in ({joined})")
        if row.get("tid")
    }
    quoted = ",".join(f"'TIC {value}'" for value in values)
    for row in _tap_csv(
        "select distinct tic_id from ps where default_flag=1 "
        f"and tic_id in ({quoted})"
    ):
        match = re.search(r"(\d+)", str(row.get("tic_id", "")))
        if match:
            known.add(int(match.group(1)))
    return known


def curated_cool_single_hosts(
    *,
    max_tmag: float = 11.5,
    max_teff: float = 4200.0,
    max_stellar_radius: float = 0.8,
    max_distance_pc: float = 100.0,
    min_period_days: float = 0.5,
    max_period_days: float = 20.0,
) -> list[dict[str, str]]:
    """Return bright, nearby, cool one-TOI systems in a deterministic order."""

    query = (
        "select toi,tid,tfopwg_disp,pl_pnum,pl_orbper,pl_tranmid,pl_trandurh,"
        "pl_trandep,st_tmag,st_teff,st_rad,st_mass,st_dist,rowupdate "
        "from toi where "
        "(tfopwg_disp='CP' or tfopwg_disp='KP') "
        "and pl_pnum=1 "
        f"and st_tmag<={max_tmag} "
        f"and st_teff<={max_teff} "
        f"and st_rad<={max_stellar_radius} "
        f"and st_dist<={max_distance_pc} "
        f"and pl_orbper>={min_period_days} and pl_orbper<={max_period_days} "
        "and pl_tranmid is not null and pl_trandurh is not null "
        "order by st_tmag,tid"
    )
    return _tap_csv(query)
