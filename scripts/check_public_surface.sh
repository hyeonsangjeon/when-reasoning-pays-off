#!/usr/bin/env bash
# check_public_surface.sh — defensive grep for the public Track A repo.
#
# Fails when the public, tracked surface of this repository contains:
#
#   1. Path references to .github/agents/ (the internal worker prompts
#      that should not live in the public tree; the dir itself is
#      git-removed under Task 034).
#   2. Internal agent role names (extreme-reasoner, first-reviewer,
#      measurement-engineer, strategy-consultant, llm-systems-engineer,
#      frontend-developer, ui-designer, git-committer) appearing in
#      tracked public files. CHANGELOG.md is excluded because it carries
#      historic entries that the redaction worker (out-of-scope for
#      Task 034) owns; this script does not attempt to redact historic
#      changelog content.
#   3. References to the private .internal/ tree from tracked public
#      files. The .internal/ directory itself is gitignored. References
#      are allowed only from CHANGELOG.md (historic) and from this
#      script (because the script lists them as policy examples).
#      Doc policy examples that legitimately reference .internal/ as a
#      scope statement (e.g. docs/15) are allowed because the script
#      excludes those specific files by path.
#   4. Secret patterns (sk-…, AZURE_OPENAI_API_KEY=…, HF_TOKEN=…,
#      Bearer …).
#
# Scope:
#   - Operates only on git-tracked files (`git ls-files`).
#   - Never reads .internal/ (it is gitignored).
#   - Never reads benchmarks/, results/, logs/, experiments/, or
#     pricing/ raw artifacts — those are the measurement-engineer
#     redaction worker's scope, not this script's.
#
# Exit codes:
#   0 — all checks passed.
#   1 — one or more checks failed; the offending paths and patterns are
#       printed to stderr.

set -euo pipefail

cd "$(dirname "$0")/.."

failed=0

# ---------------------------------------------------------------------------
# Build the list of tracked, public-tier files we want to scan.
#
# We deliberately exclude:
#   * .internal/   — gitignored (not tracked); listed for clarity.
#   * .gitignore   — must contain ".internal/" by design.
#   * CHANGELOG.md — historic content; redaction worker scope.
#   * benchmarks/, results/, logs/, experiments/, pricing/ — raw and
#     semi-raw data; redaction worker scope.
#   * tmp_task019_*/  — operator-local scratch trees pre-dating the
#     public release work; redaction worker scope.
#   * this script itself — it lists the patterns it searches for, so a
#     substring match would be a false positive.
#
# A second, narrower exclude (REDACTION_WORKER_SCOPE) covers tracked
# Python files under scripts/ and tests/ that pre-date the redaction
# policy and carry historic references in source comments (e.g. to
# .internal/tasks/NNN-…md spec paths or to legacy review roles). Those
# files are owned by the long-running measurement-engineer workflows
# (Task 019, Task 018, Task 008, Task 003, Task 007) and will be
# redacted as part of that worker's pass — not by Task 034. This
# script flags any NEW addition outside that list so the CI bar
# remains useful even before the historic sweep completes.
# ---------------------------------------------------------------------------

# Files that carry pre-existing references to .internal/ specs or to
# legacy review roles in source comments. The redaction worker will
# scrub these in a separate sweep. Keep this list alphabetized and
# narrow; do NOT extend it for new code.
REDACTION_WORKER_SCOPE='^(scripts/(_fixture_synth_03|analyze_tokens|cost_calculator|measure_cache_key_bucketing|measure_max_output_tokens_sweep|plot_results|task019_v25_adaptive)\.py$|tests/(fixtures/pricing/azure-openai-payg-2026-05\.yaml$|test_analyze_tokens\.py$|test_measure_cache_key_bucketing\.py$|test_measure_max_output_tokens_sweep\.py$|test_plot_results\.py$|test_run_benchmark\.py$|test_preflight_native_spillover\.py$))$'

# ---------------------------------------------------------------------------
# Sanitizer self-reference allowlist (Task 034 fix loop, FC1).
#
# The redaction sweep itself (`scripts/sanitize_public_artifacts.py`)
# and its test suite (`tests/test_sanitize_public_artifacts.py`) are
# the components that *implement* the public/private tier boundary
# defined in docs/16. They legitimately and necessarily reference the
# `.internal/raw-archive/` tree path because:
#   * The sanitizer reads tracked public-tree files and writes the
#     unredacted originals into `.internal/raw-archive/<archive_dir>/`
#     as part of the scientific-record preserve mandated by §6 of the
#     policy doc. The literal path `.internal/raw-archive/` therefore
#     appears in module docstrings, scope-exclusion constants
#     (`EXCLUDED_DIR_PREFIXES`), and the manifest-entry archive path
#     prefix it emits.
#   * The test suite verifies all of the above, so it must use the
#     same path literal in fixtures and assertions.
# These references are intentional, audited, and stable. Allowlisting
# them here is narrower and clearer than dropping them into the broad
# REDACTION_WORKER_SCOPE bucket (which exists for files that the
# redaction worker still needs to scrub in a separate sweep — these
# two files are the *opposite*: they are the redaction worker's own
# implementation and should not be scrubbed).
#
# Scope limits:
#   * Allowlist applies ONLY to the `internal-tree-ref` check below.
#   * Allowlist does NOT cover secret patterns, agent role names, or
#     `.github/agents/` path references — those scans still run
#     against both files.
#   * Do NOT extend this allowlist for any file other than the
#     sanitizer pair. Any new public file needing to reference
#     `.internal/` should instead be re-designed not to.
# ---------------------------------------------------------------------------
SANITIZER_INTERNAL_REF_ALLOWLIST='^(scripts/sanitize_public_artifacts\.py$|tests/test_sanitize_public_artifacts\.py$)$'

