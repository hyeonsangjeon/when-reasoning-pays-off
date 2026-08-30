#!/usr/bin/env bash
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_bin="${PYTHON_BIN:-python3}"
log_dir="${NIGHTLY_LOG_DIR:-$repo_root/.nightly-test-logs}"
mkdir -p "$log_dir"
export PYTHONPATH="$repo_root/.github/offline-python${PYTHONPATH:+:$PYTHONPATH}"
export AZURE_OPENAI_API_KEY=""
export AZURE_OPENAI_FOUNDRY_ENDPOINT=""
export AZURE_OPENAI_DEPLOYMENT_GPT_5_2=""
export AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED=""
export OPENAI_API_KEY=""
export HF_TOKEN=""

common=(-q -p no:cacheprovider --timeout=60)
campaigns=(
  tests/test_measure_max_output_tokens_sweep.py
  tests/test_measure_cache_key_bucketing.py
  tests/test_measure_dual_spillover.py
)

run_pytest() {
  local label="$1"
  shift
  local raw="$log_dir/${label}.raw"
  local safe="$log_dir/${label}.log"
  local status=0
  "$python_bin" -m pytest "${common[@]}" "$@" >"$raw" 2>&1 || status=$?
  "$python_bin" scripts/sanitize_nightly_test_log.py "$raw" "$safe"
  rm -f "$raw"
  cat "$safe"
  return "$status"
}

status=0
for suite in "${campaigns[@]}"; do
  label="collect-$(basename "$suite" .py)"
  run_pytest "$label" --collect-only "$suite" || status=1
  grep -Eq '^[1-9][0-9]* tests? collected in ' "$log_dir/${label}.log" || {
    printf 'Campaign collection was not machine-verified: %s\n' "$suite" >&2
    status=1
  }
done

run_pytest batch-runner batch-runner/tests || status=1

root_tests=()
while IFS= read -r path; do
  root_tests+=("$path")
done < <(
  find tests -type f -name 'test_*.py' \
    ! -name 'test_measure_max_output_tokens_sweep.py' \
    ! -name 'test_measure_cache_key_bucketing.py' \
    ! -name 'test_measure_dual_spillover.py' \
    | LC_ALL=C sort
)
run_pytest root-tests "${root_tests[@]}" || status=1

for suite in "${campaigns[@]}"; do
  label="$(basename "$suite" .py)"
  run_pytest "$label" "$suite" || status=1
done

exit "$status"
