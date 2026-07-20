"""Regression guards for the exp002 multi-step-reasoning quality figure.

`results/cost-curves/benchmark-02-quality.png` is a **mean judge score
(0-2) with standard-deviation error bars** chart, not a pass-rate chart.
An earlier draft of the GitHub Pages article described it as a saturated
pass-rate curve that was "flat at 100% from none to xhigh" and claimed
gpt-5.2 was perfect everywhere except a single ``mr_05`` refusal. Both
statements contradict the source data: gpt-5.2 ``low`` scores 1.93+/-0.36
(96.7 %, 58/60) because of **two** failures -- the ``mr_05`` content-filter
refusal and the ``mr_15`` code-trace wrong answer.

These tests pin the corrected figure caption / alt text across all five
locales and assert the underlying aggregate they must agree with.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXP002_DIR = (
    REPO_ROOT
    / "docs"
    / "blog"
    / "articles"
    / "reasoning-effort-retrospective"
    / "experiments"
    / "exp002-multi-step-reasoning"
)
ANALYSIS_JSON = (
    REPO_ROOT / "benchmarks" / "02-multi-step-reasoning" / "analysis.json"
)

# "en" is the top-level index.html (the canonical Korean source lives at ko/).
LOCALE_PAGES = {
    "en": EXP002_DIR / "index.html",
    "ko": EXP002_DIR / "ko" / "index.html",
    "ja": EXP002_DIR / "ja" / "index.html",
    "zh-CN": EXP002_DIR / "zh-CN" / "index.html",
    "hi": EXP002_DIR / "hi" / "index.html",
}

_FIGURE_RE = re.compile(
    r'<figure[^>]*id="figure-exp002-quality".*?</figure>', re.DOTALL
)
_FIGCAPTION_RE = re.compile(r"<figcaption>(.*?)</figcaption>", re.DOTALL)
_IMG_ALT_RE = re.compile(r'<img[^>]*\balt="([^"]*)"', re.DOTALL)


def _figure_caption_and_alt(page: Path) -> tuple[str, str]:
    html = page.read_text(encoding="utf-8")
    figure = _FIGURE_RE.search(html)
    assert figure, f"{page}: figure#figure-exp002-quality not found"
    block = figure.group(0)
    caption = _FIGCAPTION_RE.search(block)
    alt = _IMG_ALT_RE.search(block)
    assert caption, f"{page}: <figcaption> not found in figure block"
    assert alt, f"{page}: <img alt> not found in figure block"
    return caption.group(1), alt.group(1)


@pytest.mark.parametrize("locale", sorted(LOCALE_PAGES))
def test_figure_caption_and_alt_carry_the_low_cell(locale: str) -> None:
    """Caption and alt must surface the corrected low cell (1.93) and effort."""
    caption, alt = _figure_caption_and_alt(LOCALE_PAGES[locale])
    for needle in ("1.93", "low"):
        assert needle in caption, f"{locale} figcaption missing {needle!r}"
        assert needle in alt, f"{locale} img alt missing {needle!r}"


@pytest.mark.parametrize("locale", sorted(LOCALE_PAGES))
def test_alt_grounds_chart_as_mean_judge_score(locale: str) -> None:
    """Alt text must frame the chart as mean judge score with std bars + N/R."""
    _caption, alt = _figure_caption_and_alt(LOCALE_PAGES[locale])
    assert "1.50" in alt, f"{locale} alt missing gpt-4o baseline 1.50"
    assert "2.00" in alt, f"{locale} alt missing the 2.00 mean scores"
    assert "N=20" in alt, f"{locale} alt missing N=20"
    assert "R=3" in alt, f"{locale} alt missing R=3"


@pytest.mark.parametrize("locale", sorted(LOCALE_PAGES))
def test_figure_drops_flat_100pct_pass_rate_framing(locale: str) -> None:
    """The debunked "flat at 100% pass rate" framing must not return."""
    caption, alt = _figure_caption_and_alt(LOCALE_PAGES[locale])
    assert "100%" not in caption, f"{locale} figcaption still claims 100%"
    assert "100%" not in alt, f"{locale} img alt still claims 100%"


@pytest.mark.parametrize("locale", sorted(LOCALE_PAGES))
def test_both_low_failures_are_named(locale: str) -> None:
    """Both low-effort failures (mr_05 refusal, mr_15 code-trace) are named."""
    html = LOCALE_PAGES[locale].read_text(encoding="utf-8")
    assert "mr_05" in html, f"{locale} page does not mention mr_05"
    assert "mr_15" in html, f"{locale} page does not mention mr_15"


def test_analysis_json_low_has_exactly_two_zero_cells() -> None:
    """gpt-5.2 low has exactly two score-0 cells: mr_05/r1 and mr_15/r2."""
    data = json.loads(ANALYSIS_JSON.read_text(encoding="utf-8"))
    zero_cells = {
        (cell["sample_id"], cell["repeat"])
        for cell in data["cells"]
        if cell["model"] == "gpt-5.2"
        and cell["effort"] == "low"
        and cell["judge_score"] == 0
    }
    assert zero_cells == {("mr_05", 1), ("mr_15", 2)}
