"""Regression guards for the exp003 tool-using-agent quality figure.

`results/cost-curves/benchmark-03-quality.png` is a **mean judge score
(0-2) with standard-deviation error bars** chart, not a pass-rate chart.
An earlier draft of the GitHub Pages article described it as a "pass rate
by effort" curve that rose from ``none`` (98.3%) to ``low`` (100%) and
"then stays flat," with the gpt-4o baseline "near 90%." Those numbers are
pass rates, not the y-axis the chart actually plots, and "stays flat" is
wrong too: the pass rate dips to 98.3% at ``medium`` and ``high`` before
returning to 100% at ``xhigh``.

The chart's real values are mean judge scores: gpt-4o 1.85+/-0.48, and
gpt-5.2 ``none`` 1.97, ``low`` 2.00, ``medium`` 1.98, ``high`` 1.97,
``xhigh`` 2.00. The ``low`` cell is a clean 100% (60/60) -- its single
aggregate exclusion (``tu_18`` repeat 0) is a latency/token outlier that
still scored 2, not a quality failure.

These tests pin the corrected figure caption / alt text across all five
locales and assert the underlying aggregate they must agree with.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXP003_DIR = (
    REPO_ROOT
    / "docs"
    / "blog"
    / "articles"
    / "reasoning-effort-retrospective"
    / "experiments"
    / "exp003-tool-using-agent"
)
ANALYSIS_JSON = (
    REPO_ROOT / "benchmarks" / "03-tool-using-agent" / "analysis.json"
)

# "en" is the top-level index.html (the canonical Korean source lives at ko/).
LOCALE_PAGES = {
    "en": EXP003_DIR / "index.html",
    "ko": EXP003_DIR / "ko" / "index.html",
    "ja": EXP003_DIR / "ja" / "index.html",
    "zh-CN": EXP003_DIR / "zh-CN" / "index.html",
    "hi": EXP003_DIR / "hi" / "index.html",
}

_FIGURE_RE = re.compile(
    r'<figure[^>]*id="figure-exp003-quality".*?</figure>', re.DOTALL
)
_FIGCAPTION_RE = re.compile(r"<figcaption>(.*?)</figcaption>", re.DOTALL)
_IMG_ALT_RE = re.compile(r'<img[^>]*\balt="([^"]*)"', re.DOTALL)


def _figure_caption_and_alt(page: Path) -> tuple[str, str]:
    html = page.read_text(encoding="utf-8")
    figure = _FIGURE_RE.search(html)
    assert figure, f"{page}: figure#figure-exp003-quality not found"
    block = figure.group(0)
    caption = _FIGCAPTION_RE.search(block)
    alt = _IMG_ALT_RE.search(block)
    assert caption, f"{page}: <figcaption> not found in figure block"
    assert alt, f"{page}: <img alt> not found in figure block"
    return caption.group(1), alt.group(1)


@pytest.mark.parametrize("locale", sorted(LOCALE_PAGES))
def test_figure_caption_and_alt_carry_mean_scores(locale: str) -> None:
    """Caption and alt must surface the mean-score baseline and low cell."""
    caption, alt = _figure_caption_and_alt(LOCALE_PAGES[locale])
    for needle in ("1.85", "2.00", "low"):
        assert needle in caption, f"{locale} figcaption missing {needle!r}"
        assert needle in alt, f"{locale} img alt missing {needle!r}"


@pytest.mark.parametrize("locale", sorted(LOCALE_PAGES))
def test_alt_grounds_chart_as_mean_judge_score(locale: str) -> None:
    """Alt text must frame the chart as mean judge score with std bars + N/R."""
    _caption, alt = _figure_caption_and_alt(LOCALE_PAGES[locale])
    assert "1.85" in alt, f"{locale} alt missing gpt-4o baseline 1.85"
    assert "2.00" in alt, f"{locale} alt missing the 2.00 mean scores"
    assert "1.97" in alt, f"{locale} alt missing the 1.97 mean scores"
    assert "N=20" in alt, f"{locale} alt missing N=20"
    assert "R=3" in alt, f"{locale} alt missing R=3"


@pytest.mark.parametrize("locale", sorted(LOCALE_PAGES))
def test_figure_drops_pass_rate_framing(locale: str) -> None:
    """The debunked "pass rate by effort" chart framing must not return.

    98.3% is a pass rate, not a y-value of this mean-judge-score chart; the
    corrected caption/alt describe mean scores (1.85-2.00) instead. 98.3%
    still legitimately appears in the article body's pass-rate tables, so we
    only forbid it inside the figure caption and alt text.
    """
    caption, alt = _figure_caption_and_alt(LOCALE_PAGES[locale])
    assert "98.3" not in caption, f"{locale} figcaption still claims 98.3%"
    assert "98.3" not in alt, f"{locale} img alt still claims 98.3%"


def _bench03_cells() -> list[dict]:
    data = json.loads(ANALYSIS_JSON.read_text(encoding="utf-8"))
    return data["cells"]


def _score2_count(cells: list[dict], model: str, effort) -> tuple[int, int]:
    subset = [
        c for c in cells if c["model"] == model and c["effort"] == effort
    ]
    passed = sum(1 for c in subset if c["judge_score"] == 2)
    return passed, len(subset)


def test_analysis_json_low_is_a_clean_100pct() -> None:
    """gpt-5.2 low has zero score-0 cells: every one of its 60 cells scores 2."""
    cells = _bench03_cells()
    passed, total = _score2_count(cells, "gpt-5.2", "low")
    assert total == 60, f"expected 60 low cells, found {total}"
    assert passed == 60, f"low pass rate is not 100%: {passed}/{total}"
    zero_cells = [
        (c["sample_id"], c["repeat"])
        for c in cells
        if c["model"] == "gpt-5.2"
        and c["effort"] == "low"
        and c["judge_score"] == 0
    ]
    assert zero_cells == [], f"low unexpectedly has score-0 cells: {zero_cells}"


def test_analysis_json_baselines_match_caption() -> None:
    """Pin the pass rates the caption's mean scores are consistent with."""
    cells = _bench03_cells()
    # gpt-4o baseline: 54/60 = 90% pass, mean judge score 1.85.
    gpt4o_pass, gpt4o_total = _score2_count(cells, "gpt-4o", None)
    assert (gpt4o_pass, gpt4o_total) == (54, 60)
    # gpt-5.2 none: 59/60 = 98.3% pass, mean judge score 1.97.
    none_pass, none_total = _score2_count(cells, "gpt-5.2", "none")
    assert (none_pass, none_total) == (59, 60)
