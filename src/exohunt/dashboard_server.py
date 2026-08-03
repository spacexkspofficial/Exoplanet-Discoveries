"""Local-only FastAPI service for the EXOHUNT survey dashboard."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .dashboard import export_dashboard_data, survey_source_mtime_ns
from .dashboard_api import (
    ops_payload,
    star_detail_payload,
    star_page_payload,
    summary_payload,
    systematics_payload,
)
from .ledger import connect_readonly


WORKSPACE = Path(__file__).resolve().parents[2]

# A checkpoint older than this is a finished or abandoned run, not progress
# worth showing as live. Liveness itself still comes from the coordinator
# lease heartbeat; this only decides what appears in the progress list.
LIVE_CHECKPOINT_MAX_AGE_SECONDS = 900.0


def _live_campaigns(root: Path) -> list[dict[str, Any]]:
    """Summarize in-flight campaigns from their checkpoint files.

    Returns counts and a completion fraction only. The checkpoint is a cache
    -- per-target reports are the durable truth -- so nothing here is treated
    as evidence, and a malformed or missing file simply yields no entry
    rather than an error on the dashboard's polling path.
    """

    results_root = root / "results"
    if not results_root.exists():
        return []
    now = time.time()
    campaigns: list[dict[str, Any]] = []
    for path in sorted(results_root.rglob("batch_progress.json")):
        try:
            age = now - path.stat().st_mtime
            if age > LIVE_CHECKPOINT_MAX_AGE_SECONDS:
                continue
            progress = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(progress, dict):
            continue
        total = progress.get("total_targets")
        done = progress.get("completed_targets")
        if not isinstance(total, int) or not isinstance(done, int) or total <= 0:
            continue
        counts = progress.get("counts")
        results = progress.get("results")
        results = results if isinstance(results, list) else []
        runtime = progress.get("runtime") or {}
        raw_in_flight = runtime.get("in_flight")
        in_flight = [
            row for row in raw_in_flight if isinstance(row, dict)
        ] if isinstance(raw_in_flight, list) else []
        # The frontend expects the exporter's shape. Omitting `runtime`,
        # `updated_at_utc` or `sectors` does not degrade gracefully -- the
        # throughput readouts render as "--/h" and the freshness label as
        # "NaNh ago", because the arithmetic runs on undefined.
        sectors = sorted(
            {
                int(sector)
                for result in results
                if isinstance(result, dict)
                for sector in _result_sectors(result.get("sectors"))
            }
        )
        campaigns.append(
            {
                "name": path.parent.name,
                "state": progress.get("state"),
                "target_list": progress.get("target_list"),
                "sectors": sectors,
                "total_targets": total,
                "completed_targets": done,
                "counts": counts if isinstance(counts, dict) else {},
                "runtime": runtime,
                # Promoted out of `runtime` so the panel has a stable place
                # to look. Campaigns started before stage tracking existed
                # simply report an empty list, which renders as no panel
                # rather than a broken one.
                "in_flight": in_flight,
                "stages": runtime.get("stages") or [],
                "started_at_utc": progress.get("started_at_utc"),
                "updated_at_utc": progress.get("updated_at_utc"),
                # Extras beyond the exporter's shape; harmless to consumers
                # that ignore them.
                "fraction": round(min(max(done / total, 0.0), 1.0), 4),
                "checkpoint_age_seconds": round(age, 1),
            }
        )
    campaigns.sort(key=lambda row: row["checkpoint_age_seconds"])
    return campaigns


def _result_sectors(value: Any) -> list[int]:
    """Coerce a result row's sector field, which may be a list or a scalar."""

    if value is None:
        return []
    if isinstance(value, (int, float)):
        return [int(value)]
    if isinstance(value, str):
        return [int(part) for part in value.replace(";", " ").split() if part.isdigit()]
    if isinstance(value, (list, tuple)):
        out: list[int] = []
        for item in value:
            out.extend(_result_sectors(item))
        return out
    return []
DASHBOARD_DIR = WORKSPACE / "dashboard"
DIST_DIR = DASHBOARD_DIR / "dist"
CURRENT_SURVEY_SCHEMA_VERSION = 2


def _prefer_live_campaign_last(payload: dict[str, object]) -> None:
    """Keep the live/fresh campaign compatible with older dashboard bundles."""

    campaigns = payload.get("active_campaigns")
    if not isinstance(campaigns, list):
        return
    campaigns.sort(
        key=lambda campaign: (
            isinstance(campaign, dict)
            and campaign.get("state") in {"running", "finalizing"},
            str(campaign.get("updated_at_utc") or "")
            if isinstance(campaign, dict)
            else "",
        )
    )


def _needs_survey_refresh(payload: dict[str, object]) -> bool:
    """Detect data written by a still-running older campaign process."""

    schema_version = payload.get("schema_version")
    return (
        not isinstance(schema_version, int)
        or schema_version < CURRENT_SURVEY_SCHEMA_VERSION
        or not isinstance(payload.get("sector_coverage"), list)
    )


