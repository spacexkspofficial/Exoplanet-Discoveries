"""The frozen science configuration and the scientific signature."""

from __future__ import annotations

import dataclasses

import pytest

from exohunt.config import (
    CURRENT_CONFIG,
    CURRENT_IDENTITY,
    IdentityConfig,
    ScienceConfig,
    SearchConfig,
    hash_target_list,
    match_radius_arcsec,
    module_digest,
    scientific_signature,
    settings_signature,
    vetting_signature,
)

# The configuration digest the current detection identity hashes to.
# `results/p3/release_report.json` stores `trusted_release` for the signature
# built over the *previous* hash at git:36c935b, so a change here retires that
# release whether or not anyone noticed. P4 added a whole vetting layer without
# moving this pin, which is what proved the layer was added *beside* the
# detection identity rather than inside it.
#
# MOVED DELIBERATELY, 2026-08-09, owner decisions 1 and 2b (the kernel may be
# modified). The P3-certified value was:
#   dcdb2bf009a1667246d69b87af533af590befbcece8648623592990d18cd1594
# `SearchConfig` gained the near-tie peak-selection parameters (decision 1),
# `veto_spacecraft_harmonic` (decision 2b), and its `policy_version` went
# v3 -> v4-neartie, so the detection identity genuinely changed and the digest
# must follow. This is the one case the pin is *not* guarding against: an
# intended, owner-approved change to the search itself. Both changes are
# batched into a single re-calibration on purpose -- each one separately would
# cost another ~16.5 h per 1,000-star cohort.
#
# Consequence, and it is not cosmetic: **there is currently no passing release
# report for this signature**, so `--trusted-first-pass` is unsatisfiable until
# decision 5B's re-calibration completes and certifies it. Runs remain
# diagnostic in the meantime. Do not restore the old value to make a campaign
# start -- that would re-point a stored certification at code it never
# measured.
P3_CERTIFIED_CONFIG_HASH = (
    "2697cd21849202e9eafb8f12a42ecbf633be28dc1cbf8920ef7d805214e83124"
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


def test_p4_vetting_layer_does_not_move_the_p3_certified_identity() -> None:
    """The P3 release is keyed to a digest over ScienceConfig alone.

    Adding a vetting parameter to ScienceConfig would invalidate the stored
    trusted release silently -- the campaign path would simply start refusing
    `--trusted-first-pass` with a signature nobody recognised.
    """

    assert ScienceConfig().config_hash() == P3_CERTIFIED_CONFIG_HASH
    assert "identity" not in {
        field.name for field in dataclasses.fields(ScienceConfig)
    }


def test_vetting_signature_names_its_snapshot_generation() -> None:
    base = vetting_signature(
        code="modules:abc",
        identity=CURRENT_IDENTITY,
        snapshots={"nasa_toi": "hash-1", "nasa_ps": "hash-2"},
    )
    assert base.startswith("vet1:")
    # Key order must not matter; the snapshot generation must.
    assert base == vetting_signature(
        code="modules:abc",
        identity=CURRENT_IDENTITY,
        snapshots={"nasa_ps": "hash-2", "nasa_toi": "hash-1"},
    )
    assert base != vetting_signature(
        code="modules:abc",
        identity=CURRENT_IDENTITY,
        snapshots={"nasa_toi": "hash-1", "nasa_ps": "hash-CHANGED"},
    )
    # A source that was not consulted is not the same as one that was.
    assert base != vetting_signature(
        code="modules:abc",
        identity=CURRENT_IDENTITY,
        snapshots={"nasa_toi": "hash-1"},
    )
    assert base != vetting_signature(
        code="modules:abc",
        identity=dataclasses.replace(CURRENT_IDENTITY, match_radius_pixels=2.0),
        snapshots={"nasa_toi": "hash-1", "nasa_ps": "hash-2"},
    )


def test_the_kernel_version_covers_every_module_that_moves_a_result() -> None:
    """A release must survive a README edit and never survive a search change.

    `code_version` gets that backwards -- it digests the whole repository, and
    retired P3's trusted release on two documentation commits (correction 39).
    """

    from exohunt.config import DETECTION_KERNEL_MODULES, kernel_version

    assert kernel_version().startswith("kernel1:")
    assert kernel_version() == kernel_version()

    # cli.py is load-bearing here: P2's decomposition is unfinished and the
    # single-target analysis path still lives in it, so a change there is a
    # change to the science whatever the file is nominally about.
    for module in ("cli.py", "search.py", "vetoes.py", "detrend.py",
                   "detection.py", "photometry.py", "population.py",
                   "campaign.py", "config.py"):
        assert module in DETECTION_KERNEL_MODULES

    # Nothing that cannot alter a detection belongs in it.
    for module in ("dashboard.py", "dashboard_api.py", "dashboard_server.py",
                   "packet.py", "identity.py", "snapshots.py", "ledger.py"):
        assert module not in DETECTION_KERNEL_MODULES


def test_module_digest_tracks_only_the_named_modules() -> None:
    one = module_digest("identity.py")
    assert one == module_digest("identity.py")
    assert one != module_digest("identity.py", "snapshots.py")
    assert module_digest("identity.py", "snapshots.py") == module_digest(
        "snapshots.py", "identity.py"
    )
    with pytest.raises(FileNotFoundError):
        module_digest("no_such_module.py")
    with pytest.raises(ValueError):
        module_digest()


def test_match_radius_is_derived_from_the_pixel_scale() -> None:
    assert match_radius_arcsec() == pytest.approx(
        CURRENT_IDENTITY.match_radius_pixels
        * CURRENT_CONFIG.instrument.pixel_scale_arcsec
    )
    assert match_radius_arcsec(
        IdentityConfig(match_radius_pixels=2.0)
    ) == pytest.approx(2.0 * CURRENT_CONFIG.instrument.pixel_scale_arcsec)
