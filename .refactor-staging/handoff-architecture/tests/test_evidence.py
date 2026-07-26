from pathlib import Path

import exohunt.catalogs as catalogs
import exohunt.evidence as evidence
from exohunt.evidence import (
    classify_context_evidence,
    query_tess_eb_catalog,
)


def test_tess_eb_catalog_parser_returns_exact_tic_period(monkeypatch) -> None:
    html = """
    <table class="catalog-table">
      <thead><tr>
        <th>In catalog</th><th>TESS ID</th><th>Sectors</th>
        <th>t0 [BJD]</th><th>P0 [days]</th><th>&sigma;(P0) [days]</th>
        <th>Morphology</th><th>Source</th><th>Flags</th>
      </tr></thead>
      <tbody><tr>
        <td>True</td><td>0303427297</td><td>10, 36, 37, 63, 64</td>
        <td>1569.945715</td><td>4.2973723</td><td>0.0000983</td>
        <td>0.193</td><td>LCF</td><td></td>
      </tr></tbody>
    </table>
    """
    monkeypatch.setattr(evidence, "_read_text", lambda *_args, **_kwargs: html)

    result = query_tess_eb_catalog(303427297)

    assert result["match"] is True
    assert result["rows"][0]["period_days"] == 4.2973723
    assert result["rows"][0]["sectors"] == [10, 36, 37, 63, 64]


def test_context_classification_routes_period_matched_eb_out_of_survivors() -> None:
    result = classify_context_evidence(
        candidate_period_days=4.295591,
        nasa_catalog={"tois": [], "confirmed_planets": []},
        evidence={
            "tess_eclipsing_binary_catalog": {
                "status": "completed",
                "rows": [{"period_days": 4.2973723}],
            },
            "simbad": {"status": "completed", "object_types": ["*"]},
            "gaia_dr3": {"status": "completed", "rows": []},
            "tess_tce": {"status": "not_available", "matching_tces": []},
        },
        neighbors={"crowding_risk": "low"},
    )

    assert result["disposition"] == "known_eb_rediscovery"
    assert result["followup_lane"] == "stellar_eclipse_or_etv_followup"
    assert result["planet_candidate"] is False
    assert result["exact_period_matches"][0]["object_class"] == (
        "known_eclipsing_binary"
    )


def test_known_eb_host_with_different_period_keeps_circumbinary_review_lane() -> None:
    result = classify_context_evidence(
        candidate_period_days=11.0,
        nasa_catalog={"tois": [], "confirmed_planets": []},
        evidence={
            "tess_eclipsing_binary_catalog": {
                "status": "completed",
                "rows": [{"period_days": 4.3}],
            },
            "simbad": {"status": "completed", "object_types": ["EB*"]},
            "gaia_dr3": {"status": "completed", "rows": []},
            "tess_tce": {"status": "completed", "matching_tces": []},
        },
        neighbors={"crowding_risk": "none_detected"},
    )

    assert result["disposition"] == "known_eb_host_residual_review"
    assert result["known_binary_host"] is True
    assert "circumbinary" in result["followup_lane"]


def test_nasa_catalog_lookup_is_cached_and_rate_safe(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []

    def fake_tap(query: str):
        calls.append(query)
        return []

    monkeypatch.setattr(catalogs, "_tap_csv", fake_tap)
    first = catalogs.check_tic(42, cache_dir=tmp_path)
    second = catalogs.check_tic(42, cache_dir=tmp_path)

    assert first == second
    assert len(calls) == 2
    assert (tmp_path / "TIC_42.json").exists()


def test_nasa_catalog_lookup_uses_gaia_aliases_and_refreshes_old_cache(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []

    def fake_tap(query: str):
        calls.append(query)
        return []

    monkeypatch.setattr(catalogs, "_tap_csv", fake_tap)
    catalogs.check_tic(42, cache_dir=tmp_path)
    result = catalogs.check_tic(
        42,
        gaia_source_id=5672082455621978112,
        cache_dir=tmp_path,
    )
    reused = catalogs.check_tic(
        42,
        gaia_source_id=5672082455621978112,
        cache_dir=tmp_path,
    )

    assert result == reused
    assert len(calls) == 4
    confirmed_query = calls[-1]
    assert "tic_id='TIC 42'" in confirmed_query
    assert (
        "gaia_dr2_id='Gaia DR2 5672082455621978112'"
        in confirmed_query
    )
    assert (
        "gaia_dr3_id='Gaia DR3 5672082455621978112'"
        in confirmed_query
    )
    assert result["query_identifiers"] == {
        "tic_id": 42,
        "gaia_source_id": 5672082455621978112,
    }
