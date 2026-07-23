"""Local-only FastAPI service for the EXOHUNT survey dashboard."""

from __future__ import annotations

import argparse
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
