"""T5 adjudication: period AND epoch, host versus signal, absence versus gap."""

from __future__ import annotations

import pytest

from exohunt import adjudicate
from exohunt.config import CURRENT_CONFIG, EphemerisMatchConfig

# A three-day candidate with a two-hour transit at BTJD 1500.
CANDIDATE = adjudicate.Candidate(
    tic_id=1234, period_days=3.0, duration_hours=2.0, epoch_btjd=1500.0
)


def _entry(**overrides) -> adjudicate.CatalogEphemeris:
    base = dict(
        source="tess_eb",
        identifier="EB-1",
        object_class="eclipsing_binary",
        snapshot_hash="snap-a",
        period_days=3.0,
        epoch_bjd=2457000.0 + 1500.0,
        duration_hours=2.0,
    )
    base.update(overrides)
    return adjudicate.CatalogEphemeris(**base)


def test_time_base_normalisation_is_explicit() -> None:
    assert adjudicate.to_btjd(2457000.0 + 1500.0) == pytest.approx(1500.0)
    # Already a BTJD: left alone.
    assert adjudicate.to_btjd(1500.0) == pytest.approx(1500.0)
    assert adjudicate.to_btjd(None) is None


def test_matching_period_and_epoch_is_a_signal_match_and_kills() -> None:
    relation = adjudicate.relate(CANDIDATE, _entry())
    assert relation.relation == adjudicate.RELATION_EXACT
    assert relation.epoch_verdict == adjudicate.EPOCH_AGREES
    assert relation.match_level == adjudicate.MATCH_SIGNAL
    assert relation.kills is True
    assert relation.phase_offset_days == pytest.approx(0.0)


def test_the_defect_this_module_exists_to_fix() -> None:
    """Same period, different phase: the old rule called this 'known'.

    ``evidence._period_relation`` accepts any 1% period agreement with no
    epoch test. At three days that is an extremely common coincidence, and
    calling it a rediscovery throws away a real new signal permanently.
    """

    # Half a period out of phase -- as far from agreement as it is possible
    # to be.
    phase_distinct = _entry(epoch_bjd=2457000.0 + 1501.5)
    relation = adjudicate.relate(CANDIDATE, phase_distinct)
    assert relation.relation == adjudicate.RELATION_EXACT
    assert relation.epoch_verdict == adjudicate.EPOCH_DISAGREES
    assert relation.match_level == adjudicate.MATCH_HOST
    assert relation.kills is False

    # The old rule, for contrast: period-only, and it would have killed.
    from exohunt.evidence import _period_relation

    legacy = _period_relation(CANDIDATE.period_days, 3.0)
    assert legacy is not None and legacy["matches"] is True


def test_a_catalog_row_with_no_epoch_can_only_be_period_only() -> None:
    relation = adjudicate.relate(CANDIDATE, _entry(epoch_bjd=None))
    assert relation.match_level == adjudicate.MATCH_PERIOD_ONLY
    assert relation.epoch_verdict == adjudicate.EPOCH_NO_CATALOG_EPOCH
    assert relation.kills is False


def test_a_candidate_with_no_epoch_cannot_be_phase_tested() -> None:
    candidate = adjudicate.Candidate(
        tic_id=1, period_days=3.0, duration_hours=2.0, epoch_btjd=None
    )
    relation = adjudicate.relate(candidate, _entry())
    assert relation.epoch_verdict == adjudicate.EPOCH_NO_CANDIDATE_EPOCH
    assert relation.match_level == adjudicate.MATCH_PERIOD_ONLY
    assert relation.kills is False


