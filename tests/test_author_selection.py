"""Coverage for choosing which TESS reduction a search should run on."""

import pytest

from exohunt.cli import (
    AUTHOR_PREFERENCE,
    MIN_USEFUL_CADENCE_SECONDS,
    _preferred_exposure,
    resolve_light_curve_source,
)


class _Table:
    def __init__(self, exposures):
        self.colnames = ["exptime"] if exposures is not None else []
        if exposures is not None:
            self["exptime"] = exposures
        self._columns = {"exptime": exposures} if exposures is not None else {}

    def __setitem__(self, key, value):
        getattr(self, "_columns", {})[key] = value

    def __getitem__(self, key):
        return self._columns[key]


class _Search:
    def __init__(self, exposures):
        self._exposures = exposures or []
        self.table = _Table(exposures)

    def __len__(self):
        return len(self._exposures)


class _FakeLightkurve:
    """Stands in for the lightkurve module, recording what was asked for."""

    def __init__(self, available: dict[str, list[float]], failing: set[str] | None = None):
        self.available = available
        self.failing = failing or set()
        self.queries: list[str] = []

    def search_lightcurve(self, target, **kwargs):
        author = str(kwargs.get("author"))
        self.queries.append(author)
        if author in self.failing:
            raise RuntimeError("MAST is unavailable for this collection")
        return _Search(self.available.get(author, []))


def test_mission_processed_photometry_is_preferred() -> None:
    lk = _FakeLightkurve({"SPOC": [120.0], "QLP": [200.0]})

    resolved = resolve_light_curve_source(lk, "TIC 1", [100])

    assert resolved["author"] == "SPOC"
    assert resolved["cadence_seconds"] == 120.0
    assert resolved["fallback"] is False
    assert lk.queries == ["SPOC"]


def test_chain_falls_through_to_the_next_author() -> None:
    lk = _FakeLightkurve({"QLP": [600.0, 200.0]})

    resolved = resolve_light_curve_source(lk, "TIC 1", [100])

    assert resolved["author"] == "QLP"
    assert resolved["cadence_seconds"] == 200.0
    assert lk.queries == list(AUTHOR_PREFERENCE)
    assert resolved["considered"] == [
        {"author": "SPOC", "products": 0},
        {"author": "TESS-SPOC", "products": 0},
        {"author": "QLP", "products": 2},
    ]


def test_tesscut_is_only_the_last_resort() -> None:
    lk = _FakeLightkurve({})

    resolved = resolve_light_curve_source(lk, "TIC 1", [100])

    assert resolved["author"] == "TESScut"
    assert resolved["fallback"] is True
    assert resolved["cadence_seconds"] is None


def test_tesscut_fallback_can_be_refused() -> None:
    """A multi-sector request cannot fall back, because TESScut takes one."""

    lk = _FakeLightkurve({})

    with pytest.raises(RuntimeError, match="No processed TESS light curve"):
        resolve_light_curve_source(lk, "TIC 1", [100, 101], allow_tesscut=False)


def test_one_unavailable_collection_does_not_end_the_search() -> None:
    """A MAST outage for one author must not strand a target on TESScut."""

    lk = _FakeLightkurve({"QLP": [200.0]}, failing={"SPOC", "TESS-SPOC"})

    resolved = resolve_light_curve_source(lk, "TIC 1", [100])

    assert resolved["author"] == "QLP"
    assert resolved["fallback"] is False


def test_very_short_cadence_is_not_chosen_over_two_minute() -> None:
    """20-second sampling costs six times the data for no detection gain."""

    assert _preferred_exposure(_Search([20.0, 120.0, 1800.0])) == 120.0
    assert _preferred_exposure(_Search([20.0, 600.0])) == 600.0


def test_very_short_cadence_is_used_when_nothing_coarser_exists() -> None:
    assert _preferred_exposure(_Search([20.0])) == 20.0
    assert MIN_USEFUL_CADENCE_SECONDS > 20.0


def test_missing_exposure_column_is_tolerated() -> None:
    assert _preferred_exposure(_Search(None)) is None
