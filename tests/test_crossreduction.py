"""T7: independent reductions, the undetrended fold, and who may object."""

from __future__ import annotations

import pytest

from exohunt import crossreduction as t7
from exohunt.config import CrossReductionConfig


def _measurements(**overrides) -> list[t7.ReductionDepth]:
    base = [
        t7.ReductionDepth("spoc_pdcsap", 1000.0, 40.0),
        t7.ReductionDepth("qlp_sap", 1040.0, 50.0),
        t7.ReductionDepth("spoc_sap_undetrended", 980.0, 60.0, detrended=False),
    ]
    return overrides.get("measurements", base)


def _sectors() -> list[t7.SectorSupport]:
    return [
        t7.SectorSupport(10, detected=True, completeness=0.9),
        t7.SectorSupport(36, detected=True, completeness=0.8),
    ]


def _vetoes() -> t7.StackedVetoes:
    return t7.StackedVetoes(secondary_sigma=1.1, odd_even_sigma=0.7, sectors_stacked=2)


def test_a_clean_lead_is_promoted() -> None:
    decision = t7.evaluate(
        measurements=_measurements(), sectors=_sectors(), vetoes=_vetoes()
    )
    assert decision.promoted is True
    assert decision.status == "science_vetted_lead"
    assert decision.blocking == []


def test_one_reduction_is_a_statement_about_one_pipeline() -> None:
    decision = t7.evaluate(
        measurements=[
            t7.ReductionDepth("spoc_pdcsap", 1000.0, 40.0),
            t7.ReductionDepth("spoc_sap_undetrended", 980.0, 60.0, detrended=False),
        ],
        sectors=_sectors(),
        vetoes=_vetoes(),
    )
    assert decision.promoted is False
    assert any("independent reductions" in reason for reason in decision.blocking)


def test_disagreeing_reductions_block_promotion() -> None:
    decision = t7.evaluate(
        measurements=[
            t7.ReductionDepth("spoc_pdcsap", 1000.0, 20.0),
            t7.ReductionDepth("qlp_sap", 2000.0, 20.0),
            t7.ReductionDepth("spoc_sap_undetrended", 980.0, 60.0, detrended=False),
        ],
        sectors=_sectors(),
        vetoes=_vetoes(),
    )
    assert decision.promoted is False
    tension = decision.checks["depth_agreement"]["worst_tension_sigma"]
    assert tension > 3.0
    assert decision.checks["depth_agreement"]["disagreeing_pairs"]


def test_agreement_is_pairwise_not_against_a_pooled_mean() -> None:
    """A precise outlier must not drag a mean onto itself and then pass."""

    result = t7.depth_agreement(
        [
            t7.ReductionDepth("a", 1000.0, 100.0),
            t7.ReductionDepth("b", 1010.0, 100.0),
            t7.ReductionDepth("c", 2000.0, 1.0),
        ]
    )
    assert result["agrees"] is False
    pairs = {tuple(sorted(item["products"])) for item in result["disagreeing_pairs"]}
    assert ("a", "c") in pairs and ("b", "c") in pairs
    assert ("a", "b") not in pairs


def test_a_signal_absent_from_the_undetrended_fold_is_blocked() -> None:
    """The direct lesson of this project's history.

    Every detrended product inherits a dip the detrender invented, so their
    agreement says nothing about the sky.
    """

    decision = t7.evaluate(
        measurements=[
            t7.ReductionDepth("spoc_pdcsap", 1000.0, 40.0),
            t7.ReductionDepth("qlp_sap", 1040.0, 50.0),
            t7.ReductionDepth("spoc_sap_undetrended", 30.0, 60.0, detrended=False),
        ],
        sectors=_sectors(),
        vetoes=_vetoes(),
    )
    assert decision.promoted is False
    assert decision.checks["undetrended"]["present"] is False
    assert any("undetrended" in reason for reason in decision.blocking)


def test_a_missing_undetrended_measurement_is_not_a_pass() -> None:
    decision = t7.evaluate(
        measurements=[
            t7.ReductionDepth("spoc_pdcsap", 1000.0, 40.0),
            t7.ReductionDepth("qlp_sap", 1040.0, 50.0),
        ],
        sectors=_sectors(),
        vetoes=_vetoes(),
    )
    assert decision.promoted is False
    assert decision.checks["undetrended"]["measured"] is False


