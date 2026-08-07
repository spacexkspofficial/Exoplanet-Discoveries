"""The review packet, and the claim ceilings it must never cross."""

from __future__ import annotations

import json

import pytest

from exohunt import packet as pk


def _sections(**overrides) -> dict:
    sections = {
        "ephemeris": {"period_days": 3.1, "epoch_btjd": 1500.0, "duration_hours": 2.4},
        "fit_posteriors": {"rp_rs": 0.031, "b": 0.4, "state": "measured"},
        "t3_gates": {"odd_even_sigma": 0.7, "secondary_sigma": 1.1},
        "population_screen": {"verdict": "independent_timing"},
        "catalog_adjudication": {
            "relations": [],
            "snapshot_hashes": {"nasa_toi": "abc"},
            "verdict": "unresolved_transit_like_signal",
        },
        "pixel_localization": {
            "offset_pixels": 0.12,
            "significance": 0.8,
            "verdict": "consistent_with_on_target",
        },
        "multi_reduction_depths": [
            {"product": "spoc_pdcsap", "depth_ppm": 1000.0},
            {"product": "qlp_sap", "depth_ppm": 1040.0},
        ],
        "completeness": {"period_days": 3.1, "depth_ppm": 1000.0, "value": 0.62},
        "false_positive_probability": {"fpp": 0.004, "nfpp": 2e-4, "state": "measured"},
        "provenance": pk.provenance_block(
            scientific_signature="sig1:abc",
            vetting_signature="vet1:def",
            snapshot_hashes={"nasa_toi": "abc"},
            product_versions={"spoc": "v1"},
            code_version="modules:xyz",
        ),
    }
    sections.update(overrides)
    return sections


def test_a_complete_packet_is_ready() -> None:
    result = pk.assemble(1234, _sections())
    assert result.ready is True
    assert result.status == "packet_ready_for_review"
    assert result.missing_sections == []
    assert result.unmeasured_sections == []
    assert result.content_hash


def test_every_section_the_plan_names_is_required() -> None:
    assert set(pk.REQUIRED_SECTIONS) == {
        "ephemeris",
        "fit_posteriors",
        "t3_gates",
        "population_screen",
        "catalog_adjudication",
        "pixel_localization",
        "multi_reduction_depths",
        "completeness",
        "false_positive_probability",
        "provenance",
    }


def test_a_missing_section_is_named_not_papered_over() -> None:
    """A packet missing localization must not read like one that passed it."""

    sections = _sections()
    del sections["pixel_localization"]
    result = pk.assemble(1234, sections)
    assert result.ready is False
    assert result.status is None
    assert result.missing_sections == ["pixel_localization"]


def test_an_unmeasured_section_is_absence_not_a_result() -> None:
    """`not_run` is not an answer, and neither is an empty dict."""

    for value in ({"state": "not_run"}, {}, None, {"verdict": "not_evaluable"}):
        result = pk.assemble(1234, _sections(false_positive_probability=value))
        assert result.ready is False
        assert result.unmeasured_sections == ["false_positive_probability"]
        assert result.status is None


def test_a_deferred_fpp_blocks_the_packet_rather_than_being_assumed() -> None:
    """TRICERATOPS is deferred by owner decision, so FPP is `not_run`.

    That must stop a packet being called ready, not be silently treated as a
    pass -- the whole point of the section list is that a reader can trust
    every named section was actually measured.
    """

    result = pk.assemble(
        1234, _sections(false_positive_probability={"state": "not_run"})
    )
    assert result.ready is False
    assert "false_positive_probability" in result.unmeasured_sections


def test_negative_results_still_count_as_measured() -> None:
    """A section may say "we looked and it failed"; that is a result."""

    result = pk.assemble(
        1234,
        _sections(
            pixel_localization={"offset_pixels": 2.4, "verdict": "off_target"}
        ),
    )
    assert result.ready is True
    assert result.sections["pixel_localization"]["verdict"] == "off_target"


def test_provenance_without_signatures_is_not_measured() -> None:
    block = pk.provenance_block(
        scientific_signature=None,
        vetting_signature=None,
        snapshot_hashes={},
        product_versions={},
        code_version="modules:xyz",
    )
    assert block["state"] == "not_run"
    assert pk.assemble(1234, _sections(provenance=block)).ready is False


def test_provenance_carries_the_chain_that_makes_numbers_re_derivable() -> None:
    block = pk.provenance_block(
        scientific_signature="sig1:abc",
        vetting_signature="vet1:def",
        snapshot_hashes={"b": "2", "a": "1"},
        product_versions={"spoc": "v1"},
        code_version="modules:xyz",
    )
    assert block["snapshot_hashes"] == {"a": "1", "b": "2"}
    assert block["state"] == "measured"


def test_the_content_hash_tracks_the_content() -> None:
    first = pk.assemble(1234, _sections())
    same = pk.assemble(1234, _sections())
    changed = pk.assemble(
        1234, _sections(completeness={"period_days": 3.1, "value": 0.10})
    )
    assert first.content_hash == same.content_hash
    assert first.content_hash != changed.content_hash


def test_no_code_path_here_can_claim_a_candidate_or_a_planet() -> None:
    """Section 4.7's ceiling, asserted structurally rather than by review."""

    from exohunt.adjudicate import _assert_automated_status

    source = (
        __import__("pathlib").Path(pk.__file__).read_text(encoding="utf-8")
    )
    # The forbidden slugs appear only in the FORBIDDEN_CLAIMS tuple and in
    # prose, never as a value this module assigns to a status.
    assert 'status = "vetted_candidate"' not in source
    assert 'status = "confirmed_planet"' not in source
    for slug in pk.FORBIDDEN_CLAIMS:
        with pytest.raises(ValueError, match="human-stage"):
            _assert_automated_status(slug)

    # And the only status it can produce survives the automated-writer check.
    _assert_automated_status(pk.PACKET_STATUS)


def test_the_ceiling_note_travels_with_the_packet() -> None:
    note = pk.claim_ceiling_note()
    for phrase in ("not a vetted candidate", "not a validated planet", "human"):
        assert phrase in note
    assert note in pk.summarize([])["claim_ceiling"]


def test_the_summary_reports_why_packets_fell_short() -> None:
    complete = pk.assemble(1, _sections())
    no_pixels = _sections()
    del no_pixels["pixel_localization"]
    incomplete = pk.assemble(2, no_pixels)
    deferred = pk.assemble(3, _sections(false_positive_probability={"state": "not_run"}))

    summary = pk.summarize([complete, incomplete, deferred])
    assert summary["packets"] == 3
    assert summary["ready"] == 1
    assert summary["incomplete"] == 2
    assert summary["blocking_sections"]["missing:pixel_localization"] == 1
    assert summary["blocking_sections"]["unmeasured:false_positive_probability"] == 1


def test_packet_serialises_for_the_evidence_record() -> None:
    payload = pk.assemble(1234, _sections()).to_dict()
    assert json.dumps(payload, default=str)
    assert payload["schema_version"] == pk.PACKET_SCHEMA_VERSION
    assert payload["status"] == "packet_ready_for_review"
