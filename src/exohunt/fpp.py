"""False-positive probability via TRICERATOPS, out of process (decision 4).

The owner approved installing TRICERATOPS to unblock the packet contract:
``false_positive_probability`` currently reports ``not_run`` and therefore
correctly blocks every candidate packet from reaching ``ready``. The fix is to
run it, never to relax the contract (P4 close, decision 2) -- a relaxed
contract manufactures a pass, which is worse than an honest block.

Why a subprocess rather than an import
--------------------------------------
TRICERATOPS -> ``pytransit`` -> ``numba`` caps ``numpy`` below 2.4, and the
kernel is calibrated on 2.4.6. Installing TRICERATOPS into the main
environment downgrades ``numpy`` underneath the frozen modules without saying
so -- the same shape as correction 57, where a veto that could not run
reported as *not blocking* rather than as failing. Isolating it in
``.venv-triceratops`` makes the two dependency worlds independent, and the FPP
is a per-candidate computation so the spawn cost is irrelevant.

This module is not on the frozen-kernel list, so nothing here re-signs the
trusted release.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# Repo layout: src/exohunt/fpp.py -> repo root is two parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNNER = _REPO_ROOT / "tools" / "triceratops_fpp.py"
_ISOLATED_PYTHON = _REPO_ROOT / ".venv-triceratops" / "Scripts" / "python.exe"
_ISOLATED_PYTHON_POSIX = _REPO_ROOT / ".venv-triceratops" / "bin" / "python"

# TRICERATOPS samples scenarios; a candidate should not take longer than this,
# and a hung subprocess must not strand a campaign.
DEFAULT_TIMEOUT_SECONDS = 900


def isolated_interpreter() -> Path | None:
    """Return the isolated interpreter, or ``None`` if it has not been built."""

    for candidate in (_ISOLATED_PYTHON, _ISOLATED_PYTHON_POSIX):
        if candidate.exists():
            return candidate
    return None


def _absent(reason: str, **extra: Any) -> dict[str, Any]:
    return {"state": "not_run", "reason": reason, **extra}


def _invoke(request: dict[str, Any], *, timeout: int) -> dict[str, Any]:
    interpreter = isolated_interpreter()
    if interpreter is None:
        return _absent(
            "no .venv-triceratops interpreter; build it with "
            "`python -m venv .venv-triceratops` then install "
            "`triceratops pytransit==2.8.1 'setuptools<81'`"
        )
    if not _RUNNER.exists():
        return _absent(f"runner script missing at {_RUNNER}")

    try:
        completed = subprocess.run(
            [str(interpreter), str(_RUNNER)],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _absent(f"TRICERATOPS exceeded {timeout}s and was stopped")
    except Exception as exc:  # pragma: no cover - spawn failure
        return _absent(f"could not start the isolated interpreter: {exc}")

    if not completed.stdout.strip():
        return _absent(
            "isolated interpreter produced no output",
            stderr=completed.stderr[-2000:],
            returncode=completed.returncode,
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return _absent(
            f"unreadable TRICERATOPS response: {exc}",
            stdout=completed.stdout[-2000:],
            stderr=completed.stderr[-2000:],
        )


def probe(*, timeout: int = 120) -> dict[str, Any]:
    """Check the isolated environment without touching the network.

    Returns a ``measured`` block naming the TRICERATOPS and numpy versions in
    the isolated environment, or ``not_run`` with the reason. Use this in
    pre-flight rather than discovering a broken environment mid-campaign.
    """

    return _invoke({"probe": True}, timeout=timeout)


def false_positive_probability(
    *,
    tic_id: int,
    sectors: list[int],
    period_days: float,
    depth_fraction: float,
    time: list[float],
    flux: list[float],
    flux_err: float,
    aperture_pixels: list[Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Compute an FPP section for the candidate packet.

    ``depth_fraction`` is a fraction, not ppm -- TRICERATOPS' ``tdepth``
    convention. The returned mapping is the packet's
    ``false_positive_probability`` section verbatim; when it cannot be
    computed the section is ``not_run`` with a reason, which keeps the packet
    blocked rather than promoting it on absent evidence.

    Note that building the TRICERATOPS target issues a TIC cone query for
    neighbouring stars. That is a catalogue lookup rather than a photometry
    download, but it is still network traffic, so callers running at scale
    should confirm it is wanted before iterating over a cohort.
    """

    request = {
        "tic_id": int(tic_id),
        "sectors": [int(s) for s in sectors],
        "period_days": float(period_days),
        "depth_fraction": float(depth_fraction),
        "time": [float(t) for t in time],
        "flux": [float(f) for f in flux],
        "flux_err": float(flux_err),
        "aperture_pixels": aperture_pixels,
    }
    return _invoke(request, timeout=timeout)


def main(argv: list[str] | None = None) -> int:
    """`python -m exohunt.fpp` prints the environment probe."""

    result = probe()
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if result.get("state") == "measured" else 1


if __name__ == "__main__":
    raise SystemExit(main())
