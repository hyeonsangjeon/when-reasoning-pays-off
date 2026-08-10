from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


REPO_ROOT = Path(__file__).resolve().parent.parent
ARTICLE_DIR = REPO_ROOT / "docs" / "blog" / "articles" / "when-reasoning-pays-off"
KO_PAGE = ARTICLE_DIR / "ko" / "index.html"
HI_PAGE = ARTICLE_DIR / "hi" / "index.html"
HI_OVERVIEW_SVG = (
    REPO_ROOT / "docs" / "assets" / "when-reasoning-pays-off-overview.hi.svg"
)
LOCALE_DIR = REPO_ROOT / "docs" / "blog" / "data" / "chart-data" / "locales"
VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class _MainChildParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_main = False
        self.depth = 0
        self.children: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if not self.in_main:
            if tag == "main":
                self.in_main = True
            return
        if self.depth == 0:
            self.children.append(tag)
        if tag not in VOID_ELEMENTS:
            self.depth += 1

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if self.in_main and self.depth == 0:
            self.children.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.in_main:
            return
        if tag == "main" and self.depth == 0:
            self.in_main = False
            return
        if tag not in VOID_ELEMENTS:
            self.depth -= 1


def _leaf_paths(
    value: object,
    prefix: tuple[str, ...] = (),
) -> set[tuple[str, ...]]:
    if not isinstance(value, dict):
        return {prefix}
    paths: set[tuple[str, ...]] = set()
    for key, child in value.items():
        paths.update(_leaf_paths(child, prefix + (key,)))
    return paths


def _local_reference(html: str, attribute: str, filename: str) -> Path:
    match = re.search(
        rf'{attribute}=["\'](?P<url>[^"\']*{re.escape(filename)}[^"\']*)["\']',
        html,
    )
    assert match, f"missing {attribute} URL for {filename}"
    parsed = urlsplit(match.group("url"))
    assert not parsed.scheme
    assert not parsed.netloc
    return (HI_PAGE.parent / unquote(parsed.path)).resolve()


def _main_child_signature(html: str) -> list[str]:
    parser = _MainChildParser()
    parser.feed(html)
    return parser.children


def test_hindi_overview_matches_korean_baseline_blocks() -> None:
    ko_html = KO_PAGE.read_text(encoding="utf-8")
    hi_html = HI_PAGE.read_text(encoding="utf-8")
    shared_markers = (
        'id="figure-overview-one-pager"',
        'id="figure-short-factual-cost"',
        "benchmark-01-cost-per-request.png",
    )
    for marker in shared_markers:
        assert marker in ko_html
        assert marker in hi_html

    assert _main_child_signature(hi_html) == _main_child_signature(ko_html)
    assert hi_html.count("<figure") == ko_html.count("<figure")
    assert 'aria-label="इस शृंखला की संरचना"' in hi_html
    assert "when-reasoning-pays-off-overview.hi.svg" in hi_html


def test_hindi_overview_local_references_resolve() -> None:
    html = HI_PAGE.read_text(encoding="utf-8")
    references = (
        ("href", "when-reasoning-pays-off-overview.hi.svg"),
        ("src", "when-reasoning-pays-off-overview.hi.svg"),
        ("src", "benchmark-01-cost-per-request.png"),
        ("href", "cost-per-request.json"),
    )
    for attribute, filename in references:
        assert _local_reference(html, attribute, filename).is_file()


def test_hindi_overview_svg_is_present_and_localized() -> None:
    svg = HI_OVERVIEW_SVG.read_text(encoding="utf-8")
    assert "<title" in svg
    assert "रीज़निंग" in svg
    assert "टोकन" in svg
    assert "बिल" in svg
    assert "A reasoning model can share" not in svg
    assert "토큰 단가표" not in svg
    assert "推論" not in svg


def test_hindi_chart_locale_matches_korean_keyset_without_fallback() -> None:
    ko = json.loads((LOCALE_DIR / "ko.json").read_text(encoding="utf-8"))
    hi = json.loads((LOCALE_DIR / "hi.json").read_text(encoding="utf-8"))
    assert _leaf_paths(hi) == _leaf_paths(ko)
    assert hi["meta"]["fallback"] is False
    assert hi["meta"]["translation_status"] == "translated"
