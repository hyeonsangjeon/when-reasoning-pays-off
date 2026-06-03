"""Tests for the two-tier citation taxonomy (Task 029).

Pure, deterministic, stdlib-only. No measurement, no LLM, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from batch_runner.methodology import (
    Citation,
    Tier,
    assert_well_formed,
    render_for_doc_footer,
    render_for_docstring,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_PATH = REPO_ROOT / "docs" / "15-spec-vs-inference-taxonomy.md"
EXAMPLES_PATH = REPO_ROOT / "docs" / "15-spec-vs-inference-taxonomy.examples.md"
FROZEN_METHODOLOGY = REPO_ROOT / "docs" / "05-methodology.md"

# Bound on doc length per Task 029 acceptance criteria.
DOC_MAX_LINES = 250
EXAMPLES_MAX_LINES = 150

VOICE_FORBIDDEN = re.compile(
    r"dramatic|best-in-class|industry-leading|cutting-edge|breakthrough",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# Tier 1 — well-formed
# --------------------------------------------------------------------------

def test_tier1_learn_url_with_iso_date_is_well_formed():
    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="https://learn.microsoft.com/azure/ai-services/openai/concepts/provisioned-throughput",
        access_date_iso="2026-05-28",
        quoted_excerpt="retry-after-ms ... next acceptable request time",
    )
    assert_well_formed(c)


def test_tier1_openai_url_with_iso_date_is_well_formed():
    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="https://platform.openai.com/docs/guides/reasoning",
        access_date_iso="2026-05-28",
    )
    assert_well_formed(c)


def test_tier1_pinned_sdk_source_is_well_formed():
    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="Azure SDK Python: openai/_base_client.py L417 (v1.42.0)",
        access_date_iso="2026-05-28",
    )
    assert_well_formed(c)


# --------------------------------------------------------------------------
# Tier 1 — malformed
# --------------------------------------------------------------------------

def test_tier1_missing_access_date_raises():
    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="https://learn.microsoft.com/azure/ai-services/openai/concepts/provisioned-throughput",
    )
    with pytest.raises(ValueError, match="access_date_iso"):
        assert_well_formed(c)


def test_tier1_non_iso_access_date_raises():
    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="https://learn.microsoft.com/azure/ai-services/openai/concepts/provisioned-throughput",
        access_date_iso="May 28, 2026",
    )
    with pytest.raises(ValueError, match="ISO YYYY-MM-DD"):
        assert_well_formed(c)


def test_tier1_unaccepted_source_raises():
    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="https://blog.example.com/some-post",
        access_date_iso="2026-05-28",
    )
    with pytest.raises(ValueError, match="Microsoft Learn"):
        assert_well_formed(c)


def test_tier1_in_repo_path_as_source_raises():
    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="benchmarks/07-cache-hit-degradation/analysis.md",
        access_date_iso="2026-05-28",
    )
    with pytest.raises(ValueError, match="Microsoft Learn"):
        assert_well_formed(c)


def test_tier1_empty_quoted_excerpt_raises():
    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="https://learn.microsoft.com/azure/ai-services/openai/concepts/provisioned-throughput",
        access_date_iso="2026-05-28",
        quoted_excerpt="   ",
    )
    with pytest.raises(ValueError, match="quoted_excerpt"):
        assert_well_formed(c)


# --------------------------------------------------------------------------
# Tier 1 — adversarial source parsing (hostname vs substring; pinned SDK)
# --------------------------------------------------------------------------

def test_tier1_lookalike_host_substring_in_hostname_raises():
    """A hostname that merely contains ``openai.com`` as a substring
    (e.g. ``notopenai.com``) must not be accepted as Tier 1."""

    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="https://notopenai.com/fake",
        access_date_iso="2026-05-28",
    )
    with pytest.raises(ValueError, match="Microsoft Learn"):
        assert_well_formed(c)


def test_tier1_accepted_host_appearing_only_in_path_raises():
    """An accepted host name appearing in the URL path (but the actual
    hostname is unrelated) must not be accepted as Tier 1."""

    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="https://example.com/learn.microsoft.com/fake",
        access_date_iso="2026-05-28",
    )
    with pytest.raises(ValueError, match="Microsoft Learn"):
        assert_well_formed(c)


def test_tier1_subdomain_of_accepted_host_raises():
    """``blog.learn.microsoft.com`` is not on the accepted-host list and
    must be rejected; only exact hostnames are accepted."""

    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="https://blog.learn.microsoft.com/x",
        access_date_iso="2026-05-28",
    )
    with pytest.raises(ValueError, match="Microsoft Learn"):
        assert_well_formed(c)


def test_tier1_bare_azure_sdk_string_raises():
    """``"Azure SDK"`` is not pinned (no file, no line, no version);
    it must not be accepted as a Tier 1 source."""

    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="Azure SDK",
        access_date_iso="2026-05-28",
    )
    with pytest.raises(ValueError, match="Microsoft Learn"):
        assert_well_formed(c)


def test_tier1_bare_openai_sdk_string_raises():
    """``"OpenAI SDK"`` (bare) is not pinned and must be rejected."""

    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="OpenAI SDK",
        access_date_iso="2026-05-28",
    )
    with pytest.raises(ValueError, match="Microsoft Learn"):
        assert_well_formed(c)


def test_tier1_openai_sdk_pinned_form_is_well_formed():
    """Symmetric to the Azure SDK pinned form: source family + file/path
    + ``L<line>`` + non-empty version/tag is the accepted shape."""

    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="OpenAI SDK Python: src/openai/_client.py L120 (v1.30.0)",
        access_date_iso="2026-05-28",
    )
    assert_well_formed(c)


def test_tier1_sdk_missing_line_marker_raises():
    """An SDK identifier without an ``L<line>`` marker is not pinned
    enough and must be rejected."""

    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="Azure SDK Python: openai/_base_client.py (v1.42.0)",
        access_date_iso="2026-05-28",
    )
    with pytest.raises(ValueError, match="Microsoft Learn"):
        assert_well_formed(c)


def test_tier1_sdk_missing_version_tag_raises():
    """An SDK identifier without a parenthesized version/tag is not
    pinned enough and must be rejected."""

    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="Azure SDK Python: openai/_base_client.py L417",
        access_date_iso="2026-05-28",
    )
    with pytest.raises(ValueError, match="Microsoft Learn"):
        assert_well_formed(c)


def test_tier1_github_azure_sdk_blob_main_url_raises():
    """A mutable ``github.com/Azure/...`` ``blob/main`` URL is not a
    pinned source (no line, no immutable ref) and must be rejected.
    Tier 1 SDK sources must use the pinned identifier string instead."""

    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/openai/file.py",
        access_date_iso="2026-05-28",
    )
    with pytest.raises(ValueError, match="Microsoft Learn"):
        assert_well_formed(c)


def test_tier1_github_openai_blob_main_url_raises():
    """A mutable ``github.com/openai/...`` ``blob/main`` URL is not a
    pinned source and must be rejected. Tier 1 SDK sources must use
    the pinned identifier string instead."""

    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="https://github.com/openai/openai-python/blob/main/src/openai/_client.py",
        access_date_iso="2026-05-28",
    )
    with pytest.raises(ValueError, match="Microsoft Learn"):
        assert_well_formed(c)


def test_tier1_github_unapproved_owner_raises():
    """Any ``github.com`` URL (regardless of owner) is rejected as a
    Tier 1 source; SDK sources must use the pinned identifier string."""

    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="https://github.com/somebody/repo/blob/main/file.py",
        access_date_iso="2026-05-28",
    )
    with pytest.raises(ValueError, match="Microsoft Learn"):
        assert_well_formed(c)


def test_tier1_url_without_hostname_raises():
    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="http:///nohost/path",
        access_date_iso="2026-05-28",
    )
    with pytest.raises(ValueError, match="Microsoft Learn"):
        assert_well_formed(c)


# --------------------------------------------------------------------------
# Tier 2 — well-formed
# --------------------------------------------------------------------------

def test_tier2_in_repo_path_with_rationale_is_well_formed():
    c = Citation(
        tier=Tier.OPERATIONAL_INFERENCE,
        source="benchmarks/04-leak-rate-fit/analysis.md §2",
        rationale="Fitted in Task 024 from repo measurements; not a Microsoft-published constant.",
    )
    assert_well_formed(c)


def test_tier2_with_access_date_is_well_formed():
    c = Citation(
        tier=Tier.OPERATIONAL_INFERENCE,
        source="benchmarks/03-admission-controller/analysis.md §4",
        access_date_iso="2026-05-28",
        rationale="Conservative cap chosen from observed retry-after-ms p99 distribution.",
    )
    assert_well_formed(c)


# --------------------------------------------------------------------------
# Tier 2 — malformed
# --------------------------------------------------------------------------

def test_tier2_missing_rationale_raises():
    c = Citation(
        tier=Tier.OPERATIONAL_INFERENCE,
        source="benchmarks/04-leak-rate-fit/analysis.md §2",
    )
    with pytest.raises(ValueError, match="rationale"):
        assert_well_formed(c)


def test_tier2_short_rationale_raises():
    c = Citation(
        tier=Tier.OPERATIONAL_INFERENCE,
        source="benchmarks/04-leak-rate-fit/analysis.md §2",
        rationale="too short",
    )
    with pytest.raises(ValueError, match=">= 20 characters"):
        assert_well_formed(c)


def test_tier2_invalid_access_date_raises():
    c = Citation(
        tier=Tier.OPERATIONAL_INFERENCE,
        source="benchmarks/04-leak-rate-fit/analysis.md §2",
        access_date_iso="2026/05/28",
        rationale="Rationale long enough to clear the 20-char minimum.",
    )
    with pytest.raises(ValueError, match="ISO YYYY-MM-DD"):
        assert_well_formed(c)


def test_empty_source_raises():
    c = Citation(
        tier=Tier.OPERATIONAL_INFERENCE,
        source="   ",
        rationale="Rationale long enough to clear the 20-char minimum.",
    )
    with pytest.raises(ValueError, match="source"):
        assert_well_formed(c)


def test_non_citation_argument_raises_typeerror():
    with pytest.raises(TypeError):
        assert_well_formed("not a citation")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------

def test_render_for_docstring_tier1_contains_url_and_date():
    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="https://learn.microsoft.com/x",
        access_date_iso="2026-05-28",
        quoted_excerpt="hello world",
    )
    out = render_for_docstring(c)
    assert "[TIER1 OFFICIAL_SPEC]" in out
    assert "https://learn.microsoft.com/x" in out
    assert "2026-05-28" in out
    assert "hello world" in out


def test_render_for_docstring_tier2_contains_rationale():
    c = Citation(
        tier=Tier.OPERATIONAL_INFERENCE,
        source="benchmarks/04-leak-rate-fit/analysis.md §2",
        rationale="Fitted in Task 024 from repo measurements; not Microsoft-published.",
    )
    out = render_for_docstring(c)
    assert "[TIER2 OPERATIONAL_INFERENCE]" in out
    assert "rationale:" in out
    assert "Task 024" in out


def test_render_for_doc_footer_tier1_format():
    c = Citation(
        tier=Tier.OFFICIAL_SPEC,
        source="https://learn.microsoft.com/x",
        access_date_iso="2026-05-28",
        quoted_excerpt="quoted",
    )
    out = render_for_doc_footer(c)
    assert out.startswith("- **Tier 1 (official spec)** —")
    assert "accessed 2026-05-28" in out
    assert '"quoted"' in out


def test_render_for_doc_footer_tier2_format():
    c = Citation(
        tier=Tier.OPERATIONAL_INFERENCE,
        source="benchmarks/04-leak-rate-fit/analysis.md §2",
        rationale="Fitted from repo measurements; bounded validity.",
    )
    out = render_for_doc_footer(c)
    assert out.startswith("- **Tier 2 (operational inference)** —")
    assert "Rationale:" in out


def test_renderers_reject_malformed():
    bad = Citation(tier=Tier.OFFICIAL_SPEC, source="https://learn.microsoft.com/x")
    with pytest.raises(ValueError):
        render_for_docstring(bad)
    with pytest.raises(ValueError):
        render_for_doc_footer(bad)


# --------------------------------------------------------------------------
# Doc audits
# --------------------------------------------------------------------------

def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_doc_exists_and_under_line_budget():
    assert DOC_PATH.is_file()
    lines = _read_lines(DOC_PATH)
    assert len(lines) <= DOC_MAX_LINES, (
        "doc has %d lines; max is %d" % (len(lines), DOC_MAX_LINES)
    )


def test_examples_appendix_exists_and_under_line_budget():
    assert EXAMPLES_PATH.is_file()
    lines = _read_lines(EXAMPLES_PATH)
    assert len(lines) <= EXAMPLES_MAX_LINES, (
        "examples appendix has %d lines; max is %d"
        % (len(lines), EXAMPLES_MAX_LINES)
    )


def test_doc_contains_required_sections():
    text = DOC_PATH.read_text(encoding="utf-8")
    for header in (
        "## 1. Why two tiers",
        "## 2. Tier 1 — Official Spec",
        "## 3. Tier 2 — Operational Inference",
        "## 4. Why this matters",
        "## 5. How to label in code",
        "## 6. How to label in docs",
        "## 7. When a Tier 2 claim graduates to Tier 1",
        "## 8. Where this applies",
        "## 9. What this taxonomy does NOT do",
    ):
        assert header in text, "missing section header: %r" % header


def test_every_tier1_footer_bullet_has_url_and_iso_date():
    """Every '**Tier 1 (official spec)**' bullet in either doc carries a
    URL AND an ISO YYYY-MM-DD access date."""

    tier1_bullet_re = re.compile(
        r"^- \*\*Tier 1 \(official spec\)\*\* — (.+?)(?: — accessed (\S+))?$"
    )
    iso_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    url_re = re.compile(r"https?://\S+")
    sdk_re = re.compile(r"^(Azure SDK|OpenAI SDK)\b")

    for path in (DOC_PATH, EXAMPLES_PATH):
        for lineno, line in enumerate(_read_lines(path), 1):
            m = tier1_bullet_re.match(line.rstrip())
            if not m:
                continue
            source, date = m.group(1).strip(), m.group(2)
            assert url_re.search(source) or sdk_re.match(source), (
                "%s:%d Tier 1 bullet missing URL/SDK source: %s"
                % (path.name, lineno, line)
            )
            assert date is not None and iso_re.match(date), (
                "%s:%d Tier 1 bullet missing ISO access date: %s"
                % (path.name, lineno, line)
            )


def test_every_tier2_footer_bullet_has_rationale_or_in_repo_path():
    tier2_bullet_re = re.compile(r"^- \*\*Tier 2 \(operational inference\)\*\* — (.+)$")
    for path in (DOC_PATH, EXAMPLES_PATH):
        lines = _read_lines(path)
        for i, line in enumerate(lines):
            m = tier2_bullet_re.match(line.rstrip())
            if not m:
                continue
            source = m.group(1).strip()
            in_repo = ("/" in source) and not source.lower().startswith("http")
            # Look ahead a few lines for an inline 'Rationale:' continuation.
            window = " ".join(lines[i : i + 4])
            has_rationale = "Rationale:" in window or "rationale:" in window
            assert in_repo or has_rationale, (
                "%s:%d Tier 2 bullet needs in-repo path or Rationale: %s"
                % (path.name, i + 1, line)
            )


def test_doc_voice_grep_clean():
    for path in (DOC_PATH, EXAMPLES_PATH):
        text = path.read_text(encoding="utf-8")
        match = VOICE_FORBIDDEN.search(text)
        assert match is None, (
            "voice grep hit in %s: %r" % (path.name, match.group(0) if match else None)
        )


def test_frozen_methodology_doc_present_and_unchanged_marker():
    """The frozen Task 001 methodology doc must still exist; Task 029 must
    not have replaced it. We assert presence and a stable header line as
    the in-test proxy for the git-level no-diff check listed in the task."""

    assert FROZEN_METHODOLOGY.is_file()
    head = FROZEN_METHODOLOGY.read_text(encoding="utf-8").splitlines()[0]
    assert head.startswith("#"), "frozen methodology doc missing top-level header"
