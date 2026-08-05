"""Serving an already-cached light curve without asking the archive.

`search_lightcurve` used to run before every download, including when the file
was already on disk. That is a network round trip: about 2.2 s alone but near
37 s under eight-way concurrency, against 0.14 s to read the local file. It
capped a fully cached campaign at roughly 430 stars/hour.

The fast path may only substitute a *cache hit*, never a different reduction,
so these tests pin when it is allowed to engage and when it must stand aside.
"""

from __future__ import annotations

import numpy as np
import pytest

import exohunt.photometry as photometry


class _Curve:
    def __init__(self, sector: int, author: str, tic: int) -> None:
        self.meta = {"SECTOR": sector, "AUTHOR": author, "TICID": tic}


class _Collection(list):
    pass


@pytest.fixture
def cached(monkeypatch, tmp_path):
    """A namespace holding one readable SPOC product for sector 100."""

    calls: dict[str, int] = {"searched": 0, "read": 0}

    def fake_configured():
        class _LK:
            LightCurveCollection = _Collection

            @staticmethod
            def search_lightcurve(*args, **kwargs):
                calls["searched"] += 1
                raise AssertionError("the archive must not be queried")

        return _LK(), tmp_path

    monkeypatch.setattr(photometry, "_configured_lightkurve", fake_configured)
    monkeypatch.setattr(
        photometry, "_cached_products", lambda directory: [tmp_path / "a_lc.fits"]
    )

    def fake_read(lk, paths):
        calls["read"] += 1
        return _Collection([_Curve(100, "SPOC", 4242)])

    monkeypatch.setattr(photometry, "_read_cached_collection", fake_read)

    def fake_flatten(normalized, *args, **kwargs):
        time_values = np.arange(50, dtype=float)
        curve = type(
            "F", (), {"time": type("V", (), {"value": time_values})(),
                      "flux": type("V", (), {"value": np.ones(50)})()}
        )()
        return curve, {"cadence_days": 0.00139, "window_cadences": 101}

    monkeypatch.setattr(photometry, "flatten_edge_safe", fake_flatten)
    return calls


def _stitchable(monkeypatch):
    monkeypatch.setattr(
        _Collection, "stitch", lambda self, corrector_func=None: object(), raising=False
    )


def test_cached_product_is_served_without_querying_the_archive(
    cached, monkeypatch
) -> None:
    _stitchable(monkeypatch)
    _, _, metadata = photometry._download_light_curve(
        "TIC 4242", [100], "SPOC", 120.0, cache_namespace="TIC_4242_s100"
    )

    assert cached["searched"] == 0
    assert metadata["served_from_cache"] is True
    assert metadata["tic_id"] == 4242
    assert metadata["downloaded_sectors"] == [100]
    assert metadata["author"] == "SPOC"
    # Reuse compares what was *asked for*, so these must survive the fast path.
    assert metadata["requested_author"] == "SPOC"
    assert metadata["requested_sectors"] == [100]
    assert metadata["author_selection"] == "explicit"


@pytest.mark.parametrize(
    "sectors, author, why",
    [
        ([105], "SPOC", "cached sector differs from the request"),
        ([100], "QLP", "cached author differs from the request"),
    ],
)
def test_a_mismatched_cache_entry_is_not_substituted(
    cached, monkeypatch, sectors, author, why
) -> None:
    """A near-miss must fall through to the archive, not be served."""

    _stitchable(monkeypatch)
    with pytest.raises(AssertionError, match="must not be queried"):
        photometry._download_light_curve(
            "TIC 4242", sectors, author, 120.0, cache_namespace="TIC_4242_s100"
        )
    assert cached["searched"] == 1, why


def test_auto_author_still_consults_the_archive(cached, monkeypatch) -> None:
    """Auto-selection compares authors, which only the archive can answer.

    `_available_products` deliberately swallows a per-author failure so one
    unavailable author cannot end the search, so the assertion here is the
    query counter rather than the exception that eventually escapes.
    """

    _stitchable(monkeypatch)
    with pytest.raises(Exception):
        photometry._download_light_curve(
            "TIC 4242", [100], "auto", 120.0, cache_namespace="TIC_4242_s100"
        )
    assert cached["searched"] >= 1


def test_an_unreadable_cache_entry_falls_back(cached, monkeypatch) -> None:
    """A truncated file must re-fetch rather than fail the target."""

    def explode(lk, paths):
        raise OSError("truncated FITS")

    monkeypatch.setattr(photometry, "_read_cached_collection", explode)
    _stitchable(monkeypatch)
    with pytest.raises(AssertionError, match="must not be queried"):
        photometry._download_light_curve(
            "TIC 4242", [100], "SPOC", 120.0, cache_namespace="TIC_4242_s100"
        )
    assert cached["searched"] == 1


def test_identity_is_read_from_product_headers() -> None:
    tic, sectors, author = photometry._cached_identity(
        [_Curve(100, "SPOC", 777), _Curve(101, "SPOC", 777)]
    )
    assert tic == 777
    assert sectors == [100, 101]
    assert author == "SPOC"
