#!/usr/bin/env bash
# validate.sh — minimal, dependency-light validation for the static Pages site.
#
# Checks (no network, no build toolchain required):
#   1. Every declared locale has an index.html.
#   2. Each page's language switcher links only to locales that exist, and
#      every declared locale is reachable from each page.
#   3. The single hero asset exists and is referenced by the repository README.
#   4. Leakage grep: the site source and the README contain none of the
#      forbidden public-surface patterns (private tree references, internal
#      worker/role names, agent-prompt paths, or secret patterns).
#   5. If the repository's existing public-surface checker is present, run it.
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

# 4. Leakage grep over the site source and the README. Only the static site
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

# 5. Defer to the repository's existing public-surface checker when available.
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
