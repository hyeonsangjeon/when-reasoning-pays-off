"""Release-tier classification (Task 032).

Defines the three data-sensitivity tiers from Task 030 / docs/16 and the
matrix of publication targets each tier is allowed to reach. The tier
identifiers are stable string values (``RAW_PRIVATE``,
``SANITIZED_PUBLIC``, ``AGGREGATE_AZURE_SAMPLE``) so they survive
serialization into JSON sidecars and remain greppable in CI.

Tier semantics (summary; the policy document is authoritative):

* ``RAW_PRIVATE`` — original experiment output. Stays in the private
  archive. MUST NOT be published anywhere.
* ``SANITIZED_PUBLIC`` — Tier 1 with redaction rules applied. MAY be
  published in the public research repo. MUST NOT appear in the
  downstream Foundry sample repo (which is aggregate-only).
* ``AGGREGATE_AZURE_SAMPLE`` — per-cell aggregates only, no per-request
  rows, no free-text payloads. MAY appear in both the public research
  repo (as a convenience summary) and the Foundry sample repo.

The publication-target enum is the *role* of a destination, not a
specific URL or remote name; concrete remotes are configured outside
this module.
"""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Mapping


class Tier(str, Enum):
    """Data-sensitivity tier of an artifact."""

    RAW_PRIVATE = "RAW_PRIVATE"
    SANITIZED_PUBLIC = "SANITIZED_PUBLIC"
    AGGREGATE_AZURE_SAMPLE = "AGGREGATE_AZURE_SAMPLE"


class PublicationTarget(str, Enum):
    """Role-based publication destinations.

    Concrete remotes / repository names live in higher-level config; this
    enum only names the *role* so the tier matrix stays free of any
    customer- or vendor-specific identifier.
    """

    PRIVATE_ARCHIVE = "PRIVATE_ARCHIVE"
    PUBLIC_RESEARCH_REPO = "PUBLIC_RESEARCH_REPO"
    FOUNDRY_SAMPLE_REPO = "FOUNDRY_SAMPLE_REPO"


_ALLOWED: Mapping[Tier, frozenset[PublicationTarget]] = MappingProxyType(
    {
        Tier.RAW_PRIVATE: frozenset({PublicationTarget.PRIVATE_ARCHIVE}),
        Tier.SANITIZED_PUBLIC: frozenset(
            {
                PublicationTarget.PRIVATE_ARCHIVE,
                PublicationTarget.PUBLIC_RESEARCH_REPO,
            }
        ),
        Tier.AGGREGATE_AZURE_SAMPLE: frozenset(
            {
                PublicationTarget.PRIVATE_ARCHIVE,
                PublicationTarget.PUBLIC_RESEARCH_REPO,
                PublicationTarget.FOUNDRY_SAMPLE_REPO,
            }
        ),
    }
)


class PublicationNotAllowedError(ValueError):
    """Raised when an artifact's tier is not allowed at the given target."""


def allowed_targets(tier: Tier) -> frozenset[PublicationTarget]:
    """Return the set of publication targets *tier* is allowed to reach."""
    if not isinstance(tier, Tier):
        raise TypeError(f"tier must be a Tier, got {type(tier).__name__}")
    return _ALLOWED[tier]


def is_publishable(tier: Tier, target: PublicationTarget) -> bool:
    """Return ``True`` iff *tier* is allowed at *target*."""
    if not isinstance(tier, Tier):
        raise TypeError(f"tier must be a Tier, got {type(tier).__name__}")
    if not isinstance(target, PublicationTarget):
        raise TypeError(
            f"target must be a PublicationTarget, got {type(target).__name__}"
        )
    return target in _ALLOWED[tier]


def assert_publishable(tier: Tier, target: PublicationTarget) -> None:
    """Raise :class:`PublicationNotAllowedError` if *tier* may not reach *target*.

    Callers invoke this immediately before writing an artifact into a
    publication-target-bound directory; the helper exists so the policy
    check is a single greppable call instead of an open-coded ``if``.
    """
    if not is_publishable(tier, target):
        raise PublicationNotAllowedError(
            f"tier {tier.value} is not allowed at target {target.value}; "
            f"allowed targets for this tier are "
            f"{sorted(t.value for t in _ALLOWED[tier])}"
        )