def test_an_ephemeris_too_old_to_predict_phase_is_not_a_disagreement() -> None:
    """Propagated period error can exceed the period itself.

    When it does, the catalog no longer says where the transit is. Reporting
    that as "the phases disagree" would be a fabricated negative.
    """

    ancient = _entry(epoch_bjd=2457000.0 - 3000.0, period_uncertainty_days=0.03)
    relation = adjudicate.relate(CANDIDATE, ancient)
    assert relation.epoch_verdict == adjudicate.EPOCH_UNCERTAINTY_TOO_LARGE
    assert relation.match_level == adjudicate.MATCH_PERIOD_ONLY
    assert relation.kills is False

    # A precisely-known ephemeris over the same baseline stays evaluable.
    precise = _entry(
        epoch_bjd=2457000.0 - 3000.0, period_uncertainty_days=1e-7
    )
    assert adjudicate.relate(CANDIDATE, precise).epoch_verdict in {
        adjudicate.EPOCH_AGREES,
        adjudicate.EPOCH_DISAGREES,
    }


def test_alias_relations_use_the_search_ladder() -> None:
    name, factor, error = adjudicate.period_relation(1.5, 3.0)
    assert name == "half_period_alias"
    assert factor == pytest.approx(0.5)
    assert error == pytest.approx(0.0, abs=1e-12)

    assert adjudicate.period_relation(6.0, 3.0)[0] == "double_period_alias"
    assert adjudicate.period_relation(1.0, 3.0)[0] == "one_third_period_alias"
    assert adjudicate.period_relation(4.2, 3.0)[0] == adjudicate.RELATION_NONE
    # The ladder is the search config's, not a second hard-coded list.
    for ratio in CURRENT_CONFIG.search.alias_ratios:
        assert adjudicate.period_relation(3.0 * ratio, 3.0)[0] != (
            adjudicate.RELATION_NONE
        )


def test_period_tolerance_is_configurable_and_bounded() -> None:
    slightly_off = 3.0 * 1.005
    assert adjudicate.period_relation(slightly_off, 3.0)[0] == (
        adjudicate.RELATION_EXACT
    )
    strict = EphemerisMatchConfig(period_tolerance_fraction=0.001)
    assert adjudicate.period_relation(slightly_off, 3.0, matching=strict)[0] == (
        adjudicate.RELATION_NONE
    )


def test_host_match_is_not_signal_match() -> None:
    unrelated = _entry(period_days=11.7)
    relation = adjudicate.relate(CANDIDATE, unrelated)
    assert relation.match_level == adjudicate.MATCH_HOST
    assert relation.kills is False
    assert "unrelated period" in relation.note

    host_only = _entry(period_days=None, host_only=True)
    assert adjudicate.relate(CANDIDATE, host_only).match_level == (
        adjudicate.MATCH_HOST
    )


def test_eb_signal_match_routes_to_the_eb_rediscovery_lane() -> None:
    result = adjudicate.adjudicate(
        CANDIDATE, [_entry()], consulted_sources=["tess_eb", "nasa_toi"]
    )
    assert result.recommended_status == "known_eb_rediscovery"
    assert result.blocked_reason is None
    assert [item.match_level for item in result.killing_relations()] == [
        adjudicate.MATCH_SIGNAL
    ]


def test_eb_host_with_a_different_period_routes_to_residual_review() -> None:
    result = adjudicate.adjudicate(
        CANDIDATE,
        [_entry(period_days=11.7)],
        consulted_sources=["tess_eb"],
    )
    assert result.recommended_status == "known_eb_host_residual_review"
    assert result.killing_relations() == []


def test_a_planet_rediscovery_refuses_to_invent_an_automated_status() -> None:
    """Section 4.7: no automated writer may emit a human-stage status."""

    planet = _entry(
        source="nasa_ps",
        identifier="TOI-1234 b",
        object_class="confirmed_planet",
    )
    result = adjudicate.adjudicate(
        CANDIDATE, [planet], consulted_sources=["nasa_ps"]
    )
    assert result.recommended_status is None
    assert result.blocked_reason and "human-stage" in result.blocked_reason
    assert result.killing_relations()[0].match_level == adjudicate.MATCH_SIGNAL


