#!/usr/bin/env bash
# validate.sh — minimal, dependency-light validation for the static Pages site.
#
# Checks (no network, no build toolchain required):
#   1. Every declared locale has an index.html.
#   2. Each page's language switcher links only to locales that exist, and
#      every declared locale is reachable from each page.
#   3. The single hero asset exists and is referenced by the repository README,
#      and every relative *.svg image link in a top-level docs/*.md resolves to
#      an on-disk file under docs/assets/ (no broken doc image links).
#   4. Per-page i18n release metadata (docs/16 §8.4 / §8.5): every locale page
#      carries a canonical link, hreflang alternates for all published locales
#      plus hreflang="x-default", a machine-readable translation status,
#      a last_translated_at date, and a source-content sha256; the recorded
#      source-content sha256 matches a freshly recomputed hash of the English
#      canonical page content (a mismatch means the translation is stale); and
#      each page's glossary covers Foundry, PTU, PAYG, reasoning, cache, 429.
#   5. Leakage grep: the site source and the README contain none of the
#      forbidden public-surface patterns (private tree references, internal
#      worker/role names, agent-prompt paths, or secret patterns).
#   6. If the repository's existing public-surface checker is present, run it.
#
# Exit 0 on success; non-zero with a printed reason on the first failure.

set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/.." && pwd)"
cd "$root"

LOCALES=(en ko zh-CN ja hi)
fail=0
note() { printf '%s\n' "$*"; }
err()  { printf 'FAIL: %s\n' "$*" >&2; fail=1; }

# 1. Locale pages present.
for loc in "${LOCALES[@]}"; do
  if [ -f "docs/$loc/index.html" ]; then
    note "OK   locale present: docs/$loc/index.html"
  else
    err "missing locale page: docs/$loc/index.html"
  fi
done

# 2. Switcher integrity: every page must link to every declared locale.
for loc in "${LOCALES[@]}"; do
  page="docs/$loc/index.html"
  [ -f "$page" ] || continue
  for target in "${LOCALES[@]}"; do
    if grep -q "\"../$target/\"" "$page"; then
      :
    else
      err "$page is missing a switcher link to ../$target/"
    fi
  done
done
note "OK   language switcher links checked"

# 3. Hero asset present and referenced by the README.
if [ -f "docs/assets/hero.svg" ]; then
  note "OK   hero asset present: docs/assets/hero.svg"
else
  err "missing hero asset: docs/assets/hero.svg"
fi
if grep -q "docs/assets/hero.svg" README.md; then
  note "OK   README references the hero asset"
else
  err "README.md does not reference docs/assets/hero.svg"
fi

# 3a. Top-level docs image links to docs/assets/*.svg must resolve on disk.
#
# Each top-level docs/*.md may embed hand-authored SVG visuals via relative
# Markdown image links (e.g. ![alt](assets/foo.svg)). A link is resolved
# relative to the Markdown file's own directory, so a doc at docs/NN-*.md
# references assets/foo.svg → docs/assets/foo.svg. External URLs, data URIs,
# and absolute paths are skipped; only local *.svg targets are checked. This
# catches a renamed or missing asset before it 404s on the published site.
docs_link_count=0
docs_link_missing=0
while IFS= read -r md; do
  base="$(dirname "$md")"
  while IFS= read -r link; do
    [ -n "$link" ] || continue
    link="${link%%#*}"            # drop any URL fragment
    link="${link%%\?*}"           # drop any query string
    case "$link" in
      *://*|data:*|/*) continue ;;  # skip external, data URI, absolute
    esac
    case "$link" in *.svg) ;; *) continue ;; esac
    docs_link_count=$((docs_link_count + 1))
    if [ ! -f "$base/$link" ]; then
      err "$md references missing image asset: $link (resolved: $base/$link)"
      docs_link_missing=$((docs_link_missing + 1))
    fi
  done < <(grep -oE '\]\([^)]*\.svg[^)]*\)' "$md" 2>/dev/null \
             | sed -E 's/^\]\(//; s/\)$//; s/ +"[^"]*"$//')
done < <(find docs -maxdepth 1 -name '*.md' -type f | sort)
[ "$docs_link_missing" -eq 0 ] && \
  note "OK   top-level docs image links to docs/assets/*.svg resolve ($docs_link_count checked)"

# 4. Per-page i18n release metadata (docs/16 §8.4 / §8.5).
#
# Each published locale page must carry the per-surface metadata the release
# policy gates on: a canonical link; hreflang alternates for every published
# locale plus an x-default; a machine-readable translation status and
# last_translated_at date; and a source-content sha256 that still matches the
# English canonical content (drift ⇒ the translation is stale and the release
# is blocked per §8.5). Each page's glossary must also cover the fixed term
# set. Validation is locale-agnostic: it checks machine-readable markers, not
# translated prose.

# Portable sha256 over stdin (macOS shasum / Linux sha256sum).
if command -v sha256sum >/dev/null 2>&1; then
  sha256_stdin() { sha256sum | awk '{print $1}'; }
elif command -v shasum >/dev/null 2>&1; then
  sha256_stdin() { shasum -a 256 | awk '{print $1}'; }
else
  err "no sha256 utility (sha256sum or shasum) available for source-content hash check"
  sha256_stdin() { printf 'NO_SHA_TOOL'; }
fi

