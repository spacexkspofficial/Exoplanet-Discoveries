import json
from pathlib import Path

import pytest


def test_bundled_tess_sector_geometry_has_real_camera_and_ccd_boundaries() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "dashboard" / "src" / "tess-sector-footprints.json"
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assert payload["model"] == "tess-point 0.9.2"
    assert len(payload["sectors"]) == 107

    sector = payload["sectors"]["105"]
    assert sector["frame"] == "ICRS/J2000"
    assert sector["spacecraft_boresight"] == {
        "ra_deg": pytest.approx(2.5764),
        "dec_deg": pytest.approx(-50.5513),
        "roll_deg": pytest.approx(282.7413),
    }
    assert len(sector["cameras"]) == 4
    assert sector["cameras"][0]["boresight_ra_deg"] == pytest.approx(308.4060)
    assert sector["cameras"][0]["boresight_dec_deg"] == pytest.approx(-44.9984)

    for camera in sector["cameras"]:
        assert len(camera["outline"]) >= 4
        assert len(camera["ccds"]) == 4
        assert [ccd["ccd"] for ccd in camera["ccds"]] == [1, 2, 3, 4]
        for ccd in camera["ccds"]:
            assert len(ccd["corners"]) == 4
            for corner in ccd["corners"]:
                assert 0 <= corner["ra_deg"] < 360
                assert -90 <= corner["dec_deg"] <= 90
                assert (
                    corner["x"] ** 2 + corner["y"] ** 2 + corner["z"] ** 2
                ) == pytest.approx(1.0, abs=2e-8)
