"""Serving an already-cached light curve without asking the archive.

`search_lightcurve` used to run before every download, including when the file
was already on disk. That is a network round trip: about 2.2 s alone but near
37 s under eight-way concurrency, against 0.14 s to read the local file. It
capped a fully cached campaign at roughly 430 stars/hour.

The fast path may only substitute a *cache hit*, never a different reduction,
so these tests pin when it is allowed to engage and when it must stand aside.
"""

from __future__ import annotations

import sys

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


class _Stitched:
    """Minimal stand-in for a stitched light curve: just the arrays."""

    def __init__(self) -> None:
        values = np.arange(50, dtype=float)
        self.time = type("V", (), {"value": values})()
        self.flux = type("V", (), {"value": np.ones(50, dtype=np.float32)})()


def _stitchable(monkeypatch):
    monkeypatch.setattr(
        _Collection,
        "stitch",
        lambda self, corrector_func=None: _Stitched(),
        raising=False,
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


def test_preparation_preserves_the_incoming_flux_dtype() -> None:
    """Preparation must not upcast float32 mission flux.

    Moving the detrend off the coordinator meant rebuilding a light curve from
    arrays. Coercing them to float64 on the way changed Savitzky-Golay
    arithmetic by about 6e-7 in relative flux, which moved every fitted depth
    across a sixteen-target cohort and flipped one period from 5.987 d to
    5.965 d. Preserving the dtype reproduces the in-place result exactly.
    """

    pytest.importorskip("lightkurve")
    # Long enough that the half-window edge guard leaves a baseline behind.
    time_values = np.arange(4000, dtype=float) * 0.002
    flux_values = (
        1.0 + 0.001 * np.sin(2 * np.pi * time_values / 0.9)
    ).astype(np.float32)

    import lightkurve as lk

    from exohunt.detrending import flatten_edge_safe

    # What the download stage used to compute, detrending the curve in place.
    in_place, _ = flatten_edge_safe(
        lk.LightCurve(time=time_values, flux=flux_values)
    )
    expected = np.asarray(in_place.flux.value, dtype=float)

    prepared_time, prepared_flux, metadata = photometry.prepare_search_arrays(
        time_values, flux_values, {"target": "TIC 1"}
    )
    assert np.array_equal(np.asarray(prepared_flux, dtype=float), expected)

    # And the guard has teeth: upcasting first gives a different answer, which
    # is exactly the bug this pins.
    _, upcast_flux, _ = photometry.prepare_search_arrays(
        time_values, flux_values.astype(np.float64), {"target": "TIC 1"}
    )
    assert not np.array_equal(np.asarray(upcast_flux, dtype=float), expected)

    # The keys the flattening step owns must appear exactly once, here.
    assert "detrending" in metadata
    assert metadata["flatten_window_cadences"] > 0
    assert metadata["cadence_minutes"] == pytest.approx(
        float(metadata["detrending"]["cadence_days"]) * 24 * 60
    )
    assert metadata["target"] == "TIC 1"
    assert len(prepared_time) > 0


def test_preparation_orders_and_merges_stitched_cadences(monkeypatch) -> None:
    """Archive product ordering cannot make multi-sector preparation fail."""

    captured: dict[str, np.ndarray] = {}

    class _LightCurve:
        def __init__(self, *, time, flux) -> None:
            captured["time"] = np.asarray(time)
            captured["flux"] = np.asarray(flux)

    class _LK:
        LightCurve = _LightCurve

    class _Values:
        def __init__(self, values) -> None:
            self.value = np.asarray(values)

    class _Flattened:
        def __init__(self, time, flux) -> None:
            self.time = _Values(time)
            self.flux = _Values(flux)

    monkeypatch.setitem(sys.modules, "lightkurve", _LK())
    monkeypatch.setattr(
        photometry,
        "flatten_edge_safe",
        lambda curve: (
            _Flattened(captured["time"], captured["flux"]),
            {"cadence_days": 1.0, "window_cadences": 101},
        ),
    )
    time, flux, metadata = photometry.prepare_search_arrays(
        np.asarray([3.0, 1.0, 2.0, 2.0, np.nan]),
        np.asarray([3.0, 1.0, 1.8, 2.2, 9.0], dtype=np.float32),
        {"target": "TIC 1"},
    )

    assert np.array_equal(time, np.asarray([1.0, 2.0, 3.0]))
    assert np.array_equal(flux, np.asarray([1.0, 2.0, 3.0], dtype=np.float32))
    assert flux.dtype == np.float32
    assert metadata["time_axis_normalization"] == {
        "method": "stable_sort_and_mean_exact_duplicates",
        "input_cadences": 5,
        "removed_nonfinite_cadences": 1,
        "duplicate_cadences_merged": 1,
        "reordered": True,
    }


def test_unflattened_download_omits_the_detrending_metadata(
    cached, monkeypatch
) -> None:
    """flatten=False must not claim a detrend that has not happened yet.

    The analysis stage decides whether to prepare by looking for these keys,
    so leaving a stale one behind would skip preparation entirely.
    """

    _stitchable(monkeypatch)
    _, _, metadata = photometry._download_light_curve(
        "TIC 4242", [100], "SPOC", 120.0,
        cache_namespace="TIC_4242_s100", flatten=False,
    )

    for key in ("detrending", "cadence_minutes", "flatten_window_cadences"):
        assert key not in metadata, key
    assert metadata["served_from_cache"] is True


def test_identity_is_read_from_product_headers() -> None:
    tic, sectors, author = photometry._cached_identity(
        [_Curve(100, "SPOC", 777), _Curve(101, "SPOC", 777)]
    )
    assert tic == 777
    assert sectors == [100, 101]
    assert author == "SPOC"