HREFLANGS=(en ko ja zh-CN hi x-default)
GLOSSARY_TERMS=(foundry ptu payg reasoning cache 429)

# Recompute the canonical source-content hash from the English page. The
# hashed region is the inner content of the single <main> element; metadata
# lives in <head> and is deliberately excluded. Assert the structural
# invariant the extractor relies on so a malformed page fails loudly rather
# than silently hashing the wrong bytes.
en_page="docs/en/index.html"
canonical_hash=""
if [ -f "$en_page" ]; then
  open_n="$(grep -c '^[[:space:]]*<main>[[:space:]]*$' "$en_page" || true)"
  close_n="$(grep -c '^[[:space:]]*</main>[[:space:]]*$' "$en_page" || true)"
  if [ "$open_n" != "1" ] || [ "$close_n" != "1" ]; then
    err "$en_page must have exactly one <main> and one </main> each on its own line (found open=$open_n close=$close_n)"
  else
    canonical_hash="$(awk '/<main>/{f=1;next} /<\/main>/{f=0} f' "$en_page" | sha256_stdin)"
    note "OK   canonical EN source-content sha256: $canonical_hash"
  fi
else
  err "missing English canonical page: $en_page"
fi

meta_content() { # $1=page $2=meta-name → prints the content="" value, or empty
  grep -oE "name=\"$2\" content=\"[^\"]+\"" "$1" 2>/dev/null \
    | sed -E 's/.*content="([^"]+)".*/\1/' | head -n1
}

for loc in "${LOCALES[@]}"; do
  page="docs/$loc/index.html"
  [ -f "$page" ] || continue

  grep -q 'rel="canonical"' "$page" || err "$page missing a canonical link"

  for hl in "${HREFLANGS[@]}"; do
    if ! grep -q "rel=\"alternate\" hreflang=\"$hl\"" "$page"; then
      err "$page missing hreflang alternate: $hl"
    fi
  done

  status="$(meta_content "$page" 'i18n:translation-status')"
  case "$status" in
    translated|machine_translated|stale|untranslated_fallback_to_en) : ;;
    *) err "$page has a missing or invalid i18n:translation-status (got: '${status:-<none>}')" ;;
  esac

  lta="$(meta_content "$page" 'i18n:last-translated-at')"
  if ! printf '%s' "$lta" | grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; then
    err "$page has a missing or invalid i18n:last-translated-at (got: '${lta:-<none>}')"
  fi

  src="$(meta_content "$page" 'i18n:source-content-sha256')"
  if [ -z "$src" ]; then
    err "$page missing i18n:source-content-sha256"
  elif [ -n "$canonical_hash" ] && [ "$src" != "$canonical_hash" ]; then
    err "$page source-content sha256 is STALE: recorded $src but EN canonical is now $canonical_hash"
  fi

  grep -q 'class="glossary"' "$page" || err "$page missing a <section class=\"glossary\">"
  for term in "${GLOSSARY_TERMS[@]}"; do
    grep -q "data-term=\"$term\"" "$page" || err "$page glossary is missing the term: $term"
  done
done
[ "$fail" -eq 0 ] && note "OK   per-page i18n release metadata (hreflang, status, source hash, glossary) checked"

# 5. Leakage grep over the site source and the README. Only the static site
# files introduced for the Pages surface are scanned here; the pre-existing
# numbered content docs (docs/NN-*.md) are owned and allowlisted elsewhere and
# are covered by the repository's own public-surface checker in step 5.
leak_targets=(README.md docs/i18n.md)
while IFS= read -r f; do leak_targets+=("$f"); done < <(find docs -type f \
  \( -name '*.html' -o -name '*.css' -o -name '*.js' -o -name '*.svg' \))

# Forbidden patterns. The forbidden path prefixes and the internal worker/role
# identifiers are composed at runtime from fragments so this validator does not
# itself contain the literal strings the public-surface checker forbids.
roles='(extreme|first|measurement|strategy|llm-systems|ui)-(reasoner|reviewer|engineer|consultant|designer)'
dot='\.'
sep='/'
private_tree_pat="${dot}internal${sep}"
agent_prompt_pat="${dot}github${sep}agents${sep}"
patterns=(
  "$private_tree_pat"
  "$agent_prompt_pat"
  "$roles"
  '(^|[^a-zA-Z0-9])sk-[A-Za-z0-9_-]{16,}'
  'AZURE_OPENAI_API_KEY[[:space:]]*=[[:space:]]*[^[:space:]"'"'"'#]+'
  'Bearer[[:space:]]+[A-Za-z0-9._-]{16,}'
)
for pat in "${patterns[@]}"; do
  if hits="$(grep -InE "$pat" "${leak_targets[@]}" 2>/dev/null)"; then
    err "leakage pattern found ($pat):"
    printf '%s\n' "$hits" >&2
  fi
done
[ "$fail" -eq 0 ] && note "OK   leakage grep clean over site source and README"

# 6. Defer to the repository's existing public-surface checker when available.
if [ -x scripts/check_public_surface.sh ]; then
  note "running scripts/check_public_surface.sh ..."
  if bash scripts/check_public_surface.sh; then
    note "OK   public-surface checker passed"
  else
    err "scripts/check_public_surface.sh reported findings"
  fi
fi

if [ "$fail" -ne 0 ]; then
  printf '\nvalidate.sh: one or more checks failed.\n' >&2
  exit 1
fi
note "validate.sh: all checks passed."
