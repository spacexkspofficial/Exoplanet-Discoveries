"""Fill the light-curve cache ahead of a campaign, at high concurrency.

Measured on this machine, a campaign is limited by MAST round-trip latency
rather than by CPU: eight analysis processes and eight download threads held
only 2 of 16 cores busy while the read-ahead buffer sat three deep. Downloads
are almost pure waiting, so they parallelise far past what is sensible inside
a campaign process that is also scheduling and searching.

This runs that waiting separately and in bulk. It calls the *same* download
path the campaign calls, with the *same* cache namespace, so every file it
writes is a cache hit for `batch-hunt` rather than a near-miss in a
subtly different location.

It is deliberately not a campaign: it takes no coordinator lease, writes no
reports, touches no ledger, and reaches no scientific conclusion. It only
moves bytes the campaign would otherwise wait for.

Run it *before* a campaign, or alongside one working through the same list --
the campaign's rolling prune protects recently written files, so a concurrent
prefetch is not evicted underneath itself.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time as clock
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from exohunt.campaign import _batch_target_spec, _read_target_rows  # noqa: E402
from exohunt.cli import _safe_name  # noqa: E402
from exohunt.paths import resolve_cache_dir  # noqa: E402
from exohunt.photometry import _download_light_curve  # noqa: E402


def cache_namespace(spec: dict[str, Any]) -> str:
    """The exact namespace `_download_batch_target` would use.

    Any divergence here silently doubles the download bill, because the
    campaign would look in a directory this script never wrote to.
    """

    return _safe_name(
        f"TIC_{int(spec['tic_id'])}_s"
        + "-".join(str(value) for value in spec["sectors"])
    )


def already_cached(cache_root: Path, spec: dict[str, Any]) -> bool:
    namespace = cache_root / "batch_targets" / cache_namespace(spec)
    if not namespace.is_dir():
        return False
    return any(namespace.rglob("*_lc.fits"))


# MAST publishes one cURL script per sector listing every 2-minute light
# curve, which makes each product's download URL derivable from its TIC. That
# removes the per-target archive *search* -- the round trip that dominates
# fetch latency -- leaving only the transfer itself.
SECTOR_SCRIPT_URL = (
    "https://archive.stsci.edu/missions/tess/download_scripts/sector/"
    "tesscurl_sector_{sector}_lc.sh"
)

# tess<start>-s<sector>-<16-digit TIC>-<version>-<type>_lc.fits
PRODUCT_PATTERN = re.compile(
    r"(tess\d+-s(\d{4})-(\d{16})-\d+-[a-z])_lc\.fits", re.IGNORECASE
)


def sector_product_index(
    sector: int, script_cache: Path
) -> dict[int, tuple[str, str]]:
    """Map TIC -> (observation id, download URL) for one sector.

    Parsed from the sector's published cURL script, which is fetched once and
    cached. The observation id is the product filename without its ``_lc.fits``
    suffix, and it is also the directory name lightkurve expects inside
    ``mastDownload/TESS`` -- getting that wrong would leave every prefetched
    file invisible to the campaign.
    """

    script_cache.parent.mkdir(parents=True, exist_ok=True)
    if script_cache.exists():
        text = script_cache.read_text(encoding="utf-8", errors="replace")
    else:
        url = SECTOR_SCRIPT_URL.format(sector=sector)
        print(f"fetching product index: {url}", flush=True)
        try:
            with urllib.request.urlopen(url, timeout=180) as response:
                text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            # MAST publishes these per sector and the newest sectors lag, so a
            # missing script is expected rather than exceptional. Returning an
            # empty index sends every target down the search path instead of
            # ending the run, which is what this did before.
            print(
                f"no published product index for sector {sector} "
                f"(HTTP 404); falling back to the search path",
                flush=True,
            )
            return {}
        script_cache.write_text(text, encoding="utf-8")

    index: dict[int, tuple[str, str]] = {}
    for line in text.splitlines():
        if "_lc.fits" not in line or "http" not in line:
            continue
        match = PRODUCT_PATTERN.search(line)
        if not match:
            continue
        if int(match.group(2)) != int(sector):
            continue
        url = line.split()[-1]
        if not url.startswith("http"):
            continue
        index[int(match.group(3))] = (match.group(1), url)
    return index


def direct_download(
    spec: dict[str, Any],
    cache_root: Path,
    observation_id: str,
    url: str,
    timeout: float = 300.0,
) -> int:
    """Fetch one product straight into the layout lightkurve reads.

    Writes to a temporary name and renames on success, so an interrupted
    transfer can never be mistaken for a cached file by a later run.
    """

    destination = (
        cache_root
        / "batch_targets"
        / cache_namespace(spec)
        / "mastDownload"
        / "TESS"
        / observation_id
    )
    destination.mkdir(parents=True, exist_ok=True)
    final = destination / f"{observation_id}_lc.fits"
    if final.exists() and final.stat().st_size > 0:
        return 0
    partial = destination / f"{observation_id}_lc.fits.part"
    request = urllib.request.Request(
        url, headers={"User-Agent": "exohunt-prefetch/1.0"}
    )
    written = 0
    with urllib.request.urlopen(request, timeout=timeout) as response:
        with partial.open("wb") as handle:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
    if written <= 0:
        partial.unlink(missing_ok=True)
        raise RuntimeError("empty response body")
    partial.replace(final)
    return written


def warm_catalog_cache(specs: list[dict[str, Any]], *, workers: int) -> None:
    """Populate the NASA Exoplanet Archive cache ahead of analysis.

    Analysis calls ``catalogs.check_tic`` per target to find TOIs and confirmed
    planets to mask. A cache miss is a TAP round trip *inside* the analysis
    worker, so a campaign can sit at 10% CPU with fully cached photometry and
    still crawl. Warming it here moves that waiting off the critical path.

    ``check_tic`` is reused rather than reimplemented, so entries written here
    are exactly what analysis expects -- same schema, same freshness rules,
    same file lock.
    """

    from exohunt.catalogs import warm_cache_bulk

    tics = sorted({int(spec["tic_id"]) for spec in specs})
    print(f"warming catalog cache for {len(tics)} targets")
    started = clock.monotonic()

    def report(written: int, total: int) -> None:
        elapsed = clock.monotonic() - started
        rate = written / elapsed * 3600 if elapsed else 0
        print(f"  [{written}/{total}] {rate:.0f}/hour")

    stats = warm_cache_bulk(tics, progress=report)
    elapsed = clock.monotonic() - started
    print(
        f"catalog cache warmed: {stats['written']}/{stats['targets']} in "
        f"{elapsed / 60:.1f} min ({stats['written'] / elapsed * 3600:.0f}/hour) "
        f"using {stats['queries']} queries"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", required=True, type=Path)
    parser.add_argument("--author", default="SPOC")
    parser.add_argument("--cadence-seconds", type=float, default=120.0)
    parser.add_argument(
        "--workers",
        type=int,
        default=12,
        help=(
            "Concurrent downloads. This process does nothing but wait on the "
            "network, so it tolerates far more concurrency than a campaign "
            "can. Keep it civil: MAST is a shared archive."
        ),
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help=(
            "Derive each product URL from the sector's published cURL script "
            "and fetch it straight into the cache, skipping the per-target "
            "archive search. The search round trip dominates fetch latency, so "
            "this is much faster, but it only applies to SPOC 2-minute light "
            "curves whose targets all share one sector."
        ),
    )
    parser.add_argument(
        "--catalogs",
        action="store_true",
        help=(
            "Also warm the NASA Exoplanet Archive cache. Analysis queries the "
            "archive's TAP service once per target, and a miss is a network "
            "round trip inside the analysis worker -- which is why a campaign "
            "with a fully cached photometry set still ran at 10%% CPU."
        ),
    )
    parser.add_argument(
        "--catalogs-only",
        action="store_true",
        help="Warm the catalog cache and skip photometry entirely.",
    )
    parser.add_argument("--max-targets", type=int, default=None)
    parser.add_argument(
        "--max-gb",
        type=float,
        default=None,
        help=(
            "Stop once the cache reaches this size. Prefetching past the "
            "campaign's --cache-max-gb is wasted work: its rolling prune will "
            "evict the oldest files before the campaign reaches them."
        ),
    )
    parser.add_argument("--output", type=Path, default=None)
    return parser


def cache_bytes(cache_root: Path) -> int:
    total = 0
    for item in cache_root.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    cache_override = os.environ.get("EXOHUNT_CACHE_DIR")
    cache_root = resolve_cache_dir(cache_override, workspace_root=Path.cwd())
    if not cache_override:
        print(
            "warning: EXOHUNT_CACHE_DIR is unset, so this resolved to\n"
            f"  {cache_root}\n"
            "A campaign started from a shell that does have it set would look "
            "somewhere else and re-download everything. Set it explicitly.",
            file=sys.stderr,
        )
    cache_root.mkdir(parents=True, exist_ok=True)
    print(f"cache: {cache_root}")

    rows = _read_target_rows(args.targets)
    if args.max_targets:
        rows = rows[: args.max_targets]
    specs = [
        _batch_target_spec(index, row, Path("."))
        for index, row in enumerate(rows, start=1)
    ]

    if args.catalogs or args.catalogs_only:
        warm_catalog_cache(specs, workers=args.workers)
        if args.catalogs_only:
            return 0

    pending = [spec for spec in specs if not already_cached(cache_root, spec)]
    print(
        f"targets: {len(specs)}   already cached: {len(specs) - len(pending)}"
        f"   to fetch: {len(pending)}"
    )
    if not pending:
        print("nothing to do")
        return 0

    budget_bytes = int(args.max_gb * 1_000_000_000) if args.max_gb else None
    if budget_bytes is not None:
        current = cache_bytes(cache_root)
        print(
            f"cache is {current / 1e9:.2f} GB of a {args.max_gb:.2f} GB budget"
        )
        if current >= budget_bytes:
            print("budget already reached; nothing to do")
            return 0

    index: dict[int, tuple[str, str]] = {}
    if args.direct:
        sectors = {int(s) for spec in pending for s in spec["sectors"]}
        if len(sectors) != 1:
            raise SystemExit(
                "--direct needs every target in one sector; this list spans "
                f"{sorted(sectors)}. Run without --direct."
            )
        sector = sectors.pop()
        index = sector_product_index(
            sector, cache_root / "product_index" / f"sector_{sector}_lc.sh"
        )
        print(f"sector {sector} product index: {len(index)} light curves")
        missing = [s for s in pending if int(s["tic_id"]) not in index]
        if missing:
            print(
                f"{len(missing)} targets are absent from the sector index and "
                "will fall back to the search path"
            )

    lock = threading.Lock()
    started = clock.monotonic()
    last_report = [started]
    done = 0
    failed: list[dict[str, str]] = []
    stop = threading.Event()

    def fetch(spec: dict[str, Any]) -> None:
        nonlocal done
        if stop.is_set():
            return
        try:
            entry = index.get(int(spec["tic_id"])) if args.direct else None
            if entry is not None:
                direct_download(spec, cache_root, entry[0], entry[1])
            else:
                _download_light_curve(
                    str(spec["target"]),
                    list(spec["sectors"]),
                    args.author,
                    args.cadence_seconds,
                    cache_namespace=cache_namespace(spec),
                )
        except Exception as error:  # noqa: BLE001 - recorded, not swallowed
            with lock:
                failed.append({"target": str(spec["target"]), "error": repr(error)})
        finally:
            with lock:
                done += 1
                count = done
                # Report on a clock rather than every N targets: a slow search
                # path can leave minutes of silence between count milestones,
                # which reads as a hung process.
                due = clock.monotonic() - last_report[0] >= 10.0
                if due:
                    last_report[0] = clock.monotonic()
            if due:
                elapsed = clock.monotonic() - started
                rate = count / elapsed * 3600 if elapsed else 0
                remaining = len(pending) - count
                eta = remaining / (count / elapsed) / 60 if count else 0
                print(
                    f"[{count}/{len(pending)}] {rate:.0f}/hour  "
                    f"{len(failed)} failed  ETA {eta:.0f} min",
                    flush=True,
                )
                if budget_bytes is not None and cache_bytes(cache_root) >= budget_bytes:
                    print("cache budget reached; stopping", flush=True)
                    stop.set()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(fetch, spec) for spec in pending]
        for future in as_completed(futures):
            future.result()

    elapsed = clock.monotonic() - started
    report = {
        "cache_dir": str(cache_root),
        "targets": len(specs),
        "attempted": len(pending),
        "completed": done,
        "failed": failed,
        "runtime_seconds": round(elapsed, 1),
        "targets_per_hour": round(done / elapsed * 3600) if elapsed else None,
        "cache_bytes": cache_bytes(cache_root),
        "workers": args.workers,
        "stopped_on_budget": stop.is_set(),
    }
    print(
        f"\nfetched {done - len(failed)} of {len(pending)} in "
        f"{elapsed / 60:.1f} min "
        f"({report['targets_per_hour']}/hour), {len(failed)} failed"
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
