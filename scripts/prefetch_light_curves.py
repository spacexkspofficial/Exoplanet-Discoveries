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
import sys
import threading
import time as clock
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

    lock = threading.Lock()
    started = clock.monotonic()
    done = 0
    failed: list[dict[str, str]] = []
    stop = threading.Event()

    def fetch(spec: dict[str, Any]) -> None:
        nonlocal done
        if stop.is_set():
            return
        try:
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
            if count % 25 == 0:
                elapsed = clock.monotonic() - started
                rate = count / elapsed * 3600 if elapsed else 0
                remaining = len(pending) - count
                eta = remaining / (count / elapsed) / 3600 if count else 0
                print(
                    f"[{count}/{len(pending)}] {rate:.0f}/hour  "
                    f"{len(failed)} failed  ETA {eta:.1f} h"
                )
                if budget_bytes is not None and cache_bytes(cache_root) >= budget_bytes:
                    print("cache budget reached; stopping")
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
