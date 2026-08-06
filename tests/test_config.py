"""The frozen science configuration and the scientific signature."""

from __future__ import annotations

import dataclasses

import pytest

from exohunt.config import (
    CURRENT_CONFIG,
    ScienceConfig,
    SearchConfig,
    hash_target_list,
    scientific_signature,
    settings_signature,
)


def test_config_hash_is_deterministic_and_sensitive() -> None:
    assert ScienceConfig().config_hash() == CURRENT_CONFIG.config_hash()
    changed = dataclasses.replace(
        CURRENT_CONFIG,
        search=dataclasses.replace(CURRENT_CONFIG.search, sde_min_multisector=9.5),
    )
    assert changed.config_hash() != CURRENT_CONFIG.config_hash()


def test_config_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        CURRENT_CONFIG.search.sde_min_multisector = 1.0  # type: ignore[misc]


def test_signature_changes_with_every_input(tmp_path) -> None:
    targets = tmp_path / "targets.csv"
    targets.write_text("tic_id\n1\n2\n", encoding="utf-8")
    list_hash = hash_target_list(targets)
    base = scientific_signature(
        code="git:abc",
        config=CURRENT_CONFIG,
        product_family="SPOC-120s",
        target_list_hash=list_hash,
    )
    assert base.startswith("sig1:")
    assert (
        scientific_signature(
            code="git:def",
            config=CURRENT_CONFIG,
            product_family="SPOC-120s",
            target_list_hash=list_hash,
        )
        != base
    )
    assert (
        scientific_signature(
            code="git:abc",
            config=CURRENT_CONFIG,
            product_family="QLP",
            target_list_hash=list_hash,
        )
        != base
    )
    changed_config = dataclasses.replace(
        CURRENT_CONFIG,
        search=dataclasses.replace(CURRENT_CONFIG.search, min_period_days=0.6),
    )
    assert (
        scientific_signature(
            code="git:abc",
            config=changed_config,
            product_family="SPOC-120s",
            target_list_hash=list_hash,
        )
        != base
    )


def test_target_list_hash_tracks_content(tmp_path) -> None:
    first = tmp_path / "a.csv"
    second = tmp_path / "b.csv"
    first.write_text("tic_id\n1\n", encoding="utf-8")
    second.write_text("tic_id\n1\n", encoding="utf-8")
    assert hash_target_list(first) == hash_target_list(second)
    second.write_text("tic_id\n2\n", encoding="utf-8")
    assert hash_target_list(first) != hash_target_list(second)


def test_settings_signature_is_canonical_and_sensitive() -> None:
    first = settings_signature(
        code="git:abc",
        settings={"period": [0.5, 20.0], "author": "SPOC"},
        product_family="SPOC-120s",
        target_list_hash="targets",
    )
    reordered = settings_signature(
        code="git:abc",
        settings={"author": "SPOC", "period": [0.5, 20.0]},
        product_family="SPOC-120s",
        target_list_hash="targets",
    )
    changed = settings_signature(
        code="git:abc",
        settings={"author": "SPOC", "period": [0.5, 19.0]},
        product_family="SPOC-120s",
        target_list_hash="targets",
    )
    assert first == reordered
    assert first != changed


def test_search_config_documents_the_alias_ladder() -> None:
    ratios = SearchConfig().alias_ratios
    assert 0.5 in ratios and 2.0 in ratios and 1.0 in ratios
    assert ratios == tuple(sorted(ratios))