tracked_file_list="$(mktemp -t check_public_surface.XXXXXX)"
trap 'rm -f "$tracked_file_list"' EXIT

git ls-files \
  | grep -v -E '^(\.internal/|\.gitignore$|CHANGELOG\.md$|benchmarks/|results/|logs/|experiments/|pricing/|tmp_task[0-9]+|scripts/check_public_surface\.sh$)' \
  > "$tracked_file_list" || true

if [ ! -s "$tracked_file_list" ]; then
  echo "check_public_surface: no tracked public files to scan (empty list)" >&2
  exit 1
fi

scan() {
  label="$1"
  pattern="$2"
  shift 2
  # Remaining args are file-path regex patterns to drop from the scan
  # set on top of the global exclude built above.

  list="$tracked_file_list"
  if [ "$#" -gt 0 ]; then
    list_filtered="$(mktemp -t check_public_surface_scan.XXXXXX)"
    # Build a single OR-joined regex from the extra excludes.
    extra_re=""
    for pat in "$@"; do
      if [ -z "$extra_re" ]; then
        extra_re="$pat"
      else
        extra_re="$extra_re|$pat"
      fi
    done
    grep -v -E "$extra_re" "$tracked_file_list" > "$list_filtered" || true
    list="$list_filtered"
  fi

  matches="$(xargs -I{} grep -InE "$pattern" {} /dev/null < "$list" 2>/dev/null || true)"

  if [ "$list" != "$tracked_file_list" ]; then
    rm -f "$list"
  fi

  if [ -n "$matches" ]; then
    echo "FAIL [$label]: forbidden pattern found in tracked public files:" >&2
    echo "$matches" >&2
    echo >&2
    failed=1
  else
    echo "OK   [$label]"
  fi
}

# 1. Path references to .github/agents/
scan "agents-path-ref" '\.github/agents/'

# 2. Internal agent role names. Allow docs/16, docs/17, and GOVERNANCE.md
#    to name roles defensively (current text does not; future policy
#    text may). Allow the historic measurement-engineer scripts and
#    tests via REDACTION_WORKER_SCOPE.
scan "agent-role-names" \
  '(^|[^a-zA-Z0-9_-])(extreme-reasoner|first-reviewer|measurement-engineer|strategy-consultant|llm-systems-engineer|frontend-developer|ui-designer|git-committer)([^a-zA-Z0-9_-]|$)' \
  '^docs/16-release-tiers-and-redaction-policy\.md$' \
  '^docs/17-foundry-packaging-relationship\.md$' \
  '^GOVERNANCE\.md$' \
  "$REDACTION_WORKER_SCOPE"

# 3. References to .internal/ from tracked public files. docs/15 carries
#    an explicit scope statement that names .internal/REPO_SCAFFOLD_SPEC.md
#    as out-of-modify; allow it. Historic Python scripts/tests reference
#    .internal/tasks/NNN-…md spec paths in module docstrings — those are
#    redaction worker scope. The sanitizer (scripts/sanitize_public_artifacts.py)
#    and its tests legitimately reference `.internal/raw-archive/` because
#    that is the tier-boundary path they implement; they are covered by
#    SANITIZER_INTERNAL_REF_ALLOWLIST documented above.
scan "internal-tree-ref" \
  '\.internal/' \
  '^docs/15-spec-vs-inference-taxonomy\.md$' \
  "$REDACTION_WORKER_SCOPE" \
  "$SANITIZER_INTERNAL_REF_ALLOWLIST"

# 4. Secret patterns. Real secrets are forbidden anywhere. The historic
#    test_preflight_native_spillover.py file carries synthetic Bearer
#    fixture strings as test data (the deny-list it exercises); it is
#    on the redaction worker scope list and is excluded from the
#    Bearer scan to prevent CI flapping until that sweep lands.
scan "secret-openai-sk"       '(^|[^a-zA-Z0-9])sk-[A-Za-z0-9_-]{16,}'
scan "secret-azure-key"       'AZURE_OPENAI_API_KEY[[:space:]]*=[[:space:]]*[^[:space:]"'"'"'#]+'
scan "secret-openai-key"      '(^|[^A-Z_])OPENAI_API_KEY[[:space:]]*=[[:space:]]*[^[:space:]"'"'"'#]+'
scan "secret-hf-token"        'HF_TOKEN[[:space:]]*=[[:space:]]*[^[:space:]"'"'"'#]+'
scan "secret-bearer"          'Bearer[[:space:]]+[A-Za-z0-9._-]{16,}' \
  "$REDACTION_WORKER_SCOPE"

if [ "$failed" -ne 0 ]; then
  echo >&2
  echo "check_public_surface: one or more checks failed." >&2
  echo "If a finding is a known historic-content item that the redaction" >&2
  echo "worker owns (CHANGELOG.md, benchmarks/, results/, logs/, etc.)," >&2
  echo "confirm that the matching file path is on the exclude list above." >&2
  exit 1
fi

echo "check_public_surface: all checks passed."
