#!/usr/bin/env python3
"""Validate the public blog article release surface."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
BLOG_ROOT = ROOT / "docs" / "blog"
ARTICLE_ROOT = ROOT / "docs" / "blog" / "articles"
ARTICLE_DIR = ARTICLE_ROOT / "when-reasoning-pays-off"
LEDGER = ARTICLE_DIR / "numeric-claims.json"

LOCALE_PAGES = {
    "en": ARTICLE_DIR / "index.html",
    "ko": ARTICLE_DIR / "ko" / "index.html",
    "ja": ARTICLE_DIR / "ja" / "index.html",
    "zh-CN": ARTICLE_DIR / "zh-CN" / "index.html",
    "hi": ARTICLE_DIR / "hi" / "index.html",
}

EXPECTED_STATUS = {
    "en": "canonical",
    "ko": "translated",
    "ja": "translated",
    "zh-CN": "translated",
    "hi": "translated",
}

PUBLIC_SUFFIXES = {".html", ".md", ".json"}
HEX_SHA_RE = re.compile(r"^[a-f0-9]{64}$")
ROOT_RELATIVE_BLOG_URL_RE = re.compile(r"\b(?:href|src)=[\"']/blog/")

# Prose-hygiene patterns (see PROSE_PATTERN_LABELS) target human-readable prose,
# so they are evaluated against a "prose view" of each page that masks inline
# <code> spans and href/src link targets. Documented public Azure identifiers
# quoted in code and official Microsoft citation URLs are legitimate public
# references, not internal leakage; the stricter secret/path/role patterns and
# the CI-enforced check_public_surface.sh still scan the full text.
PROSE_PATTERN_LABELS = frozenset({"pricing-url", "pricing-access-date"})
INLINE_CODE_RE = re.compile(r"<code\b[^>]*>.*?</code>", re.IGNORECASE | re.DOTALL)
LINK_TARGET_RE = re.compile(r"\b(?:href|src)\s*=\s*[\"'][^\"']*[\"']", re.IGNORECASE)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {message}")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def meta_content(html: str, name: str) -> str | None:
    pattern = re.compile(
        r"<meta\s+[^>]*name=[\"']"
        + re.escape(name)
        + r"[\"'][^>]*content=[\"']([^\"']+)[\"']",
        re.IGNORECASE,
    )
    match = pattern.search(html)
    return match.group(1) if match else None


def extract_main_inner(html: str) -> str:
    matches = list(
        re.finditer(
            r"<main\b[^>]*>(?P<inner>.*?)</main>",
            html,
            re.IGNORECASE | re.DOTALL,
        )
    )
    require(len(matches) == 1, "English article must have exactly one main element")
    return matches[0].group("inner")


def article_sha(html: str) -> str:
    return hashlib.sha256(extract_main_inner(html).encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict:
    return json.loads(read_text(path))


def resolve_repo_path(value: str, field: str) -> Path:
    path = Path(value)
    require(not path.is_absolute(), f"{field} must be repo-relative: {value}")
    require(".." not in path.parts, f"{field} must not escape the repo: {value}")
    resolved = ROOT / path
    require(resolved.exists(), f"{field} does not exist: {value}")
    return resolved


def check_required_files() -> None:
    required = [ARTICLE_ROOT / "index.html", LEDGER, *LOCALE_PAGES.values()]
    for path in required:
        require(path.is_file(), f"missing required file: {rel(path)}")


def check_article_meta() -> str:
    en_html = read_text(LOCALE_PAGES["en"])
    canonical_sha = article_sha(en_html)
    require(HEX_SHA_RE.match(canonical_sha) is not None, "bad canonical article sha")
    require(meta_content(en_html, "article:locale") == "en", "English locale meta mismatch")
    require(
        meta_content(en_html, "article:translation-status") == "canonical",
        "English article must be canonical",
    )
    require('rel="canonical"' in en_html, "English article missing canonical link")

    for locale, page in LOCALE_PAGES.items():
        html = read_text(page)
        require(
            meta_content(html, "article:locale") == locale,
            f"{rel(page)} article:locale mismatch",
        )
        require(
            meta_content(html, "article:translation-status") == EXPECTED_STATUS[locale],
            f"{rel(page)} translation status mismatch",
        )
        if locale == "en":
            continue
        require(
            meta_content(html, "article:source-locale") == "en",
            f"{rel(page)} source locale must be en",
        )
        require(
            meta_content(html, "article:source-article-sha256") == canonical_sha,
            f"{rel(page)} source article sha mismatch",
        )
    return canonical_sha


def check_all_article_translation_hashes() -> int:
    checked = 0
    for manifest_path in sorted(ARTICLE_ROOT.rglob("i18n-parity.json"), key=rel):
        manifest = load_json(manifest_path)
        if manifest.get("canonical_locale", "en") != "en":
            continue

        locale_paths = manifest.get("locales")
        require(
            isinstance(locale_paths, dict),
            f"{rel(manifest_path)} must map its translated locales",
        )
        canonical_path = manifest_path.parent / locale_paths.get("en", "index.html")
        require(
            canonical_path.is_file(),
            f"{rel(manifest_path)} English canonical page is missing",
        )
        canonical_sha = article_sha(read_text(canonical_path))

        for locale, locale_path in locale_paths.items():
            if locale == "en":
                continue
            page = manifest_path.parent / locale_path
            require(page.is_file(), f"{rel(manifest_path)} locale page is missing: {locale}")
            html = read_text(page)
            source_locale = meta_content(html, "article:source-locale")
            source_sha = meta_content(html, "article:source-article-sha256")
            require(
                source_locale == "en",
                f"{rel(page)} source locale must be en",
            )
            require(
                source_sha == canonical_sha,
                f"{rel(page)} source article sha mismatch",
            )
            checked += 1
    return checked


def check_ledger(canonical_sha: str) -> int:
    ledger = load_json(LEDGER)
    require(
        ledger.get("canonical_article_sha256") == canonical_sha,
        "numeric ledger canonical_article_sha256 mismatch",
    )
    claims = ledger.get("claims")
    require(isinstance(claims, list) and claims, "numeric ledger must contain claims")

    for claim in claims:
        claim_id = claim.get("id", "<missing id>")
        require(claim.get("status") == "verified", f"{claim_id} status must be verified")

        display = claim.get("display_value")
        require(isinstance(display, str) and display, f"{claim_id} missing display_value")

        source_paths = claim.get("source_paths")
        require(
            isinstance(source_paths, list) and source_paths,
            f"{claim_id} missing source_paths",
        )
        source_texts = []
        for source_path in source_paths:
            require(isinstance(source_path, str), f"{claim_id} source path must be text")
            source_texts.append(read_text(resolve_repo_path(source_path, "source_path")))

        for source_value in claim.get("source_values", []):
            require(
                any(str(source_value) in source_text for source_text in source_texts),
                f"{claim_id} source value not found in listed sources: {source_value}",
            )

        locations = claim.get("locations")
        require(
            isinstance(locations, list) and locations,
            f"{claim_id} missing locations",
        )
        found_display = False
        for location in locations:
            require(isinstance(location, dict), f"{claim_id} location must be an object")
            location_file = location.get("file")
            require(isinstance(location_file, str), f"{claim_id} location missing file")
            location_path = resolve_repo_path(location_file, "location.file")
            if display in read_text(location_path):
                found_display = True
        require(found_display, f"{claim_id} display value not found: {display}")

    return len(claims)


def extract_section(html: str, section_id: str) -> str:
    pattern = re.compile(
        r"<section\s+id=[\"']"
        + re.escape(section_id)
        + r"[\"'][^>]*>(?P<section>.*?)</section>",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(html)
    require(match is not None, f"missing section: {section_id}")
    return match.group("section")


def check_ptu_wording() -> None:
    raw_section = extract_section(read_text(LOCALE_PAGES["en"]), "ptu-payg").lower()
    section = re.sub(r"\s+", " ", raw_section)
    require("modeled hypothesis" in section, "PTU section must say modeled hypothesis")
    require(
        "not measured ptu throughput" in section,
        "PTU section must say not measured PTU throughput",
    )


def check_korean_overview_parity() -> None:
    ko_page = LOCALE_PAGES["ko"]
    ko_html = read_text(ko_page)
    required_markers = {
        "overview one-pager": 'id="figure-overview-one-pager"',
        "Korean overview SVG": "when-reasoning-pays-off-overview.ko.svg",
        "series structure guide": 'aria-label="이 시리즈의 구성"',
        "short-factual source chart": 'id="figure-short-factual-cost"',
        "short-factual chart image": "benchmark-01-cost-per-request.png",
    }
    for label, marker in required_markers.items():
        require(marker in ko_html, f"Korean overview missing {label}: {marker}")

    required_references = {
        "Korean overview SVG": "when-reasoning-pays-off-overview.ko.svg",
        "short-factual chart image": "benchmark-01-cost-per-request.png",
        "served chart data": "cost-per-request.json",
    }
    for label, filename in required_references.items():
        match = re.search(
            rf'(?:href|src)=["\'](?P<url>[^"\']*{re.escape(filename)}[^"\']*)["\']',
            ko_html,
        )
        require(match is not None, f"Korean overview missing {label} URL")
        parsed = urlsplit(match.group("url"))
        require(
            not parsed.scheme and not parsed.netloc,
            f"Korean overview {label} must be a relative URL",
        )
        target = (ko_page.parent / unquote(parsed.path)).resolve()
        require(target.is_file(), f"Korean overview {label} target missing: {rel(target)}")


def check_no_root_relative_blog_urls() -> int:
    checked = 0
    for path in sorted(BLOG_ROOT.rglob("*.html"), key=rel):
        checked += 1
        for line_number, line in enumerate(read_text(path).splitlines(), start=1):
            if ROOT_RELATIVE_BLOG_URL_RE.search(line):
                raise SystemExit(
                    "FAIL: root-relative blog URL in "
                    f"{rel(path)}:{line_number}; use a relative URL so GitHub "
                    "Pages project paths keep working"
                )
    return checked


def git_changed_files(changed_from: str) -> list[Path]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRT", changed_from],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    files = []
    for line in result.stdout.splitlines():
        if line:
            files.append(ROOT / line)
    return files


def public_scan_paths(changed_from: str) -> list[Path]:
    candidates = {ARTICLE_ROOT / "index.html", LEDGER, *LOCALE_PAGES.values()}
    for path in git_changed_files(changed_from):
        if path.name == "CHANGELOG.md":
            continue
        if path.suffix in PUBLIC_SUFFIXES and path.exists() and ARTICLE_ROOT in path.parents:
            candidates.add(path)
    return sorted(candidates, key=rel)


def diff_added_text(path: Path, changed_from: str) -> str:
    result = subprocess.run(
        ["git", "diff", "--unified=0", changed_from, "--", rel(path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    added_lines = []
    for line in result.stdout.splitlines():
        if line.startswith("+++") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            added_lines.append(line[1:])
    return "\n".join(added_lines)


def forbidden_patterns() -> list[tuple[str, re.Pattern[str]]]:
    private_tree = re.escape(".internal") + r"/"
    prompt_dir = re.escape(".github") + r"/agents/"
    role_prefix = r"(extreme|first|measurement|strategy|llm-systems|frontend|ui|git)"
    role_suffix = r"(reasoner|reviewer|engineer|consultant|developer|designer|committer)"
    role_name = rf"(^|[^A-Za-z0-9_-]){role_prefix}-{role_suffix}([^A-Za-z0-9_-]|$)"
    return [
        ("private-local-path", re.compile(r"(/Users/|/home/|file://|[A-Za-z]:\\)")),
        ("private-tree-ref", re.compile(private_tree)),
        ("prompt-path-ref", re.compile(prompt_dir)),
        ("internal-task-label", re.compile(r"\b[Tt]ask[ -]?\d{3,}\b")),
        ("internal-role-name", re.compile(role_name)),
        # Leaked request/run IDs are opaque tokens (e.g. ``req_8f3e2d1c4b``,
        # ``run_a1b2c3d4``) and in practice always carry digits. The bare
        # ``run_`` prefix also begins ordinary public identifiers — notably the
        # documented ``scripts/run_benchmark.py`` entry point cited in the
        # README — so require an ID-shaped token (at least one digit) rather than
        # matching dictionary words like ``run_benchmark`` or ``run_report``.
        # The stricter secret patterns and CI-enforced check_public_surface.sh
        # still scan the full text for real credentials.
        (
            "request-or-run-id",
            re.compile(r"\b(req|request|run)[_-](?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{6,}\b"),
        ),
        # A concrete Azure resource endpoint hostname is a real leak vector. The
        # bare words "endpoint"/"deployment" are ordinary prose and also appear
        # inside documented public Azure identifiers (e.g. the response header
        # x-ms-spillover-from-deployment), so match the hostname form, not the
        # dictionary words.
        (
            "endpoint-host",
            re.compile(
                r"\b[a-z0-9][a-z0-9-]*\.(?:openai|cognitiveservices)\.azure\.com\b"
                r"|\b[a-z0-9][a-z0-9-]*\.services\.ai\.azure\.com\b",
                re.IGNORECASE,
            ),
        ),
        ("pricing-url", re.compile(r"https?://[^\s\"'<>]*pricing[^\s\"'<>]*", re.IGNORECASE)),
        ("pricing-access-date", re.compile(r"pricing access", re.IGNORECASE)),
        ("secret-openai-sk", re.compile(r"(^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}")),
        (
            "secret-key-assignment",
            re.compile(r"(OPENAI_API_KEY|AZURE_OPENAI_API_KEY|HF_TOKEN)\s*=\s*[^\s\"'#]+"),
        ),
        ("secret-bearer", re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}")),
    ]


def prose_view(text: str) -> str:
    """Return text with inline ``<code>`` spans and ``href``/``src`` link
    targets stripped, so prose-hygiene rules see only human-readable prose.

    Documented public API identifiers quoted in code (e.g. Azure's
    ``x-ms-spillover-from-deployment`` response header or the
    ``spilloverDeploymentName`` property) and citation link targets (e.g. the
    official Azure pricing page and its archive snapshot) are legitimate public
    references, not internal leakage. They are still covered by the stricter
    secret/path/role patterns — which scan the full text — and by the
    CI-enforced ``check_public_surface.sh``, neither of which forbids public
    Microsoft citations.
    """
    text = INLINE_CODE_RE.sub(" ", text)
    text = LINK_TARGET_RE.sub(" ", text)
    return text


def scan_text(label: str, text: str) -> None:
    prose = prose_view(text)
    for pattern_label, pattern in forbidden_patterns():
        target = prose if pattern_label in PROSE_PATTERN_LABELS else text
        match = pattern.search(target)
        if match is not None:
            raise SystemExit(
                "FAIL: forbidden public pattern "
                f"{pattern_label} in {label}: {match.group(0)!r}"
            )


def check_forbidden_public_patterns(changed_from: str, pr_metadata_file: Path | None) -> int:
    paths = public_scan_paths(changed_from)
    for path in paths:
        scan_text(rel(path), read_text(path))

    scanned_labels = {rel(path) for path in paths}
    for path in git_changed_files(changed_from):
        if path.name == "CHANGELOG.md":
            continue
        if path.suffix not in PUBLIC_SUFFIXES or not path.exists():
            continue
        label = rel(path)
        if label in scanned_labels:
            continue
        added_text = diff_added_text(path, changed_from)
        if added_text:
            scan_text(f"{label} added lines", added_text)
            scanned_labels.add(label)

    if pr_metadata_file is not None:
        require(pr_metadata_file.is_file(), f"missing PR metadata file: {pr_metadata_file}")
        scan_text(str(pr_metadata_file), read_text(pr_metadata_file))

    return len(scanned_labels)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--changed-from", required=True)
    parser.add_argument("--pr-metadata-file", type=Path)
    args = parser.parse_args()

    check_required_files()
    canonical_sha = check_article_meta()
    translation_hash_count = check_all_article_translation_hashes()
    claim_count = check_ledger(canonical_sha)
    check_ptu_wording()
    check_korean_overview_parity()
    blog_url_count = check_no_root_relative_blog_urls()
    scanned_count = check_forbidden_public_patterns(
        args.changed_from,
        args.pr_metadata_file,
    )
    print(
        "check passed: blog article release "
        f"sha={canonical_sha} claims={claim_count} "
        f"translation_hashes={translation_hash_count} "
        f"blog_html_files={blog_url_count} scanned_public_files={scanned_count}"
    )


if __name__ == "__main__":
    main()
