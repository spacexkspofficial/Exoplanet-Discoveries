"""Generate dashboard camera/CCD footprints from the TESS focal-plane model.

The generated file is deliberately committed so the local dashboard does not
need tess-point at runtime. Regenerate it after installing the optional
``geometry`` dependency:

    python -m pip install -e ".[geometry]"
    python tools/generate_tess_sector_footprints.py

Pointings and focal-plane transforms come from tess-point. Final pixel-level
science should still use the WCS in the calibrated TESS image products.
"""

from __future__ import annotations

import json
import math
from importlib.metadata import version
from pathlib import Path
from typing import Any

import numpy as np
from tess_stars2px import TESS_Spacecraft_Pointing_Data


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "dashboard" / "src" / "tess-sector-footprints.json"
SCIENCE_PIXEL_BOUNDARIES = (-0.5, 2047.5)


def _point(ra: float, dec: float) -> dict[str, float]:
    ra_rad = math.radians(float(ra))
    dec_rad = math.radians(float(dec))
    equatorial_x = math.cos(dec_rad) * math.cos(ra_rad)
    equatorial_y = math.cos(dec_rad) * math.sin(ra_rad)
    equatorial_z = math.sin(dec_rad)
    return {
        "ra_deg": round(float(ra) % 360.0, 7),
        "dec_deg": round(float(dec), 7),
        "x": round(
            -0.0548755604 * equatorial_x
            - 0.8734370902 * equatorial_y
            - 0.4838350155 * equatorial_z,
            9,
        ),
        "y": round(
            0.4941094279 * equatorial_x
            - 0.4448296300 * equatorial_y
            + 0.7469822445 * equatorial_z,
            9,
        ),
        "z": round(
            -0.8676661490 * equatorial_x
            - 0.1980763734 * equatorial_y
            + 0.4559837762 * equatorial_z,
            9,
        ),
    }


def _tangent_coordinates(
    point: dict[str, float], center_ra: float, center_dec: float
) -> tuple[float, float]:
    ra = math.radians(point["ra_deg"])
    dec = math.radians(point["dec_deg"])
    center_ra_rad = math.radians(center_ra)
    center_dec_rad = math.radians(center_dec)
    vector = (
        math.cos(dec) * math.cos(ra),
        math.cos(dec) * math.sin(ra),
        math.sin(dec),
    )
    center = (
        math.cos(center_dec_rad) * math.cos(center_ra_rad),
        math.cos(center_dec_rad) * math.sin(center_ra_rad),
        math.sin(center_dec_rad),
    )
    east = (-math.sin(center_ra_rad), math.cos(center_ra_rad), 0.0)
    north = (
        -math.sin(center_dec_rad) * math.cos(center_ra_rad),
        -math.sin(center_dec_rad) * math.sin(center_ra_rad),
        math.cos(center_dec_rad),
    )
    denominator = sum(a * b for a, b in zip(vector, center))
    return (
        sum(a * b for a, b in zip(vector, east)) / denominator,
        sum(a * b for a, b in zip(vector, north)) / denominator,
    )


def _convex_outline(
    points: list[dict[str, float]], center_ra: float, center_dec: float
) -> list[dict[str, float]]:
    projected = [
        (*_tangent_coordinates(point, center_ra, center_dec), point)
        for point in points
    ]
    projected.sort(key=lambda item: (item[0], item[1]))

    def cross(
        origin: tuple[float, float, dict[str, float]],
        first: tuple[float, float, dict[str, float]],
        second: tuple[float, float, dict[str, float]],
    ) -> float:
        return (first[0] - origin[0]) * (second[1] - origin[1]) - (
            first[1] - origin[1]
        ) * (second[0] - origin[0])

    lower: list[tuple[float, float, dict[str, float]]] = []
    for item in projected:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], item) <= 0:
            lower.pop()
        lower.append(item)
    upper: list[tuple[float, float, dict[str, float]]] = []
    for item in reversed(projected):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], item) <= 0:
            upper.pop()
        upper.append(item)
    return [item[2] for item in lower[:-1] + upper[:-1]]


def _sector_geometry(sector: int) -> dict[str, Any]:
    pointing = TESS_Spacecraft_Pointing_Data(trySector=sector)
    focal_plane = pointing.fpgObjs[0]
    cameras: list[dict[str, Any]] = []
    for camera_index in range(4):
        ccds: list[dict[str, Any]] = []
        camera_points: list[dict[str, float]] = []
        for ccd_index in range(4):
            corners = []
            for column, row in (
                (SCIENCE_PIXEL_BOUNDARIES[0], SCIENCE_PIXEL_BOUNDARIES[0]),
                (SCIENCE_PIXEL_BOUNDARIES[1], SCIENCE_PIXEL_BOUNDARIES[0]),
                (SCIENCE_PIXEL_BOUNDARIES[1], SCIENCE_PIXEL_BOUNDARIES[1]),
                (SCIENCE_PIXEL_BOUNDARIES[0], SCIENCE_PIXEL_BOUNDARIES[1]),
            ):
                ra, dec = focal_plane.pix2radec_nocheck_single(
                    camera_index,
                    ccd_index,
                    np.array([column, row], dtype=float),
                )
                corner = _point(float(ra), float(dec))
                corners.append(corner)
                camera_points.append(corner)
            ccds.append({"ccd": ccd_index + 1, "corners": corners})
        center_ra = float(pointing.camRa[camera_index, 0])
        center_dec = float(pointing.camDec[camera_index, 0])
        cameras.append(
            {
                "camera": camera_index + 1,
                "boresight_ra_deg": round(center_ra, 7),
                "boresight_dec_deg": round(center_dec, 7),
                "outline": _convex_outline(camera_points, center_ra, center_dec),
                "ccds": ccds,
            }
        )
    return {
        "sector": sector,
        "frame": "ICRS/J2000",
        "spacecraft_boresight": {
            "ra_deg": round(float(pointing.ras[0]), 7),
            "dec_deg": round(float(pointing.decs[0]), 7),
            "roll_deg": round(float(pointing.rolls[0]), 7),
        },
        "cameras": cameras,
    }


def main() -> None:
    payload = {
        "schema_version": 1,
        "model": f"tess-point {version('tess-point')}",
        "coverage": "TESS sectors 1 through 107",
        "geometry": "Four cameras; four science-pixel CCD boundaries per camera",
        "frame": "ICRS/J2000",
        "source_url": "https://pypi.org/project/tess-point/",
        "precision_note": (
            "Use calibrated image WCS for final pixel-level measurements."
        ),
        "sectors": {
            str(sector): _sector_geometry(sector)
            for sector in range(1, TESS_Spacecraft_Pointing_Data.sectors[-1] + 1)
        },
    }
    OUTPUT.write_text(
        json.dumps(payload, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT} ({OUTPUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