_SURVEY_HEADER_CACHE: dict[str, tuple[tuple[int, int], dict[str, object]]] = {}


def _survey_header(path: Path) -> dict[str, object]:
    """Return the snapshot's freshness metadata, parsed once per written file.

    The browser polls every few seconds, but the snapshot only changes when the
    exporter rewrites it. Caching on (mtime, size) turns a repeated pass over
    tens of megabytes into one parse per export. Only the few fields the
    freshness checks read are retained, so the cache stays small.
    """

    stat = path.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _SURVEY_HEADER_CACHE.get(str(path))
    if cached is not None and cached[0] == key:
        return cached[1]
    payload = json.loads(path.read_text(encoding="utf-8"))
    header: dict[str, object] = {
        "schema_version": payload.get("schema_version"),
        "source_mtime_ns": payload.get("source_mtime_ns"),
    }
    if isinstance(payload.get("sector_coverage"), list):
        # Presence is all the schema check needs; the rows themselves are not
        # worth holding in memory between requests.
        header["sector_coverage"] = []
    _SURVEY_HEADER_CACHE[str(path)] = (key, header)
    return header


def _survey_sources_are_newer(root: Path, payload: dict[str, object]) -> bool:
    """Refresh when any snapshot input changed after the snapshot was built.

    The recorded fingerprint is sampled before the exporter reads anything, so
    a checkpoint written while an export was in flight still counts as newer.
    Comparing against the snapshot's own mtime instead would mark that stale
    result fresh forever, because the export finishes after the write it
    missed.
    """

    recorded = payload.get("source_mtime_ns")
    if not isinstance(recorded, int):
        return True
    return survey_source_mtime_ns(root) > recorded


