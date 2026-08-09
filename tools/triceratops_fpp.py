"""Out-of-process TRICERATOPS runner (owner decision 4).

This script is **not** imported by ``exohunt``. It is executed by a separate
interpreter -- ``.venv-triceratops`` -- and speaks JSON over stdin/stdout.

The separation is load-bearing rather than tidiness. TRICERATOPS depends on
``pytransit``, which depends on ``numba``, which caps ``numpy`` below the
version the detection kernel is calibrated against: installing TRICERATOPS
into the main environment silently downgraded ``numpy`` 2.4.6 -> 2.3.5. The
frozen modules (``search``, ``vetoes``, ``detrend``, ``detrending``,
``detection``, ``photometry``, ``population``, ``screening``, ``campaign``,
``commonmode``, ``calibration``) are byte-identical to calibration commit
``36c935b``, and their *numerics* have to stay as fixed as their source. A
subprocess boundary is the cheapest way to have both: TRICERATOPS gets the
numpy it needs, the kernel keeps the numpy it was calibrated on, and neither
can move the other.

The FPP is a per-candidate computation on a handful of objects, so paying a
process spawn per candidate costs nothing measurable.

Protocol
--------
stdin  : one JSON object -- see ``_run`` for the accepted keys.
stdout : one JSON object with ``state`` plus, when ``state == "measured"``,
         ``fpp`` / ``nfpp`` / ``scenario_probabilities``.

``state`` is never omitted and never guessed. ``not_run`` with a ``reason``
is a legitimate answer; a fabricated number is not. The packet contract
(:func:`exohunt.packet._is_measured`) treats ``not_run`` as absence, which is
the intended behaviour -- an FPP that could not be computed must keep blocking
promotion rather than quietly passing.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any


def _fail(reason: str, **extra: Any) -> dict[str, Any]:
    """Absence that says so. Never rendered as a probability."""

    return {"state": "not_run", "reason": reason, **extra}


def _run(request: dict[str, Any]) -> dict[str, Any]:
    try:
        import numpy as np
        import triceratops.triceratops as tri
    except Exception as exc:  # pragma: no cover - environment probe
        return _fail(f"triceratops unavailable: {exc}")

    tic_id = request.get("tic_id")
    if tic_id is None:
        return _fail("no tic_id supplied")

    sectors = request.get("sectors") or []
    period_days = request.get("period_days")
    depth_fraction = request.get("depth_fraction")
    time = request.get("time")
    flux = request.get("flux")
    flux_err = request.get("flux_err")

    missing = [
        name
        for name, value in (
            ("sectors", sectors),
            ("period_days", period_days),
            ("depth_fraction", depth_fraction),
            ("time", time),
            ("flux", flux),
            ("flux_err", flux_err),
        )
        if value is None or (isinstance(value, (list, tuple)) and len(value) == 0)
    ]
    if missing:
        return _fail(f"missing required inputs: {', '.join(missing)}")

    try:
        target = tri.target(ID=int(tic_id), sectors=np.asarray(sectors, dtype=int))
    except Exception as exc:
        # A TIC cone query is a network call. Failing here means "we did not
        # look", which is exactly what `not_run` is for.
        return _fail(
            f"could not build TRICERATOPS target (TIC query failed?): {exc}"
        )

    try:
        aperture = request.get("aperture_pixels")
        if aperture is None:
            return _fail(
                "no aperture_pixels supplied; TRICERATOPS needs the photometric "
                "aperture to apportion flux among neighbours"
            )
        target.calc_depths(
            tdepth=float(depth_fraction),
            all_ap_pixels=[np.asarray(ap) for ap in aperture],
        )
        target.calc_probs(
            time=np.asarray(time, dtype=float),
            flux_0=np.asarray(flux, dtype=float),
            flux_err_0=float(flux_err),
            P_orb=float(period_days),
        )
    except Exception as exc:
        return _fail(
            f"TRICERATOPS computation failed: {exc}",
            traceback=traceback.format_exc(limit=6),
        )

    fpp = getattr(target, "FPP", None)
    nfpp = getattr(target, "NFPP", None)
    if fpp is None:
        return _fail("TRICERATOPS produced no FPP attribute")

    scenarios: dict[str, float] = {}
    probs = getattr(target, "probs", None)
    if probs is not None:
        try:
            for _, row in probs.iterrows():
                scenarios[str(row["scenario"])] = float(row["prob"])
        except Exception:
            # Scenario detail is a nice-to-have; its absence does not
            # invalidate the FPP itself, so this does not fail the run.
            scenarios = {}

    return {
        "state": "measured",
        "fpp": float(fpp),
        "nfpp": float(nfpp) if nfpp is not None else None,
        "scenario_probabilities": scenarios,
        "sectors": [int(s) for s in sectors],
        "period_days": float(period_days),
        "depth_fraction": float(depth_fraction),
    }


def main() -> int:
    try:
        request = json.load(sys.stdin)
    except Exception as exc:
        json.dump(_fail(f"unreadable request: {exc}"), sys.stdout)
        return 0
    if request.get("probe"):
        # Liveness check used by `exohunt.fpp.probe()`: proves the isolated
        # interpreter exists and can import TRICERATOPS, without any network.
        try:
            import numpy
            import triceratops

            json.dump(
                {
                    "state": "measured",
                    "probe": True,
                    "triceratops_version": getattr(
                        triceratops, "__version__", "unknown"
                    ),
                    "numpy_version": numpy.__version__,
                },
                sys.stdout,
            )
        except Exception as exc:
            json.dump(_fail(f"probe failed: {exc}"), sys.stdout)
        return 0
    json.dump(_run(request), sys.stdout, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
