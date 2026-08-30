#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_bin="${PYTHON_BIN:?PYTHON_BIN must point to the locked nightly interpreter}"
export PYTHONPATH="$repo_root/.github/offline-python${PYTHONPATH:+:$PYTHONPATH}"
export AZURE_OPENAI_API_KEY=""
export AZURE_OPENAI_FOUNDRY_ENDPOINT=""
export AZURE_OPENAI_DEPLOYMENT_GPT_5_2=""
export AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED=""
export OPENAI_API_KEY=""
export HF_TOKEN=""

common=(-q -p no:cacheprovider --timeout=60)
"$python_bin" -m pytest "${common[@]}" batch-runner/tests

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
"$python_bin" -m pytest "${common[@]}" "${root_tests[@]}"
