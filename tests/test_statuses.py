"""Contract tests for the canonical classification registry."""

from __future__ import annotations

from exohunt.status_codegen import generated_path, render_typescript
from exohunt.statuses import (
    COMMON_MODE_LABELS,
    CONTEXT_LABELS,
    SCIENCE_LABELS,
    SCREENING_LABELS,
    STATUS_DEFINITIONS,
    STATUS_REGISTRY,
    EvidenceStage,
    resolve_status,
)


def test_registry_is_complete_and_frontend_generation_cannot_drift() -> None:
    # 23 at P1, plus `packet_ready_for_review` added for P4's section 4.7
    # packet, plus `search_artifact_rejected` added for owner decision 2
    # (promoting the three computed-but-unused artifact flags to vetoes).
    # Appendix C makes registry evolution additive and requires the existing
    # slugs to be kept, so this count is raised deliberately rather than
    # relaxed -- a status appearing without this line changing is the drift
    # the pin exists to catch.
    assert len(STATUS_DEFINITIONS) == 25
    assert len(STATUS_REGISTRY) == len(STATUS_DEFINITIONS)
    assert generated_path().read_text(encoding="utf-8") == render_typescript()


def test_registry_preserves_the_exporters_existing_labels() -> None:
    assert SCREENING_LABELS == {
        "searched": "Searched - awaiting classification",
        "no_transit_detected": "No transit detected in search window",
        "screened_rejected": "Strongest signal screened out",
        "search_error": "Search error - retry needed",
        "single_event_lead": "Single-event lead - longer baseline needed",
        "automated_survivor": "Automated survivor - deeper vetting needed",
    }
    assert CONTEXT_LABELS == {
        "catalog_coverage_gap": "Public-catalog coverage gap",
        "context_incomplete": "Context checks incomplete - retry needed",
        "crowding_contamination_review": "Crowding/contamination review",
        "known_variable_star_review": "Known variable star - signal review",
        "known_eb_host_residual_review": "Known binary host - residual review",
        "known_eb_rediscovery": "Known eclipsing-binary rediscovery",
        "unresolved_transit_like_signal": "Unresolved transit-like signal",
    }
    assert SCIENCE_LABELS == {
        "pixel_offset_contamination": "Lost light localized off target",
        "single_sector_unconfirmed": "On target - single supporting sector",
        "science_vetted_lead": "On target and multi-sector coherent",
        "packet_ready_for_review": "Review packet assembled",
    }
    assert COMMON_MODE_LABELS == {
        "common_mode_systematic": "Observatory systematic - shared ephemeris",
        "localized_coincidence": "Shared ephemeris with close neighbours",
        "search_artifact_rejected": (
            "Search artifact - fit sits on an instrument or grid rail"
        ),
    }


def test_no_automated_status_can_override_any_human_outcome() -> None:
    automated = [
        definition.slug
        for definition in STATUS_DEFINITIONS
        if definition.evidence_stage is not EvidenceStage.HUMAN_OUTCOME
    ]
    human = [
        definition.slug
        for definition in STATUS_DEFINITIONS
        if definition.evidence_stage is EvidenceStage.HUMAN_OUTCOME
    ]

    for automated_status in automated:
        for human_status in human:
            assert (
                resolve_status([automated_status, human_status]) == human_status
            )
            assert (
                resolve_status([human_status, automated_status]) == human_status
            )


def test_stage_authority_is_explicit_and_independent_of_input_order() -> None:
    assert (
        resolve_status(["common_mode_systematic", "science_vetted_lead"])
        == "common_mode_systematic"
    )
    assert (
        resolve_status(["science_vetted_lead", "common_mode_systematic"])
        == "common_mode_systematic"
    )
    assert (
        resolve_status(["unresolved_transit_like_signal", "automated_survivor"])
        == "unresolved_transit_like_signal"
    )


def test_within_stage_precedence_and_equal_rank_keep_legacy_semantics() -> None:
    assert (
        resolve_status(
            ["known_eb_host_residual_review", "known_eb_rediscovery"]
        )
        == "known_eb_rediscovery"
    )
    assert (
        resolve_status(["known_eb_rediscovery", "catalog_coverage_gap"])
        == "known_eb_rediscovery"
    )
    assert resolve_status(["false_positive", "rediscovery"]) == "rediscovery"
