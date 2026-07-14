#!/usr/bin/env bash
#
# verify_setup.sh — prove, in ~10 seconds, that this repo runs on your machine.
#
# It checks three tiers of reproducibility, from "needs nothing" to "runs the
# real runner", WITHOUT ever calling a cloud service:
#
#   Tier A  (needs only `pyyaml`)  — introspect the experiment catalog and call
#           the deterministic primitives the runners are built on. Zero network,
#           zero credentials.
#   Tier B  (needs the runtime deps) — execute a runner end-to-end in --dry-run
#           mode with placeholder Azure config. No HTTPS call is made; synthetic
#           records are written to a temp location and cleaned up afterwards.
#
# Real evidence runs (Tier C) need an Azure OpenAI GPT-5.2 deployment and are
# intentionally out of scope here — see README.md "Reproducing these
# measurements".
#
# Usage:
#   bash scripts/verify_setup.sh            # Tier A + B
#   bash scripts/verify_setup.sh --quick    # Tier A only (no runner execution)
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

QUICK=0
[ "${1:-}" = "--quick" ] && QUICK=1

pass=0
fail=0
ok()   { printf '  [ OK ] %s\n' "$1"; pass=$((pass + 1)); }
no()   { printf '  [FAIL] %s\n' "$1"; fail=$((fail + 1)); }
note() { printf '  [note] %s\n' "$1"; }

# --------------------------------------------------------------------------
# 0. Interpreter
# --------------------------------------------------------------------------
echo "== 0. Python interpreter =="
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  no "no 'python3' or 'python' on PATH — install Python 3.11+ and re-run"
  echo ""
  echo "RESULT: FAIL (no Python interpreter)"
  exit 1
fi
PYVER="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
ok "using '$PY' (Python $PYVER)"
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  note "this repo targets Python 3.11+; $PYVER may work for the light path but is unsupported"
fi

# --------------------------------------------------------------------------
# 1. Minimal dependency (pyyaml) — the only thing Tier A needs
# --------------------------------------------------------------------------
echo ""
echo "== 1. Dependencies =="
if "$PY" -c 'import yaml' >/dev/null 2>&1; then
  ok "pyyaml is importable (enough for the catalog + pure-function tier)"
else
  no "pyyaml not found — run:  $PY -m pip install -r requirements.txt"
  echo ""
  echo "RESULT: FAIL (install dependencies first:  $PY -m pip install -r requirements.txt)"
  exit 1
fi

# --------------------------------------------------------------------------
# 2. Tier A — introspection + pure functions (no network, no credentials)
# --------------------------------------------------------------------------
echo ""
echo "== 2. Tier A — catalog + pure functions (no network, no credentials) =="
if out="$("$PY" experiments/examples/describe_all.py 2>&1)" \
   && echo "$out" | grep -qE '[0-9]+ experiments'; then
  n="$(printf '%s\n' "$out" | grep -oE '^[0-9]+ experiments' | head -1)"
  ok "experiments.describe / list_experiments works ($n)"
else
  no "experiments/examples/describe_all.py failed"
  printf '%s\n' "$out" | tail -5
fi

if out="$("$PY" experiments/examples/pure_functions.py 2>&1)" \
   && echo "$out" | grep -qi 'zero network calls'; then
  ok "pure primitives (select_bucket / reactive_decide / estimators) return output"
else
  no "experiments/examples/pure_functions.py failed"
  printf '%s\n' "$out" | tail -5
fi

# --------------------------------------------------------------------------
# 3. Tier B — run a runner end-to-end in dry-run (no HTTPS, cleaned up)
# --------------------------------------------------------------------------
if [ "$QUICK" -eq 0 ]; then
  echo ""
  echo "== 3. Tier B — runner dry-run (no HTTPS call; placeholder Azure config) =="

  RUNS="benchmarks/01-short-factual/runs"
  SNAP="$(mktemp)"
  cleanup_dryrun() {
    if [ -f "$SNAP" ]; then
      # Delete only the files this script created, leaving committed runs intact.
      find "$RUNS" -type f 2>/dev/null | sort \
        | comm -13 "$SNAP" - 2>/dev/null \
        | while IFS= read -r f; do [ -n "$f" ] && rm -f "$f"; done
      rm -f "$SNAP"
    fi
  }
  trap cleanup_dryrun EXIT
  mkdir -p "$RUNS"
  find "$RUNS" -type f 2>/dev/null | sort > "$SNAP"

  if out="$(env \
        AZURE_OPENAI_FOUNDRY_ENDPOINT='https://placeholder.invalid/' \
        AZURE_OPENAI_DEPLOYMENT_GPT_5_2='placeholder' \
        AZURE_OPENAI_DEPLOYMENT_GPT_4O='placeholder' \
        AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED='placeholder' \
        AZURE_AUTH_MODE='entra' \
        MAX_COST_PER_BENCHMARK_USD='100' \
        MAX_TOTAL_COST_USD='500' \
        "$PY" -m scripts.run_benchmark \
          --experiment experiments/exp001_short-factual_baseline.yaml \
          --dry-run --allow-dirty --max-samples 1 2>&1)"; then
    cells="$(printf '%s\n' "$out" | grep -oE 'cells_written[^0-9]*[0-9]+' | grep -oE '[0-9]+' | head -1)"
    ok "run_benchmark --dry-run completed (exit 0${cells:+, cells_written=$cells})"
  else
    no "run_benchmark --dry-run failed (see below)"
    printf '%s\n' "$out" | tail -8
    note "if this is an import error, install runtime deps:  $PY -m pip install -r requirements.txt"
  fi
else
  echo ""
  echo "== 3. Tier B — skipped (--quick) =="
fi

# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------
echo ""
echo "======================================================================"
if [ "$fail" -eq 0 ]; then
  echo "RESULT: PASS  ($pass checks)"
  echo ""
  echo "You can reproduce everything that does not need a cloud account:"
  echo "  - browse the catalog : $PY experiments/examples/describe_all.py"
  echo "  - run any experiment : $PY experiments/examples/run_any_experiment.py <exp>.yaml"
  echo "  - the test suite     : $PY -m pytest -q -m 'not adaptive_calibration' batch-runner/tests/"
  echo ""
  echo "For real evidence runs (Tier C) you need an Azure OpenAI GPT-5.2"
  echo "deployment + 'az login' — see README.md 'Reproducing these measurements'."
  exit 0
else
  echo "RESULT: FAIL  ($fail of $((pass + fail)) checks failed)"
  echo "Most failures are fixed by:  $PY -m pip install -r requirements.txt"
  exit 1
fi
