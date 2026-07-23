"""Local-only FastAPI service for the EXOHUNT survey dashboard."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .dashboard import export_dashboard_data


WORKSPACE = Path(__file__).resolve().parents[2]
DASHBOARD_DIR = WORKSPACE / "dashboard"
DIST_DIR = DASHBOARD_DIR / "dist"


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


def create_app(workspace: str | Path = WORKSPACE) -> FastAPI:
    """Create an app that reads and serves only files in the local workspace."""

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
        return JSONResponse(
            {
                "status": "ok",
                "scope": "localhost-only",
                "dashboard_built": (dist_dir / "index.html").exists(),
            }
        )

    @app.get("/data/survey.json")
    def survey_data() -> FileResponse:
        output = dashboard_dir / "public" / "data" / "survey.json"
        if not output.exists():
            output = export_dashboard_data(root)
        if output is None or not output.exists():
            raise HTTPException(status_code=404, detail="Survey data is unavailable.")
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
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not (DIST_DIR / "index.html").exists():
        parser.error(
            "dashboard/dist/index.html is missing; run `npm.cmd run build` "
            "inside the dashboard directory first"
        )

    print(f"EXOHUNT dashboard: http://127.0.0.1:{args.port}")
    print("Network scope: loopback only (not reachable from LAN or internet)")
    uvicorn.run(
        "exohunt.dashboard_server:app",
        host="127.0.0.1",
        port=args.port,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
