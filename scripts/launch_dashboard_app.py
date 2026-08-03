"""Open the local dashboard as a fullscreen desktop app window.

The dashboard is a local-only web application (loopback, see
``dashboard_server.main``), but reading it in a normal browser tab buries a
3D star map behind tab strips and address bars. This launcher starts the
server if it is not already up and then opens the page in an app-mode
browser window: no tab strip, no address bar, no browser UI at all.

Two details are not cosmetic:

* **A dedicated ``--user-data-dir`` is required.** Chromium-family browsers
  hand a new URL to an already-running process for the same profile, and
  that process was started without these switches -- the window appears but
  every GPU and app-mode flag is silently dropped. A separate profile
  directory forces a separate browser process where the switches actually
  apply, and it leaves the user's own browsing profile untouched.
* **The GPU switches request the discrete adapter and refuse a software
  fallback.** The star map is WebGL; rendering it through a software
  rasterizer is the difference between an orbiting camera and a slideshow.

This launcher only opens a window onto data that already exists. It runs no
search, writes no evidence, and changes no scientific result.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Loopback only, matching dashboard_server.main; this must never become a
# routable address.
HOST = "127.0.0.1"
DEFAULT_PORT = 8765

BROWSER_CANDIDATES: dict[str, tuple[str, ...]] = {
    "edge": (
        r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe",
        r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe",
    ),
    "chrome": (
        r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
        r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
    ),
}

# Ask for the high-performance adapter, keep hardware acceleration on even if
# the driver is on a conservative blocklist, and let the compositor use the
# GPU for raster work.
GPU_SWITCHES: tuple[str, ...] = (
    "--force_high_performance_gpu",
    "--ignore-gpu-blocklist",
    "--enable-gpu-rasterization",
    "--enable-zero-copy",
)


def _state_root() -> Path:
    """Return the project's unsynced local state root.

    Mirrors :func:`exohunt.paths.state_root` and falls back to the same
    locations when the package is not importable, so the launcher works from
    a plain interpreter as well as the project virtual environment.
    """

    try:
        from exohunt.paths import state_root
    except ImportError:
        override = os.environ.get("EXOHUNT_STATE_DIR")
        if override:
            return Path(override).expanduser()
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "exohunt"
        return Path.home() / ".local" / "state" / "exohunt"
    return state_root()


def _find_browser(preference: str) -> Path | None:
    """Locate an installed Chromium-family browser."""

    order = (
        (preference,)
        if preference != "auto"
        else ("edge", "chrome")
    )
    for name in order:
        for template in BROWSER_CANDIDATES.get(name, ()):
            candidate = Path(os.path.expandvars(template))
            if candidate.is_file():
                return candidate
        found = shutil.which(f"{'msedge' if name == 'edge' else 'chrome'}")
        if found:
            return Path(found)
    return None


def _health(port: int, timeout: float = 2.0) -> dict | None:
    """Return the server's health payload, or None when it is not answering."""

    url = f"http://{HOST}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def _start_server(port: int) -> subprocess.Popen | None:
    """Start the dashboard server detached, preferring the installed script.

    A second server exits 0 by design (``dashboard_server.main`` holds a
    machine-wide lock), so a redundant start here is harmless rather than a
    duplicate.
    """

    scripts_dir = Path(sys.executable).parent
    entry = scripts_dir / "exohunt-dashboard.exe"
    if entry.is_file():
        command = [str(entry), "--port", str(port)]
    else:
        command = [
            sys.executable,
            "-m",
            "exohunt.dashboard_server",
            "--port",
            str(port),
        ]
    creation_flags = 0
    if os.name == "nt":
        # Detach so closing this launcher does not take the server with it.
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008
    return subprocess.Popen(
        command,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )


def _wait_for_health(port: int, deadline_seconds: float) -> dict | None:
    """Poll until the server answers or the deadline passes."""

    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        payload = _health(port)
        if payload is not None:
            return payload
        time.sleep(0.5)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Open the local EXOHUNT dashboard as a fullscreen desktop app "
            "window on this computer only."
        )
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--browser",
        choices=("auto", "edge", "chrome"),
        default="auto",
        help="Which installed browser engine hosts the app window.",
    )
    parser.add_argument(
        "--kiosk",
        action="store_true",
        help=(
            "Lock the window into kiosk fullscreen. The default uses ordinary "
            "fullscreen, which F11 can toggle."
        ),
    )
    parser.add_argument(
        "--no-server",
        action="store_true",
        help="Do not start the server; fail if it is not already running.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Seconds to wait for the server to become healthy.",
    )
    args = parser.parse_args(argv)

    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    browser = _find_browser(args.browser)
    if browser is None:
        print(
            "No Microsoft Edge or Google Chrome installation was found. "
            "Install one, or pass --browser with an available engine.",
            file=sys.stderr,
        )
        return 1

    health = _health(args.port)
    if health is None:
        if args.no_server:
            print(
                f"No dashboard server is answering on {HOST}:{args.port}.",
                file=sys.stderr,
            )
            return 1
        print(f"Starting the dashboard server on {HOST}:{args.port} ...")
        _start_server(args.port)
        health = _wait_for_health(args.port, args.timeout)
        if health is None:
            print(
                f"The dashboard server did not become healthy within "
                f"{args.timeout:.0f}s.",
                file=sys.stderr,
            )
            return 1

    if not health.get("dashboard_built", False):
        print(
            "The dashboard front end is not built. Run `npm ci` and "
            "`npm run build` inside the dashboard directory first.",
            file=sys.stderr,
        )
        return 1
    if not health.get("ledger_available", False):
        # Not fatal: the page still renders, but the survey panels will be
        # empty until `exohunt ledger-import` has run.
        print(
            "Warning: the ledger is not available; survey panels will be "
            "empty until `exohunt ledger-import --workspace . --parity` runs.",
            file=sys.stderr,
        )

    profile_dir = _state_root() / "desktop-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    command = [
        str(browser),
        f"--app=http://{HOST}:{args.port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--kiosk" if args.kiosk else "--start-fullscreen",
        *GPU_SWITCHES,
    ]
    subprocess.Popen(command, cwd=str(REPO_ROOT))

    print(f"EXOHUNT dashboard: http://{HOST}:{args.port}")
    print("Network scope: loopback only (not reachable from LAN or internet)")
    print(f"App window: {browser.name} ({'kiosk' if args.kiosk else 'fullscreen'})")
    print(f"App profile: {profile_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
