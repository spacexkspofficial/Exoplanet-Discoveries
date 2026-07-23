"""Small read-only checks against NASA Exoplanet Archive TAP tables."""

from __future__ import annotations

import csv
import io
import re
import time
import urllib.error
import urllib.parse
import urllib.request


TAP_URL = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync"


def _tap_csv(
    query: str,
    timeout: int = 30,
    *,
    attempts: int = 3,
) -> list[dict[str, str]]:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    url = TAP_URL + "?" + urllib.parse.urlencode({"query": query, "format": "csv"})
    request = urllib.request.Request(url, headers={"User-Agent": "exohunt-starter/0.1"})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8")
            return list(csv.DictReader(io.StringIO(text)))
        except urllib.error.HTTPError as exc:
            if attempt >= attempts or (exc.code != 429 and exc.code < 500):
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt >= attempts:
                raise
        time.sleep(min(2 ** (attempt - 1), 4))
    raise RuntimeError("NASA Exoplanet Archive request failed without an exception")


def check_tic(tic_id: int) -> dict[str, object]:
    """Find TOIs and confirmed planets already associated with a TIC ID."""

    if tic_id <= 0:
        raise ValueError("TIC ID must be a positive integer.")
    tois = _tap_csv(
        "select toi,tid,ctoi_alias,tfopwg_disp,pl_orbper,pl_tranmid,"
        "pl_trandurh,pl_trandep,rowupdate "
        f"from toi where tid={tic_id}"
    )
    confirmed = _tap_csv(
        "select pl_name,hostname,pl_orbper,pl_tranmid,pl_trandur,pl_rade,"
        "tran_flag,discoverymethod,disc_year "
        f"from ps where default_flag=1 and tic_id='TIC {tic_id}'"
    )
    return {"tic_id": tic_id, "tois": tois, "confirmed_planets": confirmed}


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
        "pl_trandep,st_tmag,st_teff,st_rad,st_dist,rowupdate from toi where "
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
