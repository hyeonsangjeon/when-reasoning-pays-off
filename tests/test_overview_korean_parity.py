from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


REPO_ROOT = Path(__file__).resolve().parent.parent
ARTICLE_DIR = REPO_ROOT / "docs" / "blog" / "articles" / "when-reasoning-pays-off"
EN_PAGE = ARTICLE_DIR / "index.html"
KO_PAGE = ARTICLE_DIR / "ko" / "index.html"
KO_OVERVIEW_SVG = REPO_ROOT / "docs" / "assets" / "when-reasoning-pays-off-overview.ko.svg"


def test_korean_overview_restores_canonical_content_blocks() -> None:
    en_html = EN_PAGE.read_text(encoding="utf-8")
    ko_html = KO_PAGE.read_text(encoding="utf-8")

    shared_markers = (
        'id="figure-overview-one-pager"',
        'id="figure-short-factual-cost"',
        "benchmark-01-cost-per-request.png",
    )
    for marker in shared_markers:
        assert marker in en_html
        assert marker in ko_html

    assert 'aria-label="이 시리즈의 구성"' in ko_html
    assert "when-reasoning-pays-off-overview.ko.svg" in ko_html


def test_korean_overview_local_references_resolve() -> None:
    html = KO_PAGE.read_text(encoding="utf-8")
    for filename in (
        "when-reasoning-pays-off-overview.ko.svg",
        "benchmark-01-cost-per-request.png",
        "cost-per-request.json",
    ):
        match = re.search(
            rf'(?:href|src)=["\'](?P<url>[^"\']*{re.escape(filename)}[^"\']*)["\']',
            html,
        )
        assert match, f"missing URL for {filename}"
        parsed = urlsplit(match.group("url"))
        assert not parsed.scheme
        assert not parsed.netloc
        assert (KO_PAGE.parent / unquote(parsed.path)).resolve().is_file()


def test_korean_overview_svg_is_present_and_localized() -> None:
    svg = KO_OVERVIEW_SVG.read_text(encoding="utf-8")
    assert "<title" in svg
    assert "추론이 값을 하는 경우와 그렇지 않은 경우" in svg
    assert "토큰 단가표" in svg
    assert "청구액이 전혀 달라질 수 있습니다." in svg
    assert "A reasoning model can share" not in svg
    assert 'text x="116" y="356" font-size="11.5" font-weight="600" fill="#A33D00"' in svg
    assert 'text x="64" y="644" font-size="12.5" fill="#5C6470"' in svg
    assert '<text x="116" y="356" font-size="11.5" font-weight="600" fill="#D55E00"' not in svg
    assert '<text x="64" y="644" font-size="12.5" fill="#9AA3AD"' not in svg
