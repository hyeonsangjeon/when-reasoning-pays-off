"""Tests for the release-tier enum and publication-target matrix (Task 032)."""

from __future__ import annotations

import pytest

from batch_runner.release.tiers import (
    PublicationNotAllowedError,
    PublicationTarget,
    Tier,
    allowed_targets,
    assert_publishable,
    is_publishable,
)


def test_tier_has_exactly_three_values():
    assert {t.value for t in Tier} == {
        "RAW_PRIVATE",
        "SANITIZED_PUBLIC",
        "AGGREGATE_AZURE_SAMPLE",
    }


def test_tier_values_are_stable_strings():
    # Stable identifiers — used in serialized JSON sidecars and CI greps.
    assert Tier.RAW_PRIVATE.value == "RAW_PRIVATE"
    assert Tier.SANITIZED_PUBLIC.value == "SANITIZED_PUBLIC"
    assert Tier.AGGREGATE_AZURE_SAMPLE.value == "AGGREGATE_AZURE_SAMPLE"


def test_publication_target_has_three_role_values():
    assert {t.value for t in PublicationTarget} == {
        "PRIVATE_ARCHIVE",
        "PUBLIC_RESEARCH_REPO",
        "FOUNDRY_SAMPLE_REPO",
    }


@pytest.mark.parametrize(
    "tier,allowed",
    [
        (Tier.RAW_PRIVATE, {PublicationTarget.PRIVATE_ARCHIVE}),
        (
            Tier.SANITIZED_PUBLIC,
            {
                PublicationTarget.PRIVATE_ARCHIVE,
                PublicationTarget.PUBLIC_RESEARCH_REPO,
            },
        ),
        (
            Tier.AGGREGATE_AZURE_SAMPLE,
            {
                PublicationTarget.PRIVATE_ARCHIVE,
                PublicationTarget.PUBLIC_RESEARCH_REPO,
                PublicationTarget.FOUNDRY_SAMPLE_REPO,
            },
        ),
    ],
)
def test_allowed_targets_matrix(tier, allowed):
    assert allowed_targets(tier) == frozenset(allowed)


def test_raw_private_blocked_on_every_public_target():
    for target in (
        PublicationTarget.PUBLIC_RESEARCH_REPO,
        PublicationTarget.FOUNDRY_SAMPLE_REPO,
    ):
        assert not is_publishable(Tier.RAW_PRIVATE, target)
        with pytest.raises(PublicationNotAllowedError):
            assert_publishable(Tier.RAW_PRIVATE, target)


def test_sanitized_public_blocked_on_foundry_sample_repo():
    # Tier 2 is per-request; Foundry sample is aggregate-only.
    assert not is_publishable(
        Tier.SANITIZED_PUBLIC, PublicationTarget.FOUNDRY_SAMPLE_REPO
    )
    with pytest.raises(PublicationNotAllowedError):
        assert_publishable(
            Tier.SANITIZED_PUBLIC, PublicationTarget.FOUNDRY_SAMPLE_REPO
        )


def test_sanitized_public_allowed_on_public_research_repo():
    assert is_publishable(
        Tier.SANITIZED_PUBLIC, PublicationTarget.PUBLIC_RESEARCH_REPO
    )
    assert_publishable(
        Tier.SANITIZED_PUBLIC, PublicationTarget.PUBLIC_RESEARCH_REPO
    )


def test_aggregate_allowed_on_both_public_targets():
    for target in (
        PublicationTarget.PUBLIC_RESEARCH_REPO,
        PublicationTarget.FOUNDRY_SAMPLE_REPO,
    ):
        assert is_publishable(Tier.AGGREGATE_AZURE_SAMPLE, target)
        assert_publishable(Tier.AGGREGATE_AZURE_SAMPLE, target)


def test_every_tier_is_publishable_to_private_archive():
    for tier in Tier:
        assert is_publishable(tier, PublicationTarget.PRIVATE_ARCHIVE)


def test_assert_publishable_error_names_the_tier_and_target():
    with pytest.raises(PublicationNotAllowedError) as excinfo:
        assert_publishable(
            Tier.RAW_PRIVATE, PublicationTarget.PUBLIC_RESEARCH_REPO
        )
    msg = str(excinfo.value)
    assert "RAW_PRIVATE" in msg
    assert "PUBLIC_RESEARCH_REPO" in msg


def test_is_publishable_rejects_non_enum_args():
    with pytest.raises(TypeError):
        is_publishable("RAW_PRIVATE", PublicationTarget.PRIVATE_ARCHIVE)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        is_publishable(Tier.RAW_PRIVATE, "PRIVATE_ARCHIVE")  # type: ignore[arg-type]


def test_allowed_targets_rejects_non_tier_arg():
    with pytest.raises(TypeError):
        allowed_targets("RAW_PRIVATE")  # type: ignore[arg-type]


def test_allowed_targets_returns_frozenset():
    # Caller should not be able to mutate the policy matrix.
    result = allowed_targets(Tier.RAW_PRIVATE)
    assert isinstance(result, frozenset)


def test_tier_is_string_enum_for_json_serialization():
    # Tier values flow through JSON as their string value; str(Tier.X)
    # returning the enum repr would break sidecars. Check that the
    # underlying value is the canonical string.
    assert Tier.RAW_PRIVATE.value == "RAW_PRIVATE"
    # Cast-construct: Tier("RAW_PRIVATE") works.
    assert Tier("RAW_PRIVATE") is Tier.RAW_PRIVATE


def test_unknown_tier_value_raises():
    with pytest.raises(ValueError):
        Tier("UNKNOWN_TIER")
