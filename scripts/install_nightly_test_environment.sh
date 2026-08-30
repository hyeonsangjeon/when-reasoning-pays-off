#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 2 ]]; then
  echo "usage: $0 VENV_DIR {locked|current}" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv_dir="$1"
graph="$2"
python_bootstrap="${PYTHON_BOOTSTRAP:-python}"
release_lock="$repo_root/batch-runner/batch_runner/data/dependencies/release-py311-linux-x86_64.txt"
test_lock="$repo_root/batch-runner/batch_runner/data/dependencies/nightly-test-tools-py311-linux-x86_64.txt"

if [[ -e "$venv_dir" ]]; then
  echo "nightly environment path already exists: $venv_dir" >&2
  exit 2
fi

"$python_bootstrap" -m venv "$venv_dir"
py="$venv_dir/bin/python"

case "$graph" in
  locked)
    "$py" -m pip install --require-hashes -r "$release_lock"
    "$py" -m pip install --no-deps "$repo_root"
    "$py" -m pip install --require-hashes -r "$test_lock"
    ;;
  current)
    "$py" -m pip install --upgrade --upgrade-strategy eager \
      "$repo_root[all,dev]" pytest-timeout
    ;;
  *)
    echo "unknown nightly dependency graph: $graph" >&2
    exit 2
    ;;
esac

"$py" -m pip check
