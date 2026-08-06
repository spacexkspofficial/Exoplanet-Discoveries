"""The P4 known-object regression suite (MASTER_PLAN.md section 4.1).

~500 real catalogued objects plus deliberate near-miss impostors, frozen from
real snapshot generations by ``scripts/build_p4_known_objects.py``. Every
identity/catalog change must resolve the whole suite before merge.

The suite is self-contained: each case carries the catalog row it was built
from, so this runs offline and does not depend on the operator's snapshot
directory. What it does depend on is the *code*, which is the point.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exohunt import adjudicate

SUITE_PATH = (
    Path(__file__).resolve().parents[1]
    / "results"
    / "p4"
    / "known_objects_v1"
    / "known_objects.json"
)

pytestmark = pytest.mark.skipif(
    not SUITE_PATH.is_file(),
    reason="known-object suite not built; run scripts/build_p4_known_objects.py",
)


def _suite() -> dict:
    return json.loads(SUITE_PATH.read_text(encoding="utf-8"))


def resolve(case: dict) -> dict:
    entry = case["catalog"]
    relation = adjudicate.relate(
        adjudicate.Candidate(tic_id=entry["tic_id"], **case["candidate"]),
        adjudicate.CatalogEphemeris(
            source=entry["source"],
            identifier=entry["identifier"],
            object_class=entry["object_class"],
            snapshot_hash=entry["snapshot_hash"],
            period_days=entry["period_days"],
            epoch_bjd=entry["epoch_bjd"],
            duration_hours=entry["duration_hours"],
            disposition=entry["disposition"],
            period_uncertainty_days=entry["period_uncertainty_days"],
            epoch_uncertainty_days=entry["epoch_uncertainty_days"],
        ),
    )
    return relation.to_dict()


def test_the_whole_suite_resolves_correctly() -> None:
    suite = _suite()
    failures: list[str] = []
    for case in suite["cases"]:
        actual = resolve(case)
        for key, expected in case["expected"].items():
            if actual.get(key) != expected:
                failures.append(
                    f"{case['case_id']} [{case['kind']}] {key}: "
                    f"expected {expected!r}, got {actual.get(key)!r}"
                )
    assert not failures, (
        f"{len(failures)} of {len(suite['cases'])} known-object cases "
        "regressed:\n" + "\n".join(failures[:25])
    )


def test_the_suite_contains_the_impostors_that_give_it_teeth() -> None:
    """A suite of true positives only proves the matcher says yes."""

    suite = _suite()
    kinds = suite["counts"]["by_kind"]
    assert kinds.get("impostor_phase_distinct", 0) >= 25
    assert kinds.get("impostor_period_detuned", 0) >= 25
    assert kinds.get("true_match", 0) >= 300
    assert suite["counts"]["cases"] >= 450

    # Every impostor must be a case the matcher is required *not* to kill.
    impostors = [
        case for case in suite["cases"] if case["kind"].startswith("impostor_")
    ]
    assert impostors
    assert all(case["expected"]["kills"] is False for case in impostors)
    # ...and every impostor sits on a genuinely catalogued star, so the only
    # thing separating it from a true match is the ephemeris.
    assert all(case["catalog"]["tic_id"] > 0 for case in impostors)


def test_every_case_cites_the_snapshot_generation_it_came_from() -> None:
    suite = _suite()
    hashes = set(suite["snapshot_hashes"].values())
    assert hashes
    assert suite["vetting_signature"].startswith("vet1:")
    for case in suite["cases"]:
        assert case["catalog"]["snapshot_hash"] in hashes
        assert case["rationale"]


def test_all_three_object_classes_are_represented() -> None:
    suite = _suite()
    classes = {case["catalog"]["object_class"] for case in suite["cases"]}
    assert classes == {"confirmed_planet", "toi", "eclipsing_binary"}


def test_the_suite_would_catch_a_loosened_epoch_rule() -> None:
    """The gate that proves this gate works.

    A matcher that ignores the epoch test passes every true match and every
    phase-distinct impostor as a kill. If the suite cannot see that, it is
    decoration.
    """

    suite = _suite()
    phase_impostors = [
        case
        for case in suite["cases"]
        if case["kind"] == "impostor_phase_distinct"
    ]
    assert phase_impostors

    # Simulate the pre-P4 rule: period agreement alone decides.
    from exohunt.evidence import _period_relation

    would_kill = 0
    for case in phase_impostors:
        legacy = _period_relation(
            case["candidate"]["period_days"], case["catalog"]["period_days"]
        )
        if legacy and legacy["matches"]:
            would_kill += 1
    assert would_kill == len(phase_impostors), (
        "every phase-distinct impostor should be a false 'known' under the "
        "period-only rule; that is the regression this suite guards"
    )
