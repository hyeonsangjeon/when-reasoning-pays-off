"""Keep the numbered guide and standalone Pages guide on one command contract."""

from __future__ import annotations

import html
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MARKDOWN = REPO_ROOT / "docs/19-five-minute-provenance-report.md"
HTML = REPO_ROOT / "docs/guides/five-minute-report/index.html"


def _html_text(source: str) -> str:
    return " ".join(
        html.unescape(re.sub(r"<[^>]+>", " ", source)).split()
    )


def test_markdown_and_pages_guide_share_commands_contracts_and_citations():
    markdown = MARKDOWN.read_text(encoding="utf-8")
    html_source = HTML.read_text(encoding="utf-8")
    page_text = _html_text(html_source)
    required = [
        "reasoning-payoff analyze examples/five-minute/usage.jsonl",
        "--workload examples/five-minute/workload.yaml",
        "reasoning-payoff init --out .reasoning-payoff",
        "reasoning-payoff report report/report.json",
        "report.json",
        "report.md",
        "report.html",
        "policy.json",
        "cached_input_tokens",
        "reasoning_tokens",
        "retry_after_ms",
        "max_reasoning_output_ratio",
        "expected_rpm",
        "mean_max_output_tokens",
        "NOT_MEASURED",
        "https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/reasoning",
        "https://learn.microsoft.com/en-us/azure/ai-services/openai/how-to/prompt-caching",
        "https://learn.microsoft.com/en-us/azure/ai-services/openai/concepts/provisioned-throughput",
    ]
    for value in required:
        assert value in markdown
        assert value in page_text or value in html_source


def test_pages_guide_has_no_runtime_or_external_asset_dependency():
    source = HTML.read_text(encoding="utf-8").lower()
    assert "<script" not in source
    assert not re.search(
        r"<link[^>]+rel=[\"']stylesheet[\"']",
        source,
    )
    assert "@import" not in source
    assert not re.search(r"\bsrc=[\"']https?://", source)
    assert 'rel="icon"' in source
    assert 'href="../../assets/hero.svg"' in source


def test_guide_navigation_and_readme_links_exist():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    guide_index = (REPO_ROOT / "docs/guides/index.html").read_text(encoding="utf-8")
    assert "docs/19-five-minute-provenance-report.md" in readme
    assert "docs/guides/five-minute-report/" in readme
    assert "./five-minute-report/" in guide_index
    assert 'href="../assets/hero.svg"' in guide_index
    assert 'href="../../">Project site</a>' in HTML.read_text(encoding="utf-8")