def _phase_curve_for_tic(root: Path, tic_id: int) -> dict[str, object] | None:
    """Load one compact curve without exposing arbitrary report files."""

    results_root = (root / "results").resolve()
    if not results_root.exists():
        return None

    state_paths = list(results_root.rglob("batch_progress.json"))
    state_paths.extend(results_root.rglob("batch_summary.json"))
    state_paths.sort(
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    for state_path in state_paths:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for result in state.get("results", []):
            try:
                result_tic_id = int(result.get("tic_id"))
            except (TypeError, ValueError):
                continue
            if result_tic_id != tic_id:
                continue
            report_text = result.get("report")
            if not isinstance(report_text, str) or not report_text:
                continue
            report_path = Path(report_text)
            if not report_path.is_absolute():
                report_path = root / report_path
            report_path = report_path.resolve()
            try:
                report_path.relative_to(results_root)
            except ValueError:
                continue
            if report_path.suffix.lower() != ".json" or not report_path.is_file():
                continue
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            curve = report.get("phase_curve")
            if isinstance(curve, dict):
                return curve
    return None


def create_app(
    workspace: str | Path = WORKSPACE,
    *,
    db_path: str | Path | None = None,
) -> FastAPI:
    """Create the local service over a physically read-only ledger."""

    root = Path(workspace).resolve()
    dashboard_dir = root / "dashboard"
    dist_dir = dashboard_dir / "dist"
    app = FastAPI(
        title="EXOHUNT Local Dashboard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )

    def read_ledger(
        builder: Callable[[sqlite3.Connection], dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        try:
            conn = connect_readonly(db_path)
        except (FileNotFoundError, OSError, RuntimeError, sqlite3.Error) as error:
            raise HTTPException(
                status_code=503,
                detail="The EXOHUNT evidence ledger is unavailable.",
            ) from error
        try:
            return builder(conn)
        except sqlite3.Error as error:
            raise HTTPException(
                status_code=503,
                detail="The EXOHUNT evidence ledger is temporarily unavailable.",
            ) from error
        finally:
            conn.close()

    @app.middleware("http")
    async def local_security_headers(request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "base-uri 'none'; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'; "
            "img-src 'self' data:; "
            "object-src 'none'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/api/health")
    def health() -> JSONResponse:
        try:
            conn = connect_readonly(db_path)
        except (FileNotFoundError, OSError, RuntimeError, sqlite3.Error):
            ledger_available = False
        else:
            ledger_available = True
            conn.close()
        return JSONResponse(
            {
                "status": "ok",
                "scope": "localhost-only",
                "dashboard_built": (dist_dir / "index.html").exists(),
                "ledger_available": ledger_available,
            }
        )

    @app.get("/api/summary")
    def summary() -> JSONResponse:
        payload = read_ledger(summary_payload)
        # The ledger only learns about a campaign once `ledger-import` runs,
        # so a run in flight is invisible to the DB-backed payload. The
        # checkpoint files know, and the frontend already renders this list,
        # so read them here rather than in dashboard_api, which is
        # deliberately file-free. Progress is provenance, not science: it
        # never votes on a status.
        if isinstance(payload, dict):
            payload["active_campaigns"] = _live_campaigns(WORKSPACE)
        return JSONResponse(payload)

    @app.get("/api/stars")
    def stars(
        lane: str | None = None,
        status: str | None = None,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=250, ge=1, le=1_000),
    ) -> JSONResponse:
        return JSONResponse(
            read_ledger(
                lambda conn: star_page_payload(
                    conn,
                    lane=lane,
                    status=status,
                    page=page,
                    page_size=page_size,
                )
            )
        )

    @app.get("/api/star/{tic_id}")
    def star(tic_id: int) -> JSONResponse:
        if tic_id <= 0:
            raise HTTPException(status_code=400, detail="Invalid TIC identifier.")
        payload = read_ledger(
            lambda conn: star_detail_payload(conn, tic_id)
        )
        if payload is None:
            raise HTTPException(status_code=404, detail="TIC not found.")
        return JSONResponse(payload)

    @app.get("/api/ops")
    def operations() -> JSONResponse:
        return JSONResponse(read_ledger(ops_payload))

    @app.get("/api/systematics")
    def systematics() -> JSONResponse:
        return JSONResponse(read_ledger(systematics_payload))

    @app.get("/data/survey.json")
    def survey_data() -> Response:
        output = dashboard_dir / "public" / "data" / "survey.json"
        if not output.exists():
            output = export_dashboard_data(root)
        if output is None or not output.exists():
            raise HTTPException(status_code=404, detail="Survey data is unavailable.")

        try:
            header = _survey_header(output)
        except (OSError, json.JSONDecodeError) as error:
            raise HTTPException(
                status_code=503, detail="Survey data is temporarily unavailable."
            ) from error

        if _needs_survey_refresh(header) or _survey_sources_are_newer(root, header):
            # A long-running campaign may have imported the previous exporter
            # before the dashboard was upgraded, while context/science vetters
            # update their own checkpoints. Preserve those workers and upgrade
            # only the derived, replaceable browser snapshot.
            output = export_dashboard_data(root)
            if output is None or not output.exists():
                raise HTTPException(
                    status_code=503, detail="Survey data is temporarily unavailable."
                )

        # The snapshot is served straight from disk. Parsing and re-encoding it
        # would cost a full pass over tens of megabytes on every poll, and the
        # exporter already writes active_campaigns in the order the browser
        # expects, so there is nothing left to rewrite here.
        return FileResponse(
            output,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/targets/{tic_id}/phase-curve")
    def phase_curve(tic_id: int) -> JSONResponse:
        if tic_id <= 0:
            raise HTTPException(status_code=400, detail="Invalid TIC identifier.")
        curve = _phase_curve_for_tic(root, tic_id)
        if curve is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "No actual curve is available because this star was searched "
                    "before the feature was added."
                ),
            )
        return JSONResponse({"tic_id": tic_id, "phase_curve": curve})

    assets_dir = dist_dir / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{requested_path:path}")
    def frontend(requested_path: str) -> FileResponse:
        index_path = dist_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(
                status_code=503,
                detail="Dashboard is not built. Run `npm.cmd run build` in dashboard/.",
            )

        if requested_path:
            candidate = (dist_dir / requested_path).resolve()
            try:
                candidate.relative_to(dist_dir.resolve())
            except ValueError:
                raise HTTPException(status_code=404, detail="File not found.") from None
            if candidate.is_file():
                return FileResponse(candidate)
        return FileResponse(index_path)

    return app


app = create_app()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve the EXOHUNT dashboard on this computer only."
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--allow-sleep",
        action="store_true",
        help=(
            "Let the computer sleep normally while the dashboard runs. By "
            "default the dashboard holds the system awake so unattended "
            "campaigns are not interrupted."
        ),
    )
    parser.add_argument(
        "--keep-display-awake",
        action="store_true",
        help=(
            "Also keep the monitor on. Off by default: an unattended run "
            "does not need a lit screen."
        ),
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not (DIST_DIR / "index.html").exists():
        parser.error(
            "dashboard/dist/index.html is missing; run `npm.cmd run build` "
            "inside the dashboard directory first"
        )

    # Two dashboard servers have been observed running side by side (one per
    # launching actor). One is enough; a duplicate exits successfully so
    # whatever launched it treats the outcome as "already handled".
    from .lease import DASHBOARD_LOCK_NAME, acquire_machine_lock

    guard = acquire_machine_lock(DASHBOARD_LOCK_NAME)
    if guard is None:
        print(
            "An EXOHUNT dashboard server is already running on this machine; "
            "exiting without starting a second one."
        )
        return 0
    from .keepawake import KeepAwake

    awake = KeepAwake(keep_display_on=args.keep_display_awake)
    if not args.allow_sleep:
        awake.start()
    try:
        print(f"EXOHUNT dashboard: http://127.0.0.1:{args.port}")
        print("Network scope: loopback only (not reachable from LAN or internet)")
        # Say which it is rather than implying success: a refused or
        # unsupported request must not read as "the machine will stay up".
        print(
            f"Power: {awake.reason}"
            if not args.allow_sleep
            else "Power: normal sleep allowed (--allow-sleep)"
        )
        uvicorn.run(
            "exohunt.dashboard_server:app",
            host="127.0.0.1",
            port=args.port,
            access_log=False,
        )
        return 0
    finally:
        awake.stop()
        guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