def test_human_stage_statuses_are_rejected_at_the_boundary() -> None:
    for slug in ("rediscovery", "known_tce_rediscovery", "false_positive",
                 "vetted_candidate", "confirmed_planet"):
        with pytest.raises(ValueError, match="human-stage"):
            adjudicate._assert_automated_status(slug)
    with pytest.raises(ValueError, match="not in the status registry"):
        adjudicate._assert_automated_status("not_a_status")
    # Everything the module can actually route to must survive this check.
    for slug in adjudicate._AUTOMATED_STATUS.values():
        adjudicate._assert_automated_status(slug)


def test_nothing_found_is_unresolved_when_sources_were_consulted() -> None:
    result = adjudicate.adjudicate(
        CANDIDATE, [], consulted_sources=["nasa_ps", "nasa_toi", "tess_eb"]
    )
    assert result.recommended_status == "unresolved_transit_like_signal"
    assert result.coverage_gaps == []


def test_nothing_found_is_a_coverage_gap_when_nothing_was_consulted() -> None:
    result = adjudicate.adjudicate(
        CANDIDATE, [], consulted_sources=[], coverage_gaps=["vsx", "gaia_dr3"]
    )
    assert result.recommended_status == "catalog_coverage_gap"
    assert result.coverage_gaps == ["gaia_dr3", "vsx"]


def test_disagreeing_sources_never_auto_resolve() -> None:
    planet = _entry(
        source="nasa_ps", identifier="TOI-1 b", object_class="confirmed_planet"
    )
    result = adjudicate.adjudicate(
        CANDIDATE, [planet, _entry()], consulted_sources=["nasa_ps", "tess_eb"]
    )
    assert result.conflicts
    conflict = result.conflicts[0]
    assert conflict["kind"] == "disagreeing_object_classes"
    assert conflict["object_classes"] == ["confirmed_planet", "eclipsing_binary"]
    # Both claims survive in the record.
    assert len(result.killing_relations()) == 2


def test_snapshot_rows_adapt_with_worst_case_uncertainties() -> None:
    rows = [
        {
            "toi": "101.01",
            "pl_orbper": "3.0",
            "pl_tranmid": "2458500.0",
            "pl_trandurh": "2.0",
            "tfopwg_disp": "PC",
            "pl_orbpererr1": "0.001",
            "pl_orbpererr2": "-0.004",
            "pl_tranmiderr1": "0.002",
            "pl_tranmiderr2": "-0.001",
        },
        {"toi": "102.01", "pl_orbper": "", "pl_tranmid": ""},
    ]
    entries = adjudicate.catalog_entries_from_snapshot_rows(
        rows,
        source="nasa_toi",
        object_class="toi",
        snapshot_hash="snap-b",
        identifier_key="toi",
        period_key="pl_orbper",
        epoch_key="pl_tranmid",
        duration_key="pl_trandurh",
        disposition_key="tfopwg_disp",
        period_error_keys=("pl_orbpererr1", "pl_orbpererr2"),
        epoch_error_keys=("pl_tranmiderr1", "pl_tranmiderr2"),
    )
    assert len(entries) == 2
    first = entries[0]
    assert first.identifier == "101.01"
    assert first.disposition == "PC"
    # The larger magnitude of the asymmetric pair wins.
    assert first.period_uncertainty_days == pytest.approx(0.004)
    assert first.epoch_uncertainty_days == pytest.approx(0.002)
    # A row with no ephemeris becomes a host-level fact, not a signal.
    assert entries[1].host_only is True
    assert adjudicate.relate(CANDIDATE, entries[1]).match_level == (
        adjudicate.MATCH_HOST
    )


def test_adjudication_serialises_for_the_evidence_record() -> None:
    result = adjudicate.adjudicate(
        CANDIDATE, [_entry()], consulted_sources=["tess_eb"]
    )
    payload = result.to_dict()
    assert payload["tic_id"] == CANDIDATE.tic_id
    assert payload["relations"][0]["snapshot_hash"] == "snap-a"
    # Every relation names the generation it was adjudicated against.
    assert all(item["snapshot_hash"] for item in payload["relations"])