def test_an_insensitive_sector_abstains_instead_of_objecting() -> None:
    """The asymmetry that keeps real signals alive.

    A sector where injection says the signal was unlikely to be recovered
    carries no information when it does not see it. Counting that as evidence
    of absence is how a genuine detection gets thrown away.
    """

    decision = t7.evaluate(
        measurements=_measurements(),
        sectors=[
            t7.SectorSupport(10, detected=True, completeness=0.9),
            t7.SectorSupport(36, detected=False, completeness=0.2),
        ],
        vetoes=_vetoes(),
    )
    assert decision.promoted is True
    support = decision.checks["sector_support"]
    assert support["objecting_sectors"] == []
    assert [item["sector"] for item in support["abstaining_sectors"]] == [36]


def test_a_sensitive_sector_that_saw_nothing_does_object() -> None:
    decision = t7.evaluate(
        measurements=_measurements(),
        sectors=[
            t7.SectorSupport(10, detected=True, completeness=0.9),
            t7.SectorSupport(36, detected=False, completeness=0.95),
        ],
        vetoes=_vetoes(),
    )
    assert decision.promoted is False
    assert decision.checks["sector_support"]["objecting_sectors"] == [36]


def test_unknown_completeness_abstains_rather_than_assuming_sensitivity() -> None:
    result = t7.sector_support(
        [
            t7.SectorSupport(10, detected=True, completeness=0.9),
            t7.SectorSupport(36, detected=False, completeness=None),
        ]
    )
    assert result["objecting_sectors"] == []
    assert result["supported"] is True


def test_the_stacked_secondary_rule() -> None:
    """TIC 181014443: 2.3 sigma in one sector, 5.9 sigma stacked."""

    decision = t7.evaluate(
        measurements=_measurements(),
        sectors=_sectors(),
        vetoes=t7.StackedVetoes(
            secondary_sigma=5.9, odd_even_sigma=0.5, sectors_stacked=7
        ),
    )
    assert decision.promoted is False
    assert "stacked secondary eclipse" in decision.checks["stacked_vetoes"]["failures"]


def test_stacked_odd_even_also_blocks() -> None:
    decision = t7.evaluate(
        measurements=_measurements(),
        sectors=_sectors(),
        vetoes=t7.StackedVetoes(
            secondary_sigma=0.4, odd_even_sigma=4.2, sectors_stacked=5
        ),
    )
    assert decision.promoted is False
    assert (
        "stacked odd/even depth difference"
        in decision.checks["stacked_vetoes"]["failures"]
    )


def test_unmeasured_stacked_vetoes_block_promotion() -> None:
    decision = t7.evaluate(
        measurements=_measurements(), sectors=_sectors(), vetoes=None
    )
    assert decision.promoted is False
    assert decision.checks["stacked_vetoes"]["measured"] is False


def test_a_common_mode_kill_outranks_everything() -> None:
    """Sector coherence cannot clear a shared-ephemeris verdict.

    An observatory systematic repeats identically in every sector, so
    coherence is precisely what it looks like -- the evidence a promotion
    would lean on is the evidence against it.
    """

    for verdict in ("common_mode_systematic", "localized_coincidence"):
        decision = t7.evaluate(
            measurements=_measurements(),
            sectors=_sectors(),
            vetoes=_vetoes(),
            common_mode_verdict=verdict,
        )
        assert decision.promoted is False
        assert decision.status == verdict
        assert "outranks" in decision.blocking[0]
        # It short-circuits: no cross-reduction result is even computed, so
        # none of it can appear to have argued the other way.
        assert "depth_agreement" not in decision.checks


def test_promotion_never_emits_a_human_stage_status() -> None:
    """Section 4.7 applies here too."""

    from exohunt.adjudicate import _assert_automated_status

    for status in (t7.PROMOTED_STATUS, t7.WITHHELD_STATUS):
        _assert_automated_status(status)
    with pytest.raises(ValueError, match="human-stage"):
        _assert_automated_status("vetted_candidate")


def test_thresholds_are_configurable_and_serialisable() -> None:
    strict = CrossReductionConfig(depth_agreement_sigma=0.5)
    decision = t7.evaluate(
        measurements=_measurements(),
        sectors=_sectors(),
        vetoes=_vetoes(),
        config=strict,
    )
    assert decision.promoted is False

    import json

    payload = t7.evaluate(
        measurements=_measurements(), sectors=_sectors(), vetoes=_vetoes()
    ).to_dict()
    assert json.dumps(payload)
    assert payload["checks"]["policy_version"] == "t7-independent-reduction-v1"
