"""Two-tier citation taxonomy (Task 029).

Tier 1 — Official Spec
    A claim whose exact wording can be found in Microsoft Learn, OpenAI
    public documentation, an Azure REST API reference, or pinned Azure
    SDK source. Citation MUST carry the URL (or an accepted source
    identifier) AND an ISO ``YYYY-MM-DD`` access date.

Tier 2 — Operational Inference
    A claim supported by this repo's measurements, external field
    observation, or interpretation of Tier 1 sources that goes beyond
    their verbatim wording. Citation MUST carry a non-empty rationale
    (>= 20 chars). An in-repo path is an accepted source; the access
    date is optional.

The library is deterministic, pure (no I/O, no network, no SDK calls),
and stdlib-only. It is consumed by docstrings, doc footers, and the
per-field category tags in ``batch_runner.observability.schema``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

__all__ = [
    "Citation",
    "Tier",
    "assert_well_formed",
    "render_for_doc_footer",
    "render_for_docstring",
]


class Tier(Enum):
    """The two citation tiers."""

    OFFICIAL_SPEC = "official_spec"
    OPERATIONAL_INFERENCE = "operational_inference"


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# Exact hostnames accepted as Tier 1 public sources. Matched against the
# parsed URL hostname (lower-cased); substring matches in URL text are
# NOT accepted, so adversarial sources like ``https://notopenai.com/x``
# or ``https://example.com/learn.microsoft.com/x`` are rejected.
#
# ``github.com`` is deliberately NOT on this list. A GitHub ``blob/main``
# URL is a mutable reference with no line/version pin and therefore
# cannot anchor a Tier 1 claim. SDK sources must be cited as a pinned
# identifier string (see ``_TIER1_SDK_RE`` below) carrying file path,
# line number, and version tag.
_ACCEPTED_TIER1_HOSTS = frozenset(
    {
        "learn.microsoft.com",
        "docs.microsoft.com",
        "platform.openai.com",
        "openai.com",
        "azure.microsoft.com",
    }
)

# Pinned SDK source identifier. Must show source family + a file path
# (containing ``/`` and a file extension) + an ``L<line>`` marker + a
# non-empty parenthesized version/tag. Example:
#     "Azure SDK Python: openai/_base_client.py L417 (v1.42.0)"
#     "OpenAI SDK Python: src/openai/_client.py L120 (v1.30.0)"
# Bare strings such as ``"Azure SDK"`` or ``"OpenAI SDK"`` are rejected,
# as are mutable GitHub ``blob/main`` URLs.
_TIER1_SDK_RE = re.compile(
    r"^(?:Azure SDK|OpenAI SDK)\b[^:\n]*:\s*\S*/\S+\.\w+\s+L\d+\s+\([^)\s][^)]*\)\s*$"
)

_RATIONALE_MIN_CHARS = 20


@dataclass(frozen=True)
class Citation:
    """A single citation record.

    Parameters
    ----------
    tier:
        The taxonomy tier (Tier 1 — official spec; Tier 2 — operational
        inference).
    source:
        For Tier 1: a public URL (preferred), or a pinned SDK source
        identifier such as ``"Azure SDK Python: openai/_base_client.py
        L417 (v1.42.0)"``. For Tier 2: a URL, an in-repo path
        (``"benchmarks/07-.../analysis.md §3"``), or a short label
        naming the field observation.
    access_date_iso:
        ISO ``YYYY-MM-DD`` date the source was accessed. Required for
        Tier 1; optional for Tier 2 (especially when the source is an
        in-repo path versioned by git).
    quoted_excerpt:
        Optional verbatim quote from a Tier 1 source. Strongly
        recommended for Tier 1 claims whose wording matters.
    rationale:
        Required for Tier 2: a short paragraph naming the inference and
        its limits. Must be >= 20 characters.
    """

    tier: Tier
    source: str
    access_date_iso: Optional[str] = None
    quoted_excerpt: Optional[str] = None
    rationale: Optional[str] = None


def _is_iso_date(s: str) -> bool:
    if not _ISO_DATE_RE.match(s):
        return False
    year, month, day = (int(part) for part in s.split("-"))
    if not (1 <= month <= 12):
        return False
    if not (1 <= day <= 31):
        return False
    return 1970 <= year <= 2999


def _is_accepted_tier1_source(source: str) -> bool:
    if _URL_RE.match(source):
        try:
            parsed = urlparse(source)
        except ValueError:
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        return host in _ACCEPTED_TIER1_HOSTS
    return bool(_TIER1_SDK_RE.match(source))


def _is_in_repo_path(source: str) -> bool:
    if _URL_RE.match(source):
        return False
    head = source.split()[0] if source else ""
    if not head:
        return False
    if head.startswith("/") or head.startswith("./") or head.startswith("../"):
        return True
    return "/" in head and not head.startswith("http")


def assert_well_formed(c: Citation) -> None:
    """Validate a citation per the Task 029 taxonomy.

    Raises
    ------
    ValueError
        If the citation does not satisfy the rules for its tier.
    TypeError
        If ``c`` is not a :class:`Citation` or ``c.tier`` is not a
        :class:`Tier`.
    """

    if not isinstance(c, Citation):
        raise TypeError("expected Citation, got %r" % type(c).__name__)
    if not isinstance(c.tier, Tier):
        raise TypeError("Citation.tier must be a Tier enum value")
    if not isinstance(c.source, str) or not c.source.strip():
        raise ValueError("Citation.source must be a non-empty string")

    if c.tier is Tier.OFFICIAL_SPEC:
        if c.access_date_iso is None or not _is_iso_date(c.access_date_iso):
            raise ValueError(
                "Tier 1 (OFFICIAL_SPEC) requires access_date_iso in "
                "ISO YYYY-MM-DD form; got %r" % (c.access_date_iso,)
            )
        if not _is_accepted_tier1_source(c.source):
            raise ValueError(
                "Tier 1 (OFFICIAL_SPEC) source must be a Microsoft Learn / "
                "OpenAI / Azure URL or a pinned SDK source identifier; "
                "got %r" % (c.source,)
            )
        if c.quoted_excerpt is not None and not c.quoted_excerpt.strip():
            raise ValueError(
                "Tier 1 quoted_excerpt, if set, must be non-empty"
            )
        return

    # Tier 2 — OPERATIONAL_INFERENCE
    if c.rationale is None or not c.rationale.strip():
        raise ValueError(
            "Tier 2 (OPERATIONAL_INFERENCE) requires a non-empty rationale"
        )
    if len(c.rationale.strip()) < _RATIONALE_MIN_CHARS:
        raise ValueError(
            "Tier 2 rationale must be >= %d characters; got %d"
            % (_RATIONALE_MIN_CHARS, len(c.rationale.strip()))
        )
    if not (
        _URL_RE.match(c.source)
        or _is_in_repo_path(c.source)
        or len(c.source.strip()) >= 4
    ):
        raise ValueError(
            "Tier 2 source must be a URL, an in-repo path, or a labeled "
            "identifier (>= 4 chars); got %r" % (c.source,)
        )
    if c.access_date_iso is not None and not _is_iso_date(c.access_date_iso):
        raise ValueError(
            "Tier 2 access_date_iso, if set, must be ISO YYYY-MM-DD; "
            "got %r" % (c.access_date_iso,)
        )


def render_for_docstring(c: Citation) -> str:
    """Render a citation as a single-line docstring tag."""

    assert_well_formed(c)
    if c.tier is Tier.OFFICIAL_SPEC:
        parts = [
            "[TIER1 OFFICIAL_SPEC]",
            c.source,
            "(accessed %s)" % c.access_date_iso,
        ]
        if c.quoted_excerpt:
            parts.append('— "%s"' % c.quoted_excerpt.strip())
        return " ".join(parts)
    parts = ["[TIER2 OPERATIONAL_INFERENCE]", c.source]
    if c.access_date_iso:
        parts.append("(accessed %s)" % c.access_date_iso)
    parts.append("— rationale: %s" % c.rationale.strip())
    return " ".join(parts)


def render_for_doc_footer(c: Citation) -> str:
    """Render a citation as a Markdown doc-footer bullet.

    Tier 1 footers carry the URL and ISO access date; Tier 2 footers
    carry the source and rationale.
    """

    assert_well_formed(c)
    if c.tier is Tier.OFFICIAL_SPEC:
        line = "- **Tier 1 (official spec)** — %s — accessed %s" % (
            c.source,
            c.access_date_iso,
        )
        if c.quoted_excerpt:
            line += '\n  > "%s"' % c.quoted_excerpt.strip()
        return line
    line = "- **Tier 2 (operational inference)** — %s" % c.source
    if c.access_date_iso:
        line += " — accessed %s" % c.access_date_iso
    line += "\n  Rationale: %s" % c.rationale.strip()
    return line
