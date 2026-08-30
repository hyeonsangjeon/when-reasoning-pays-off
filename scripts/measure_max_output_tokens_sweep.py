"""scripts/measure_max_output_tokens_sweep.py — Task 019 v2.1
max_output_tokens admission-reservation proxy benchmark.

Runs a controlled log2 sweep over ``max_output_tokens`` ∈ ``[256, 512, 1024,
2048, 4096, 8192, 16384]`` against the ``ptu-deploy-throttled`` deployment
(PAYG GlobalStandard, 60K TPM / 600 RPM). All controls — system prompt
identity, user-prompt subset, ``reasoning.effort``, SDK retries,
concurrency, arrival schedule shape — are pinned across cells; the only
varying variable is the admission-time token reservation
(``max_output_tokens``). A monotone-decreasing 429-onset RPM curve, with
visible output flat across cells (±20%), is PAYG throttled-quota / Azure
admission-reservation **proxy** evidence for Hypothesis I. It is NOT
direct PTU evidence (no PTU deployment exists in this repo).

Pinned controls — single source of truth (v2.0 + v2.1 hotfixes); each is
re-validated at YAML load time and echoed into every per-request record
and every ``runs/*.summary.json``:

* ``client.api_version = "preview"`` (Foundry v1).
* ``client.max_retries = 0`` (SDK silently absorbs the first 429 onset
  signal at the default of 2; mandatory zero).
* ``request_template.reasoning.effort = "low"`` (gpt-5.2 rejects
  ``"minimal"``; ``"low"`` keeps actual reasoning small so visible output
  is roughly the visible-output measurement).
* ``runtime.dispatcher = "async_scheduled"`` (wall-clock pacer +
  asyncio.Semaphore-guarded in-flight bound).
* ``runtime.concurrency = 96`` (Task 018 v2.4 pin — sized for sporadic
  100s+ retry-induced outliers; max_retries=0 makes 429s fail fast so
  steady-state in-flight stays well below 96).
* ``runtime.prewarm_calls_per_cell = 12`` (4-minute pre-warm at 0.05 TPS;
  same at smoke and full so the warm criterion is enforceable at smoke —
  v2.1 blocker #3).
* ``runtime.peak_ramp_tps = 0.33`` (v2.1 blocker #4 retune from 0.35 to
  recover explicit margin under the 0.85 × quota = 51,000 TPM smallest-
  cell threshold).
* ``sweep.max_output_tokens = [256, 512, 1024, 2048, 4096, 8192, 16384]``
  (the ONLY varying knob; small → large ordering preserves truncated-
  curve usefulness if mid-run USD halt fires).
* Prompt identity contract (v2.1 blocker #1): source corpus
  ``benchmarks/04-spillover-simulation/system_prompt_corpus.json`` READ-
  ONLY with SHA ``6a8ab5a...``; assembled via
  ``scripts.simulate_spillover.build_system_prompt(..., corpus_seed=4242,
  target_tokens=2000)`` to assembled SHA ``f8a74528...``; user prompts
  source ``benchmarks/04-spillover-simulation/user_prompts.json`` READ-
  ONLY with SHA ``45f4a95b...``; 10-prompt working subset selected in
  memory by the pinned index set ``[0, 3, 6, 9, 12, 15, 18, 21, 24, 27]``.
  Runner aborts with exit code 7 on any SHA mismatch. NO Task-019-
  specific corpus or prompt files are written.
* Canonical TPM feasibility formula (v2.1 blocker #4, single source of
  truth across YAML, runner gate, pytest, and Cost & Time Budget):
  ``projected_tpm(cell) = 60 × peak_ramp_tps × (base_prompt_tokens_for_gate
  + cell.max_output_tokens)`` where ``base_prompt_tokens_for_gate =
  max(assembled_system_prompt_tokens, target_system_prompt_tokens) + 100``
  (cold-cache evaluation, used at startup before any pre-warm runs). At
  v2.1 pins ``max(2058, 2000) + 100 = 2158``; smallest cell projects
  47,797.2 TPM (≤ 0.85 × 60,000 = 51,000; margin 3,202.8), largest cell
  projects 367,131.6 TPM (≥ 1.25 × 60,000 = 75,000; ~292K headroom).
* Per-cell unique ``prompt_cache_key``: namespaced
  ``task019_card1_{run_id_short}_cell{max_output_tokens:05d}``. NO cross-
  cell or cross-run prompt-cache reuse — same prefix alone is NOT
  sufficient; cache identity requires the (key, prefix) pair.
* Warm criterion: ≥ 50% of last 6 pre-warm records show
  ``cached_tokens > 0``; cells failing the criterion are flagged
  ``cache_not_warm: true`` and EXCLUDED from 429-onset analysis (JSONL
  preserved).
* Run-lock at ``benchmarks/07-max-output-tokens-reservation/.runlock`` via
  ``fcntl.flock(LOCK_EX | LOCK_NB)``; fail-fast exit code 4 if held by
  a live PID; stale-PID reclaim logs WARNING; holder metadata echoed
  into every ``runs/*.summary.json``.
* USD preflight gate aborts at ``projected_usd > 0.9 × hard_ceiling``
  (exit 6); USD mid-run halt gate halts cleanly at
  ``cumulative_usd > 0.85 × hard_ceiling`` and writes
  ``runs/<ts>.partial.json`` (exit 0).
* Pricing freshness gate aborts (exit 5) if
  ``pricing_snapshot_path`` is missing OR its ``accessed_date`` is
  older than 90 days.
* ``backlog_excessive`` cell-fail flag (Task 018 v2.4 pattern verbatim):
  ``P95(dispatch_backlog_ms) > 1500 ms`` OR ``max > 5000 ms`` excludes
  the cell from 429-onset analysis. Pre-admission vs post-admission
  failure partitioning preserved (only ``token_cap_exceeded`` is
  pre-admission; in this task we may not produce token-cap rejections,
  but the partition pattern is preserved for robustness).

CLI contract::

    python -m scripts.measure_max_output_tokens_sweep \\
        --experiment experiments/exp007_max_output_tokens_sweep.yaml \\
        [--dry-run | --smoke | --stage {dry-run,smoke,evidence}]

Exit codes:
    0 = success OR clean mid-run USD halt (partial run is legitimate)
    1 = preflight USD abort, TPM feasibility abort, runtime error
    2 = endpoint misconfiguration / auth / preflight reachability failed
    3 = corpus / user-prompts file missing or malformed
    4 = run-lock conflict (held by another live PID)
    5 = pricing snapshot missing or stale (> 90 days)
    6 = USD preflight gate aborted (projected > 0.9 × hard ceiling)
    7 = prompt-identity SHA mismatch (source corpus, assembled prompt,
        or user-prompts source)
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import datetime
import errno
import fcntl
import hashlib
import inspect
import json
import logging
import math
import os
import pathlib
import re
import socket
import struct
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import yaml

from scripts._azure_pricing import (
    LIVE_MEASUREMENT,
    PRICING_POLICY_MODES,
    PricingPolicyError,
    verify_campaign_pricing,
)
from scripts._pricing_types import PaygPricing, TokenUsage
from scripts.cost_calculator import (
    load_payg_pricing,
    payg_cost_per_call,
)
from scripts.simulate_spillover import (
    DEFAULT_TOKEN_ESTIMATE_DIVISOR,
    build_system_prompt,
)


__all__ = [
    "ASSEMBLED_PROMPT_TOKENS",
    "BASE_PROMPT_TOKENS_FOR_GATE",
    "BUCKET_KEY_RE",
    "BudgetHaltError",
    "CitationsBuilder",
    "CONCURRENCY_PINNED",
    "CorpusMissingError",
    "EndpointMisconfiguredError",
    "EXPECTED_ASSEMBLED_SYSTEM_PROMPT_SHA256",
    "EXPECTED_SOURCE_CORPUS_SHA256",
    "EXPECTED_USER_PROMPTS_SOURCE_SHA256",
    "ExperimentConfig",
    "MAX_OUTPUT_TOKENS_SWEEP",
    "MeasurementResult",
    "PEAK_RAMP_TPS",
    "PREWARM_CALLS_PER_CELL",
    "PREWARM_TPS",
    "PreflightBudgetAbortError",
    "PreflightReachabilityError",
    "PricingStaleError",
    "PromptIdentitySHAMismatchError",
    "RunLockHeldError",
    "RpmTracker",
    "TpmFeasibilityAbortError",
    "USER_PROMPTS_INDEX_SET",
    "acquire_runlock",
    "build_arrival_schedule",
    "build_prompt_cache_key",
    "compute_projected_tpm_cell",
    "compute_projected_usd",
    "evaluate_smoke_gate_block",
    "load_experiment",
    "main",
    "release_runlock",
    "run_measurement",
    "verify_prompt_identity_or_exit7",
]

logger = logging.getLogger("scripts.measure_max_output_tokens_sweep")


# ----------------------------------------------------------------------------
# Constants — Foundry v1 + Task 019 v2.1 pinned controls
# ----------------------------------------------------------------------------

FOUNDRY_API_VERSION = "preview"
"""Foundry v1 API version literal. Copied verbatim from Task 013 / 018."""

# Robust token provider tuning — identical to Task 018 v2.4. These guard
# transient Entra credential refresh failures; they do NOT add SDK-level
# retries to the user's request (max_retries=0 is enforced separately).
DEFAULT_TOKEN_MAX_RETRIES = 5
DEFAULT_TOKEN_BASE_BACKOFF_SECONDS = 1.0
DEFAULT_TOKEN_MAX_BACKOFF_SECONDS = 30.0

# v2.1 prompt-identity pins. The v2.1 banner blocker #1 fix: these SHA
# values are pinned IN SPEC at v2.1 spec-write time and verified at
# runtime; any mismatch aborts the run with exit code 7.
EXPECTED_SOURCE_CORPUS_SHA256 = (
    "6a8ab5a3cb1ad3dace030a82ec1327496b39e65b77a627714a27c39017ca19e3"
)
EXPECTED_ASSEMBLED_SYSTEM_PROMPT_SHA256 = (
    "f8a74528164b22eed27d30a5fa089b1d0fbfb38440cc341b043c2cb24e9289c7"
)
EXPECTED_USER_PROMPTS_SOURCE_SHA256 = (
    "45f4a95b5cfe208a3555683fe22c6eb74f1b0e46d621723b1ea1480713ab3087"
)
EXPECTED_ASSEMBLED_PROMPT_CHARS = 8233
"""Length in chars of the assembled system prompt at the pinned build
parameters (corpus_seed=4242, target_tokens=2000) — informational; the
authoritative pin is EXPECTED_ASSEMBLED_SYSTEM_PROMPT_SHA256."""

EXPECTED_SOURCE_CORPUS_PATH = (
    "benchmarks/04-spillover-simulation/system_prompt_corpus.json"
)
EXPECTED_USER_PROMPTS_SOURCE_PATH = (
    "benchmarks/04-spillover-simulation/user_prompts.json"
)
EXPECTED_CORPUS_SEED = 4242
EXPECTED_TARGET_SYSTEM_PROMPT_TOKENS = 2000
USER_PROMPTS_INDEX_SET: tuple[int, ...] = (0, 3, 6, 9, 12, 15, 18, 21, 24, 27)
"""Pinned deterministic 10-prompt working subset (every 3rd prompt from
index 0). Constant, NOT a runtime computation, NOT a 'shortest-output'
judgment. Each request rotates round-robin through these 10 prompts."""

ASSEMBLED_PROMPT_TOKENS = 2058
"""Pinned assembled-prompt token count at v2.1 pins (8233 chars / 4 chars
per token). Logged with the assembled SHA at v2.1 spec-write time."""

TARGET_SYSTEM_PROMPT_TOKENS = 2000
"""v2.1 ``target_system_prompt_tokens`` pin. The build function rounds
upward of this target until char count is reached; the assembled prompt
ends up at ~2058 approximate tokens."""

BASE_PROMPT_TOKENS_FOR_GATE = (
    max(ASSEMBLED_PROMPT_TOKENS, TARGET_SYSTEM_PROMPT_TOKENS) + 100
)
"""Canonical cold-cache base for the TPM feasibility gate (v2.1 blocker
#4). max(2058, 2000) + 100 = 2158. The +100 covers user-prompt tokens.
This is the worst-case projection used at startup BEFORE any pre-warm has
run."""

PEAK_RAMP_TPS = 0.33
"""v2.1 pinned ``runtime.peak_ramp_tps`` (v2.1 blocker #4 retune from
0.35 to recover explicit margin under the 0.85 × quota = 51,000 TPM
smallest-cell threshold). At BASE_PROMPT_TOKENS_FOR_GATE=2158 the
smallest cell (256) projects 47,797.2 TPM (margin 3,202.8 below the
0.85 threshold)."""

PREWARM_TPS = 0.05
"""v2.1 pinned pre-warm TPS. 12 calls at 0.05 TPS = 240 s = 4 min."""

PREWARM_CALLS_PER_CELL = 12
"""v2.1 pinned pre-warm count per cell. SAME at smoke and full so the
warm criterion is enforceable at smoke (v2.1 blocker #3 fix)."""

WARM_CRITERION_LAST_N = 6
"""Warm criterion looks at the LAST N pre-warm records."""

WARM_CRITERION_MIN_RATIO = 0.5
"""Warm criterion: ≥ 50% (≥ 3 of 6 by default) of the last
WARM_CRITERION_LAST_N pre-warm records must show ``cached_tokens > 0``."""

CONCURRENCY_PINNED = 96
"""v2.1 pinned ``runtime.concurrency`` (inherited from Task 018 v2.4)."""

DISPATCHER_PINNED = "async_scheduled"
"""v2.1 pinned ``runtime.dispatcher``. The runner ONLY supports
async_scheduled."""

SDK_MAX_RETRIES_PINNED = 0
"""v2.1 pinned ``client.max_retries`` — MANDATORY zero. The default of 2
silently absorbs the first 429 onset signal this benchmark exists to
measure."""

DEPLOYMENT_TPM_QUOTA_DEFAULT = 60000
"""v2.1 pinned ``metadata.deployment_tpm_quota`` for the throttled
ptu-deploy-throttled deployment (Task 013 v1 HOTFIX 2026-05-21:
GlobalStandard PAYG, 60K TPM / 600 RPM). Denominator of the TPM
feasibility preflight gate."""

DEPLOYMENT_RPM_QUOTA_DEFAULT = 600
"""v2.1 pinned ``deployment.rpm`` for the throttled deployment.
Informational; not used by any gate."""

TPM_LOWER_GATE_FRACTION = 0.85
"""TPM feasibility preflight: smallest cell projection must be
≤ TPM_LOWER_GATE_FRACTION × deployment_tpm_quota (otherwise the smallest
cell would 429 at peak ramp, eliminating signal contrast)."""

TPM_UPPER_GATE_FRACTION = 1.25
"""TPM feasibility preflight: largest cell projection must be
≥ TPM_UPPER_GATE_FRACTION × deployment_tpm_quota (otherwise the largest
cell would NOT 429, eliminating signal contrast)."""

PRICING_SNAPSHOT_MAX_AGE_DAYS = 90
"""Reject a pricing snapshot whose ``accessed_date`` is older than this."""

EVIDENCE_PROJECTED_OUTPUT_TOKENS = 500
"""Conservative per-call output-token assumption for the preflight USD
projection (visible ~200 + reasoning headroom at ``effort=low``;
modeled as 500 total billed at output rate per the Cost & Time Budget
in the v2.1 spec)."""

EVIDENCE_CACHED_FRACTION = 0.85
"""Conservative steady-state cached-token fraction for the preflight
USD projection (in-cell amortization estimate; cross-cell amortization
is structurally impossible under the per-cell-unique key model)."""

MAX_OUTPUT_TOKENS_SWEEP: tuple[int, ...] = (
    256, 512, 1024, 2048, 4096, 8192, 16384,
)
"""v2.1 pinned 7-cell log2 sweep. Small → large ordering preserves
truncated-curve usefulness if mid-run USD halt fires."""

BACKLOG_P95_FAIL_MS = 1500.0
"""Task 018 v2.4 pattern verbatim — P95 dispatch backlog cell-fail
threshold."""

BACKLOG_MAX_FAIL_MS = 5000.0
"""Task 018 v2.4 pattern verbatim — max dispatch backlog cell-fail
threshold."""

PRE_ADMISSION_FAILURE_REASONS: frozenset[str] = frozenset({
    "token_cap_exceeded",
})
"""Failure reasons whose records never issued a meaningful HTTP call.
Task 018 v2.4 hotfix-2 pattern verbatim. Pre-admission failures are
EXCLUDED from admitted RPM / backlog / in-flight aggregates; every
other failed record (including ``rate_limited_observed`` — the 429
capture that is exactly this task's signal) DOES contribute. In Task
019 we do not produce token-cap rejections (no cap is set), but the
partition pattern is preserved for robustness against future
extensions."""

# Citations — Microsoft Learn URLs for the analysis Citations block.
AZURE_DOC_PROMPT_CACHING_URL = (
    "https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/prompt-caching"
)
AZURE_DOC_ACCESSED_DATE = "2026-05-29"

AZURE_RATE_LIMIT_DOC_URL = (
    "https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/quota"
)
AZURE_RATE_LIMIT_DOC_ACCESSED_DATE = "2026-05-29"
AZURE_RATE_LIMIT_DOC_CLAIMS_CITED: tuple[str, ...] = (
    "deployment TPM quota is enforced as a sliding window over "
    "estimated processed tokens (input + max_output)",
    "exceeding the sliding window returns HTTP 429 with retry-after",
    "max_output_tokens contributes to admission-time token reservation",
)

AZURE_PTU_DOC_URL = (
    "https://learn.microsoft.com/en-us/azure/ai-foundry/openai/concepts/provisioned-throughput"
)
AZURE_PTU_DOC_ACCESSED_DATE = "2026-05-29"
AZURE_PTU_DOC_CLAIMS_CITED: tuple[str, ...] = (
    "admission reserves capacity against (input_tokens + max_output_tokens)",
    "actual generated output may be smaller; the reservation is what is "
    "debited at admission",
)

# Anonymization audit regex — matches the cell-unique prompt_cache_key
# strings this script emits. ``run_id_short`` is an 8-char lowercase hex
# slice; ``max_output_tokens`` is zero-padded to 5 digits to keep the
# namespace lexicographically sortable.
BUCKET_KEY_RE = re.compile(
    r"^task019_card1_[a-f0-9]{8}_cell\d{5}$"
)


# Exit code constants — every typed error maps to one of these.
EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_AUTH = 2
EXIT_DATASET = 3
EXIT_RUNLOCK = 4
EXIT_PRICING = 5
EXIT_USD_PREFLIGHT = 6
EXIT_SHA_MISMATCH = 7
EXIT_CALIBRATION_TERMINAL = 8
"""v2.2.1 NEW. Stage 0.5 calibration terminated without
``outcome == "selected"``. Raised for every member of the 7-member
outcome enum except ``selected``."""
EXIT_LINKAGE_FAIL = 9
"""v2.2.1 NEW. Inter-stage linkage validation failed: smoke without
``--calibration-result``, stale / mismatched calibration result, missing
or mismatched smoke summary, ``--peak-ramp-tps`` override (forbidden),
ad-hoc ``candidate_tps_grid`` member, or any of the ten enumerated
evidence-runner exit-9 reasons in the spec."""


# ----------------------------------------------------------------------------
# v2.2.1 — Stage 0.5 calibration constants
# ----------------------------------------------------------------------------

CALIBRATION_CANDIDATE_TPS_GRID: tuple[float, ...] = (
    0.33, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0,
)
"""v2.2.1 PINNED predeclared candidate-TPS grid. Ad-hoc values outside
this set are REJECTED at YAML load (``candidate_tps_grid_contains_ad_hoc_value``)
and the runner refuses any ``--peak-ramp-tps`` override (exit 9)."""

CALIBRATION_PROBE_DURATION_S = 180
"""Constant-rate probe window per candidate (NOT a ramp)."""

CALIBRATION_PROBE_MAX_CALLS = 600
"""Hard call-count cap per probe (defensive — at TPS=3.0 the 180-s
window yields ~540 calls)."""

CALIBRATION_PROBE_MAX_USD = 60.0
"""v2.3 — raised from v2.2.1's $5 to fit Phase B probes (per-probe spend
cap; defensive — 429s are not billed on PAYG GlobalStandard so realised
spend is typically far lower). Microfix 2026-05-30 Microfix A."""

CALIBRATION_TOTAL_MAX_USD = 220.0
"""v2.3 — raised from v2.2.1's $20 to the conservative-but-useful $220
total calibration cap. Microfix 2026-05-30 Microfix A corrects the prior
v2.3-draft typo of $800 to the conservative-but-useful $220 pinned
everywhere else in v2.3. The runner halts cleanly at
``0.85 × 220 = $187`` mid-run with ``calibration_total_usd_exhausted``
rather than raising the cap to force Phase B exhaustive completion."""

CALIBRATION_PREWARM_CALLS = 12
"""Same as full evidence — IDENTICAL prompt-identity contract."""

CALIBRATION_PREWARM_TPS = 0.05
"""Same as full evidence — IDENTICAL pre-warm cadence."""

CALIBRATION_INTER_PROBE_COOLDOWN_S = 120
"""Idle between probe candidates (matches full-run inter-cell cooldown)."""

CALIBRATION_MAX_AGE_HOURS = 24
"""Smoke + evidence refuse to start against a calibration result whose
``completed_at_iso`` is older than this. Auditor-revisable."""

CALIBRATION_LARGEST_CELL_MO = 16384
CALIBRATION_SMALLEST_CELL_MO = 256

SMOKE_HARD_CEILING_USD = 50.0
"""v2.3 — raised from v2.2.1's $15 to the conservative-but-useful $50.
Sized so the deterministic conservative TPS=3.0 projection of $12.29
and TPS=8.0 projection of ~$33 both fit under the 0.9×50 = $45
preflight gate; TPS=12.0 projection of ~$49 ABORTS at preflight with
``smoke_preflight_over_ceiling_narrow_sweep_or_file_new_spec``."""

EVIDENCE_HARD_CEILING_USD = 100.0
"""v2.3 — raised from v2.2.1's $75 to the conservative-but-useful $100.
Sized so the deterministic conservative TPS=3.0 projection of $58.40
fits under the 0.9×100 = $90 preflight gate; TPS=5.0 projection of
~$97 ABORTS at preflight with
``evidence_preflight_over_ceiling_narrow_sweep_or_file_new_spec``."""

TASK_TOTAL_HARD_CEILING_USD = 400.0
"""v2.3 — raised from v2.2.1's $120 to the conservative-but-useful
total Task 019 live cap of $400 (calibration $220 + smoke $50 +
evidence $100 + contingency $30). NOT a spend target — the cap is an
accounting guardrail. Do not exhaust shared budget; cap is an
accounting guardrail, not a spend target."""

CONTINGENCY_HARD_CEILING_USD = 30.0
"""v2.3 NEW — contingency budget within the $400 Task 019 cap."""

DETERMINISTIC_PER_CALL_USD = 0.009
"""Deterministic conservative per-call cost (v2.2.1 — no 429-no-bill
discount). Derivation: ~2,000-token assembled prompt × cached/uncached
mix + ~500 output-token reservation @ gpt-5.2 PAYG rates ≈ $0.0078 →
$0.009 (~16% margin against per-call cost overruns). Cost & Time Budget
§ pegs this as the single source of truth across the deterministic
estimator, the preflight gates, and the pinned test values."""


# ----------------------------------------------------------------------------
# v2.3 — Two-phase calibration: Phase B grid + admitted-pressure + bracket
# ----------------------------------------------------------------------------

CALIBRATION_CANDIDATE_TPS_GRID_PHASE_B: tuple[float, ...] = (
    5.0, 8.0, 12.0, 16.0, 24.0, 32.0,
)
"""v2.3 PINNED Phase B escalation grid. Per Microfix 2026-05-30 Microfix
C, the YAML validator REQUIRES this list to EQUAL the spec-pinned
six-member grid in EXACT ascending order — not a strict subset, not a
re-ordered list, not a superset, no duplicates. Four mutations abort at
YAML-load (exit code 9) with distinct explicit reasons. Changing or
shortening the Phase B grid requires a NEW SPEC REVISION."""

CALIBRATION_PROBE_MAX_CALLS_PHASE_B = 6624
"""v2.3 — Phase B per-probe call-count cap, sized for max Phase B TPS.
``ceil(max(candidate_tps_grid_phase_b) × probe_duration_s × 1.15) =
ceil(32 × 180 × 1.15) = 6624`` (the 1.15 is a 15% defensive margin for
dispatcher jitter). Phase A retains ``probe_max_calls: 600``."""

CONCURRENCY_PHASE_B_PINNED = 512
"""v2.3 NEW Phase B concurrency override.
``max(96, ceil(max(grid_phase_b) × 16)) = max(96, 32 × 16) = 512``.
Phase A, smoke, and evidence retain v2.2.1's ``runtime.concurrency = 96``.
Bracket probes use whichever concurrency matches the phase they are
rooted in (bracket on Phase A T_low/T_high → 96; bracket on Phase B
T_low/T_high → 512)."""

ADMITTED_PRESSURE_FLOOR_RATIO = 0.70
"""v2.3 NEW — admitted-pressure floor ratio. A probe with zero 429s but
``admitted_peak_rpm_observed_last_30s < 0.70 × candidate_tps × 60`` is
ineligible (bounded retry with ``_retry1_admp`` suffix; on second
failure → ``calibration_probe_inconclusive_admitted_pressure_insufficient``)."""

ADMITTED_PRESSURE_WINDOW_S = 30
"""v2.3 NEW — admitted-pressure observation window in seconds (last 30 s
of the probe). Auditor-revisable at spec-revision time only."""

BRACKET_MAX_DEPTH = 3
"""v2.3 NEW — bounded bracket-search recursion cap. On no-usable-contrast
(smallest-cell control probe at T observes ≥ 1 real 429), the runner
attempts a bounded bracket search at geometric midpoints
``sqrt(T_low × T)`` before accepting the v2.2.1 terminal
``no_usable_contrast_at_this_prompt_deployment`` verdict. Same-phase
pre-condition required (bracket never spans Phase A → Phase B)."""

V23_GUARDRAIL_STRING = (
    "Do not exhaust shared budget; cap is an accounting guardrail, "
    "not a spend target"
)
"""v2.3 literal guardrail string — the spec asserts this exact phrase
must be carried by the runner's accounting messages and tests."""


# Anonymization audit regex for v2.2.1 calibration probe keys.
# v2.3 EXTENSION: accept the new _retry1_admp (admitted-pressure retry)
# and _bracketN (N ∈ 1..3) suffixes alongside the v2.2.1 _retry1 suffix.
# v2.3 fix loop #5 (auditor BLOCKER 2) EXTENSION: bracket probes now
# carry parent-style bounded-retry semantics, so the suffix may compose
# as _bracketN_retry1 or _bracketN_retry1_admp.
CALIB_BUCKET_KEY_RE = re.compile(
    r"^task019_calib_[a-f0-9]{8}_cell\d{5}_tps\d{4,5}"
    r"(_retry1|_retry1_admp|_bracket[1-3](?:_retry1|_retry1_admp)?)?$"
)


# ----------------------------------------------------------------------------
# Typed errors
# ----------------------------------------------------------------------------


class PreflightBudgetAbortError(RuntimeError):
    """``projected_usd > 0.9 × hard_ceiling_usd`` — runner aborts before
    the first network call. Maps to exit code 6.

    v2.3 — optional ``reason`` field carries a stage-specific abort
    reason for smoke/evidence so downstream readers see the documented
    string (e.g. ``smoke_preflight_over_ceiling_narrow_sweep_or_file_new_spec``,
    ``evidence_preflight_over_ceiling_narrow_sweep_or_file_new_spec``)
    rather than the generic v2.1 message.
    """

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class BudgetHaltError(RuntimeError):
    """Catastrophic budget violation. The mid-run gate halts CLEANLY via
    ``MeasurementResult.halt_reason``; this exception is reserved for
    unrecoverable paths."""


class EndpointMisconfiguredError(RuntimeError):
    """Required Azure env vars missing or empty, OR the YAML's deployment
    template does not resolve to the throttled deployment (this task
    requires the throttled deployment for 429 onset to be observable).
    Maps to exit code 2."""


class CorpusMissingError(FileNotFoundError):
    """Source corpus or user-prompts file cannot be found / parsed. Maps
    to exit code 3."""


class PreflightReachabilityError(RuntimeError):
    """The single-deployment reachability ping failed. Maps to exit
    code 2."""


class PricingStaleError(RuntimeError):
    """Pricing snapshot missing OR ``accessed_date`` older than
    ``PRICING_SNAPSHOT_MAX_AGE_DAYS``. Maps to exit code 5."""


class TpmFeasibilityAbortError(RuntimeError):
    """v2.1 TPM feasibility preflight gate aborted. The runner aborts
    BEFORE any client construction or network call. Maps to exit
    code 1.

    v2.4 EXTENSION: when the abort fires DOWNSTREAM of a v2.4 empirical-
    promotion-gate denial (i.e. cold-cache fallback ALSO denies), the
    raiser sets ``self.v24_empirical_denied_reason`` to the §8 stable
    ``empirical_promotion_disabled_*`` identifier so the outer
    ``main()`` handler can write the §9.4 abort envelope. When the
    abort fires for the v2.1 cold-cache path with NO empirical
    promotion in scope (dry-run / calibration / v2.3 backwards-compat),
    the attribute remains ``None`` and the outer handler emits the
    legacy v2.1 behaviour."""

    def __init__(
        self,
        message: str,
        *,
        v24_empirical_denied_reason: str | None = None,
        v24_stage: str | None = None,
    ) -> None:
        super().__init__(message)
        self.v24_empirical_denied_reason = v24_empirical_denied_reason
        self.v24_stage = v24_stage


class PromptIdentitySHAMismatchError(RuntimeError):
    """v2.1 prompt-identity contract violation — source corpus SHA,
    assembled system prompt SHA, or user-prompts source SHA does not
    match the pinned constant. Maps to exit code 7."""


class RunLockHeldError(RuntimeError):
    """The run-lock is held by another live PID. Maps to exit code 4."""


class MiniProbeAttemptedMoreThanOncePerRunError(RuntimeError):
    """v2.4 §7 defensive — a single smoke / evidence run attempted to
    invoke the mini-probe more than the pinned cap
    (``mini_probe_max_attempts_per_run = 1``). Caller (runner) should
    surface this as exit code 9 reason
    ``mini_probe_attempted_more_than_once_per_run``."""


# ----------------------------------------------------------------------------
# v2.2.1 — Stage 0.5 calibration outcome enum + typed errors
# ----------------------------------------------------------------------------

CALIBRATION_OUTCOME_SELECTED = "selected"
CALIBRATION_OUTCOME_NO_LARGEST_429 = (
    "no_largest_cell_429_at_any_candidate_tps"
)
"""v2.3 — RETIRED as a terminal calibration verdict. Under v2.3 this
string persists ONLY as an INTERNAL intra-calibration signal meaning
"Phase A grid exhausted with admitted-pressure validated → enter
Phase B". It is NOT a member of ``CALIBRATION_OUTCOME_ENUM``."""

CALIBRATION_OUTCOME_NO_CONTRAST = (
    "no_usable_contrast_at_this_prompt_deployment"
)
CALIBRATION_OUTCOME_CONTROL_CAP_HIT = (
    "smallest_cell_control_probe_inconclusive_cap_hit"
)
CALIBRATION_OUTCOME_TOTAL_USD_EXHAUSTED = (
    "calibration_total_usd_exhausted"
)
CALIBRATION_OUTCOME_INCONCLUSIVE_CACHE = (
    "calibration_probe_inconclusive_cache_not_warm"
)
CALIBRATION_OUTCOME_INCONCLUSIVE_BACKLOG = (
    "calibration_probe_inconclusive_backlog_excessive"
)
# v2.3 NEW outcomes.
CALIBRATION_OUTCOME_INCONCLUSIVE_ADMITTED_PRESSURE = (
    "calibration_probe_inconclusive_admitted_pressure_insufficient"
)
"""v2.3 NEW. Both the initial probe AND its ``_retry1_admp`` retry
showed ``admitted_peak_rpm_observed_last_30s < 0.70 × candidate_tps × 60``
AND observed zero real 429s."""

CALIBRATION_OUTCOME_PHASE_B_ENDPOINT_NOT_THROTTLING = (
    "no_largest_cell_429_at_any_phase_b_candidate_tps_endpoint_not_throttling"
)
"""v2.3 NEW. Phase B exhausted with admitted-pressure validated on every
Phase B candidate AND zero 429s — a meaningful negative finding for
Hypothesis I at this proxy + prompt identity."""

CALIBRATION_OUTCOME_PHASE_B_DRIVER_PRESSURE_INSUFFICIENT = (
    "no_largest_cell_429_at_any_phase_b_candidate_tps_driver_pressure_insufficient"
)
"""v2.3 NEW. Phase B exhausted with admitted-pressure gate failing on
at least one Phase B candidate's bounded retry — a driver / host-
capacity finding, NOT a Hypothesis I verdict."""

CALIBRATION_OUTCOME_ENUM: frozenset[str] = frozenset({
    CALIBRATION_OUTCOME_SELECTED,
    CALIBRATION_OUTCOME_NO_CONTRAST,
    CALIBRATION_OUTCOME_CONTROL_CAP_HIT,
    CALIBRATION_OUTCOME_TOTAL_USD_EXHAUSTED,
    CALIBRATION_OUTCOME_INCONCLUSIVE_CACHE,
    CALIBRATION_OUTCOME_INCONCLUSIVE_BACKLOG,
    CALIBRATION_OUTCOME_INCONCLUSIVE_ADMITTED_PRESSURE,
    CALIBRATION_OUTCOME_PHASE_B_ENDPOINT_NOT_THROTTLING,
    CALIBRATION_OUTCOME_PHASE_B_DRIVER_PRESSURE_INSUFFICIENT,
})
"""v2.3 PINNED 9-member outcome enum (the v2.2.1 7-member set is
SUPERSEDED: the v2.2.1 terminal ``no_largest_cell_429_at_any_candidate_tps``
is retired as terminal — it persists as an INTERNAL signal meaning
"Phase A exhausted, enter Phase B"; three v2.3 NEW outcomes are added).
The set's exact membership is verified by ``TestCalibrationOutcomeEnum``."""

# v2.6 — Adaptive Stage 0.5.C non-selected outcomes that may surface
# through the production calibration terminal handoff in
# ``_run_calibration_async``. These are NOT members of the v2.3 9-member
# enum; the v2.3 enum is preserved byte-identical. The CalibrationTerminalError
# constructor accepts the UNION of these and ``CALIBRATION_OUTCOME_ENUM``
# so that a valid v2.5 C3 / cap-terminal adaptive run can route through
# the existing exit-8 path without a spurious ValueError.
CALIBRATION_OUTCOME_V25_NO_PROMOTABLE_CONTRAST = (
    "no_promotable_contrast_at_this_prompt_deployment"
)
CALIBRATION_OUTCOME_V25_ADAPTIVE_BUDGET_EXHAUSTED = (
    "adaptive_calibration_budget_exhausted"
)
CALIBRATION_OUTCOME_V25_ADAPTIVE_WALL_TIME_EXHAUSTED = (
    "adaptive_calibration_wall_time_exhausted"
)
CALIBRATION_OUTCOME_V25_ADAPTIVE_API_CONNECTION_UNSTABLE = (
    "adaptive_calibration_api_connection_unstable"
)
V25_ADAPTIVE_NON_SELECTED_OUTCOMES: frozenset[str] = frozenset({
    CALIBRATION_OUTCOME_V25_NO_PROMOTABLE_CONTRAST,
    CALIBRATION_OUTCOME_V25_ADAPTIVE_BUDGET_EXHAUSTED,
    CALIBRATION_OUTCOME_V25_ADAPTIVE_WALL_TIME_EXHAUSTED,
    CALIBRATION_OUTCOME_V25_ADAPTIVE_API_CONNECTION_UNSTABLE,
})
"""v2.6 — additive non-selected outcomes Stage 0.5.C may emit. C3 ADMIT
yields ``no_promotable_contrast_at_this_prompt_deployment``; cap-terminal
halts yield one of the three ``adaptive_calibration_*_exhausted/unstable``
strings (see ``_adaptive_cap_terminal_outcome``)."""


class CalibrationTerminalError(RuntimeError):
    """v2.2.1 (v2.3 EXTENDED to 9-member enum). Stage 0.5 calibration
    terminated without ``outcome == "selected"``. Carries the 9-member-enum
    reason on ``self.outcome`` plus optional ``inconclusive_probe_role``,
    ``inconclusive_at_candidate_tps``, ``inconclusive_reason_detail``
    fields for the calibration-result schema. Maps to exit 8."""

    def __init__(
        self,
        message: str,
        *,
        outcome: str,
        inconclusive_probe_role: str | None = None,
        inconclusive_at_candidate_tps: float | None = None,
        inconclusive_reason_detail: str | None = None,
    ) -> None:
        super().__init__(message)
        # v2.6 — accept the UNION of the v2.3 9-member terminal enum and
        # the v2.5 adaptive Stage 0.5.C non-selected outcomes
        # (``V25_ADAPTIVE_NON_SELECTED_OUTCOMES``). The v2.3 set is
        # unchanged; v2.5 adaptive C3-ADMIT / cap-terminal results can
        # now route through the production terminal handoff in
        # ``_run_calibration_async`` without a spurious ValueError.
        _allowed = CALIBRATION_OUTCOME_ENUM | V25_ADAPTIVE_NON_SELECTED_OUTCOMES
        if outcome not in _allowed:
            raise ValueError(
                f"outcome {outcome!r} not in v2.3 9-member enum "
                f"{sorted(CALIBRATION_OUTCOME_ENUM)} nor v2.6 adaptive "
                f"non-selected set {sorted(V25_ADAPTIVE_NON_SELECTED_OUTCOMES)}"
            )
        if outcome == CALIBRATION_OUTCOME_SELECTED:
            raise ValueError(
                "CalibrationTerminalError is only raised for FAILURE "
                "outcomes; 'selected' is the success path"
            )
        self.outcome = outcome
        self.inconclusive_probe_role = inconclusive_probe_role
        self.inconclusive_at_candidate_tps = inconclusive_at_candidate_tps
        self.inconclusive_reason_detail = inconclusive_reason_detail


class LinkageValidationError(RuntimeError):
    """v2.2.1 NEW (v2.3 EXTENDED with Phase B grid + admitted-pressure
    reasons). Inter-stage linkage validation failed. Carries the spec's
    enumerated ``reason`` on ``self.reason``. Maps to exit 9.

    Allowed reasons (v2.2.1):

    - ``peak_ramp_tps_override_forbidden_use_calibration_result``
    - ``candidate_tps_grid_contains_ad_hoc_value``
    - ``candidate_tps_grid_not_sorted_ascending``
    - ``lowest_tps_overshoots_smallest_cell_at_cold_cache``
    - ``highest_tps_undershoots_largest_cell_at_cold_cache``
    - ``calibration_result_missing``
    - ``calibration_result_invalid_schema``
    - ``calibration_did_not_select_peak_tps``
    - ``calibration_stale_must_re_run``
    - ``calibration_prompt_identity_mismatch``
    - ``smoke_summary_missing``
    - ``smoke_summary_sha256_mismatch``
    - ``smoke_did_not_pass_gate``
    - ``smoke_selected_peak_tps_mismatches_calibration``
    - ``smoke_calibration_reference_mismatch``
    - ``smoke_prompt_identity_mismatch``
    - ``smoke_stale_must_re_run``

    v2.3 NEW reasons (Microfix C — Phase B grid completeness contract):

    - ``candidate_tps_grid_phase_b_member_missing``
    - ``candidate_tps_grid_phase_b_contains_ad_hoc_value``
    - ``candidate_tps_grid_phase_b_contains_duplicate_value``
    - ``candidate_tps_grid_phase_b_not_sorted_ascending``

    v2.3 NEW evidence-runner reasons:

    - ``smoke_selected_at_phase_mismatches_calibration``
    - ``smoke_admitted_pressure_failed``

    v2.4 NEW evidence-runner reasons (microfix #1 blocker 3 /
    §11.21):

    - ``evidence_summary_missing_smoke_promotion_path_echo``
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class ProbeScheduleIntendedRateInsufficientError(RuntimeError):
    """v2.3 NEW — runtime invariant. The calibration probe's pre-generated
    ``intended_dispatch_iso_list`` did NOT contain at least
    ``floor(0.70 × candidate_tps × admitted_pressure_window_s)`` entries
    within the last ``admitted_pressure_window_s`` of the probe window.
    This is a SCHEDULE-GENERATION bug (the dispatcher is innocent); the
    runner aborts the probe with exit code 8 rather than running the
    admitted-pressure gate against an under-scheduled probe.

    Reason string: ``probe_schedule_intended_rate_insufficient``."""

    def __init__(
        self,
        message: str,
        *,
        candidate_tps: float,
        intended_in_window: int,
        required_in_window: int,
    ) -> None:
        super().__init__(message)
        self.reason = "probe_schedule_intended_rate_insufficient"
        self.candidate_tps = candidate_tps
        self.intended_in_window = intended_in_window
        self.required_in_window = required_in_window


# ----------------------------------------------------------------------------
# Pure helpers — TPM gate, prompt_cache_key, arrival schedule, RPM
# ----------------------------------------------------------------------------


def compute_projected_tpm_cell(
    *,
    peak_ramp_tps: float,
    base_prompt_tokens_for_gate: int,
    max_output_tokens: int,
) -> float:
    """Canonical v2.1 TPM feasibility projection for one cell.

    Formula (pinned by v2.1 blocker #4 — single source of truth across
    YAML, runner gate, pytest expected values, and Cost & Time Budget):
        ``projected_tpm(cell) = 60 × peak_ramp_tps ×
            (base_prompt_tokens_for_gate + cell.max_output_tokens)``
    where ``base_prompt_tokens_for_gate =
    max(assembled_system_prompt_tokens, target_system_prompt_tokens) +
    100`` (cold-cache evaluation; the +100 covers user-prompt tokens).

    Args:
        peak_ramp_tps: Peak ramp TPS (v2.1 pin = 0.33).
        base_prompt_tokens_for_gate: Cold-cache base from the canonical
            ``max(..., ...) + 100`` formula. At v2.1 pins = 2158.
        max_output_tokens: The cell's ``max_output_tokens`` value.

    Returns:
        Projected TPM as float.

    Raises:
        ValueError: Any argument is non-positive.
    """
    if peak_ramp_tps <= 0:
        raise ValueError(
            f"peak_ramp_tps must be > 0; got {peak_ramp_tps}"
        )
    if base_prompt_tokens_for_gate <= 0:
        raise ValueError(
            f"base_prompt_tokens_for_gate must be > 0; got "
            f"{base_prompt_tokens_for_gate}"
        )
    if max_output_tokens <= 0:
        raise ValueError(
            f"max_output_tokens must be > 0; got {max_output_tokens}"
        )
    return (
        60.0
        * float(peak_ramp_tps)
        * float(base_prompt_tokens_for_gate + max_output_tokens)
    )


def compute_projected_usd(
    *,
    sweep: list[int],
    calls_per_cell: int,
    pricing: PaygPricing,
    model: str,
    input_tokens: float,
    output_tokens: float,
    cached_fraction: float,
) -> float:
    """Project worst-case USD spend for the sweep at conservative cache
    assumptions.

    Args:
        sweep: List of ``max_output_tokens`` values (the cell ordering;
            length is the number of cells).
        calls_per_cell: Per-cell call count (pre-warm + ramp).
        pricing: Loaded PAYG pricing snapshot.
        model: PAYG model key (e.g. ``"gpt-5.2"``).
        input_tokens: Per-call input token count.
        output_tokens: Per-call output token count (visible + reasoning).
        cached_fraction: Steady-state cached-token fraction (0..1).

    Returns:
        Projected total USD across ``len(sweep) × calls_per_cell``.

    Raises:
        ValueError: ``cached_fraction`` out of range.
    """
    if not 0.0 <= cached_fraction <= 1.0:
        raise ValueError(
            f"cached_fraction must be in [0.0, 1.0]; got {cached_fraction}"
        )
    cached_t = input_tokens * cached_fraction
    usage = TokenUsage(
        input_tokens=input_tokens,
        cached_tokens=cached_t,
        output_tokens=output_tokens,
        reasoning_tokens=0.0,
    )
    per_call = payg_cost_per_call(usage, pricing, model=model).usd_per_request
    n_calls = len(sweep) * calls_per_cell
    return per_call * n_calls


def build_prompt_cache_key(*, run_id_short: str, max_output_tokens: int) -> str:
    """Construct the per-cell unique prompt_cache_key.

    Format: ``task019_card1_{run_id_short}_cell{max_output_tokens:05d}``.
    Distinct per cell AND per run; matches ``BUCKET_KEY_RE``.

    Args:
        run_id_short: 8-char lowercase hex slice of a UUID-v4 (per run).
        max_output_tokens: The cell's ``max_output_tokens`` value.

    Returns:
        Namespace string.

    Raises:
        ValueError: invalid ``run_id_short`` shape or non-positive cell
            value.
    """
    if not re.fullmatch(r"[a-f0-9]{8}", run_id_short):
        raise ValueError(
            f"run_id_short must be 8 lowercase hex chars; got "
            f"{run_id_short!r}"
        )
    if max_output_tokens <= 0 or max_output_tokens > 99999:
        raise ValueError(
            f"max_output_tokens must be in 1..99999; got {max_output_tokens}"
        )
    return f"task019_card1_{run_id_short}_cell{max_output_tokens:05d}"


def build_calibration_cache_key(
    *,
    run_id_short: str,
    max_output_tokens: int,
    tps: float,
    retry: bool = False,
    suffix: str | None = None,
) -> str:
    """v2.2.1 — construct a unique calibration-probe ``prompt_cache_key``.

    Format: ``task019_calib_{run_id_short}_cell{cap:05d}_tps{tps_int:04d}``
    (TPS values ≥ 10 use a 5-digit ``tps_int`` formatter to accommodate
    the v2.3 Phase B grid up to TPS=32 → ``tps32000``; TPS < 10 retains
    the v2.2.1 4-digit form). v2.3 NEW: ``suffix`` can be one of
    ``"_retry1"``, ``"_retry1_admp"``, or ``"_bracket{N}"`` (N ∈ 1..3).
    Backwards compatibility: ``retry=True`` is equivalent to
    ``suffix="_retry1"`` and is preserved for v2.2.1 callers.

    Calibration probes use a DISTINCT namespace (``calib``) from
    smoke/evidence (``card1``) so they CAN'T accidentally collide with
    real-measurement cache buckets.

    Raises:
        ValueError: malformed ``run_id_short``, out-of-range
            ``max_output_tokens``, or non-positive ``tps``.
    """
    if not re.fullmatch(r"[a-f0-9]{8}", run_id_short):
        raise ValueError(
            f"run_id_short must be 8 lowercase hex chars; got "
            f"{run_id_short!r}"
        )
    if max_output_tokens <= 0 or max_output_tokens > 99999:
        raise ValueError(
            f"max_output_tokens must be in 1..99999; got {max_output_tokens}"
        )
    if tps <= 0:
        raise ValueError(f"tps must be > 0; got {tps!r}")
    tps_int = int(round(tps * 1000))
    if tps_int <= 0 or tps_int > 99999:
        raise ValueError(
            f"tps_int must be in 1..99999; got {tps_int} from tps={tps!r}"
        )
    # v2.3 — Phase B TPS up to 32 produces tps_int=32000 (5 digits);
    # Phase A TPS up to 3.0 produces tps_int=3000 (4 digits with leading
    # zero padding for the v2.2.1 namespace stability).
    tps_str = f"{tps_int:04d}" if tps_int <= 9999 else str(tps_int)
    # Resolve suffix — `retry=True` is the v2.2.1 boolean form; v2.3
    # adds an explicit `suffix` parameter accepting `_retry1`,
    # `_retry1_admp`, `_bracket1`, `_bracket2`, `_bracket3`.
    if suffix is not None and retry:
        raise ValueError(
            "build_calibration_cache_key: pass either retry=True OR "
            "suffix=..., not both"
        )
    if suffix is not None:
        # v2.3 fix loop #5 (auditor BLOCKER 2) — bracket probes now
        # carry bounded-retry semantics matching parent calibration.
        # The retry suffix is composed onto the base bracket suffix
        # to keep the artifact trail unambiguous about (bracket depth,
        # retry cause).
        allowed = {
            "_retry1", "_retry1_admp",
            "_bracket1", "_bracket2", "_bracket3",
            "_bracket1_retry1", "_bracket2_retry1", "_bracket3_retry1",
            "_bracket1_retry1_admp",
            "_bracket2_retry1_admp",
            "_bracket3_retry1_admp",
        }
        if suffix not in allowed:
            raise ValueError(
                f"suffix must be one of {sorted(allowed)}; got "
                f"{suffix!r}"
            )
        resolved_suffix = suffix
    elif retry:
        resolved_suffix = "_retry1"
    else:
        resolved_suffix = ""
    return (
        f"task019_calib_{run_id_short}_cell{max_output_tokens:05d}"
        f"_tps{tps_str}{resolved_suffix}"
    )


def evaluate_candidate_grid_phase_b_exact(
    *,
    candidate_tps_grid_phase_b: tuple[float, ...] | list[float],
) -> None:
    """v2.3 NEW — Phase B grid exact-match validator (Microfix C).

    The YAML's ``calibration.candidate_tps_grid_phase_b`` MUST EQUAL the
    spec-pinned six-member list ``[5.0, 8.0, 12.0, 16.0, 24.0, 32.0]``
    in EXACT ascending order — not a strict subset, not a re-ordered
    list, not a superset, no duplicates. Four mutations each abort at
    YAML-load time (exit code 9) with distinct explicit reason strings.

    Raises:
        LinkageValidationError: with one of four v2.3 reason strings.
    """
    g = list(candidate_tps_grid_phase_b)
    pinned = list(CALIBRATION_CANDIDATE_TPS_GRID_PHASE_B)
    pinned_set = set(pinned)
    g_set = set(g)
    # 1. Duplicate detection (must come BEFORE missing/ad-hoc because a
    #    list like [5.0, 5.0, 8.0, 12.0, 16.0, 24.0, 32.0] has the right
    #    set members but a duplicate; the missing/ad-hoc rules would not
    #    fire and the user would be silently advanced).
    if len(g) != len(g_set):
        seen: set[float] = set()
        dups: list[float] = []
        for v in g:
            if v in seen and v not in dups:
                dups.append(v)
            seen.add(v)
        raise LinkageValidationError(
            f"candidate_tps_grid_phase_b contains duplicate value(s) "
            f"{dups}; the pinned six-member grid is {pinned}; "
            f"duplicate_values={dups}",
            reason="candidate_tps_grid_phase_b_contains_duplicate_value",
        )
    # 2. Ad-hoc value (any value outside the pinned six).
    offending = [v for v in g if v not in pinned_set]
    if offending:
        raise LinkageValidationError(
            f"candidate_tps_grid_phase_b contains ad hoc value(s) "
            f"{offending} not in pinned six-member grid {pinned}; "
            f"offending_values={offending}",
            reason="candidate_tps_grid_phase_b_contains_ad_hoc_value",
        )
    # 3. Member-missing (any of the pinned six absent).
    missing = [v for v in pinned if v not in g_set]
    if missing:
        raise LinkageValidationError(
            f"candidate_tps_grid_phase_b is missing pinned member(s) "
            f"{missing}; spec requires exact six-member grid {pinned}; "
            f"missing_members={missing}",
            reason="candidate_tps_grid_phase_b_member_missing",
        )
    # 4. Non-ascending order (set is correct but order is wrong).
    if g != sorted(g):
        raise LinkageValidationError(
            f"candidate_tps_grid_phase_b must be strictly ascending; "
            f"got {g}; observed_order={g}",
            reason="candidate_tps_grid_phase_b_not_sorted_ascending",
        )


def compute_admitted_pressure_block(
    *,
    admitted_dispatch_iso_list: list[str | datetime.datetime],
    candidate_tps: float,
    probe_window_end_iso: str | datetime.datetime,
    window_s: int = ADMITTED_PRESSURE_WINDOW_S,
    floor_ratio: float = ADMITTED_PRESSURE_FLOOR_RATIO,
    observed_n_429: int = 0,
) -> dict[str, Any]:
    """v2.3 NEW — compute the admitted-pressure validation block.

    Implements the spec formula:

        admitted_peak_rpm_observed_last_30s = (len(admitted_in_window)
                                                 / window_s) × 60
        target_peak_rpm = candidate_tps × 60
        admitted_pressure_floor_rpm = floor_ratio × target_peak_rpm
        admitted_pressure_passed = (
            admitted_peak_rpm_observed_last_30s
            >= admitted_pressure_floor_rpm
        )

    Skipped (and reported as ``admitted_pressure_skipped_due_to_429: true``)
    when ``observed_n_429 >= 1`` — the 429 itself is proof the deployment
    crossed its admission ceiling.

    Returns the block as a dict suitable for embedding under
    ``probe_summary["admitted_pressure"]``.
    """
    if isinstance(probe_window_end_iso, str):
        end_dt = _parse_iso8601_z(probe_window_end_iso)
    else:
        end_dt = probe_window_end_iso
    start_dt = end_dt - datetime.timedelta(seconds=window_s)
    admitted_in_window: list[datetime.datetime] = []
    for t in admitted_dispatch_iso_list:
        if isinstance(t, str):
            try:
                dt = _parse_iso8601_z(t)
            except ValueError:
                continue
        else:
            dt = t
        if start_dt <= dt < end_dt:
            admitted_in_window.append(dt)
    target_peak_rpm = float(candidate_tps) * 60.0
    floor_rpm = float(floor_ratio) * target_peak_rpm
    if observed_n_429 >= 1:
        observed_rpm = (
            (len(admitted_in_window) / float(window_s)) * 60.0
            if window_s > 0 else 0.0
        )
        return {
            "admitted_peak_rpm_observed_last_30s": observed_rpm,
            "target_peak_rpm": target_peak_rpm,
            "admitted_pressure_floor_rpm": floor_rpm,
            "admitted_pressure_floor_ratio": float(floor_ratio),
            "admitted_pressure_window_s": int(window_s),
            "admitted_pressure_passed": True,
            "admitted_pressure_skipped_due_to_429": True,
        }
    observed_rpm = (
        (len(admitted_in_window) / float(window_s)) * 60.0
        if window_s > 0 else 0.0
    )
    return {
        "admitted_peak_rpm_observed_last_30s": observed_rpm,
        "target_peak_rpm": target_peak_rpm,
        "admitted_pressure_floor_rpm": floor_rpm,
        "admitted_pressure_floor_ratio": float(floor_ratio),
        "admitted_pressure_window_s": int(window_s),
        "admitted_pressure_passed": observed_rpm >= floor_rpm,
        "admitted_pressure_skipped_due_to_429": False,
    }


def assert_probe_schedule_intended_rate(
    *,
    intended_dispatch_iso_list: list[str | datetime.datetime],
    candidate_tps: float,
    probe_window_end_iso: str | datetime.datetime,
    window_s: int = ADMITTED_PRESSURE_WINDOW_S,
    floor_ratio: float = ADMITTED_PRESSURE_FLOOR_RATIO,
) -> None:
    """v2.3 NEW — runtime invariant. Assert the schedule itself intended
    to dispatch at least ``floor(floor_ratio × candidate_tps × window_s)``
    records within the last ``window_s`` of the probe window.

    Raises ``ProbeScheduleIntendedRateInsufficientError`` if the
    schedule under-generated (caller must abort the probe with exit 8
    rather than running the admitted-pressure gate against an
    under-scheduled probe — which would falsely blame the dispatcher).
    """
    if isinstance(probe_window_end_iso, str):
        end_dt = _parse_iso8601_z(probe_window_end_iso)
    else:
        end_dt = probe_window_end_iso
    start_dt = end_dt - datetime.timedelta(seconds=window_s)
    intended_in_window = 0
    for t in intended_dispatch_iso_list:
        if isinstance(t, str):
            try:
                dt = _parse_iso8601_z(t)
            except ValueError:
                continue
        else:
            dt = t
        if start_dt <= dt < end_dt:
            intended_in_window += 1
    required = int(floor_ratio * float(candidate_tps) * float(window_s))
    if intended_in_window < required:
        raise ProbeScheduleIntendedRateInsufficientError(
            f"probe schedule intended only {intended_in_window} dispatches "
            f"in the last {window_s} s window; the v2.3 invariant requires "
            f">= {required} = floor({floor_ratio} × candidate_tps "
            f"{candidate_tps} × window_s {window_s})",
            candidate_tps=float(candidate_tps),
            intended_in_window=intended_in_window,
            required_in_window=required,
        )


def compute_bracket_geometric_midpoint(t_low: float, t_high: float) -> float:
    """v2.3 NEW — geometric midpoint for bounded bracket search.

    Returns ``sqrt(t_low * t_high)`` (spec-pinned formula). Arithmetic
    midpoint is the documented alternative but is NOT what the runner
    uses."""
    if t_low <= 0 or t_high <= 0:
        raise ValueError(
            f"bracket bounds must be > 0; got t_low={t_low}, t_high={t_high}"
        )
    if t_low >= t_high:
        raise ValueError(
            f"bracket requires t_low < t_high; got t_low={t_low}, "
            f"t_high={t_high}"
        )
    return math.sqrt(t_low * t_high)


def evaluate_candidate_grid_sanity(
    *,
    candidate_tps_grid: tuple[float, ...] | list[float],
    smallest_cell_max_output_tokens: int,
    largest_cell_max_output_tokens: int,
    base_prompt_tokens: int,
    deployment_tpm_quota: int,
) -> None:
    """v2.2.1 active TPM gate (candidate-grid sanity check).

    Replaces v2.1's single-TPS feasibility math (``peak_ramp_tps=0.33``)
    with a two-sided check across the predeclared grid:

    - **Lowest TPS keeps the smallest cell ≤ 0.85 × quota.** Catches
      operators who shrink the grid down to a TPS so low the smallest
      cell sails through every reasonable quota — leaving no usable
      contrast for selection.
    - **Highest TPS pushes the largest cell ≥ 1.25 × quota.** Catches
      operators who shrink the grid up to a TPS so high the largest cell
      still doesn't saturate quota — so calibration would burn the full
      budget without producing a single real 429.

    Raises ``LinkageValidationError`` with the spec-listed ``reason``
    on either failure. Network-free; suitable for unit tests."""
    if not candidate_tps_grid:
        raise LinkageValidationError(
            "candidate_tps_grid must be non-empty",
            reason="candidate_tps_grid_contains_ad_hoc_value",
        )
    lowest_tps = min(candidate_tps_grid)
    highest_tps = max(candidate_tps_grid)
    # Projected TPM at lowest TPS, smallest cell = 60 × tps × (prompt + cap).
    smallest_cell_projected_tpm_at_lowest = (
        60.0 * lowest_tps
        * (base_prompt_tokens + smallest_cell_max_output_tokens)
    )
    if smallest_cell_projected_tpm_at_lowest > 0.85 * deployment_tpm_quota:
        raise LinkageValidationError(
            f"lowest candidate TPS {lowest_tps} would push smallest cell "
            f"({smallest_cell_max_output_tokens} max_output_tokens) "
            f"projected TPM = {smallest_cell_projected_tpm_at_lowest:.0f} "
            f"over 0.85 × {deployment_tpm_quota} = "
            f"{0.85 * deployment_tpm_quota:.0f}; smallest-cell control "
            f"probe will not have meaningful headroom",
            reason="lowest_tps_overshoots_smallest_cell_at_cold_cache",
        )
    largest_cell_projected_tpm_at_highest = (
        60.0 * highest_tps
        * (base_prompt_tokens + largest_cell_max_output_tokens)
    )
    if largest_cell_projected_tpm_at_highest < 1.25 * deployment_tpm_quota:
        raise LinkageValidationError(
            f"highest candidate TPS {highest_tps} keeps largest cell "
            f"({largest_cell_max_output_tokens} max_output_tokens) "
            f"projected TPM = {largest_cell_projected_tpm_at_highest:.0f} "
            f"under 1.25 × {deployment_tpm_quota} = "
            f"{1.25 * deployment_tpm_quota:.0f}; largest-cell probe will "
            f"not saturate quota — calibration cannot produce real 429s",
            reason="highest_tps_undershoots_largest_cell_at_cold_cache",
        )


def deterministic_conservative_cost_estimator(
    *,
    stage: str,
    peak_tps: float,
    n_cells: int = 7,
    prewarm_calls_per_cell: int = PREWARM_CALLS_PER_CELL,
    prewarm_tps: float = PREWARM_TPS,
    ramp_duration_s: float | None = None,
    cool_down_seconds: int | None = None,
    per_call_usd: float = DETERMINISTIC_PER_CALL_USD,
    candidate_tps_grid: tuple[float, ...] = CALIBRATION_CANDIDATE_TPS_GRID,
    probe_duration_s: int = CALIBRATION_PROBE_DURATION_S,
    calibration_prewarm_calls: int = CALIBRATION_PREWARM_CALLS,
) -> float:
    """v2.2.1 — deterministic conservative cost estimator.

    Used both for the operator-facing budget projection AND for the
    preflight gate. Critically, it does NOT discount for the PAYG
    GlobalStandard 429-no-bill quirk — operators must NOT rely on free
    429s. The pinned reference values used by tests are:

    - ``stage='smoke', peak_tps=3.0`` → ``$12.29``
    - ``stage='evidence', peak_tps=3.0`` → ``$58.40``
    - ``stage='calibration_pessimistic'`` (full 7-probe-grid worst-case
      × largest-cell saturating) → ``$17.23``

    Args:
        stage: One of ``"smoke" | "evidence" | "calibration_pessimistic"``.
        peak_tps: ``selected_peak_tps`` for smoke/evidence; ignored for
            calibration.
        n_cells: 7 (the pinned sweep cardinality).
        per_call_usd: Per-call deterministic floor; default
            ``DETERMINISTIC_PER_CALL_USD`` ($0.009).

    Returns:
        Projected USD as a float.

    Raises:
        ValueError: unknown ``stage`` or non-positive ``peak_tps`` for
            smoke/evidence.
    """
    if stage not in ("smoke", "evidence", "calibration_pessimistic"):
        raise ValueError(
            f"stage must be 'smoke' | 'evidence' | "
            f"'calibration_pessimistic'; got {stage!r}"
        )
    if stage in ("smoke", "evidence") and peak_tps <= 0:
        raise ValueError(
            f"peak_tps must be > 0 for stage {stage!r}; got {peak_tps!r}"
        )
    if stage == "smoke":
        # Smoke: 2-min ramp + 30-s cooldown per cell; prewarm_calls_per_cell
        # static at 12. Calls per cell ≈ prewarm + ramp_integral.
        if ramp_duration_s is None:
            ramp_duration_s = 120.0
        ramp_calls = ramp_duration_s * (prewarm_tps + peak_tps) / 2.0
        calls_per_cell = prewarm_calls_per_cell + ramp_calls
        n = n_cells * calls_per_cell
        return float(n) * per_call_usd
    if stage == "evidence":
        # Evidence: 10-min ramp + 120-s cooldown per cell.
        if ramp_duration_s is None:
            ramp_duration_s = 600.0
        ramp_calls = ramp_duration_s * (prewarm_tps + peak_tps) / 2.0
        calls_per_cell = prewarm_calls_per_cell + ramp_calls
        n = n_cells * calls_per_cell
        return float(n) * per_call_usd
    # calibration_pessimistic — deterministic-conservative pessimistic
    # walk per spec § Cost & Time Budget Stage 0.5:
    #   7 candidates × (prewarm + probe_duration_s × candidate_tps) calls
    #   + 1 smallest-cell control probe at TPS=1.0 (12 + 180 = 192 calls)
    # at $0.009 per call. The spec pins the result to $17.23 (1914 calls).
    largest_probe_calls = sum(
        calibration_prewarm_calls + probe_duration_s * tps
        for tps in candidate_tps_grid
    )
    smallest_control_calls = calibration_prewarm_calls + probe_duration_s * 1.0
    total_calls = largest_probe_calls + smallest_control_calls
    return float(total_calls) * per_call_usd


def compute_calibration_result_sha256(path: pathlib.Path | str) -> str:
    """v2.2.1 — convenience wrapper over ``_sha256_file`` for calibration
    results. Returns the hex sha256."""
    return _sha256_file(pathlib.Path(path))


STARTUP_ABORT_PHASE_B_REASONS: frozenset[str] = frozenset({
    "candidate_tps_grid_phase_b_member_missing",
    "candidate_tps_grid_phase_b_contains_ad_hoc_value",
    "candidate_tps_grid_phase_b_contains_duplicate_value",
    "candidate_tps_grid_phase_b_not_sorted_ascending",
})
"""v2.3 microfix 2026-05-30 (finding #3) — the four pinned YAML-load
abort reasons that trigger the ``startup_abort_reason`` deterministic
artifact. Each maps to exit code 9."""


EXPERIMENT_YAML_MALFORMED_REASON: str = "experiment_yaml_malformed"
"""v2.3 microfix 2026-05-30 fix loop #2 (auditor finding #2) — pinned
``startup_abort_reason`` value emitted when ``yaml.safe_load`` raises
``yaml.YAMLError`` on the experiment YAML. ``main`` catches the
``yaml.YAMLError``, suppresses the stack trace, writes the deterministic
``calibration_startup_abort.result.json`` artifact with this reason via
``emit_startup_abort_artifact``, and returns ``EXIT_LINKAGE_FAIL`` (exit
9) — consistent with the other startup-abort paths in v2.3."""


def emit_startup_abort_artifact(
    runs_dir: pathlib.Path | str,
    *,
    experiment_id: str,
    startup_abort_reason: str,
    message: str,
    timestamp_label: str | None = None,
) -> pathlib.Path:
    """v2.3 microfix 2026-05-30 (auditor finding #3) — write a
    deterministic ``calibration_result.json`` artifact that carries
    the ``startup_abort_reason`` field on YAML-load failures so
    downstream audit can detect the abort without scraping stderr.

    The spec requires the four Phase B grid mutations
    (``candidate_tps_grid_phase_b_*``) abort at YAML-load with
    ``startup_abort_reason`` recorded to BOTH stderr AND
    ``calibration_result.json``. Wider YAML-load failures may also
    use this helper to emit an equivalent deterministic artifact per
    the spec's "or an equivalent deterministic artifact" clause.

    Args:
        runs_dir: Target directory; created if missing.
        experiment_id: Experiment id (echoed into the artifact).
        startup_abort_reason: The pinned reason string (e.g.
            ``candidate_tps_grid_phase_b_member_missing``).
        message: Human-readable error message (echoed for audit).
        timestamp_label: Optional; defaults to UTC ``YYYYMMDDTHHMMSSZ``.

    Returns:
        The absolute path of the written artifact.

    Raises:
        OSError: directory cannot be created or file cannot be written.
    """
    runs_dir_p = pathlib.Path(runs_dir)
    runs_dir_p.mkdir(parents=True, exist_ok=True)
    ts = timestamp_label or _utc_now().strftime("%Y%m%dT%H%M%SZ")
    artifact_path = (
        runs_dir_p
        / f"{ts}_{experiment_id}_calibration_startup_abort.result.json"
    )
    doc = {
        "schema_version": "task019.v2.3.calibration_result",
        "experiment_id": experiment_id,
        "outcome": None,
        "selected_peak_tps": None,
        "selected_via": None,
        "selected_at_phase": None,
        "selected_bracket_root_phase": None,
        "selected_at_candidate_idx": None,
        "selected_at_bracket_depth": None,
        "startup_abort_reason": startup_abort_reason,
        "startup_abort_message": message,
        "completed_at_iso": _iso8601_z(_utc_now()),
    }
    artifact_path.write_text(
        json.dumps(doc, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return artifact_path


def build_arrival_schedule(
    *,
    seed_str: str,
    prewarm_calls: int,
    prewarm_tps: float,
    ramp_duration_s: float,
    peak_ramp_tps: float,
) -> tuple[list[float], list[float]]:
    """Build a deterministic arrival schedule for one cell.

    Returns ``(prewarm_times_s, ramp_times_s)``. Each value is a
    cell-start-relative dispatch time in seconds.

    Pre-warm: ``prewarm_calls`` evenly spaced at ``prewarm_tps`` cadence
    (deterministic, no PRNG). Pre-warm occupies
    ``prewarm_calls / prewarm_tps`` seconds.

    Ramp: linear TPS ramp from ``prewarm_tps`` to ``peak_ramp_tps`` over
    ``ramp_duration_s`` seconds. Arrival count = integral of TPS over the
    ramp ≈ ``ramp_duration_s × (prewarm_tps + peak_ramp_tps) / 2``. The
    integer arrival count is computed deterministically from the seed
    via SHA-256 → first 32 bits → modulo a small jitter window so the
    schedule is bit-stable across runs but the count is not always the
    rounded-down truncation. Arrival times are placed at the inverse-CDF
    of the linear-TPS function.

    Args:
        seed_str: Deterministic seed (e.g.
            ``"exp007_max_output_tokens_sweep_cell00256"``).
        prewarm_calls: Number of pre-warm calls.
        prewarm_tps: Pre-warm cadence.
        ramp_duration_s: Ramp duration in seconds.
        peak_ramp_tps: Peak ramp TPS at the end of the ramp.

    Returns:
        Tuple ``(prewarm_times_s, ramp_times_s)`` of dispatch-time lists
        (cell-start-relative seconds).

    Raises:
        ValueError: Any non-positive duration or rate, or non-positive
            prewarm_calls.
    """
    if prewarm_calls <= 0:
        raise ValueError(f"prewarm_calls must be > 0; got {prewarm_calls}")
    if prewarm_tps <= 0:
        raise ValueError(f"prewarm_tps must be > 0; got {prewarm_tps}")
    if ramp_duration_s <= 0:
        raise ValueError(
            f"ramp_duration_s must be > 0; got {ramp_duration_s}"
        )
    if peak_ramp_tps < prewarm_tps:
        raise ValueError(
            f"peak_ramp_tps ({peak_ramp_tps}) must be >= prewarm_tps "
            f"({prewarm_tps})"
        )

    prewarm_interval = 1.0 / prewarm_tps
    prewarm_times = [i * prewarm_interval for i in range(prewarm_calls)]
    prewarm_end_s = prewarm_calls * prewarm_interval

    # Average TPS over the linear ramp.
    avg_ramp_tps = (prewarm_tps + peak_ramp_tps) / 2.0
    base_ramp_calls = int(ramp_duration_s * avg_ramp_tps)

    # Deterministic jitter ∈ {0, 1} (drives schedule reproducibility tests
    # without inflating call counts materially). SHA-256 of the seed →
    # first 32 bits → modulo 2.
    digest = hashlib.sha256(seed_str.encode("utf-8")).digest()
    jitter = struct.unpack(">I", digest[:4])[0] % 2
    ramp_calls = base_ramp_calls + jitter
    if ramp_calls <= 0:
        return prewarm_times, []

    # Linear TPS r(t) = a + b * t where a = prewarm_tps, b = slope.
    # Cumulative arrival count up to time t in the ramp:
    #   N(t) = a*t + 0.5 * b * t^2
    # Total over the ramp:
    #   N(T) = a*T + 0.5 * b * T^2  (= ramp_duration_s * avg_ramp_tps)
    # Place arrival i at t such that N(t) = i + 0.5 (centered placement).
    a = prewarm_tps
    b = (peak_ramp_tps - prewarm_tps) / ramp_duration_s
    ramp_times: list[float] = []
    for i in range(ramp_calls):
        target_n = i + 0.5
        # Solve 0.5 * b * t^2 + a * t - target_n = 0 → quadratic.
        if abs(b) < 1e-12:
            t = target_n / a
        else:
            disc = a * a + 2.0 * b * target_n
            if disc < 0:
                # Numerical guard — should never happen with positive
                # slope and positive target_n.
                disc = 0.0
            t = (-a + (disc) ** 0.5) / b
        if t < 0:
            t = 0.0
        if t > ramp_duration_s:
            t = ramp_duration_s
        ramp_times.append(prewarm_end_s + t)
    return prewarm_times, ramp_times


class RpmTracker:
    """Rolling 60-second arrival-rate tracker. Pure-Python deque."""

    def __init__(self, window_s: float = 60.0) -> None:
        self.window_s = float(window_s)
        self._dq: collections.deque[float] = collections.deque()

    def record(self, ts: float) -> None:
        self._dq.append(float(ts))

    def count(self, now: float) -> int:
        cutoff = float(now) - self.window_s
        while self._dq and self._dq[0] < cutoff:
            self._dq.popleft()
        return len(self._dq)


# ----------------------------------------------------------------------------
# Run-lock helpers (v2.1; spec § Implementation Notes → Run-lock)
# ----------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """Return True iff ``pid`` exists on this host. Signal 0 does not
    deliver a signal; it just performs the existence check."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — still "alive".
        return True
    except OSError as exc:
        # ESRCH = No such process.
        return exc.errno != errno.ESRCH
    return True


def acquire_runlock(
    runlock_path: pathlib.Path,
    *,
    experiment_id: str,
    expected_duration_min: int,
    pid: int | None = None,
    hostname: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Acquire an exclusive non-blocking flock on ``runlock_path``.

    Stale-PID reclaim: if the lock file is present but the recorded PID
    no longer exists, reclaim with a WARNING log and proceed. If the
    lock is held by a live PID, raise ``RunLockHeldError`` (caller maps
    to exit code 4).

    Args:
        runlock_path: Path to the lock file.
        experiment_id: Echoed into the holder JSON for audit.
        expected_duration_min: Expected duration; informs the
            ``expected_completion_iso`` field.
        pid: Override for ``os.getpid()`` (test injection).
        hostname: Override for ``socket.gethostname()`` (test injection).

    Returns:
        Tuple ``(lock_fd, holder_metadata_dict)``. The caller MUST call
        ``release_runlock(lock_fd)`` (or close the fd) at process exit;
        the OS releases the flock when the fd is closed regardless.

    Raises:
        RunLockHeldError: A live PID holds the lock.
    """
    runlock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(
        str(runlock_path), os.O_CREAT | os.O_RDWR, 0o600
    )
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Possibly held — check for stale PID.
        try:
            with open(str(runlock_path), "r", encoding="utf-8") as fh:
                holder = json.load(fh)
            holder_pid = int(holder.get("pid", 0))
            if not _pid_alive(holder_pid):
                logger.warning(
                    "RUNLOCK_STALE_RECLAIM stale_pid=%s host=%s "
                    "experiment_id=%s started=%s",
                    holder.get("pid"),
                    holder.get("hostname"),
                    holder.get("experiment_id"),
                    holder.get("started_at_iso"),
                )
                os.close(lock_fd)
                lock_fd = os.open(
                    str(runlock_path),
                    os.O_CREAT | os.O_RDWR | os.O_TRUNC,
                    0o600,
                )
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    os.close(lock_fd)
                    raise RunLockHeldError(
                        f"runlock at {runlock_path} still held after "
                        f"stale-PID reclaim attempt"
                    ) from exc
            else:
                os.close(lock_fd)
                raise RunLockHeldError(
                    f"runlock at {runlock_path} held by live pid="
                    f"{holder_pid} host={holder.get('hostname')} "
                    f"experiment_id={holder.get('experiment_id')!r} "
                    f"started={holder.get('started_at_iso')}; refuse to "
                    f"start (Task 019 v2.1 requires throttled-deployment "
                    f"exclusivity)"
                )
        except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
            os.close(lock_fd)
            raise RunLockHeldError(
                f"runlock at {runlock_path} present but unreadable; "
                f"refusing to start"
            )

    now = _utc_now()
    expected = now + datetime.timedelta(minutes=int(expected_duration_min))
    holder_metadata = {
        "pid": pid if pid is not None else os.getpid(),
        "hostname": (
            hostname if hostname is not None else socket.gethostname()
        ),
        "experiment_id": experiment_id,
        "started_at_iso": _iso8601_z(now),
        "expected_completion_iso": _iso8601_z(expected),
    }
    os.lseek(lock_fd, 0, 0)
    os.ftruncate(lock_fd, 0)
    os.write(
        lock_fd,
        json.dumps(holder_metadata, sort_keys=True).encode("utf-8"),
    )
    return lock_fd, holder_metadata


def release_runlock(lock_fd: int) -> None:
    """Release the flock and close the fd. Errors are swallowed (the OS
    releases the flock on process exit regardless)."""
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(lock_fd)
    except OSError:
        pass


# ----------------------------------------------------------------------------
# Prompt-identity verification (v2.1 blocker #1)
# ----------------------------------------------------------------------------


def verify_prompt_identity_or_exit7(
    *,
    corpus_path: pathlib.Path,
    user_prompts_path: pathlib.Path,
    corpus_seed: int,
    target_tokens: int,
    expected_source_corpus_sha: str = EXPECTED_SOURCE_CORPUS_SHA256,
    expected_assembled_sha: str = EXPECTED_ASSEMBLED_SYSTEM_PROMPT_SHA256,
    expected_user_prompts_sha: str = EXPECTED_USER_PROMPTS_SOURCE_SHA256,
    index_set: tuple[int, ...] = USER_PROMPTS_INDEX_SET,
) -> tuple[str, list[str], list[str]]:
    """Verify the three pinned SHAs and return the assembled prompt and
    user-prompt subset.

    Returns ``(assembled_system_prompt, all_user_prompts,
    selected_user_prompts)``. ``selected_user_prompts`` is the in-memory
    index-set subset of length ``len(index_set)``.

    Raises:
        CorpusMissingError: Either file missing or malformed.
        PromptIdentitySHAMismatchError: Any of the three SHAs does not
            match the pinned expected value. The runner maps this to
            exit code 7.
    """
    if not corpus_path.is_file():
        raise CorpusMissingError(
            f"source corpus not found at {corpus_path} (Task 019 v2.1 "
            f"prompt-identity contract requires READ-ONLY access to the "
            f"existing Task 012 corpus at "
            f"{EXPECTED_SOURCE_CORPUS_PATH})"
        )
    if not user_prompts_path.is_file():
        raise CorpusMissingError(
            f"user-prompts source not found at {user_prompts_path} (Task "
            f"019 v2.1 prompt-identity contract requires READ-ONLY access "
            f"to the existing Task 012 user prompts at "
            f"{EXPECTED_USER_PROMPTS_SOURCE_PATH})"
        )

    # (a) source corpus SHA.
    corpus_bytes = corpus_path.read_bytes()
    actual_corpus_sha = hashlib.sha256(corpus_bytes).hexdigest()
    if actual_corpus_sha != expected_source_corpus_sha:
        raise PromptIdentitySHAMismatchError(
            f"source corpus SHA mismatch at {corpus_path}: actual="
            f"{actual_corpus_sha} expected={expected_source_corpus_sha} "
            f"(Task 019 v2.1 prompt-identity contract — exit code 7)"
        )

    # (b) assembled system prompt SHA — via the IMPORTED Task 012 builder
    # (READ-ONLY; not reimplemented).
    try:
        assembled_prompt = build_system_prompt(
            corpus_path,
            corpus_seed=corpus_seed,
            target_tokens=target_tokens,
        )
    except Exception as exc:
        raise CorpusMissingError(
            f"failed to assemble system prompt from {corpus_path} via "
            f"scripts.simulate_spillover.build_system_prompt(corpus_seed="
            f"{corpus_seed}, target_tokens={target_tokens}): {exc}"
        ) from exc
    actual_assembled_sha = hashlib.sha256(
        assembled_prompt.encode("utf-8")
    ).hexdigest()
    if actual_assembled_sha != expected_assembled_sha:
        raise PromptIdentitySHAMismatchError(
            f"assembled system prompt SHA mismatch: actual="
            f"{actual_assembled_sha} expected={expected_assembled_sha} "
            f"(build_system_prompt({corpus_path}, corpus_seed="
            f"{corpus_seed}, target_tokens={target_tokens}) produced a "
            f"prompt whose SHA does not match the v2.1 pinned constant — "
            f"exit code 7)"
        )

    # (c) user-prompts source SHA.
    user_bytes = user_prompts_path.read_bytes()
    actual_user_sha = hashlib.sha256(user_bytes).hexdigest()
    if actual_user_sha != expected_user_prompts_sha:
        raise PromptIdentitySHAMismatchError(
            f"user-prompts source SHA mismatch at {user_prompts_path}: "
            f"actual={actual_user_sha} expected={expected_user_prompts_sha}"
            f" (Task 019 v2.1 prompt-identity contract — exit code 7)"
        )

    # Load all user prompts and select the pinned subset in-memory.
    try:
        all_prompts = json.loads(user_bytes.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise CorpusMissingError(
            f"failed to parse user_prompts {user_prompts_path}: {exc}"
        ) from exc
    if not isinstance(all_prompts, list) or not all_prompts:
        raise CorpusMissingError(
            f"user_prompts {user_prompts_path} must be a non-empty JSON "
            f"list of strings"
        )
    if not all(isinstance(s, str) and s for s in all_prompts):
        raise CorpusMissingError(
            f"user_prompts {user_prompts_path} entries must all be "
            f"non-empty strings"
        )
    max_idx = max(index_set)
    if max_idx >= len(all_prompts):
        raise CorpusMissingError(
            f"user_prompts {user_prompts_path} has {len(all_prompts)} "
            f"entries but index_set requires index {max_idx}"
        )
    selected = [all_prompts[i] for i in index_set]
    return assembled_prompt, list(all_prompts), selected



# ----------------------------------------------------------------------------
# Citations block
# ----------------------------------------------------------------------------


class CitationsBuilder:
    """Builds the Task 019 v2.1 Citations block. Includes BOTH the Azure
    rate-limit / quota concept doc (the proxy mechanism this benchmark
    measures) AND the Azure PTU concept doc (the mechanism whose shape
    we are proxying), so the reader can verify the proxy interpretation
    themselves without leaving the analysis."""

    def __init__(
        self,
        *,
        pricing_path: str,
        pricing_source_url: str,
        pricing_accessed_date: str,
    ) -> None:
        self.pricing_path = pricing_path
        self.pricing_source_url = pricing_source_url
        self.pricing_accessed_date = pricing_accessed_date

    def to_dict(self) -> dict[str, Any]:
        return {
            "azure_rate_limit_doc": {
                "url": AZURE_RATE_LIMIT_DOC_URL,
                "accessed_date": AZURE_RATE_LIMIT_DOC_ACCESSED_DATE,
                "claims_cited": list(AZURE_RATE_LIMIT_DOC_CLAIMS_CITED),
            },
            "azure_ptu_doc": {
                "url": AZURE_PTU_DOC_URL,
                "accessed_date": AZURE_PTU_DOC_ACCESSED_DATE,
                "claims_cited": list(AZURE_PTU_DOC_CLAIMS_CITED),
            },
            "azure_prompt_caching_doc": {
                "url": AZURE_DOC_PROMPT_CACHING_URL,
                "accessed_date": AZURE_DOC_ACCESSED_DATE,
                "claims_cited": [
                    "prompt_cache_key combined with prefix hash influences "
                    "routing; per-(key, prefix) cache identity"
                ],
            },
            "pricing": {
                "path": self.pricing_path,
                "source_url": self.pricing_source_url,
                "accessed_date": self.pricing_accessed_date,
            },
        }


# ----------------------------------------------------------------------------
# Experiment YAML dataclasses + loader
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class _DeploymentBlock:
    deployment_template: str
    deployment_name: str
    family: str
    version: str
    endpoint_env: str
    deployment_env: str
    auth_mode: str
    tpm: int | None
    rpm: int | None


@dataclass(frozen=True)
class _ClientBlock:
    api_version: str
    max_retries: int


@dataclass(frozen=True)
class _RequestTemplate:
    reasoning_effort: str


@dataclass(frozen=True)
class _EmpiricalPromotionBlock:
    """v2.4 §10 — parsed `runtime.empirical_promotion.*` YAML block.

    Backwards compatibility: every field is optional in YAML. v2.3-
    shaped YAMLs missing the entire `empirical_promotion` block parse
    cleanly with PIN defaults. When YAML CARRIES a value, the loader
    enforces equality against the §10 PIN (no per-run loosening).

    The default values below are literal mirrors of the §10 module-
    level PIN constants defined alongside the v2.4 module further down
    in this file. ``_assert_empirical_promotion_pins_match_defaults``
    (called once at module load) guarantees the defaults stay in sync
    with the module-level constants — auditor checklist §13(c) "single
    source of truth".
    """

    cache_hit_floor_smallest_control: float = 0.80
    cache_hit_floor_largest: float = 0.80
    calibration_max_age_hours: int = 24
    minimum_records_at_selected_tps: int = 30
    mini_probe_enabled: bool = False
    mini_probe_max_usd: float = 1.00
    mini_probe_max_attempts_per_run: int = 1

    def to_config(self) -> "EmpiricalPromotionConfig":
        return EmpiricalPromotionConfig(
            cache_hit_floor_smallest_control=(
                self.cache_hit_floor_smallest_control
            ),
            cache_hit_floor_largest=self.cache_hit_floor_largest,
            calibration_max_age_hours=self.calibration_max_age_hours,
            minimum_records_at_selected_tps=(
                self.minimum_records_at_selected_tps
            ),
            mini_probe_enabled=self.mini_probe_enabled,
            mini_probe_max_usd=self.mini_probe_max_usd,
            mini_probe_max_attempts_per_run=(
                self.mini_probe_max_attempts_per_run
            ),
        )


@dataclass(frozen=True)
class _RuntimeBlock:
    concurrency: int
    dispatcher: str
    prewarm_calls_per_cell: int
    prewarm_tps: float
    ramp_duration_seconds: int
    peak_ramp_tps: float
    cool_down_seconds: int
    smoke_ramp_duration_seconds: int
    smoke_cool_down_seconds: int
    # v2.3 NEW (optional in YAML for backwards compatibility; defaults
    # populated by load_experiment when absent so v2.2.1 YAMLs keep
    # loading). Phase B + Phase-B-rooted bracket probes use this
    # concurrency; Phase A / smoke / evidence retain `concurrency`.
    concurrency_phase_b: int = CONCURRENCY_PHASE_B_PINNED
    # v2.4 NEW (optional in YAML for backwards compatibility; defaults
    # populated by load_experiment when absent so v2.3 YAMLs keep
    # loading). All sub-fields are §10 PINs — loader enforces equality
    # when the YAML CARRIES a value.
    empirical_promotion: _EmpiricalPromotionBlock = field(
        default_factory=_EmpiricalPromotionBlock
    )
    # Task 019 v2.6 (§3.2) — optional adaptive_calibration sub-block
    # parsed from YAML. Defaults to a disabled block so v2.4 YAMLs keep
    # loading unchanged. When ``enabled`` is true, the §3.2 trigger
    # predicate may admit Stage 0.5.C; auditor-approval + disclosure
    # path are already preflight-enforced by load_experiment via the
    # v2.5 ``validate_adaptive_calibration_yaml_block`` helper.
    adaptive_calibration: "_AdaptiveCalibrationBlock" = field(
        default_factory=lambda: _AdaptiveCalibrationBlock()
    )


@dataclass(frozen=True)
class _AdaptiveCalibrationBlock:
    """v2.6 — parsed ``runtime.adaptive_calibration`` YAML sub-block.

    Only carries the fields the runner needs to evaluate the §3.2
    trigger and to populate the adaptive summary's
    ``auditor_approval_comment_verbatim`` and
    ``disclosed_prior_calibrations`` fields. The v2.5 preflight
    (``validate_adaptive_calibration_yaml_block``) is the authoritative
    enforcer — this block stores already-validated values.
    """

    enabled: bool = False
    prior_calibrations_disclosure_path: str | None = None
    auditor_approval_comment: str | None = None


@dataclass(frozen=True)
class _SweepBlock:
    max_output_tokens: list[int]


@dataclass(frozen=True)
class _BudgetBlock:
    evidence_estimated_usd: float
    evidence_hard_ceiling_usd: float
    smoke_estimated_usd: float
    smoke_hard_ceiling_usd: float
    confirmed: bool
    # v2.2.1 NEW (optional in YAML for backwards compatibility — defaults
    # populated by load_experiment when absent so v2.1 YAMLs keep loading).
    calibration_estimated_usd: float = 0.0
    calibration_hard_ceiling_usd: float = CALIBRATION_TOTAL_MAX_USD
    calibration_probe_hard_ceiling_usd: float = CALIBRATION_PROBE_MAX_USD
    total_task_hard_ceiling_usd: float = TASK_TOTAL_HARD_CEILING_USD
    contingency_hard_ceiling_usd: float = CONTINGENCY_HARD_CEILING_USD


@dataclass(frozen=True)
class _CalibrationBlock:
    """v2.2.1 NEW (v2.3 EXTENDED). Parsed `calibration:` YAML block.

    ``candidate_tps_grid`` is validated at load time against the pinned
    7-member allowed set (``CALIBRATION_CANDIDATE_TPS_GRID``); any ad-hoc
    value or non-ascending order raises ``LinkageValidationError`` with
    the appropriate reason.

    v2.3 NEW fields:

    - ``candidate_tps_grid_phase_b``: predeclared Phase B escalation
      grid; MUST equal the pinned six-member list in EXACT ascending
      order (Microfix C).
    - ``probe_max_calls_phase_b``: per-probe call-count cap for Phase B.
    - ``admitted_pressure_floor_ratio`` / ``admitted_pressure_window_s``:
      v2.3 admitted-pressure gate config.
    - ``bracket_max_depth``: bounded bracket-search recursion cap.
    """

    candidate_tps_grid: tuple[float, ...]
    prewarm_calls: int
    prewarm_tps: float
    probe_duration_s: int
    probe_max_calls: int
    probe_max_usd: float
    total_max_usd: float
    largest_cell_max_output_tokens: int
    smallest_cell_max_output_tokens: int
    early_stop_on_first_429_largest: bool
    early_stop_on_first_429_smallest: bool
    inter_probe_cooldown_s: int
    calibration_max_age_hours: int
    # v2.3 NEW (optional in YAML for backwards compatibility — defaults
    # populated by load_experiment when absent so v2.2.1 YAMLs still
    # load, but the Phase B grid is REQUIRED for v2.3 calibration runs).
    candidate_tps_grid_phase_b: tuple[float, ...] = (
        CALIBRATION_CANDIDATE_TPS_GRID_PHASE_B
    )
    probe_max_calls_phase_b: int = CALIBRATION_PROBE_MAX_CALLS_PHASE_B
    admitted_pressure_floor_ratio: float = ADMITTED_PRESSURE_FLOOR_RATIO
    admitted_pressure_window_s: int = ADMITTED_PRESSURE_WINDOW_S
    bracket_max_depth: int = BRACKET_MAX_DEPTH


@dataclass(frozen=True)
class ExperimentConfig:
    """Parsed Task 019 v2.2.1 experiment YAML."""

    path: pathlib.Path
    experiment_id: str
    description: str
    parent_experiment: str | None
    benchmark: str
    deployment: _DeploymentBlock
    request_template: _RequestTemplate
    client: _ClientBlock
    runtime: _RuntimeBlock
    sweep: _SweepBlock
    budget: _BudgetBlock
    corpus_seed: int
    target_system_prompt_tokens: int
    source_corpus_path: str
    source_corpus_sha256: str
    expected_assembled_system_prompt_sha256: str
    user_prompts_source_path: str
    user_prompts_source_sha256: str
    user_prompts_index_set: list[int]
    pricing_snapshot_path: str
    metadata: dict[str, Any]
    deployment_tpm_quota: int
    # v2.2.1 NEW. Optional so v2.1-shaped YAML still loads (loader
    # populates a default block constructed from v2.2.1 constants).
    calibration: _CalibrationBlock | None = None


def _require(d: dict, key: str, where: str) -> Any:
    if key not in d:
        raise ValueError(f"{where}: missing required key {key!r}")
    return d[key]


_ENV_TEMPLATE_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _extract_env_name(template: str) -> str | None:
    m = _ENV_TEMPLATE_RE.fullmatch(template.strip())
    return m.group(1) if m else None


def _parse_adaptive_calibration_block(
    raw: dict[str, Any],
) -> "_AdaptiveCalibrationBlock":
    """Task 019 v2.6 — pull the ``runtime.adaptive_calibration`` block.

    Returns a disabled ``_AdaptiveCalibrationBlock`` when the block is
    absent or ``enabled`` is false. The v2.5 preflight
    (``validate_adaptive_calibration_yaml_block``) is invoked from
    ``load_experiment`` BEFORE this function; failures raise
    ``LinkageValidationError`` and never reach here. This parser only
    captures the already-validated values needed at runtime by the §3.2
    trigger predicate and the adaptive summary writer.
    """
    rt = raw.get("runtime") or {}
    block = rt.get("adaptive_calibration") or {}
    if not isinstance(block, dict) or not block.get("enabled", False):
        return _AdaptiveCalibrationBlock()
    approval = block.get("adaptive_calibration_auditor_approval") or {}
    comment = None
    if isinstance(approval, dict):
        comment = approval.get("comment")
    return _AdaptiveCalibrationBlock(
        enabled=True,
        prior_calibrations_disclosure_path=(
            block.get("prior_calibrations_disclosure_path")
        ),
        auditor_approval_comment=comment,
    )


def load_experiment(path: str | pathlib.Path) -> ExperimentConfig:
    """Load + validate the Task 019 v2.1 experiment YAML.

    Every pinned control is enforced at YAML-load time so a bad
    configuration never reaches the network. Mismatched SHA pins are
    detected here AND re-checked at runtime against the actual file
    contents (v2.1 prompt-identity contract).

    Raises:
        FileNotFoundError: ``path`` does not exist.
        ValueError: any schema violation or pin mismatch.
    """
    p = pathlib.Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"experiment YAML not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ValueError(
            f"{p}: experiment YAML must be a mapping at top level"
        )

    where = str(p)
    exp_id = _require(raw, "experiment_id", where)
    description = _require(raw, "description", where)
    benchmark = _require(raw, "benchmark", where)
    if benchmark != "07-max-output-tokens-reservation":
        raise ValueError(
            f"{where}: benchmark must be "
            f"'07-max-output-tokens-reservation'; got {benchmark!r}"
        )
    parent = raw.get("parent_experiment")

    # ---- deployment block ----
    dep_raw = _require(raw, "deployment", where)
    if not isinstance(dep_raw, dict):
        raise ValueError(f"{where}: deployment must be a mapping")
    family = _require(dep_raw, "family", f"{where}.deployment")
    if family != "gpt-5.2":
        raise ValueError(
            f"{where}: deployment.family must be 'gpt-5.2'; got {family!r}"
        )
    auth_mode = _require(dep_raw, "auth_mode", f"{where}.deployment")
    if auth_mode != "entra":
        raise ValueError(
            f"{where}: deployment.auth_mode must be 'entra'; got "
            f"{auth_mode!r}"
        )
    deployment_template = str(
        _require(dep_raw, "deployment", f"{where}.deployment")
    )
    deployment_env = _extract_env_name(deployment_template) or ""
    if not deployment_env:
        raise ValueError(
            f"{where}: deployment.deployment must be a ${{VAR}} template; "
            f"got {deployment_template!r}"
        )
    # Task 019 v2.1 REQUIRES the throttled deployment (the unthrottled
    # gpt-5.2 deployment has 500K TPM, would not produce 429s at the
    # arrival rates this sweep uses, eliminating the signal).
    if "THROTTLED" not in deployment_env.upper():
        raise ValueError(
            f"{where}: deployment.deployment env-var name {deployment_env!r}"
            f" does NOT contain 'THROTTLED'; Task 019 v2.1 requires the "
            f"throttled ptu-deploy-throttled deployment (60K TPM PAYG) so "
            f"429 onset is observable. The unthrottled gpt-5.2 deployment "
            f"would not produce 429s at this sweep's arrival rates."
        )
    deployment = _DeploymentBlock(
        deployment_template=deployment_template,
        deployment_name=str(
            _require(dep_raw, "deployment_name", f"{where}.deployment")
        ),
        family=family,
        version=str(dep_raw.get("version", "")),
        endpoint_env=str(
            dep_raw.get("endpoint_env", "AZURE_OPENAI_FOUNDRY_ENDPOINT")
        ),
        deployment_env=deployment_env,
        auth_mode=auth_mode,
        tpm=(int(dep_raw["tpm"]) if "tpm" in dep_raw else None),
        rpm=(int(dep_raw["rpm"]) if "rpm" in dep_raw else None),
    )

    # ---- client block ----
    client_raw = _require(raw, "client", where)
    if not isinstance(client_raw, dict):
        raise ValueError(f"{where}: client must be a mapping")
    api_version = str(_require(client_raw, "api_version", f"{where}.client"))
    if api_version != FOUNDRY_API_VERSION:
        raise ValueError(
            f"{where}: client.api_version must be {FOUNDRY_API_VERSION!r} "
            f"(Foundry v1 pin); got {api_version!r}"
        )
    max_retries = int(_require(client_raw, "max_retries", f"{where}.client"))
    if max_retries != SDK_MAX_RETRIES_PINNED:
        raise ValueError(
            f"{where}: client.max_retries must be {SDK_MAX_RETRIES_PINNED} "
            f"(Task 019 v2.1 MANDATORY zero — the SDK default of 2 retries "
            f"silently absorbs the FIRST 429 onset signal this benchmark "
            f"exists to measure); got {max_retries}"
        )
    client = _ClientBlock(
        api_version=api_version, max_retries=max_retries,
    )

    # ---- request_template block ----
    req_raw = _require(raw, "request_template", where)
    if not isinstance(req_raw, dict):
        raise ValueError(f"{where}: request_template must be a mapping")
    reasoning_raw = _require(req_raw, "reasoning", f"{where}.request_template")
    if not isinstance(reasoning_raw, dict):
        raise ValueError(
            f"{where}: request_template.reasoning must be a mapping"
        )
    effort = str(
        _require(reasoning_raw, "effort", f"{where}.request_template.reasoning")
    )
    if effort != "low":
        raise ValueError(
            f"{where}: request_template.reasoning.effort must be 'low' "
            f"(Task 019 v2.1 pinned control — gpt-5.2 rejects 'minimal'; "
            f"'low' keeps actual reasoning small so visible output is "
            f"approximately the visible-output measurement); got {effort!r}"
        )
    # max_output_tokens MUST NOT appear in request_template (it varies per
    # cell via sweep.max_output_tokens; declaring it at request_template
    # level would let it silently override the sweep).
    if "max_output_tokens" in req_raw:
        raise ValueError(
            f"{where}: request_template.max_output_tokens MUST NOT be set "
            f"in Task 019 v2.1 — it varies per cell via "
            f"sweep.max_output_tokens. The cell's value is passed at HTTP "
            f"send time."
        )
    request_template = _RequestTemplate(reasoning_effort=effort)

    # ---- runtime block ----
    runtime_raw = _require(raw, "runtime", where)
    if not isinstance(runtime_raw, dict):
        raise ValueError(f"{where}: runtime must be a mapping")
    concurrency = int(_require(runtime_raw, "concurrency", f"{where}.runtime"))
    if concurrency != CONCURRENCY_PINNED:
        raise ValueError(
            f"{where}: runtime.concurrency must be {CONCURRENCY_PINNED} "
            f"(Task 019 v2.1 pinned control — same as Task 018 v2.4; sized "
            f"for ~50% headroom over Little's-Law steady-state in-flight "
            f"under observed live gpt-5.2 P95 TTFT ≈ 128 s); got "
            f"{concurrency}"
        )
    dispatcher = str(_require(runtime_raw, "dispatcher", f"{where}.runtime"))
    if dispatcher != DISPATCHER_PINNED:
        raise ValueError(
            f"{where}: runtime.dispatcher must be {DISPATCHER_PINNED!r} "
            f"(Task 019 v2.1 pinned control); got {dispatcher!r}"
        )
    prewarm_calls = int(
        _require(runtime_raw, "prewarm_calls_per_cell", f"{where}.runtime")
    )
    if prewarm_calls != PREWARM_CALLS_PER_CELL:
        raise ValueError(
            f"{where}: runtime.prewarm_calls_per_cell must be "
            f"{PREWARM_CALLS_PER_CELL} (Task 019 v2.1 pinned control — "
            f"SAME at smoke and full so the warm criterion is enforceable "
            f"at smoke per v2.1 blocker #3); got {prewarm_calls}"
        )
    prewarm_tps = float(
        _require(runtime_raw, "prewarm_tps", f"{where}.runtime")
    )
    if abs(prewarm_tps - PREWARM_TPS) > 1e-9:
        raise ValueError(
            f"{where}: runtime.prewarm_tps must be {PREWARM_TPS} "
            f"(Task 019 v2.1 pin); got {prewarm_tps}"
        )
    peak_tps = float(
        _require(runtime_raw, "peak_ramp_tps", f"{where}.runtime")
    )
    if abs(peak_tps - PEAK_RAMP_TPS) > 1e-9:
        raise ValueError(
            f"{where}: runtime.peak_ramp_tps must be {PEAK_RAMP_TPS} "
            f"(Task 019 v2.1 blocker #4 retune from 0.35 to recover "
            f"explicit margin under the 0.85 × quota = 51,000 TPM "
            f"smallest-cell threshold); got {peak_tps}"
        )
    ramp_dur = int(
        _require(runtime_raw, "ramp_duration_seconds", f"{where}.runtime")
    )
    if ramp_dur <= 0:
        raise ValueError(
            f"{where}: runtime.ramp_duration_seconds must be > 0; got "
            f"{ramp_dur}"
        )
    cool = int(
        _require(runtime_raw, "cool_down_seconds", f"{where}.runtime")
    )
    if cool < 0:
        raise ValueError(
            f"{where}: runtime.cool_down_seconds must be >= 0; got {cool}"
        )
    smoke_ramp = int(
        runtime_raw.get("smoke_ramp_duration_seconds", 120)
    )
    if smoke_ramp <= 0:
        raise ValueError(
            f"{where}: runtime.smoke_ramp_duration_seconds must be > 0"
        )
    smoke_cool = int(runtime_raw.get("smoke_cool_down_seconds", 30))
    if smoke_cool < 0:
        raise ValueError(
            f"{where}: runtime.smoke_cool_down_seconds must be >= 0"
        )
    # v2.3 NEW — optional Phase B concurrency override (defaults to the
    # pinned 512 when absent so v2.2.1-shaped YAMLs still load).
    concurrency_phase_b = int(
        runtime_raw.get("concurrency_phase_b", CONCURRENCY_PHASE_B_PINNED)
    )
    if concurrency_phase_b <= 0:
        raise ValueError(
            f"{where}: runtime.concurrency_phase_b must be > 0; got "
            f"{concurrency_phase_b}"
        )
    # v2.4 NEW — optional empirical-promotion block. Every field is a §10
    # PIN; the loader enforces equality against the module-level
    # constants when the YAML CARRIES a value. v2.3-shaped YAMLs missing
    # the block load with defaults.
    ep_raw = runtime_raw.get("empirical_promotion")
    if ep_raw is not None and not isinstance(ep_raw, dict):
        raise ValueError(
            f"{where}: runtime.empirical_promotion must be a mapping"
        )
    ep_raw = ep_raw or {}
    ep_pins: tuple[tuple[str, object], ...] = (
        (
            "cache_hit_floor_smallest_control",
            EMPIRICAL_PROMOTION_CACHE_HIT_FLOOR_SMALLEST_CONTROL,
        ),
        (
            "cache_hit_floor_largest",
            EMPIRICAL_PROMOTION_CACHE_HIT_FLOOR_LARGEST,
        ),
        (
            "calibration_max_age_hours",
            EMPIRICAL_PROMOTION_CALIBRATION_MAX_AGE_HOURS,
        ),
        (
            "minimum_records_at_selected_tps",
            EMPIRICAL_PROMOTION_MINIMUM_RECORDS_AT_SELECTED_TPS,
        ),
        (
            "mini_probe_max_usd",
            EMPIRICAL_PROMOTION_MINI_PROBE_MAX_USD,
        ),
        (
            "mini_probe_max_attempts_per_run",
            EMPIRICAL_PROMOTION_MINI_PROBE_MAX_ATTEMPTS_PER_RUN,
        ),
    )
    for key, pinned in ep_pins:
        if key in ep_raw and ep_raw[key] != pinned:
            raise ValueError(
                f"{where}: runtime.empirical_promotion.{key} is a v2.4 "
                f"§10 PIN; carried value {ep_raw[key]!r} must equal "
                f"{pinned!r}"
            )
    mini_enabled_yaml = ep_raw.get(
        "mini_probe_enabled", EMPIRICAL_PROMOTION_MINI_PROBE_ENABLED_DEFAULT
    )
    if not isinstance(mini_enabled_yaml, bool):
        raise ValueError(
            f"{where}: runtime.empirical_promotion.mini_probe_enabled must "
            f"be a boolean; got {mini_enabled_yaml!r}"
        )
    if mini_enabled_yaml:
        # §7 — require the auditor-approved comment immediately above
        # the key in the raw YAML text.
        raw_text = p.read_text(encoding="utf-8")
        enabled, has_comment = (
            yaml_mini_probe_enabled_with_auditor_comment(raw_text)
        )
        if not (enabled and has_comment):
            raise LinkageValidationError(
                f"{where}: runtime.empirical_promotion.mini_probe_enabled "
                f"is true, but no auditor-approved comment of shape "
                f"'# auditor-approved-YYYY-MM-DD: <handle>' was found "
                f"in the lines immediately above the key — refer to "
                f"v2.4 §7",
                reason=(
                    MINI_PROBE_YAML_ENABLED_WITHOUT_AUDITOR_APPROVED_COMMENT
                ),
            )
    empirical_promotion = _EmpiricalPromotionBlock(
        cache_hit_floor_smallest_control=(
            EMPIRICAL_PROMOTION_CACHE_HIT_FLOOR_SMALLEST_CONTROL
        ),
        cache_hit_floor_largest=(
            EMPIRICAL_PROMOTION_CACHE_HIT_FLOOR_LARGEST
        ),
        calibration_max_age_hours=(
            EMPIRICAL_PROMOTION_CALIBRATION_MAX_AGE_HOURS
        ),
        minimum_records_at_selected_tps=(
            EMPIRICAL_PROMOTION_MINIMUM_RECORDS_AT_SELECTED_TPS
        ),
        mini_probe_enabled=mini_enabled_yaml,
        mini_probe_max_usd=EMPIRICAL_PROMOTION_MINI_PROBE_MAX_USD,
        mini_probe_max_attempts_per_run=(
            EMPIRICAL_PROMOTION_MINI_PROBE_MAX_ATTEMPTS_PER_RUN
        ),
    )
    runtime = _RuntimeBlock(
        concurrency=concurrency,
        dispatcher=dispatcher,
        prewarm_calls_per_cell=prewarm_calls,
        prewarm_tps=prewarm_tps,
        ramp_duration_seconds=ramp_dur,
        peak_ramp_tps=peak_tps,
        cool_down_seconds=cool,
        smoke_ramp_duration_seconds=smoke_ramp,
        smoke_cool_down_seconds=smoke_cool,
        concurrency_phase_b=concurrency_phase_b,
        empirical_promotion=empirical_promotion,
        adaptive_calibration=_parse_adaptive_calibration_block(raw),
    )

    # ---- sweep block ----
    sweep_raw = _require(raw, "sweep", where)
    if not isinstance(sweep_raw, dict):
        raise ValueError(f"{where}: sweep must be a mapping")
    mo_raw = _require(sweep_raw, "max_output_tokens", f"{where}.sweep")
    if not isinstance(mo_raw, list) or not mo_raw:
        raise ValueError(
            f"{where}: sweep.max_output_tokens must be a non-empty list of "
            f"ints"
        )
    sweep_vals: list[int] = []
    seen: set[int] = set()
    for v in mo_raw:
        iv = int(v)
        if iv <= 0 or iv > 1_000_000:
            raise ValueError(
                f"{where}: sweep.max_output_tokens entries must be > 0 and "
                f"<= 1_000_000; got {v!r}"
            )
        if iv in seen:
            raise ValueError(
                f"{where}: sweep.max_output_tokens entries must be unique "
                f"(duplicate prompt_cache_key cells are not permitted under "
                f"Task 019 v2.1 — the per-cell unique namespace key includes "
                f"the cell's max_output_tokens value); duplicate={iv}"
            )
        seen.add(iv)
        sweep_vals.append(iv)
    # Verify it matches the pinned canonical sweep at v2.1.
    if tuple(sweep_vals) != MAX_OUTPUT_TOKENS_SWEEP:
        raise ValueError(
            f"{where}: sweep.max_output_tokens must be exactly "
            f"{list(MAX_OUTPUT_TOKENS_SWEEP)} (Task 019 v2.1 pinned 7-cell "
            f"log2 sweep); got {sweep_vals}"
        )
    sweep = _SweepBlock(max_output_tokens=sweep_vals)

    # ---- budget block ----
    budget_raw = _require(raw, "budget", where)
    if not isinstance(budget_raw, dict):
        raise ValueError(f"{where}: budget must be a mapping")
    ev_est = float(
        _require(budget_raw, "evidence_estimated_usd", f"{where}.budget")
    )
    ev_hard = float(
        _require(budget_raw, "evidence_hard_ceiling_usd", f"{where}.budget")
    )
    sm_est = float(
        _require(budget_raw, "smoke_estimated_usd", f"{where}.budget")
    )
    sm_hard = float(
        _require(budget_raw, "smoke_hard_ceiling_usd", f"{where}.budget")
    )
    confirmed = bool(budget_raw.get("confirmed", False))
    if ev_hard <= 0 or sm_hard <= 0:
        raise ValueError(
            f"{where}: budget hard ceilings must be > 0"
        )
    # v2.2.1 NEW budget fields (optional; default to v2.2.1 constants when
    # absent so v2.1-shaped YAMLs still load).
    calib_est = float(budget_raw.get("calibration_estimated_usd", 0.0))
    calib_hard = float(
        budget_raw.get(
            "calibration_hard_ceiling_usd", CALIBRATION_TOTAL_MAX_USD
        )
    )
    calib_probe_hard = float(
        budget_raw.get(
            "calibration_probe_hard_ceiling_usd", CALIBRATION_PROBE_MAX_USD
        )
    )
    total_task_hard = float(
        budget_raw.get(
            "total_task_hard_ceiling_usd", TASK_TOTAL_HARD_CEILING_USD
        )
    )
    contingency_hard = float(
        budget_raw.get(
            "contingency_hard_ceiling_usd", CONTINGENCY_HARD_CEILING_USD
        )
    )
    if (
        calib_hard <= 0
        or calib_probe_hard <= 0
        or total_task_hard <= 0
        or contingency_hard <= 0
    ):
        raise ValueError(
            f"{where}: v2.2.1 budget hard ceilings must be > 0"
        )
    budget = _BudgetBlock(
        evidence_estimated_usd=ev_est,
        evidence_hard_ceiling_usd=ev_hard,
        smoke_estimated_usd=sm_est,
        smoke_hard_ceiling_usd=sm_hard,
        confirmed=confirmed,
        calibration_estimated_usd=calib_est,
        calibration_hard_ceiling_usd=calib_hard,
        calibration_probe_hard_ceiling_usd=calib_probe_hard,
        total_task_hard_ceiling_usd=total_task_hard,
        contingency_hard_ceiling_usd=contingency_hard,
    )

    # ---- calibration block (v2.2.1 NEW; OPTIONAL for backwards compat) ----
    calibration_block: _CalibrationBlock | None = None
    calib_raw = raw.get("calibration")
    if calib_raw is not None:
        if not isinstance(calib_raw, dict):
            raise ValueError(f"{where}: calibration must be a mapping")
        cwhere = f"{where}.calibration"
        grid_raw = _require(calib_raw, "candidate_tps_grid", cwhere)
        if not isinstance(grid_raw, list) or not grid_raw:
            raise ValueError(
                f"{cwhere}.candidate_tps_grid must be a non-empty list"
            )
        try:
            grid_vals = tuple(float(v) for v in grid_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{cwhere}.candidate_tps_grid: every member must be a "
                f"number; got {grid_raw!r}"
            ) from exc
        # v2.2.1: validate ad-hoc rejection + sorted-ascending.
        for v in grid_vals:
            if v not in CALIBRATION_CANDIDATE_TPS_GRID:
                raise LinkageValidationError(
                    f"{cwhere}.candidate_tps_grid contains ad hoc value "
                    f"{v!r}; allowed set is "
                    f"{list(CALIBRATION_CANDIDATE_TPS_GRID)} (Task 019 "
                    f"v2.2.1 pinned)",
                    reason="candidate_tps_grid_contains_ad_hoc_value",
                )
        if list(grid_vals) != sorted(grid_vals):
            raise LinkageValidationError(
                f"{cwhere}.candidate_tps_grid must be sorted ascending; "
                f"got {list(grid_vals)}",
                reason="candidate_tps_grid_not_sorted_ascending",
            )
        # v2.2.1 candidate-grid sanity check vs deployment quota.
        # NOTE: at this point in the loader the local `metadata` block has
        # not yet been parsed — reach into `raw` directly so we still
        # validate against the configured deployment_tpm_quota.
        _meta_raw = raw.get("metadata")
        _dep_quota_for_sanity = (
            int(_meta_raw["deployment_tpm_quota"])
            if isinstance(_meta_raw, dict)
            and "deployment_tpm_quota" in _meta_raw
            else DEPLOYMENT_TPM_QUOTA_DEFAULT
        )
        evaluate_candidate_grid_sanity(
            candidate_tps_grid=grid_vals,
            smallest_cell_max_output_tokens=int(
                calib_raw.get(
                    "smallest_cell_max_output_tokens",
                    CALIBRATION_SMALLEST_CELL_MO,
                )
            ),
            largest_cell_max_output_tokens=int(
                calib_raw.get(
                    "largest_cell_max_output_tokens",
                    CALIBRATION_LARGEST_CELL_MO,
                )
            ),
            base_prompt_tokens=BASE_PROMPT_TOKENS_FOR_GATE,
            deployment_tpm_quota=_dep_quota_for_sanity,
        )
        # v2.3 Microfix C — Phase B grid completeness contract. The
        # Phase B grid is REQUIRED for v2.3 calibration runs; the
        # auditor microfix 2026-05-30 (finding #3) RETIRES the prior
        # "default to pinned" fallback that silently accepted v2.2.1-
        # shaped YAMLs missing the Phase B block. Missing key now aborts
        # YAML-load with reason `candidate_tps_grid_phase_b_member_missing`
        # (the same reason a partial subset would trigger — a missing
        # key is the maximal case of "all six members missing"), exit
        # code 9. Changing or shortening the Phase B grid requires a
        # NEW SPEC REVISION (a re-audited v2.4 banner item), NOT a
        # runtime YAML omission.
        if "candidate_tps_grid_phase_b" not in calib_raw:
            raise LinkageValidationError(
                f"{cwhere}.candidate_tps_grid_phase_b is REQUIRED in v2.3; "
                f"the v2.2.1 default-to-pinned fallback has been retired "
                f"by the auditor microfix 2026-05-30. The YAML MUST "
                f"specify the spec-pinned six-member grid "
                f"{list(CALIBRATION_CANDIDATE_TPS_GRID_PHASE_B)} in "
                f"EXACT ascending order. Missing members: "
                f"{list(CALIBRATION_CANDIDATE_TPS_GRID_PHASE_B)}",
                reason="candidate_tps_grid_phase_b_member_missing",
            )
        grid_phase_b_raw = calib_raw["candidate_tps_grid_phase_b"]
        if not isinstance(grid_phase_b_raw, list):
            raise LinkageValidationError(
                f"{cwhere}.candidate_tps_grid_phase_b must be a list; "
                f"got {type(grid_phase_b_raw).__name__}",
                reason="candidate_tps_grid_phase_b_contains_ad_hoc_value",
            )
        try:
            grid_phase_b_vals = tuple(float(v) for v in grid_phase_b_raw)
        except (TypeError, ValueError) as exc:
            raise LinkageValidationError(
                f"{cwhere}.candidate_tps_grid_phase_b: every member "
                f"must be a number; got {grid_phase_b_raw!r}",
                reason="candidate_tps_grid_phase_b_contains_ad_hoc_value",
            ) from exc
        evaluate_candidate_grid_phase_b_exact(
            candidate_tps_grid_phase_b=grid_phase_b_vals,
        )
        calibration_block = _CalibrationBlock(
            candidate_tps_grid=grid_vals,
            prewarm_calls=int(
                calib_raw.get("prewarm_calls", CALIBRATION_PREWARM_CALLS)
            ),
            prewarm_tps=float(
                calib_raw.get("prewarm_tps", CALIBRATION_PREWARM_TPS)
            ),
            probe_duration_s=int(
                calib_raw.get("probe_duration_s", CALIBRATION_PROBE_DURATION_S)
            ),
            probe_max_calls=int(
                calib_raw.get("probe_max_calls", CALIBRATION_PROBE_MAX_CALLS)
            ),
            probe_max_usd=float(
                calib_raw.get("probe_max_usd", CALIBRATION_PROBE_MAX_USD)
            ),
            total_max_usd=float(
                calib_raw.get("total_max_usd", CALIBRATION_TOTAL_MAX_USD)
            ),
            largest_cell_max_output_tokens=int(
                calib_raw.get(
                    "largest_cell_max_output_tokens",
                    CALIBRATION_LARGEST_CELL_MO,
                )
            ),
            smallest_cell_max_output_tokens=int(
                calib_raw.get(
                    "smallest_cell_max_output_tokens",
                    CALIBRATION_SMALLEST_CELL_MO,
                )
            ),
            early_stop_on_first_429_largest=bool(
                calib_raw.get("early_stop_on_first_429_largest", True)
            ),
            early_stop_on_first_429_smallest=bool(
                calib_raw.get("early_stop_on_first_429_smallest", True)
            ),
            inter_probe_cooldown_s=int(
                calib_raw.get(
                    "inter_probe_cooldown_s",
                    CALIBRATION_INTER_PROBE_COOLDOWN_S,
                )
            ),
            calibration_max_age_hours=int(
                calib_raw.get(
                    "calibration_max_age_hours", CALIBRATION_MAX_AGE_HOURS
                )
            ),
            candidate_tps_grid_phase_b=grid_phase_b_vals,
            probe_max_calls_phase_b=int(
                calib_raw.get(
                    "probe_max_calls_phase_b",
                    CALIBRATION_PROBE_MAX_CALLS_PHASE_B,
                )
            ),
            admitted_pressure_floor_ratio=float(
                calib_raw.get(
                    "admitted_pressure_floor_ratio",
                    ADMITTED_PRESSURE_FLOOR_RATIO,
                )
            ),
            admitted_pressure_window_s=int(
                calib_raw.get(
                    "admitted_pressure_window_s",
                    ADMITTED_PRESSURE_WINDOW_S,
                )
            ),
            bracket_max_depth=int(
                calib_raw.get("bracket_max_depth", BRACKET_MAX_DEPTH)
            ),
        )

    # ---- prompt-identity block ----
    corpus_seed = int(_require(raw, "corpus_seed", where))
    if corpus_seed != EXPECTED_CORPUS_SEED:
        raise ValueError(
            f"{where}: corpus_seed must be {EXPECTED_CORPUS_SEED} "
            f"(Task 019 v2.1 pin); got {corpus_seed}"
        )
    target_tokens = int(_require(raw, "target_system_prompt_tokens", where))
    if target_tokens != EXPECTED_TARGET_SYSTEM_PROMPT_TOKENS:
        raise ValueError(
            f"{where}: target_system_prompt_tokens must be "
            f"{EXPECTED_TARGET_SYSTEM_PROMPT_TOKENS} (Task 019 v2.1 pin); "
            f"got {target_tokens}"
        )
    source_corpus_path = str(_require(raw, "source_corpus_path", where))
    if source_corpus_path != EXPECTED_SOURCE_CORPUS_PATH:
        raise ValueError(
            f"{where}: source_corpus_path must be "
            f"{EXPECTED_SOURCE_CORPUS_PATH!r} (Task 019 v2.1 contract: "
            f"READ-ONLY reuse of the Task 012 source corpus; NO Task-019-"
            f"specific corpus file is written); got {source_corpus_path!r}"
        )
    source_corpus_sha = str(_require(raw, "source_corpus_sha256", where))
    if source_corpus_sha != EXPECTED_SOURCE_CORPUS_SHA256:
        raise ValueError(
            f"{where}: source_corpus_sha256 must be "
            f"{EXPECTED_SOURCE_CORPUS_SHA256!r} (Task 019 v2.1 pin); got "
            f"{source_corpus_sha!r}"
        )
    expected_assembled_sha = str(
        _require(raw, "expected_assembled_system_prompt_sha256", where)
    )
    if expected_assembled_sha != EXPECTED_ASSEMBLED_SYSTEM_PROMPT_SHA256:
        raise ValueError(
            f"{where}: expected_assembled_system_prompt_sha256 must be "
            f"{EXPECTED_ASSEMBLED_SYSTEM_PROMPT_SHA256!r} (Task 019 v2.1 "
            f"pin); got {expected_assembled_sha!r}"
        )
    user_prompts_source_path = str(
        _require(raw, "user_prompts_source_path", where)
    )
    if user_prompts_source_path != EXPECTED_USER_PROMPTS_SOURCE_PATH:
        raise ValueError(
            f"{where}: user_prompts_source_path must be "
            f"{EXPECTED_USER_PROMPTS_SOURCE_PATH!r}; got "
            f"{user_prompts_source_path!r}"
        )
    user_prompts_source_sha = str(
        _require(raw, "user_prompts_source_sha256", where)
    )
    if user_prompts_source_sha != EXPECTED_USER_PROMPTS_SOURCE_SHA256:
        raise ValueError(
            f"{where}: user_prompts_source_sha256 must be "
            f"{EXPECTED_USER_PROMPTS_SOURCE_SHA256!r}; got "
            f"{user_prompts_source_sha!r}"
        )
    idx_raw = _require(raw, "user_prompts_index_set", where)
    if not isinstance(idx_raw, list):
        raise ValueError(
            f"{where}: user_prompts_index_set must be a list"
        )
    idx_set = [int(v) for v in idx_raw]
    if tuple(idx_set) != USER_PROMPTS_INDEX_SET:
        raise ValueError(
            f"{where}: user_prompts_index_set must be "
            f"{list(USER_PROMPTS_INDEX_SET)} (Task 019 v2.1 pin); got "
            f"{idx_set}"
        )

    pricing_snap = str(_require(raw, "pricing_snapshot_path", where))

    # ---- metadata block ----
    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError(f"{where}: metadata must be a mapping")
    if metadata.get("simulation", None) is not False:
        raise ValueError(
            f"{where}: metadata.simulation must be false (Task 019 v2.1 "
            f"is a live-Azure measurement, not a simulation)"
        )
    if metadata.get("ptu_evidence", None) is not False:
        raise ValueError(
            f"{where}: metadata.ptu_evidence must be false (Task 019 v2.1 "
            f"is PAYG GlobalStandard throttled-quota / Azure admission-"
            f"reservation PROXY for Hypothesis I — NOT direct PTU evidence)"
        )
    if "deployment_tpm_quota" not in metadata:
        raise ValueError(
            f"{where}: metadata.deployment_tpm_quota is required "
            f"(Task 019 v2.1 — denominator of the TPM feasibility "
            f"preflight gate); expected {DEPLOYMENT_TPM_QUOTA_DEFAULT}"
        )
    dep_quota = int(metadata["deployment_tpm_quota"])
    if dep_quota != DEPLOYMENT_TPM_QUOTA_DEFAULT:
        raise ValueError(
            f"{where}: metadata.deployment_tpm_quota must be "
            f"{DEPLOYMENT_TPM_QUOTA_DEFAULT} (Task 019 v2.1 pin — the "
            f"throttled ptu-deploy-throttled deployment is provisioned at "
            f"60K TPM / 600 RPM PAYG GlobalStandard); got {dep_quota}"
        )

    # ---- Task 019 v2.5 adaptive_calibration preflight (§0.9 + §3.2) ----
    # When the optional `runtime.adaptive_calibration.enabled` is true,
    # require `prior_calibrations_disclosure_path` AND the auditor-
    # approval comment matching the §3.2 regex. The validator returns
    # the sub-block unchanged when disabled (default).
    #
    # Task 019 v2.5 fix-loop #1 (first-reviewer BLOCK + MAJOR safety):
    # (1) AdaptiveCalibrationYAMLPreflightError MUST be caught and
    #     re-raised as LinkageValidationError so main()'s existing
    #     EXIT_LINKAGE_FAIL branch produces a deterministic operator-
    #     facing failure (token: ``LINKAGE_VALIDATION_FAILED reason=...``)
    #     instead of a Python traceback when an operator flips
    #     ``runtime.adaptive_calibration.enabled: true`` with an invalid
    #     ``prior_calibrations_disclosure_path`` or auditor comment.
    # (2) ImportError on the v2.5 helper MUST fail closed (same exit 9
    #     path with a distinct reason) rather than silently degrading
    #     the preflight to a no-op. The helper lives in the same repo
    #     and has no optional dependency; the ImportError branch is a
    #     defensive guard against accidental refactor breakage, not an
    #     advertised disable-switch.
    try:
        from scripts.task019_v25_adaptive import (
            AdaptiveCalibrationYAMLPreflightError as _V25PreflightError,
            validate_adaptive_calibration_yaml_block as _v25_validate,
        )
    except ImportError as _v25_import_exc:
        raise LinkageValidationError(
            f"v2.5 adaptive_calibration helper failed to import: "
            f"{_v25_import_exc}",
            reason="adaptive_calibration_helper_import_failed",
        ) from _v25_import_exc
    try:
        _v25_validate(raw, repo_root=p.resolve().parents[1])
    except _V25PreflightError as _v25_preflight_exc:
        raise LinkageValidationError(
            str(_v25_preflight_exc),
            reason=_v25_preflight_exc.reason,
        ) from _v25_preflight_exc

    return ExperimentConfig(
        path=p,
        experiment_id=exp_id,
        description=description,
        parent_experiment=parent,
        benchmark=benchmark,
        deployment=deployment,
        request_template=request_template,
        client=client,
        runtime=runtime,
        sweep=sweep,
        budget=budget,
        corpus_seed=corpus_seed,
        target_system_prompt_tokens=target_tokens,
        source_corpus_path=source_corpus_path,
        source_corpus_sha256=source_corpus_sha,
        expected_assembled_system_prompt_sha256=expected_assembled_sha,
        user_prompts_source_path=user_prompts_source_path,
        user_prompts_source_sha256=user_prompts_source_sha,
        user_prompts_index_set=idx_set,
        pricing_snapshot_path=pricing_snap,
        metadata=dict(metadata),
        deployment_tpm_quota=dep_quota,
        calibration=calibration_block,
    )



# ----------------------------------------------------------------------------
# Env / git / time / hash helpers
# ----------------------------------------------------------------------------


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso8601_z(dt: datetime.datetime) -> str:
    """Format ``dt`` (assumed UTC) as ISO-8601 with trailing ``Z``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    s = dt.astimezone(datetime.timezone.utc).isoformat()
    if s.endswith("+00:00"):
        s = s[:-6] + "Z"
    elif not s.endswith("Z"):
        s = s + "Z"
    return s


def _today_iso(now: datetime.datetime | None = None) -> str:
    n = now or _utc_now()
    return n.strftime("%Y-%m-%d")


def _timestamp_label(now: datetime.datetime | None = None) -> str:
    """File-safe UTC timestamp label, e.g. ``20260529T140530Z``."""
    n = now or _utc_now()
    return n.strftime("%Y%m%dT%H%M%SZ")


def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------------
# v2.2.1 — Durable linkage helpers (calibration ↔ smoke ↔ evidence)
# ----------------------------------------------------------------------------


def write_smoke_summary_sidecar_sha256(summary_path: pathlib.Path) -> pathlib.Path:
    """v2.2.1 — write the smoke summary's own sha256 to a sibling
    ``<summary>.sha256`` sidecar (NEVER inside the summary itself).

    Returns the sidecar path.

    The sidecar contains exactly the hex sha256 string (no trailing
    newline beyond what file open with text mode supplies), so its
    content can be diffed directly against ``_sha256_file(summary_path)``.
    """
    sha = _sha256_file(summary_path)
    sidecar = summary_path.with_suffix(summary_path.suffix + ".sha256")
    sidecar.write_text(sha + "\n", encoding="utf-8")
    return sidecar


def _parse_iso8601_z(s: str) -> datetime.datetime:
    """Parse an ISO-8601 ``Z`` timestamp into a tz-aware UTC datetime."""
    if not isinstance(s, str):
        raise ValueError(f"expected ISO-8601 string; got {type(s).__name__}")
    cleaned = s.rstrip("Z")
    if cleaned == s:
        # Allow explicit +00:00 form too.
        try:
            dt = datetime.datetime.fromisoformat(s)
        except ValueError as exc:
            raise ValueError(f"unparseable ISO-8601 {s!r}: {exc}") from exc
    else:
        try:
            dt = datetime.datetime.fromisoformat(cleaned + "+00:00")
        except ValueError as exc:
            raise ValueError(f"unparseable ISO-8601 {s!r}: {exc}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def validate_calibration_result(
    path: pathlib.Path | str,
    *,
    expected_source_corpus_sha256: str,
    expected_assembled_prompt_sha256: str,
    expected_user_prompts_source_sha256: str,
    expected_user_prompts_index_set: tuple[int, ...] = USER_PROMPTS_INDEX_SET,
    now: datetime.datetime | None = None,
    max_age_hours: int = CALIBRATION_MAX_AGE_HOURS,
    allow_stale: bool = False,
) -> dict[str, Any]:
    """v2.2.1 — validate a calibration result file referenced by
    smoke/evidence via ``--calibration-result``.

    Returns the parsed dict on success. Raises ``LinkageValidationError``
    with the spec's ``reason`` on every failure path.

    Validation steps (in order — short-circuit on first failure):

    1. File exists. Otherwise ``calibration_result_missing``.
    2. Schema is well-formed JSON with required top-level keys.
       Otherwise ``calibration_result_invalid_schema``.
    3. ``outcome == "selected"`` (smoke/evidence refuse to start against
       any other outcome). Otherwise ``calibration_did_not_select_peak_tps``.
    4. ``completed_at_iso`` is within ``max_age_hours`` of ``now``.
       Otherwise ``calibration_stale_must_re_run`` — UNLESS
       ``allow_stale=True`` (v2.4 §6/§7: when the v2.4 empirical-
       promotion gate will run downstream, stale calibration is no
       longer a pre-gate hard-exit; the gate's invariant 12 routes
       stale calibrations to either ``cold_cache_strict`` fallback or
       the opt-in mini-probe revalidation path).
    5. Prompt-identity SHAs match the smoke/evidence run's pins.
       Otherwise ``calibration_prompt_identity_mismatch``.

    Args:
        allow_stale: When ``True``, the freshness window check at step
            4 is SKIPPED — the stale calibration's parsed dict is
            returned so the v2.4 empirical-promotion gate can decide
            cold-cache-vs-mini-probe per §3.1 invariant 12. Default
            ``False`` preserves the v2.2.1/v2.3 legacy strict-freshness
            behaviour for direct callers (tests, ad-hoc tools) that
            never reach the v2.4 gate. ``run_measurement`` passes
            ``True`` so the real smoke/evidence CLI path no longer
            short-circuits stale calibrations before the v2.4 gate.
    """
    p = pathlib.Path(path)
    if not p.is_file():
        raise LinkageValidationError(
            f"calibration result file does not exist: {p}",
            reason="calibration_result_missing",
        )
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise LinkageValidationError(
            f"calibration result file unreadable / invalid JSON {p}: {exc}",
            reason="calibration_result_invalid_schema",
        ) from exc
    if not isinstance(data, dict):
        raise LinkageValidationError(
            f"calibration result must be a JSON object; got "
            f"{type(data).__name__}",
            reason="calibration_result_invalid_schema",
        )
    required_top = {
        "schema_version",
        "outcome",
        "completed_at_iso",
        "prompt_identity",
    }
    missing = required_top - set(data)
    if missing:
        raise LinkageValidationError(
            f"calibration result missing required keys "
            f"{sorted(missing)}",
            reason="calibration_result_invalid_schema",
        )
    if data["schema_version"] not in (
        "task019.v2.2.1.calibration_result",
        "task019.v2.3.calibration_result",
    ):
        raise LinkageValidationError(
            f"calibration result schema_version must be one of "
            f"'task019.v2.2.1.calibration_result' (v2.2.1) or "
            f"'task019.v2.3.calibration_result' (v2.3); got "
            f"{data['schema_version']!r}",
            reason="calibration_result_invalid_schema",
        )
    outcome = data["outcome"]
    # v2.3 — accept either the 9-member terminal enum OR the retired
    # internal Phase-A→B transition signal (v2.2.1
    # `no_largest_cell_429_at_any_candidate_tps`). The retired signal is
    # still a legal value in archived v2.2.1 calibration_result files;
    # downstream gates handle the not-selected case via the next check.
    legal_outcomes = set(CALIBRATION_OUTCOME_ENUM) | {
        CALIBRATION_OUTCOME_NO_LARGEST_429,
    }
    if outcome not in legal_outcomes:
        raise LinkageValidationError(
            f"calibration result outcome {outcome!r} not in v2.3 "
            f"9-member enum {sorted(CALIBRATION_OUTCOME_ENUM)} "
            f"(or v2.2.1 retired internal signal "
            f"{CALIBRATION_OUTCOME_NO_LARGEST_429!r})",
            reason="calibration_result_invalid_schema",
        )
    if outcome != CALIBRATION_OUTCOME_SELECTED:
        raise LinkageValidationError(
            f"calibration result outcome was {outcome!r}; smoke/evidence "
            f"refuse to start against any outcome other than 'selected'",
            reason="calibration_did_not_select_peak_tps",
        )
    # v2.3 — selected_peak_tps must be present AND consistent with
    # `selected_via` / `selected_at_phase` / `selected_at_bracket_depth`
    # / `selected_bracket_root_phase`.
    #
    # Three legal selection paths:
    #   (a) Phase A grid (v2.2.1 back-compat — `selected_via` may be
    #       absent OR equal to "grid_ascending" with `selected_at_phase`
    #       in {None, "A"}, with BOTH `selected_bracket_root_phase` and
    #       `selected_at_bracket_depth` null): membership in the Phase
    #       A grid.
    #   (b) Phase B grid (v2.3 — `selected_via == "grid_ascending"` AND
    #       `selected_at_phase == "B"`, with BOTH
    #       `selected_bracket_root_phase` and `selected_at_bracket_depth`
    #       null): membership in the Phase B grid.
    #   (c) Bracket search (v2.3 — `selected_via == "bracket_search"`):
    #       any positive float; `selected_at_phase` MUST be the literal
    #       string `"bracket"`; `selected_bracket_root_phase` MUST be
    #       exactly `"A"` or `"B"`; `selected_at_bracket_depth` MUST be
    #       set to an integer in 1..bracket_max_depth.
    #
    # v2.3 microfix 2026-05-30 fix loop #8 (methodology-auditor
    # REQUEST-CHANGES) — cross-field invariants between `selected_via`
    # and the bracket-phase markers (`selected_at_phase=='bracket'`,
    # non-null `selected_bracket_root_phase`, non-null
    # `selected_at_bracket_depth`) are now enforced BEFORE the dispatch
    # branches so a forged result cannot populate bracket lineage on a
    # grid-ascending path and misdrive downstream
    # `phase_b_concurrency_used` (which reads
    # `selected_bracket_root_phase` to recover Phase-A vs Phase-B
    # lineage when `selected_at_phase=='bracket'` hides it).
    sel_tps = data.get("selected_peak_tps")
    sel_via = data.get("selected_via")
    sel_phase = data.get("selected_at_phase")
    sel_bracket_depth = data.get("selected_at_bracket_depth")
    sel_bracket_root_phase = data.get("selected_bracket_root_phase")
    if sel_tps is None:
        raise LinkageValidationError(
            "calibration result selected_peak_tps must be a positive "
            "float on outcome='selected'; got None",
            reason="calibration_result_invalid_schema",
        )
    try:
        sel_tps_f = float(sel_tps)
    except (TypeError, ValueError) as exc:
        raise LinkageValidationError(
            f"calibration result selected_peak_tps {sel_tps!r} is not "
            f"a number",
            reason="calibration_result_invalid_schema",
        ) from exc
    if sel_via not in (
        None,
        "grid_ascending",
        "bracket_search",
        # Task 019 v2.5 (spec §6 item 1): v2.4 preflight admits v2.5
        # adaptive selection provenance unchanged. C1 / C2 selection
        # provenance is now valid; only OTHER values still fail.
        "adaptive_strict_separating_tps",
        "adaptive_onset_separation_replicate_confirmed",
    ):
        raise LinkageValidationError(
            f"calibration result selected_via {sel_via!r} not in "
            f"{{None, 'grid_ascending', 'bracket_search', "
            f"'adaptive_strict_separating_tps', "
            f"'adaptive_onset_separation_replicate_confirmed'}}",
            reason="calibration_result_invalid_schema",
        )
    if sel_phase not in (None, "A", "B", "bracket", "adaptive"):
        # Task 019 v2.5 (spec §6 item 1): selected_at_phase admits
        # "adaptive" provenance from the §3.2 0.5.C runner.
        raise LinkageValidationError(
            f"calibration result selected_at_phase {sel_phase!r} not in "
            f"{{None, 'A', 'B', 'bracket', 'adaptive'}}",
            reason="calibration_result_invalid_schema",
        )
    # v2.3 microfix 2026-05-30 fix loop #9 (methodology-auditor
    # REQUEST-CHANGES): cross-field invariant between ``selected_via``
    # and ``selected_at_phase`` — ``selected_at_phase == "B"`` is legal
    # ONLY when ``selected_via == "grid_ascending"``. Equivalently, the
    # v2.2.1 unset-via back-compat path (``selected_via is None``) is
    # reserved for the Phase A grid only: ``selected_at_phase`` must be
    # ``None`` or ``"A"`` when ``selected_via`` is unset.
    #
    # Pre-fix-loop-#9 the validator accepted a forged Phase B tuple of
    # the shape ``(selected_via=None, selected_at_phase="B",
    # selected_peak_tps in pinned Phase B grid, bracket markers null)``
    # by falling through to the ``elif sel_phase == "B":`` Phase B grid
    # dispatch, which only checked pinned-grid membership and never
    # required the runtime-emitted ``selected_via == "grid_ascending"``
    # marker that distinguishes a legitimate Phase B selection from a
    # v2.2.1 result that should have been routed to Phase A. Downstream
    # ``phase_b_concurrency_used`` and audit fields then claimed Phase B
    # lineage on a result that never recorded the v2.3 selection-via
    # marker — silently misdriving the headline measurement.
    #
    # This check fires BEFORE the bracket-marker null check (fix loop
    # #8) and BEFORE the ``bracket_search`` / Phase B / Phase A
    # dispatch, so the forged tuple is rejected at the schema layer
    # with ``calibration_result_invalid_schema``. The ``"bracket_search"``
    # + ``"B"`` combination is also caught here (its existing fix
    # loop #6/#7 message inside the bracket_search branch is now
    # unreachable but kept as a defense-in-depth guard; the regression
    # test for that case asserts only on the reason string and the
    # substrings ``"bracket"`` and ``"selected_at_phase"``, both of
    # which appear in the new diagnostic).
    if sel_phase == "B" and sel_via != "grid_ascending":
        raise LinkageValidationError(
            f"calibration result selected_at_phase='B' is legal only "
            f"when selected_via=='grid_ascending'; got "
            f"selected_via={sel_via!r}. The v2.2.1 unset-via "
            f"back-compat path (selected_via is None) is reserved for "
            f"the Phase A grid only — a Phase B label on an unset-via "
            f"or bracket_search path indicates a forged result "
            f"attempting to claim Phase B lineage without the "
            f"runtime-emitted selected_via=='grid_ascending' marker "
            f"and would misdrive downstream phase_b_concurrency_used",
            reason="calibration_result_invalid_schema",
        )
    # Task 019 v2.5 (spec §6 item 1) cross-field invariant: the
    # adaptive selected_via values are paired EXCLUSIVELY with
    # selected_at_phase == "adaptive", and vice versa. This mirrors
    # the v2.3 bracket/B cross-field checks above and prevents a
    # forged "adaptive" phase label from claiming a non-adaptive
    # selection path (or vice versa).
    _V25_ADAPTIVE_SELECTED_VIA = (
        "adaptive_strict_separating_tps",
        "adaptive_onset_separation_replicate_confirmed",
    )
    if sel_via in _V25_ADAPTIVE_SELECTED_VIA and sel_phase != "adaptive":
        raise LinkageValidationError(
            f"calibration result selected_via={sel_via!r} (v2.5 adaptive) "
            f"requires selected_at_phase=='adaptive'; got "
            f"selected_at_phase={sel_phase!r}",
            reason="calibration_result_invalid_schema",
        )
    if sel_phase == "adaptive" and sel_via not in _V25_ADAPTIVE_SELECTED_VIA:
        raise LinkageValidationError(
            f"calibration result selected_at_phase='adaptive' (v2.5) is "
            f"legal only when selected_via in "
            f"{set(_V25_ADAPTIVE_SELECTED_VIA)}; got "
            f"selected_via={sel_via!r}",
            reason="calibration_result_invalid_schema",
        )
    # v2.3 microfix 2026-05-30 fix loop #8 (methodology-auditor
    # REQUEST-CHANGES): bracket-phase markers are reserved EXCLUSIVELY
    # for ``selected_via == "bracket_search"`` selection paths. Non-
    # bracket selection paths (``selected_via`` is ``None`` or
    # ``"grid_ascending"``) MUST report all three bracket-phase markers
    # (``selected_at_phase=='bracket'``,
    # ``selected_bracket_root_phase``, ``selected_at_bracket_depth``)
    # as null / absent. Without this cross-field check the validator
    # silently accepted four classes of forged result that the auditor
    # flagged:
    #
    #   (i)   ``selected_via=='grid_ascending'`` with
    #         ``selected_at_phase=='bracket'`` and
    #         ``selected_bracket_root_phase=='B'`` plus a
    #         Phase-A-grid TPS — the Phase-A else branch's grid
    #         membership check passed and the bracket lineage fields
    #         were carried through to ``phase_b_concurrency_used``
    #         despite no bracket search having actually run.
    #   (ii)  ``selected_via is None`` with
    #         ``selected_at_phase=='bracket'`` and
    #         ``selected_bracket_root_phase=='B'`` — same shape as
    #         (i) but via the v2.2.1 unset-via back-compat path.
    #   (iii) ``selected_via=='grid_ascending'`` with
    #         ``selected_at_phase=='A'`` (legitimate Phase A grid) but
    #         a non-null ``selected_bracket_root_phase`` — forged
    #         bracket lineage on a grid path.
    #   (iv)  ``selected_via=='grid_ascending'`` with
    #         ``selected_at_phase=='B'`` (legitimate Phase B grid) but
    #         a non-null ``selected_at_bracket_depth`` — forged
    #         bracket depth on a grid path.
    #
    # All four are now rejected with
    # ``calibration_result_invalid_schema``. v2.2.1 back-compat is
    # preserved: ``selected_via is None`` with ``selected_at_phase``
    # in ``{None, "A"}`` and both bracket markers null falls through
    # to the Phase A grid path below. v2.3 Phase B grid is preserved:
    # ``selected_via == "grid_ascending"`` AND
    # ``selected_at_phase == "B"`` with both bracket markers null
    # falls through to the Phase B grid path below.
    if sel_via != "bracket_search":
        if sel_phase == "bracket":
            raise LinkageValidationError(
                f"calibration result selected_at_phase='bracket' is "
                f"legal only when selected_via='bracket_search'; got "
                f"selected_via={sel_via!r}. Non-bracket selection "
                f"paths must report selected_at_phase in "
                f"{{None, 'A', 'B'}} — a 'bracket' phase label on a "
                f"grid-ascending or unset path indicates a forged "
                f"result attempting to claim bracket lineage and "
                f"misdrive downstream phase_b_concurrency_used",
                reason="calibration_result_invalid_schema",
            )
        if sel_bracket_root_phase is not None:
            raise LinkageValidationError(
                f"calibration result selected_bracket_root_phase must "
                f"be null on non-bracket selection paths "
                f"(selected_via={sel_via!r}); got "
                f"{sel_bracket_root_phase!r}. The root_phase marker "
                f"is reserved for bracket-search selections — "
                f"populating it on a grid path indicates a forged "
                f"result attempting to misdrive downstream "
                f"phase_b_concurrency_used Phase-A vs Phase-B "
                f"lineage recovery",
                reason="calibration_result_invalid_schema",
            )
        if sel_bracket_depth is not None:
            raise LinkageValidationError(
                f"calibration result selected_at_bracket_depth must "
                f"be null on non-bracket selection paths "
                f"(selected_via={sel_via!r}); got "
                f"{sel_bracket_depth!r}. The bracket depth marker is "
                f"reserved for bracket-search selections",
                reason="calibration_result_invalid_schema",
            )
    if sel_via == "bracket_search":
        # v2.3 microfix 2026-05-30 fix loop #7 (auditor final-review
        # REQUEST-CHANGES): bracket-search selections MUST carry the
        # bracket-phase markers set by the runtime emit path in
        # ``_run_calibration_async``. Specifically:
        #
        #   * ``selected_at_phase`` MUST be exactly the literal string
        #     ``"bracket"``. The pre-fix-loop-#6 stale variants
        #     ``"A"`` / ``"B"`` (which conflated the bracket with its
        #     parent grid) are NEVER legal on a v2.3 result and must
        #     be rejected as ``calibration_result_invalid_schema``.
        #     ``None`` is likewise rejected because the runtime ALWAYS
        #     pins ``"bracket"`` on a bracket success branch.
        #   * ``selected_bracket_root_phase`` MUST be exactly one of
        #     ``"A"`` or ``"B"`` (the parent grid that rooted the
        #     bracket). Missing / ``None`` / any other value (e.g.
        #     ``"C"``) is rejected. Downstream consumers
        #     (``phase_b_concurrency_used`` computation, audit) rely on
        #     this field to recover the Phase-A vs Phase-B lineage that
        #     ``selected_at_phase='bracket'`` deliberately hides.
        #   * ``selected_at_bracket_depth`` MUST be an int in
        #     ``1..BRACKET_MAX_DEPTH``.
        #   * ``selected_peak_tps`` MUST be a positive float.
        if sel_phase != "bracket":
            raise LinkageValidationError(
                f"calibration bracket-search selected_at_phase must be "
                f"exactly 'bracket'; got {sel_phase!r}. Pre-fix-loop-#6 "
                f"stale variants ('A' / 'B') are rejected on v2.3 "
                f"results — the runtime now always pins "
                f"selected_at_phase='bracket' on the bracket success "
                f"branch and records the parent grid lineage under "
                f"selected_bracket_root_phase",
                reason="calibration_result_invalid_schema",
            )
        if sel_bracket_root_phase not in ("A", "B"):
            raise LinkageValidationError(
                f"calibration bracket-search selected_bracket_root_phase "
                f"must be exactly 'A' or 'B'; got "
                f"{sel_bracket_root_phase!r}. The runtime emits the "
                f"parent grid phase (Phase A or Phase B) that rooted "
                f"the bracket; a missing or invalid root indicates a "
                f"forged / pre-fix-loop-#6 stale result",
                reason="calibration_result_invalid_schema",
            )
        if sel_tps_f <= 0.0:
            raise LinkageValidationError(
                f"calibration bracket-search selected_peak_tps must be "
                f"> 0; got {sel_tps_f}",
                reason="calibration_result_invalid_schema",
            )
        if not isinstance(sel_bracket_depth, int) or not (
            1 <= sel_bracket_depth <= BRACKET_MAX_DEPTH
        ):
            raise LinkageValidationError(
                f"calibration bracket-search selected_at_bracket_depth "
                f"must be int in 1..{BRACKET_MAX_DEPTH}; got "
                f"{sel_bracket_depth!r}",
                reason="calibration_result_invalid_schema",
            )
    elif sel_phase == "adaptive":
        # Task 019 v2.5 (spec §6 items 1, 4, 5): adaptive C1 / C2
        # selections produce ``selected_peak_tps`` from the §3.2
        # 0.5.C runner; the value is NOT constrained to the v2.3
        # pinned Phase A / Phase B grids (the adaptive search emits
        # an arbitrary positive float by construction). The v2.4
        # preflight treats this identically to a bracket-search
        # selection: only the schema invariants are enforced (positive
        # float, null bracket markers — already enforced above), and
        # the §3.1 invariants 3 / 4 (largest-cell 429-positive,
        # smallest-control 429-zero at ``selected_peak_tps``) are
        # then evaluated downstream against the v2.5 calibration
        # result's probe observations.
        if sel_tps_f <= 0.0:
            raise LinkageValidationError(
                f"calibration v2.5 adaptive selected_peak_tps must be "
                f"> 0; got {sel_tps_f}",
                reason="calibration_result_invalid_schema",
            )
    elif sel_phase == "B":
        # v2.3 Phase B grid selection.
        #
        # v2.3 microfix 2026-05-30 (auditor fix loop #2, finding #1):
        # ALWAYS validate ``selected_peak_tps`` against the PINNED
        # module constant ``CALIBRATION_CANDIDATE_TPS_GRID_PHASE_B``.
        # NEVER trust a result-provided ``candidate_tps_grid_phase_b``
        # as authoritative. A forged result with
        # ``candidate_tps_grid_phase_b=[7.0]`` and
        # ``selected_peak_tps=7.0`` MUST be rejected; the previous
        # default-to-pinned-only-when-missing fallback accepted it.
        #
        # Additionally, if the result file DOES carry
        # ``candidate_tps_grid_phase_b``, it MUST equal the pinned grid
        # exactly (same length, same members, same ascending order).
        # Any deviation — including a forged subset, superset, or
        # reordering — is rejected as ``calibration_result_invalid_schema``.
        # An absent key on a Phase B selection is also rejected (no
        # silent default — the calibration runner ALWAYS echoes the
        # pinned grid into v2.3 results).
        pinned_phase_b = list(CALIBRATION_CANDIDATE_TPS_GRID_PHASE_B)
        if "candidate_tps_grid_phase_b" not in data:
            raise LinkageValidationError(
                f"calibration result selected_at_phase='B' but "
                f"candidate_tps_grid_phase_b is missing; v2.3 calibration "
                f"results MUST echo the pinned Phase B grid "
                f"{pinned_phase_b}",
                reason="calibration_result_invalid_schema",
            )
        result_phase_b_raw = data["candidate_tps_grid_phase_b"]
        if not isinstance(result_phase_b_raw, list):
            raise LinkageValidationError(
                f"calibration result candidate_tps_grid_phase_b must be "
                f"a list; got {type(result_phase_b_raw).__name__}",
                reason="calibration_result_invalid_schema",
            )
        try:
            result_phase_b_f = [float(v) for v in result_phase_b_raw]
        except (TypeError, ValueError) as exc:
            raise LinkageValidationError(
                f"calibration result candidate_tps_grid_phase_b contains "
                f"non-numeric member(s): {result_phase_b_raw!r}",
                reason="calibration_result_invalid_schema",
            ) from exc
        if result_phase_b_f != pinned_phase_b:
            raise LinkageValidationError(
                f"calibration result candidate_tps_grid_phase_b "
                f"{result_phase_b_f} does not equal the pinned v2.3 "
                f"Phase B grid {pinned_phase_b}; result-provided grids "
                f"are NEVER authoritative — the pinned grid is the "
                f"single source of truth and any deviation indicates a "
                f"forged or stale result",
                reason="calibration_result_invalid_schema",
            )
        # Membership check uses the PINNED constant directly — never
        # the result-provided list (even after the equality check, we
        # keep the check explicit so future refactors cannot regress).
        if sel_tps_f not in pinned_phase_b:
            raise LinkageValidationError(
                f"calibration result selected_peak_tps {sel_tps_f!r} not "
                f"in pinned v2.3 Phase B grid {pinned_phase_b}",
                reason="calibration_result_invalid_schema",
            )
    else:
        # Phase A (default — back-compat with v2.2.1 results that omit
        # selected_via and selected_at_phase).
        #
        # v2.3 microfix 2026-05-30 fix loop #10 (final-code-reviewer
        # REQUEST-CHANGES): the PINNED module constant
        # ``CALIBRATION_CANDIDATE_TPS_GRID`` is the SINGLE SOURCE OF
        # TRUTH for Phase A membership — a result-provided
        # ``candidate_tps_grid`` is NEVER authoritative. Pre-fix-loop-#10
        # the validator used ``data.get("candidate_tps_grid",
        # list(CALIBRATION_CANDIDATE_TPS_GRID))`` and then membership-
        # checked ``selected_peak_tps`` against THAT list, which silently
        # accepted the forged tuple
        # ``(selected_via=None, selected_at_phase='A',
        # selected_peak_tps=5.0, candidate_tps_grid=[5.0])`` because
        # ``5.0 in [5.0]`` was True. The fix mirrors the Phase B handling
        # above (Phase B microfix fix loop #2): if the result file
        # carries ``candidate_tps_grid`` it MUST equal the pinned grid
        # EXACTLY (same length, same members, same ascending order — any
        # forged subset, superset, reordering, duplicate, or ad-hoc
        # value rejected), and the ``selected_peak_tps`` membership
        # check is ALWAYS performed against the pinned constant
        # directly. An absent key is permitted as v2.2.1 back-compat for
        # archived results that omitted the field; the v2.3 runtime
        # ALWAYS echoes the pinned 7-member grid into emitted results.
        pinned_phase_a = list(CALIBRATION_CANDIDATE_TPS_GRID)
        if "candidate_tps_grid" in data:
            result_phase_a_raw = data["candidate_tps_grid"]
            if not isinstance(result_phase_a_raw, list):
                raise LinkageValidationError(
                    f"calibration result candidate_tps_grid must be "
                    f"a list; got {type(result_phase_a_raw).__name__}",
                    reason="calibration_result_invalid_schema",
                )
            try:
                result_phase_a_f = [float(v) for v in result_phase_a_raw]
            except (TypeError, ValueError) as exc:
                raise LinkageValidationError(
                    f"calibration result candidate_tps_grid contains "
                    f"non-numeric member(s): {result_phase_a_raw!r}",
                    reason="calibration_result_invalid_schema",
                ) from exc
            if result_phase_a_f != pinned_phase_a:
                raise LinkageValidationError(
                    f"calibration result candidate_tps_grid "
                    f"{result_phase_a_f} does not equal the pinned v2.3 "
                    f"Phase A grid {pinned_phase_a}; result-provided "
                    f"grids are NEVER authoritative — the pinned grid "
                    f"is the single source of truth and any deviation "
                    f"(forged subset, superset, reordering, duplicate, "
                    f"or ad-hoc value) indicates a forged or stale "
                    f"result",
                    reason="calibration_result_invalid_schema",
                )
        # Membership check uses the PINNED constant directly — never
        # the result-provided list (even after the equality check, we
        # keep the check explicit so future refactors cannot regress).
        if sel_tps_f not in pinned_phase_a:
            raise LinkageValidationError(
                f"calibration result selected_peak_tps {sel_tps_f!r} not "
                f"in pinned v2.3 Phase A grid {pinned_phase_a}",
                reason="calibration_result_invalid_schema",
            )
    # Freshness window.
    n = now or _utc_now()
    try:
        completed_at = _parse_iso8601_z(data["completed_at_iso"])
    except ValueError as exc:
        raise LinkageValidationError(
            f"calibration result completed_at_iso unparseable: {exc}",
            reason="calibration_result_invalid_schema",
        ) from exc
    age = n - completed_at
    if age > datetime.timedelta(hours=max_age_hours) and not allow_stale:
        raise LinkageValidationError(
            f"calibration result is {age.total_seconds() / 3600:.2f}h "
            f"old (> {max_age_hours}h freshness window); must re-run "
            f"calibration",
            reason="calibration_stale_must_re_run",
        )
    # Prompt-identity.
    pi = data["prompt_identity"]
    if not isinstance(pi, dict):
        raise LinkageValidationError(
            f"calibration result prompt_identity must be a mapping; "
            f"got {type(pi).__name__}",
            reason="calibration_result_invalid_schema",
        )
    expected_pi = {
        "source_corpus_sha256": expected_source_corpus_sha256,
        "assembled_system_prompt_sha256": expected_assembled_prompt_sha256,
        "user_prompts_source_sha256": expected_user_prompts_source_sha256,
        "user_prompts_index_set": list(expected_user_prompts_index_set),
    }
    for k, exp in expected_pi.items():
        got = pi.get(k)
        # Normalize list comparison for user_prompts_index_set.
        if isinstance(exp, list):
            if list(got or []) != exp:
                raise LinkageValidationError(
                    f"calibration prompt_identity.{k} mismatch: "
                    f"calibration={got} vs current={exp}",
                    reason="calibration_prompt_identity_mismatch",
                )
        else:
            if got != exp:
                raise LinkageValidationError(
                    f"calibration prompt_identity.{k} mismatch: "
                    f"calibration={got!r} vs current={exp!r}",
                    reason="calibration_prompt_identity_mismatch",
                )
    return data


def validate_smoke_summary(
    path: pathlib.Path | str,
    *,
    calibration_result_path: pathlib.Path | str,
    calibration_result_sha256: str,
    calibration_run_id_short: str,
    selected_peak_tps: float,
    expected_source_corpus_sha256: str,
    expected_assembled_prompt_sha256: str,
    expected_user_prompts_source_sha256: str,
    now: datetime.datetime | None = None,
    max_age_hours: int = CALIBRATION_MAX_AGE_HOURS,
    expected_selected_at_phase: str | None = None,
) -> dict[str, Any]:
    """v2.2.1 — validate a smoke summary referenced by an evidence run
    via ``--smoke-summary``.

    Cross-checks ten linkage failure modes (raises
    ``LinkageValidationError`` with the spec ``reason``):

    - file missing → ``smoke_summary_missing``
    - sidecar ``.sha256`` mismatch → ``smoke_summary_sha256_mismatch``
    - smoke ``smoke_gate.passed`` is falsy → ``smoke_did_not_pass_gate``
    - smoke's ``selected_peak_tps`` ≠ calibration's →
      ``smoke_selected_peak_tps_mismatches_calibration``
    - smoke's ``calibration_result_path`` /
      ``calibration_result_sha256`` / ``calibration_run_id_short`` mismatch
      → ``smoke_calibration_reference_mismatch``
    - smoke prompt-identity SHAs mismatch → ``smoke_prompt_identity_mismatch``
    - smoke ``completed_at_iso`` older than ``max_age_hours`` →
      ``smoke_stale_must_re_run``

    v2.3 fix loop #5 (auditor BLOCKER 3) — also enforces:

    - smoke ``selected_at_phase`` ≠ calibration's →
      ``smoke_selected_at_phase_mismatches_calibration``
    - any per-cell ``admitted_pressure_passed == false`` (with no 429
      observed on that cell, i.e. the gate is not skipped-by-429) →
      ``smoke_admitted_pressure_failed``

    v2.3 fix loop #6 (auditor BLOCKER 3) — schema-version gated
    per-cell ``admitted_pressure_passed`` PRESENCE check. Fresh v2.3
    summaries (``schema_version == "task019.v2.3.measurement_summary"``)
    MUST carry per-cell ``admitted_pressure_passed`` on every cell
    that observed zero 429s; a missing field on such a cell raises
    ``smoke_admitted_pressure_failed`` (a v2.3 runner emits the field
    unconditionally so a missing field indicates a hand-edited /
    forged / schema-incomplete summary that evidence cannot link
    against). Legacy ``task019.v2.2.1.measurement_summary`` summaries
    continue to skip the field-absent case for back-compat — the
    v2.2.1 runner did not echo the per-cell field.
    """
    p = pathlib.Path(path)
    if not p.is_file():
        raise LinkageValidationError(
            f"smoke summary file does not exist: {p}",
            reason="smoke_summary_missing",
        )
    # Sidecar sha256 check (v2.2.1 contract: smoke summary's own sha256
    # is stored ONLY in the sibling .sha256 sidecar, NEVER inside the
    # summary itself).
    sidecar = p.with_suffix(p.suffix + ".sha256")
    if not sidecar.is_file():
        raise LinkageValidationError(
            f"smoke summary sidecar sha256 file missing: {sidecar}",
            reason="smoke_summary_sha256_mismatch",
        )
    expected_sha = sidecar.read_text(encoding="utf-8").strip()
    actual_sha = _sha256_file(p)
    if expected_sha != actual_sha:
        raise LinkageValidationError(
            f"smoke summary sidecar sha256 mismatch: sidecar="
            f"{expected_sha} vs computed={actual_sha}",
            reason="smoke_summary_sha256_mismatch",
        )
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise LinkageValidationError(
            f"smoke summary unreadable / invalid JSON {p}: {exc}",
            reason="smoke_summary_missing",
        ) from exc
    # Gate passed.
    gate = data.get("smoke_gate") or {}
    if not gate.get("passed", False):
        raise LinkageValidationError(
            f"smoke summary smoke_gate.passed is falsy "
            f"(reason={gate.get('reason')!r}); evidence cannot proceed",
            reason="smoke_did_not_pass_gate",
        )
    # selected_peak_tps match.
    smoke_sel = data.get("selected_peak_tps")
    if smoke_sel != selected_peak_tps:
        raise LinkageValidationError(
            f"smoke summary selected_peak_tps {smoke_sel!r} ≠ calibration "
            f"selected_peak_tps {selected_peak_tps!r}",
            reason="smoke_selected_peak_tps_mismatches_calibration",
        )
    # Calibration reference triple match.
    smoke_calib_path = data.get("calibration_result_path")
    smoke_calib_sha = data.get("calibration_result_sha256")
    smoke_calib_run = data.get("calibration_run_id_short")
    ref_path = str(pathlib.Path(calibration_result_path))
    if (
        smoke_calib_path != ref_path
        or smoke_calib_sha != calibration_result_sha256
        or smoke_calib_run != calibration_run_id_short
    ):
        raise LinkageValidationError(
            f"smoke summary calibration reference mismatch: "
            f"path(smoke={smoke_calib_path!r} vs cli={ref_path!r}), "
            f"sha(smoke={smoke_calib_sha!r} vs computed="
            f"{calibration_result_sha256!r}), "
            f"run(smoke={smoke_calib_run!r} vs calibration="
            f"{calibration_run_id_short!r})",
            reason="smoke_calibration_reference_mismatch",
        )
    # Prompt identity SHAs.
    pi_keys = (
        ("source_corpus_sha256", expected_source_corpus_sha256),
        ("system_prompt_sha256", expected_assembled_prompt_sha256),
        ("user_prompts_source_sha256", expected_user_prompts_source_sha256),
    )
    for k, exp in pi_keys:
        got = data.get(k)
        if got != exp:
            raise LinkageValidationError(
                f"smoke summary {k} mismatch: smoke={got!r} vs current="
                f"{exp!r}",
                reason="smoke_prompt_identity_mismatch",
            )
    # Freshness on smoke completion (run_lock_metadata.acquired_at_iso
    # is the closest universally-available smoke-summary timestamp; fall
    # back to completed_at_iso if present).
    completed_iso = (
        data.get("completed_at_iso")
        or (data.get("run_lock_metadata") or {}).get("acquired_at_iso")
    )
    if completed_iso:
        try:
            completed_at = _parse_iso8601_z(completed_iso)
        except ValueError:
            completed_at = None
        if completed_at is not None:
            n = now or _utc_now()
            age = n - completed_at
            if age > datetime.timedelta(hours=max_age_hours):
                raise LinkageValidationError(
                    f"smoke summary is {age.total_seconds() / 3600:.2f}h "
                    f"old (> {max_age_hours}h freshness window); must "
                    f"re-run smoke",
                    reason="smoke_stale_must_re_run",
                )
    # v2.3 fix loop #5 (auditor BLOCKER 3) — selected_at_phase match.
    # When the calibration produced a non-None selected_at_phase the
    # smoke summary MUST echo the same value; a mismatch indicates the
    # smoke run was paired against a different calibration or that the
    # smoke summary was edited in transit.
    if expected_selected_at_phase is not None:
        smoke_phase = data.get("selected_at_phase")
        if smoke_phase != expected_selected_at_phase:
            raise LinkageValidationError(
                f"smoke summary selected_at_phase {smoke_phase!r} ≠ "
                f"calibration selected_at_phase "
                f"{expected_selected_at_phase!r}",
                reason="smoke_selected_at_phase_mismatches_calibration",
            )
    # v2.3 fix loop #5 (auditor BLOCKER 3) — admitted-pressure check.
    # Any smoke cell that observed zero 429s AND failed the admitted-
    # pressure floor is a driver/host-capacity finding; evidence cannot
    # legitimately use that smoke as its gate, so refuse the linkage.
    # Cells that observed ≥ 1 real 429 have the admitted-pressure gate
    # skipped-by-429 and are treated as passing.
    #
    # v2.3 fix loop #6 (auditor BLOCKER 3) — schema-version gated
    # enforcement. Fresh v2.3 summaries (schema_version ==
    # "task019.v2.3.measurement_summary") MUST carry per-cell
    # `admitted_pressure_passed`; the runner echoes it
    # unconditionally. A missing field on a v2.3 summary indicates
    # silently-edited, hand-rolled, or genuinely stale-schema content
    # and MUST raise `smoke_admitted_pressure_failed` so the evidence
    # run cannot link against unaudited content. Legacy back-compat
    # for the explicit older `task019.v2.2.1.measurement_summary`
    # schema continues to skip the per-cell field-absent case (the
    # v2.2.1 runner did not emit the field).
    smoke_schema = data.get("schema_version") or ""
    v23_summary = smoke_schema == "task019.v2.3.measurement_summary"
    failing_cells: list[str] = []
    missing_field_cells: list[str] = []
    for cell in data.get("cell_summaries") or []:
        passed = cell.get("admitted_pressure_passed")
        n_429 = int(cell.get("n_429_records", 0) or 0)
        if passed is None:
            # Cells with ≥ 1 real 429 have the gate skipped-by-429
            # regardless of schema version — the 429 itself is the
            # signal, so a missing field on a 429-bearing cell is OK.
            if n_429 >= 1:
                continue
            if v23_summary:
                missing_field_cells.append(
                    str(cell.get("max_output_tokens"))
                )
            # Legacy v2.2.1: skip (back-compat — field absent is OK
            # on cells that pre-date v2.3's per-cell propagation).
            continue
        if not passed and n_429 == 0:
            failing_cells.append(str(cell.get("max_output_tokens")))
    if missing_field_cells:
        raise LinkageValidationError(
            "v2.3 smoke summary missing per-cell "
            "`admitted_pressure_passed` on cell(s) "
            f"max_output_tokens={','.join(missing_field_cells)} "
            f"(schema_version={smoke_schema!r}); the v2.3 runner "
            "echoes this field unconditionally so a missing field "
            "indicates a forged or schema-incomplete summary",
            reason="smoke_admitted_pressure_failed",
        )
    if failing_cells:
        raise LinkageValidationError(
            "smoke summary admitted-pressure floor failed on cell(s) "
            f"max_output_tokens={','.join(failing_cells)} (zero 429s "
            "observed; cannot use this smoke for evidence linkage)",
            reason="smoke_admitted_pressure_failed",
        )
    return data


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return _iso8601_z(obj) if isinstance(obj, datetime.datetime) else obj.isoformat()
    if isinstance(obj, pathlib.Path):
        return str(obj)
    if isinstance(obj, set):
        return sorted(obj)
    raise TypeError(f"unserializable type: {type(obj).__name__}")


def _zero_usage_dict() -> dict[str, int]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
    }


def _usage_to_token_usage(usage: dict[str, Any]) -> TokenUsage:
    """Map a Responses-API ``usage`` dict to ``TokenUsage`` for pricing.

    The Azure OpenAI Responses API surfaces token counts under
    ``input_tokens`` / ``output_tokens`` (NOT the legacy Chat-Completions
    ``prompt_tokens`` / ``completion_tokens``), with the cached subset
    nested under ``input_tokens_details.cached_tokens`` and the reasoning
    subset under ``output_tokens_details.reasoning_tokens``. The legacy
    field names are accepted as a fall-back so the helper also accepts
    Chat-Completions-shaped payloads (and the synthetic dry-run
    ``_zero_usage_dict`` produces both shapes).
    """
    in_det = usage.get("input_tokens_details") or {}
    out_det = usage.get("output_tokens_details") or {}
    in_tok = usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
    out_tok = usage.get(
        "output_tokens", usage.get("completion_tokens", 0)
    ) or 0
    cached = in_det.get("cached_tokens", usage.get("cached_tokens", 0)) or 0
    reasoning = (
        out_det.get("reasoning_tokens", usage.get("reasoning_tokens", 0))
        or 0
    )
    return TokenUsage(
        input_tokens=float(in_tok),
        cached_tokens=float(cached),
        output_tokens=float(out_tok),
        reasoning_tokens=float(reasoning),
    )


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return float(s[int(k)])
    return float(s[f] + (s[c] - s[f]) * (k - f))


def _resolve_env_value(template: str, env: dict[str, str]) -> str:
    name = _extract_env_name(template)
    if name is None:
        return template
    val = env.get(name, "")
    if not val:
        raise EndpointMisconfiguredError(
            f"required env var {name!r} (template {template!r}) is unset "
            f"or empty"
        )
    return val


def _resolve_git_commit() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=pathlib.Path(__file__).resolve().parent.parent,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return "unknown"


def _git_is_clean() -> bool:
    try:
        out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=pathlib.Path(__file__).resolve().parent.parent,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip() == ""
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False


# ----------------------------------------------------------------------------
# Pricing freshness gate
# ----------------------------------------------------------------------------


def _check_pricing_freshness(
    pricing: PaygPricing, *, today: datetime.date,
) -> None:
    """Raise ``PricingStaleError`` if ``pricing.accessed_date`` is older
    than ``PRICING_SNAPSHOT_MAX_AGE_DAYS``."""
    accessed = getattr(pricing, "accessed_date", None)
    if accessed is None:
        raise PricingStaleError(
            "pricing snapshot has no accessed_date — Task 019 v2.1 "
            "requires a fresh PAYG pricing snapshot (max age "
            f"{PRICING_SNAPSHOT_MAX_AGE_DAYS} days)"
        )
    if isinstance(accessed, str):
        try:
            accessed_d = datetime.date.fromisoformat(accessed)
        except ValueError as exc:
            raise PricingStaleError(
                f"pricing accessed_date {accessed!r} is not ISO-8601"
            ) from exc
    elif isinstance(accessed, datetime.date):
        accessed_d = accessed
    else:
        raise PricingStaleError(
            f"pricing accessed_date has unexpected type "
            f"{type(accessed).__name__}"
        )
    age_days = (today - accessed_d).days
    if age_days > PRICING_SNAPSHOT_MAX_AGE_DAYS:
        raise PricingStaleError(
            f"pricing snapshot accessed_date {accessed_d.isoformat()} is "
            f"{age_days} days old (> {PRICING_SNAPSHOT_MAX_AGE_DAYS}-day "
            f"freshness gate)"
        )


# ----------------------------------------------------------------------------
# Token provider + live client (Foundry v1, max_retries=0)
# ----------------------------------------------------------------------------


def _make_robust_token_provider(
    *,
    scope: str = "https://ai.azure.com/.default",
    max_retries: int = DEFAULT_TOKEN_MAX_RETRIES,
    base_backoff: float = DEFAULT_TOKEN_BASE_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_TOKEN_MAX_BACKOFF_SECONDS,
) -> tuple[Callable[[], Any], Any]:
    """Async bearer-token provider for ``openai.AsyncOpenAI``.

    The OpenAI Python SDK calls ``api_key`` per request and ``await``-s the
    result when it is a coroutine. We therefore use ``azure.identity.aio``
    (the async credential surface) and return an *async callable* — passing
    a sync callable here yields ``TypeError: object str can't be used in
    'await' expression`` at the first request.

    This wrapper guards the credential acquisition only (NOT the SDK's
    request retry budget — the latter is pinned to ``max_retries=0`` in
    Task 019 v2.1). It applies bounded exponential backoff to transient
    credential errors (``CredentialUnavailableError`` /
    ``asyncio.TimeoutError``).

    Returns:
        Tuple of ``(provider_callable, credential)``. The caller MUST
        ``await credential.close()`` (or equivalent) at process exit; not
        closing the async ``DefaultAzureCredential`` is what produces the
        ``Unclosed client session`` warning at runtime end.
    """
    from azure.identity.aio import (
        DefaultAzureCredential,
        get_bearer_token_provider,
    )

    try:
        from azure.identity import CredentialUnavailableError
    except ImportError:  # pragma: no cover

        class CredentialUnavailableError(Exception):  # type: ignore[no-redef]
            pass

    retryable_excs: tuple[type[BaseException], ...] = (
        CredentialUnavailableError,
        asyncio.TimeoutError,
        TimeoutError,
    )

    credential = DefaultAzureCredential()
    inner = get_bearer_token_provider(credential, scope)
    lock = asyncio.Lock()

    async def _provider() -> str:
        async with lock:
            attempt = 0
            delay = base_backoff
            while True:
                try:
                    return await inner()
                except retryable_excs as exc:
                    if attempt >= max_retries:
                        logger.error(
                            "TOKEN_PROVIDER_EXHAUSTED attempts=%d exc=%s",
                            attempt + 1,
                            type(exc).__name__,
                        )
                        raise
                    logger.warning(
                        "TOKEN_PROVIDER_TRANSIENT_FAILURE attempt=%d/%d "
                        "exc=%s backoff_seconds=%.2f",
                        attempt + 1,
                        max_retries + 1,
                        type(exc).__name__,
                        delay,
                    )
                    await asyncio.sleep(min(delay, max_backoff))
                    delay = min(delay * 2.0, max_backoff)
                    attempt += 1

    return _provider, credential


async def _aclose_quiet(obj: Any) -> None:
    """Best-effort async close of an ``AsyncOpenAI`` client or
    ``DefaultAzureCredential`` (async). Swallows AttributeError /
    RuntimeError so a missing ``close``/``aclose`` never masks the real
    failure being unwound."""
    if obj is None:
        return
    for attr in ("close", "aclose"):
        meth = getattr(obj, attr, None)
        if meth is None:
            continue
        try:
            result = meth()
            if asyncio.iscoroutine(result):
                await result
            return
        except (AttributeError, RuntimeError) as exc:  # pragma: no cover
            logger.debug(
                "ACLOSE_QUIET ignored exc on %s.%s: %s",
                type(obj).__name__, attr, exc,
            )
            return


def _build_live_client(
    *, endpoint_value: str, max_retries: int = SDK_MAX_RETRIES_PINNED,
) -> tuple[Any, Any]:
    """Construct an ``AsyncOpenAI`` client targeting Foundry v1.

    The endpoint is canonicalised to ``{endpoint}/openai/v1/`` and the
    client is configured with ``api_version="preview"`` and
    ``max_retries=max_retries`` (Task 019 v2.1 pins this to 0).

    Returns:
        Tuple of ``(AsyncOpenAI client, async DefaultAzureCredential)``.
        The caller MUST close both at process exit (use
        :func:`_aclose_quiet` for each) to avoid the
        ``Unclosed client session`` warning."""
    if max_retries != SDK_MAX_RETRIES_PINNED:
        raise EndpointMisconfiguredError(
            f"_build_live_client received max_retries={max_retries}; Task "
            f"019 v2.1 forbids any value other than "
            f"{SDK_MAX_RETRIES_PINNED} (the SDK default of 2 silently "
            f"absorbs first-429 onset)"
        )
    from openai import AsyncOpenAI

    if not endpoint_value:
        raise EndpointMisconfiguredError(
            "endpoint value is empty; set AZURE_OPENAI_FOUNDRY_ENDPOINT"
        )
    base = endpoint_value.rstrip("/")
    if not base.endswith("/openai/v1"):
        base = f"{base}/openai/v1"
    base = base + "/"
    provider, credential = _make_robust_token_provider()

    # NOTE on Foundry v1 + api-version: the v1 path (.../openai/v1/...)
    # explicitly rejects the legacy `api-version` query parameter
    # ("api-version query parameter is not allowed when using /v1 path",
    # 400 BadRequest). The Foundry v1 surface fixes API version semantics
    # at the path level, so we MUST NOT pass `default_query={"api-version":
    # ...}`. We pin the value `FOUNDRY_API_VERSION = "preview"` only as
    # provenance — it is echoed into every JSONL record as
    # `request_api_version` for auditability, but it is NEVER sent on the
    # wire when calling Foundry v1. Task 018 v2.4
    # (scripts/measure_cache_key_bucketing.py:1300-1301) uses the same
    # construction (no default_query); Task 019 v2.1 inherits it.
    client = AsyncOpenAI(
        base_url=base,
        api_key=provider,  # bearer-token provider — SDK calls it per request
        max_retries=max_retries,
    )

    return client, credential


def _parse_response_headers(headers: Any) -> dict[str, str | None]:
    """Extract retry-after-ms and retry-after from a response headers
    mapping (case-insensitive). Returns string values verbatim or None."""
    out: dict[str, str | None] = {
        "retry_after_ms": None,
        "retry_after": None,
    }
    if headers is None:
        return out
    # httpx Headers / openai response headers expose get() with case-
    # insensitive lookup.
    try:
        v = headers.get("retry-after-ms")
        if v is not None:
            out["retry_after_ms"] = str(v)
    except Exception:
        pass
    try:
        v = headers.get("retry-after")
        if v is not None:
            out["retry_after"] = str(v)
    except Exception:
        pass
    return out



# ----------------------------------------------------------------------------
# HTTP call layer — Task 019 v2.1: max_retries=0, capture first 429 verbatim
# ----------------------------------------------------------------------------


async def _create_with_raw_response(
    *, client: Any, call_kwargs: dict
) -> tuple[Any, Any]:
    """Issue one ``responses.create`` returning ``(response, raw_headers)``."""
    raw_api = getattr(client.responses, "with_raw_response", None)
    if raw_api is not None and hasattr(raw_api, "create"):
        raw_resp = await raw_api.create(**call_kwargs)
        raw_headers = getattr(raw_resp, "headers", None)
        if hasattr(raw_resp, "parse"):
            parsed = raw_resp.parse()
            if inspect.isawaitable(parsed):
                parsed = await parsed
            response = parsed
        else:
            response = raw_resp
        return response, raw_headers
    response = await client.responses.create(**call_kwargs)
    raw_headers = getattr(response, "headers", None)
    return response, raw_headers


async def _call_no_retry(
    *, client: Any, call_kwargs: dict, request_idx: int
) -> dict[str, Any]:
    """One Responses API call WITH ZERO RETRIES.

    Task 019 v2.1 deliberately disables retries: the entire signal we are
    measuring is the FIRST observed 429. The SDK default of 2 retries
    would silently absorb that first 429 and replace it with either a
    success (corrupting the latency tail with hidden waits) or a third
    429 (corrupting the 429-onset timeline by minutes).

    Returns a dict with: ``usage``, ``first_token_latency_ms``,
    ``total_latency_ms``, ``rate_limited`` (True iff HTTP 429),
    ``headers`` (``retry_after_ms``, ``retry_after``), ``raised``
    (the exception object on non-429 failure, else None).
    """
    started = time.monotonic()
    last_headers = _parse_response_headers(None)
    try:
        response, raw_headers = await _create_with_raw_response(
            client=client, call_kwargs=call_kwargs
        )
    except Exception as exc:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        status = getattr(exc, "status_code", None) or getattr(
            exc, "status", None
        )
        resp_obj = getattr(exc, "response", None)
        raw_headers = (
            getattr(resp_obj, "headers", None) if resp_obj else None
        )
        last_headers = _parse_response_headers(raw_headers)
        if status == 429:
            logger.warning(
                "RATE_LIMITED_429 request_idx=%d retry_after_ms=%s "
                "retry_after=%s (Task 019 v2.1: NOT retrying — this is the "
                "signal)",
                request_idx,
                last_headers.get("retry_after_ms"),
                last_headers.get("retry_after"),
            )
            return {
                "usage": None,
                "first_token_latency_ms": elapsed_ms,
                "total_latency_ms": elapsed_ms,
                "rate_limited": True,
                "headers": last_headers,
                "raised": None,
            }
        logger.warning(
            "REQUEST_EXCEPTION request_idx=%d exc_type=%s status=%s",
            request_idx,
            type(exc).__name__,
            status,
        )
        return {
            "usage": None,
            "first_token_latency_ms": elapsed_ms,
            "total_latency_ms": elapsed_ms,
            "rate_limited": False,
            "headers": last_headers,
            "raised": exc,
        }
    elapsed_ms = (time.monotonic() - started) * 1000.0
    usage_obj = getattr(response, "usage", None)
    if usage_obj is None:
        usage_dict: dict[str, Any] = {}
    elif hasattr(usage_obj, "model_dump"):
        usage_dict = usage_obj.model_dump()
    else:
        usage_dict = dict(usage_obj)
    if raw_headers is None:
        raw_headers = getattr(response, "headers", None)
    headers_parsed = _parse_response_headers(raw_headers)
    return {
        "usage": usage_dict,
        "first_token_latency_ms": elapsed_ms,
        "total_latency_ms": elapsed_ms,
        "rate_limited": False,
        "headers": headers_parsed,
        "raised": None,
    }


async def _preflight_reachability(
    *, client: Any, deployment: str
) -> dict[str, Any]:
    """Issue one tiny request to confirm the deployment is alive."""
    try:
        resp = await client.responses.create(
            model=deployment,
            input="ping",
            max_output_tokens=16,
            reasoning={"effort": "low"},
        )
    except Exception as exc:
        raise PreflightReachabilityError(
            f"pre-flight reachability failed for deployment={deployment}: "
            f"{type(exc).__name__}: {exc}; check auth + endpoint + "
            f"deployment name"
        ) from exc
    usage_obj = getattr(resp, "usage", None)
    out_tok = 0
    if usage_obj is not None:
        out_tok = int(getattr(usage_obj, "output_tokens", 0) or 0)
    if out_tok <= 0:
        raise PreflightReachabilityError(
            f"pre-flight reachability for deployment={deployment} "
            f"returned zero output_tokens"
        )
    logger.info(
        "PREFLIGHT_OK deployment=%s output_tokens=%d", deployment, out_tok
    )
    return {"deployment": deployment, "reachable": True, "output_tokens": out_tok}


# ----------------------------------------------------------------------------
# Per-request record assembly (Task 019 v2.1 schema)
# ----------------------------------------------------------------------------


def _assemble_record(
    *,
    cfg: ExperimentConfig,
    cell_idx: int,
    cell_max_output_tokens: int,
    arrival_idx_within_cell: int,
    global_request_idx: int,
    is_prewarm: bool,
    prompt_cache_key_used: str,
    usage_dict: dict[str, Any],
    first_token_latency_ms: float,
    total_latency_ms: float,
    rate_limited: bool,
    headers_parsed: dict[str, Any],
    relative_time_s: float,
    deployment_used: str,
    scheduled_dispatch_cell_elapsed_ms: int,
    admitted_dispatch_cell_elapsed_ms: int,
    dispatch_backlog_ms: int,
    in_flight_at_dispatch: int,
    arrival_rpm_at_request_time: int,
    request_estimated_processed_tokens: int,
    failed: bool,
    failure_reason: str | None,
    git_commit: str,
    dirty: bool,
    system_sha: str,
    user_prompts_source_sha: str,
    source_corpus_sha: str,
    pricing_snapshot_path: str,
    dry_run: bool,
    run_id_short: str,
    intended_dispatch_iso: str | None = None,
    scheduled_dispatch_iso: str | None = None,
    admitted_dispatch_iso: str | None = None,
    adaptive_step: str | None = None,
) -> dict[str, Any]:
    # Extract canonical input/cached/output/reasoning token fields.
    in_det = usage_dict.get("input_tokens_details") or {}
    out_det = usage_dict.get("output_tokens_details") or {}
    cached_tokens = (
        int(in_det.get("cached_tokens", 0) or 0)
        if isinstance(in_det, dict) else 0
    )
    input_tokens = int(
        usage_dict.get("input_tokens", usage_dict.get("prompt_tokens", 0))
        or 0
    )
    output_tokens = int(
        usage_dict.get("output_tokens", usage_dict.get("completion_tokens", 0))
        or 0
    )
    reasoning_tokens = (
        int(out_det.get("reasoning_tokens", 0) or 0)
        if isinstance(out_det, dict) else 0
    )
    # If output_tokens_details was absent but usage carried reasoning_tokens
    # at the top level (some Responses API shapes), respect that.
    if reasoning_tokens == 0 and "reasoning_tokens" in usage_dict:
        try:
            reasoning_tokens = int(usage_dict["reasoning_tokens"] or 0)
        except (TypeError, ValueError):
            reasoning_tokens = 0
    visible_output_tokens = max(0, output_tokens - reasoning_tokens)
    now_iso = _iso8601_z(_utc_now())
    return {
        "experiment_id": cfg.experiment_id,
        "git_commit": git_commit,
        "dirty": dirty,
        "timestamp_utc": now_iso,
        "wallclock_timestamp_iso": now_iso,
        "api_version": cfg.client.api_version,
        "model": cfg.deployment.family,
        "deployment_used": deployment_used,
        "auth_mode": cfg.deployment.auth_mode,
        # Pinned-confounds echo (Task 019 v2.1 — every record carries the
        # exact pinned controls so analyses can re-verify them).
        "request_reasoning_effort": cfg.request_template.reasoning_effort,
        "request_api_version": cfg.client.api_version,
        "request_concurrency": cfg.runtime.concurrency,
        "request_peak_ramp_tps": cfg.runtime.peak_ramp_tps,
        "request_prewarm_tps": cfg.runtime.prewarm_tps,
        "dispatcher_kind": cfg.runtime.dispatcher,
        "sdk_max_retries": cfg.client.max_retries,
        # Task 019 sweep + cell identity.
        "cell_max_output_tokens": cell_max_output_tokens,
        "max_output_tokens_sent": cell_max_output_tokens,
        "is_prewarm": is_prewarm,
        "prompt_cache_key_used": prompt_cache_key_used,
        # Token usage (canonical fields).
        "visible_output_tokens": visible_output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cached_tokens": cached_tokens,
        "canonical_input_tokens": input_tokens,
        "canonical_output_tokens": output_tokens,
        # 429 capture (Task 019 v2.1 first-429-onset signal).
        "429_observed": rate_limited,
        "rate_limited": rate_limited,
        "retry_after_ms": headers_parsed.get("retry_after_ms"),
        "retry_after": headers_parsed.get("retry_after"),
        # Dispatch telemetry.
        "scheduled_dispatch_cell_elapsed_ms": scheduled_dispatch_cell_elapsed_ms,
        "admitted_dispatch_cell_elapsed_ms": admitted_dispatch_cell_elapsed_ms,
        "dispatch_backlog_ms": dispatch_backlog_ms,
        "in_flight_at_dispatch": in_flight_at_dispatch,
        "arrival_rpm_at_request_time": arrival_rpm_at_request_time,
        # v2.3 NEW — three dispatch timestamps (intended/scheduled/admitted).
        # intended_dispatch_iso is the schedule's target dispatch time
        # (deterministic, bit-stable across runs at the same seed);
        # scheduled_dispatch_iso is post-pacer pre-semaphore; and
        # admitted_dispatch_iso is post-semaphore pre-HTTP-send. The
        # *_cell_elapsed_ms integers are preserved for v2.2.1 analysis
        # compatibility. Optional kwargs default to None for v2.2.1
        # call sites that do not yet supply them.
        "intended_dispatch_iso": intended_dispatch_iso,
        "scheduled_dispatch_iso": scheduled_dispatch_iso,
        "admitted_dispatch_iso": admitted_dispatch_iso,
        # Cell / arrival indexing.
        "cell_idx": cell_idx,
        "arrival_idx_within_cell": arrival_idx_within_cell,
        "request_idx": global_request_idx,
        "relative_time_s": relative_time_s,
        "request_estimated_processed_tokens": request_estimated_processed_tokens,
        # Latency.
        "first_token_latency_ms": first_token_latency_ms,
        "total_latency_ms": total_latency_ms,
        "usage": usage_dict,
        # Failure marker.
        "failed": failed,
        "failure_reason": failure_reason,
        # Provenance.
        "dry_run": dry_run,
        "system_prompt_sha256": system_sha,
        "user_prompts_source_sha256": user_prompts_source_sha,
        "source_corpus_sha256": source_corpus_sha,
        "cell_metadata": {
            "system_prompt_sha256": system_sha,
            "corpus_seed": cfg.corpus_seed,
            "run_id_short": run_id_short,
        },
        "pricing_snapshot_path": pricing_snapshot_path,
        # Task 019 v2.7 — adaptive Stage 0.5.C provenance. ``None`` for
        # v2.4 smoke/evidence/calibration records; one of
        # ``ADAPTIVE_STEP_NAMES`` for v2.5/v2.6/v2.7 adaptive probes.
        # Plumbed end-to-end so every JSONL record dispatched under an
        # adaptive probe is unambiguously attributable to its step,
        # including failure-path records (transport errors, 429s).
        "adaptive_step": adaptive_step,
    }


# ----------------------------------------------------------------------------
# Cell aggregation (Task 019 v2.1 — warm criterion, backlog, 429-onset RPM)
# ----------------------------------------------------------------------------


def _is_pre_admission_failure(record: dict[str, Any]) -> bool:
    """Same partition contract as Task 018 v2.4. Currently
    ``PRE_ADMISSION_FAILURE_REASONS`` is empty for Task 019 (no per-cell
    token cap), but the partition pattern is preserved for robustness."""
    if not record.get("failed", False):
        return False
    reason = record.get("failure_reason") or ""
    return reason in PRE_ADMISSION_FAILURE_REASONS


def _compute_warm_criterion_from_prewarm(
    prewarm_records: list[dict[str, Any]],
) -> tuple[bool, int, int]:
    """Apply the v2.1 warm criterion: ≥ 50% of last 6 pre-warm records
    have ``cached_tokens > 0``.

    Returns ``(passed, hits, considered)`` where ``considered`` is
    ``min(WARM_CRITERION_LAST_N, len(prewarm_records))``.
    """
    if not prewarm_records:
        return False, 0, 0
    tail = prewarm_records[-WARM_CRITERION_LAST_N:]
    hits = sum(
        1 for r in tail if int(r.get("cached_tokens", 0) or 0) > 0
    )
    considered = len(tail)
    if considered == 0:
        return False, 0, 0
    ratio = hits / considered
    return (ratio >= WARM_CRITERION_MIN_RATIO), hits, considered


def _aggregate_cell(
    records: list[dict[str, Any]],
    *,
    cell_max_output_tokens: int,
) -> dict[str, Any]:
    """Aggregate per-cell stats for Task 019 v2.1.

    Computes:
    - Warm criterion over pre-warm-only records.
    - Backlog excessive over admitted records.
    - 429-onset RPM = arrival_rpm_at_request_time at the FIRST non-prewarm
      record with ``429_observed=true``.
    - Cache hit / TTFT over non-prewarm, non-failed records (steady-state
      proxy).
    """
    if not records:
        return {
            "n_records": 0,
            "n_prewarm_records": 0,
            "n_ramp_records": 0,
            "n_failed_records": 0,
            "n_admitted_records": 0,
            "n_429_records": 0,
            "cache_hit_ratio_steady_state": 0.0,
            "first_token_latency_ms_p50_steady_state": 0.0,
            "first_token_latency_ms_p95_steady_state": 0.0,
            "visible_output_tokens_p50_steady_state": 0.0,
            "visible_output_tokens_p95_steady_state": 0.0,
            "reasoning_tokens_p50_steady_state": 0.0,
            "p95_dispatch_backlog_ms": 0.0,
            "max_dispatch_backlog_ms": 0.0,
            "max_in_flight_observed": 0,
            "backlog_excessive": False,
            "warm_criterion_passed": False,
            "warm_criterion_hits": 0,
            "warm_criterion_considered": 0,
            "cache_not_warm": True,
            "first_429_arrival_rpm": None,
            "first_429_admitted_ms": None,
            "first_429_arrival_idx": None,
            "first_429_relative_time_s": None,
        }
    prewarm_records = [r for r in records if r.get("is_prewarm", False)]
    ramp_records = [r for r in records if not r.get("is_prewarm", False)]
    failed_count = sum(1 for r in records if r.get("failed", False))
    admitted = [r for r in records if not _is_pre_admission_failure(r)]
    rate_limited_n = sum(
        1 for r in records if r.get("429_observed", False)
    )

    warm_passed, warm_hits, warm_considered = (
        _compute_warm_criterion_from_prewarm(prewarm_records)
    )
    cache_not_warm = not warm_passed

    # Cache-hit + latency: post-prewarm successes only.
    cache_target = [
        r for r in ramp_records
        if not r.get("failed", False)
        and not r.get("429_observed", False)
    ]
    in_sum = sum(
        int(r.get("canonical_input_tokens", 0) or 0) for r in cache_target
    )
    cached_sum = sum(
        int(r.get("cached_tokens", 0) or 0) for r in cache_target
    )
    cache_hit = (cached_sum / in_sum) if in_sum > 0 else 0.0
    latencies = [
        float(r["first_token_latency_ms"])
        for r in cache_target
        if isinstance(r.get("first_token_latency_ms"), (int, float))
    ]
    visible_out_toks = [
        float(r.get("visible_output_tokens", 0) or 0)
        for r in cache_target
    ]
    reasoning_toks = [
        float(r.get("reasoning_tokens", 0) or 0)
        for r in cache_target
    ]

    # Backlog over admitted (success + post-admission failure) — Task 018
    # partition pattern preserved.
    backlogs = [
        float(r.get("dispatch_backlog_ms", 0) or 0)
        for r in admitted
    ]
    if backlogs:
        p95_backlog = _percentile(backlogs, 95.0)
        max_backlog = max(backlogs)
    else:
        p95_backlog = 0.0
        max_backlog = 0.0
    backlog_excessive = (
        p95_backlog > BACKLOG_P95_FAIL_MS
        or max_backlog > BACKLOG_MAX_FAIL_MS
    )

    in_flights = [
        int(r.get("in_flight_at_dispatch", 0) or 0)
        for r in admitted
    ]
    max_in_flight = max(in_flights) if in_flights else 0

    # 429-onset RPM: first non-prewarm record with 429_observed=True.
    first_429: dict[str, Any] | None = None
    for r in ramp_records:
        if r.get("429_observed", False):
            first_429 = r
            break

    return {
        "n_records": len(records),
        "n_prewarm_records": len(prewarm_records),
        "n_ramp_records": len(ramp_records),
        "n_failed_records": failed_count,
        "n_admitted_records": len(admitted),
        "n_429_records": rate_limited_n,
        "cache_hit_ratio_steady_state": cache_hit,
        "first_token_latency_ms_p50_steady_state": _percentile(latencies, 50.0),
        "first_token_latency_ms_p95_steady_state": _percentile(latencies, 95.0),
        "visible_output_tokens_p50_steady_state": _percentile(visible_out_toks, 50.0),
        "visible_output_tokens_p95_steady_state": _percentile(visible_out_toks, 95.0),
        "reasoning_tokens_p50_steady_state": _percentile(reasoning_toks, 50.0),
        "p95_dispatch_backlog_ms": p95_backlog,
        "max_dispatch_backlog_ms": max_backlog,
        "max_in_flight_observed": max_in_flight,
        "backlog_excessive": backlog_excessive,
        "warm_criterion_passed": warm_passed,
        "warm_criterion_hits": warm_hits,
        "warm_criterion_considered": warm_considered,
        "cache_not_warm": cache_not_warm,
        "first_429_arrival_rpm": (
            first_429.get("arrival_rpm_at_request_time")
            if first_429 else None
        ),
        "first_429_admitted_ms": (
            first_429.get("admitted_dispatch_cell_elapsed_ms")
            if first_429 else None
        ),
        "first_429_arrival_idx": (
            first_429.get("arrival_idx_within_cell")
            if first_429 else None
        ),
        "first_429_relative_time_s": (
            first_429.get("relative_time_s") if first_429 else None
        ),
        "cell_max_output_tokens": cell_max_output_tokens,
    }


def evaluate_smoke_gate_block(
    *,
    cell_summaries: list[dict[str, Any]],
    sweep_planned: list[int],
) -> dict[str, Any]:
    """Evaluate the Task 019 v2.1 Stage-1 smoke acceptance gate (also used
    in Stage 2 as the 429-contrast sanity check).

    Spec contract (`.internal/tasks/019-max-output-tokens-reservation.md`
    v2.1, § Stage 1 acceptance):

    - **Largest cell must observe ≥ 1 real 429** in the ramp (proves the
      admission layer is reachable at the configured peak TPS — otherwise
      we cannot conclude *anything* about reservation-at-cap because the
      whole sweep stayed under quota).
    - **Smallest cell must observe 0 real 429s** in the ramp (proves the
      cap actually controls reservation — otherwise both ends are
      saturated and the sweep has no signal contrast).

    A run that completes 7/7 cells warm with backlog-OK but ZERO 429s in
    the largest cell does NOT satisfy the gate and MUST NOT be promoted to
    the Stage-2 evidence run.

    Args:
        cell_summaries: list of per-cell aggregate dicts (the same shape
            written to ``runs/*.summary.json → cell_summaries[]``). Each
            must carry ``max_output_tokens`` and ``n_429_records``.
        sweep_planned: ordered list of ``max_output_tokens`` values the
            run was supposed to execute. Used to detect partial runs that
            never reached the largest cell.

    Returns:
        Dict with keys::

            passed: bool                    — overall verdict
            reason: str                     — short, human-readable
            largest_cell_max_output_tokens  — int or None (when sweep
                empty)
            largest_cell_n_429              — int (0 if no record)
            smallest_cell_max_output_tokens — int or None
            smallest_cell_n_429             — int
            cells_completed                 — len(cell_summaries)
            cells_planned                   — len(sweep_planned)
            stage2_promotable               — bool (True only if passed
                AND no partial-run condition)

        ``passed=False`` reasons include:

        - ``no_cell_summaries`` (empty run)
        - ``largest_cell_not_reached`` (partial run that never executed
          the largest sweep value)
        - ``no_429_in_largest_cell`` (the failure mode that triggered the
          v2.1 protocol-correction work)
        - ``unexpected_429_in_smallest_cell`` (sweep cannot resolve
          contrast)
    """
    cells_planned = len(sweep_planned)
    cells_completed = len(cell_summaries)
    if cells_completed == 0:
        return {
            "passed": False,
            "reason": "no_cell_summaries",
            "largest_cell_max_output_tokens": None,
            "largest_cell_n_429": 0,
            "smallest_cell_max_output_tokens": None,
            "smallest_cell_n_429": 0,
            "cells_completed": 0,
            "cells_planned": cells_planned,
            "stage2_promotable": False,
        }
    by_mo = {int(c["max_output_tokens"]): c for c in cell_summaries}
    smallest_mo = min(by_mo)
    largest_mo = max(by_mo)
    smallest_n = int(by_mo[smallest_mo].get("n_429_records", 0) or 0)
    largest_n = int(by_mo[largest_mo].get("n_429_records", 0) or 0)
    planned_largest = max(sweep_planned) if sweep_planned else None
    largest_reached = (
        planned_largest is not None and largest_mo == planned_largest
    )
    if not largest_reached:
        return {
            "passed": False,
            "reason": "largest_cell_not_reached",
            "largest_cell_max_output_tokens": largest_mo,
            "largest_cell_n_429": largest_n,
            "smallest_cell_max_output_tokens": smallest_mo,
            "smallest_cell_n_429": smallest_n,
            "cells_completed": cells_completed,
            "cells_planned": cells_planned,
            "stage2_promotable": False,
        }
    if largest_n < 1:
        return {
            "passed": False,
            "reason": "no_429_in_largest_cell",
            "largest_cell_max_output_tokens": largest_mo,
            "largest_cell_n_429": largest_n,
            "smallest_cell_max_output_tokens": smallest_mo,
            "smallest_cell_n_429": smallest_n,
            "cells_completed": cells_completed,
            "cells_planned": cells_planned,
            "stage2_promotable": False,
        }
    if smallest_n > 0:
        return {
            "passed": False,
            "reason": "unexpected_429_in_smallest_cell",
            "largest_cell_max_output_tokens": largest_mo,
            "largest_cell_n_429": largest_n,
            "smallest_cell_max_output_tokens": smallest_mo,
            "smallest_cell_n_429": smallest_n,
            "cells_completed": cells_completed,
            "cells_planned": cells_planned,
            "stage2_promotable": False,
        }
    return {
        "passed": True,
        "reason": "ok",
        "largest_cell_max_output_tokens": largest_mo,
        "largest_cell_n_429": largest_n,
        "smallest_cell_max_output_tokens": smallest_mo,
        "smallest_cell_n_429": smallest_n,
        "cells_completed": cells_completed,
        "cells_planned": cells_planned,
        "stage2_promotable": True,
    }


# ----------------------------------------------------------------------------
# MeasurementResult dataclass
# ----------------------------------------------------------------------------


@dataclass
class MeasurementResult:
    """Top-level result of one Task 019 v2.1 measurement run.

    v2.3 fix loop #5 (auditor BLOCKER 1) adds ``total_committed_usd``:
    the deterministic conservative committed spend (dispatched calls ×
    per-call rate) — the SOURCE OF TRUTH for cap enforcement. The
    pre-existing ``total_usd`` remains the realized billed cost for
    audit reporting.
    """

    cells_completed: int
    cells_planned: int
    total_usd: float
    jsonl_path: pathlib.Path
    summary_path: pathlib.Path
    partial: bool
    halt_reason: str | None
    cell_summaries: list[dict[str, Any]] = field(default_factory=list)
    run_lock_metadata: dict[str, Any] | None = None
    total_committed_usd: float = 0.0


# ----------------------------------------------------------------------------
# Cell runner — prewarm + ramp + cooldown
# ----------------------------------------------------------------------------


async def _run_cell(
    *,
    cfg: ExperimentConfig,
    cell_idx: int,
    cell_max_output_tokens: int,
    prewarm_calls: int,
    prewarm_tps: float,
    ramp_duration_s: float,
    peak_ramp_tps: float,
    cool_down_s: float,
    concurrency: int,
    client: Any,
    deployment: str,
    system_prompt: str,
    user_prompts: list[str],
    git_commit: str,
    dirty: bool,
    system_sha: str,
    user_prompts_source_sha: str,
    source_corpus_sha: str,
    pricing_snapshot_path: str,
    pricing: PaygPricing,
    dry_run: bool,
    out_fh: Any,
    global_request_offset: int,
    sim_started_mono: float,
    run_id_short: str,
    cache_key_override: str | None = None,
    constant_rate: bool = False,
    probe_max_usd: float | None = None,
    probe_max_calls: int | None = None,
    total_max_usd_stop_event: asyncio.Event | None = None,
    early_stop_on_first_429: bool = False,
    adaptive_step: str | None = None,
) -> tuple[list[dict[str, Any]], float, float, int, str | None]:
    """Run one ``max_output_tokens`` cell.

    Phase 1 — Pre-warm: ``prewarm_calls`` calls evenly spaced at
    ``prewarm_tps`` cadence; each record carries ``is_prewarm=true``.

    Phase 2 — Ramp: linear TPS ramp from ``prewarm_tps`` →
    ``peak_ramp_tps`` over ``ramp_duration_s`` seconds, scheduled via
    the deterministic arrival schedule from
    ``build_arrival_schedule(seed)``. Records carry ``is_prewarm=false``.

    Phase 3 — Cool-down: ``await asyncio.sleep(cool_down_s)`` (skipped in
    dry-run). Drains in-flight queue before the next cell starts.

    v2.2.1 calibration-only optional kwargs (left at defaults for
    smoke/evidence cells to preserve byte-for-byte v2.1 behaviour):

    - ``cache_key_override``: when non-None, use this exact
      ``prompt_cache_key`` for every call in the cell instead of the
      ``task019_card1_*`` namespace produced by ``build_prompt_cache_key``.
      Required by the calibration runner to use the ``task019_calib_*``
      namespace + ``_retry1`` suffix.
    - ``constant_rate``: when True, the ramp phase dispatches at a
      CONSTANT TPS of ``peak_ramp_tps`` instead of a linear ramp from
      ``prewarm_tps`` → ``peak_ramp_tps``. Required by calibration probes
      (per spec § Stage 0.5 — probes are constant-rate, not ramped).
    - ``probe_max_usd``: when non-None, applies the v2.3 NON-BLOCKING
      advisory admission check using the **deterministic conservative
      committed cost** — BEFORE ``asyncio.create_task`` the dispatcher
      synchronously checks
      ``cell_committed_usd + DETERMINISTIC_PER_CALL_USD > probe_max_usd``
      (where ``cell_committed_usd`` is dispatched-call count ×
      ``DETERMINISTIC_PER_CALL_USD``, *not* realized billed cost) and
      breaks the dispatch loop if so. 429 responses, transport errors,
      and zero-usage stubs all count for the FULL per-call rate; there
      is no 429-no-bill discount and no zero-usage bypass — the cap is
      a deterministic admission gate, not realized-spend tracking. The
      check is O(1) and never inserts ``await call()`` into the dispatch
      loop body. The v2.2.1 sequential-await pattern is FORBIDDEN by
      spec Microfix B.
    - ``probe_max_calls``: v2.3 microfix 2026-05-30 (auditor finding
      #4) — when non-None, applies the same O(1) advisory admission
      check pattern as ``probe_max_usd`` against the ramp-phase
      dispatched-call counter (NOT cumulative across cells; per-cell
      ramp-phase counter only). Pinned values: Phase A probes = 600,
      Phase B probes = 6624 (= ceil(32 * 207)). Enforced via the same
      "check BEFORE ``asyncio.create_task``" pattern, NEVER via
      sequential ``await call(); if ...: break`` (FORBIDDEN by spec
      Microfix B).
    - ``total_max_usd_stop_event``: when non-None, the dispatch loop
      checks ``stop_event.is_set()`` after each pacer sleep returns and
      BEFORE ``asyncio.create_task``. Set by a SEPARATE accounting
      coroutine when **deterministic committed spend** crosses
      ``0.85 × total_max_usd`` (i.e. ``total_committed_usd``, never
      realized cost — a fast zero-usage stub or 429-only response
      stream cannot bypass the guardrail). Already-in-flight tasks
      complete normally; no NEW dispatch occurs after the event is set.
    - ``early_stop_on_first_429``: v2.3 microfix 2026-05-30 fix loop #4
      (auditor finding #3). When True, an internal ``asyncio.Event`` is
      created and set by ``_admit_and_call`` the first time a real 429
      (``rate_limited=True``) is observed. The dispatch loop consults
      the event BEFORE ``asyncio.create_task`` (same non-blocking
      pattern as ``total_max_usd_stop_event``; NEVER inserts an
      ``await call()`` into the dispatch loop body — spec Microfix B
      forbids sequential-await dispatch under any cap). On first 429:
      (a) the event is set, (b) ``halt_reason`` is recorded as
      ``"first_429_observed"``, (c) no new ramp dispatches occur,
      (d) already-in-flight tasks complete normally (their cost is
      already committed under the deterministic conservative
      estimator and a single in-flight wave may include additional
      429s; the FIRST observed 429 is the signal). Calibration
      probes use this flag based on role and the YAML's
      ``calibration.early_stop_on_first_429_largest`` /
      ``early_stop_on_first_429_smallest`` controls; smoke + evidence
      leave it at the default ``False`` (they measure the full 429-
      onset curve, not first 429 only).

    Returns ``(records_admitted_order, cell_usd_realized,
    cell_committed_usd, max_in_flight_observed, halt_reason)``.
    ``cell_usd_realized`` is the actually-billed total under PAYG
    pricing (429s and transport errors contribute 0). ``cell_committed_usd``
    is the deterministic conservative count =
    ``n_dispatched × DETERMINISTIC_PER_CALL_USD`` and is the SOURCE OF
    TRUTH for ``probe_max_usd`` / ``total_max_usd`` cap enforcement
    (Microfix B/C — every dispatched call billed at full per-call rate).
    ``halt_reason`` is ``None`` when no cap fired; one of
    ``"probe_max_calls_hit"``, ``"probe_max_usd_hit"``,
    ``"total_max_usd_stop_event_set"``, ``"first_429_observed"`` when a
    cap or early-stop signal interrupted ramp dispatch. Records are
    written to ``out_fh`` in admitted-elapsed-ms order.
    """
    sem = asyncio.Semaphore(concurrency)
    cell_start_mono = time.monotonic()
    cell_start_utc = _utc_now()
    in_flight = 0
    max_in_flight = 0
    cost_lock = asyncio.Lock()
    # v2.3 fix loop #5 — track BOTH realized billed cost ("cell_usd")
    # and deterministic conservative committed cost
    # ("committed_usd" = dispatched-call count ×
    # DETERMINISTIC_PER_CALL_USD). The committed counter is the SOURCE
    # OF TRUTH for cap admission and stop-event accounting; the realized
    # counter is kept for audit-only reporting. A 429-no-bill or
    # zero-usage stub MUST NOT reduce the committed counter — that
    # would let a fast failure mode bypass the guardrail.
    nonlocal_state = {"cell_usd": 0.0, "committed_usd": 0.0}
    # v2.3 microfix fix loop #4 — early-stop-on-first-429 event. Set
    # inside `_admit_and_call` when the FIRST `rate_limited=True`
    # response is observed (and `early_stop_on_first_429=True`). The
    # dispatch loop consults this BEFORE `asyncio.create_task` so no
    # NEW ramp dispatch occurs after the first 429. Already-in-flight
    # tasks complete normally.
    early_stop_429_event: asyncio.Event = asyncio.Event()
    cell_prompt_cache_key = (
        cache_key_override
        if cache_key_override is not None
        else build_prompt_cache_key(
            run_id_short=run_id_short,
            max_output_tokens=cell_max_output_tokens,
        )
    )
    seed_str = (
        f"exp007_max_output_tokens_sweep_cell{cell_max_output_tokens:05d}"
    )
    if constant_rate:
        # v2.2.1 calibration: dispatch at a constant TPS of
        # `peak_ramp_tps` over `ramp_duration_s` (probe_duration_s).
        # The deterministic arrival schedule expects linear ramp from
        # `prewarm_tps` → `peak_ramp_tps`; for constant-rate we just
        # set prewarm_tps == peak_ramp_tps so the integral degenerates.
        prewarm_times_s, ramp_times_s = build_arrival_schedule(
            seed_str=seed_str,
            prewarm_calls=prewarm_calls,
            prewarm_tps=prewarm_tps,
            ramp_duration_s=ramp_duration_s,
            peak_ramp_tps=peak_ramp_tps,
        )
        # Override the ramp times to be CONSTANT-RATE at peak_ramp_tps.
        n_constant = int(ramp_duration_s * peak_ramp_tps)
        if n_constant > 0:
            spacing = 1.0 / peak_ramp_tps
            # Offset by prewarm duration so admitted-elapsed remains
            # comparable to the prewarm phase.
            prewarm_dur = prewarm_calls / max(prewarm_tps, 1e-9)
            ramp_times_s = [
                prewarm_dur + i * spacing for i in range(n_constant)
            ]
    else:
        prewarm_times_s, ramp_times_s = build_arrival_schedule(
            seed_str=seed_str,
            prewarm_calls=prewarm_calls,
            prewarm_tps=prewarm_tps,
            ramp_duration_s=ramp_duration_s,
            peak_ramp_tps=peak_ramp_tps,
        )
    # v2.3 NEW — pre-generate the deterministic intended-dispatch-ISO
    # list (one entry per scheduled dispatch). Bit-stable across runs at
    # the same seed + cell_max_output_tokens + (prewarm_tps,
    # peak_ramp_tps, ramp_duration_s, prewarm_calls). The runtime
    # invariant `probe_schedule_intended_rate_insufficient` is asserted
    # by the caller from THIS list (NOT from any post-hoc admitted
    # timing), so the schedule's own intent vs the dispatcher's actual
    # admission is unambiguously distinguishable.
    intended_iso_prewarm = [
        _iso8601_z(cell_start_utc + datetime.timedelta(seconds=t))
        for t in prewarm_times_s
    ]
    intended_iso_ramp = [
        _iso8601_z(cell_start_utc + datetime.timedelta(seconds=t))
        for t in ramp_times_s
    ]
    # Arrival rate tracker — rolling 60s admitted-RPM.
    rpm_tracker = RpmTracker(window_s=60.0)
    sys_chars = len(system_prompt)

    async def _admit_and_call(
        arrival_idx: int,
        scheduled_time_s: float,
        is_prewarm: bool,
        intended_iso: str,
    ) -> dict[str, Any] | None:
        nonlocal in_flight, max_in_flight
        now_offset = time.monotonic() - cell_start_mono
        delay = scheduled_time_s - now_offset
        if delay > 0 and not dry_run:
            await asyncio.sleep(delay)
        # v2.3 microfix fix loop #4 (auditor finding #3) — after the
        # pacer sleep returns AND before the semaphore acquire, check
        # the early-stop-on-first-429 event. If a prior in-flight call
        # has already observed a 429 and the calibration probe asked
        # for early-stop, this call returns None immediately: no HTTP
        # call, no record emitted, no in_flight increment, no cost.
        # Pre-warm calls are NEVER early-stopped (the 429 onset signal
        # is a ramp-phase concept; pre-warm calls precede ramp). The
        # dispatch loop also consults the same event before
        # `asyncio.create_task` (the standard non-blocking pattern),
        # but `create_task` returns synchronously so within a single
        # cell the in-`_admit_and_call` check is what actually halts
        # further admissions. The Nones are filtered out of the
        # returned records list and never written to the JSONL.
        if (
            early_stop_on_first_429
            and not is_prewarm
            and early_stop_429_event.is_set()
        ):
            return None
        scheduled_mono = time.monotonic()
        scheduled_elapsed_ms = int(
            round((scheduled_mono - cell_start_mono) * 1000.0)
        )
        scheduled_iso = _iso8601_z(_utc_now())
        await sem.acquire()
        try:
            admitted_mono = time.monotonic()
            admitted_elapsed_ms = int(
                round((admitted_mono - cell_start_mono) * 1000.0)
            )
            admitted_iso = _iso8601_z(_utc_now())
            backlog_ms = admitted_elapsed_ms - scheduled_elapsed_ms
            in_flight_snapshot = in_flight
            in_flight += 1
            if in_flight > max_in_flight:
                max_in_flight = in_flight
            try:
                user_text = user_prompts[
                    (global_request_offset + arrival_idx) % len(user_prompts)
                ]
                est_tokens = (
                    int((sys_chars + len(user_text)) / DEFAULT_TOKEN_ESTIMATE_DIVISOR)
                    + cell_max_output_tokens
                )
                relative_time_s = admitted_mono - sim_started_mono
                # Record arrival in the rolling RPM tracker BEFORE we
                # compute arrival_rpm_at_request_time so the just-admitted
                # call is counted (rolling-60s-up-to-and-including-now
                # semantics).
                rpm_tracker.record(admitted_mono - cell_start_mono)
                arrival_rpm = rpm_tracker.count(
                    admitted_mono - cell_start_mono
                )

                call_kwargs: dict[str, Any] = {
                    "model": deployment,
                    "input": system_prompt + "\n\n" + user_text,
                    "reasoning": {
                        "effort": cfg.request_template.reasoning_effort
                    },
                    "max_output_tokens": cell_max_output_tokens,
                    "prompt_cache_key": cell_prompt_cache_key,
                }
                if dry_run:
                    usage_dict = _zero_usage_dict()
                    first_token_latency_ms = 0.0
                    total_latency_ms = 0.0
                    rate_limited = False
                    headers_parsed = _parse_response_headers(None)
                    per_call_usd = 0.0
                    failed = False
                    failure_reason: str | None = None
                else:
                    res = await _call_no_retry(
                        client=client,
                        call_kwargs=call_kwargs,
                        request_idx=arrival_idx,
                    )
                    if res["raised"] is not None:
                        usage_dict = _zero_usage_dict()
                        first_token_latency_ms = res["first_token_latency_ms"]
                        total_latency_ms = res["total_latency_ms"]
                        rate_limited = False
                        headers_parsed = res["headers"]
                        per_call_usd = 0.0
                        failed = True
                        failure_reason = (
                            f"transport_exception:{type(res['raised']).__name__}"
                        )
                    elif res["rate_limited"]:
                        usage_dict = _zero_usage_dict()
                        first_token_latency_ms = res["first_token_latency_ms"]
                        total_latency_ms = res["total_latency_ms"]
                        rate_limited = True
                        headers_parsed = res["headers"]
                        per_call_usd = 0.0
                        failed = True
                        failure_reason = "rate_limited_observed"
                        # v2.3 microfix fix loop #4 — early-stop on first
                        # 429. If the calibration probe asked for early
                        # stop, set the event NOW (idempotent — Event.set()
                        # is safe under repeated calls). The dispatch loop
                        # consults this before `asyncio.create_task` so no
                        # NEW ramp dispatch occurs; in-flight tasks finish.
                        if early_stop_on_first_429:
                            early_stop_429_event.set()
                    else:
                        usage_dict = res["usage"] or _zero_usage_dict()
                        first_token_latency_ms = res["first_token_latency_ms"]
                        total_latency_ms = res["total_latency_ms"]
                        rate_limited = False
                        headers_parsed = res["headers"]
                        tu = _usage_to_token_usage(usage_dict)
                        per_call_usd = payg_cost_per_call(
                            tu, pricing, model=cfg.deployment.family
                        ).usd_per_request
                        failed = False
                        failure_reason = None

                async with cost_lock:
                    nonlocal_state["cell_usd"] += per_call_usd

                record = _assemble_record(
                    cfg=cfg,
                    cell_idx=cell_idx,
                    cell_max_output_tokens=cell_max_output_tokens,
                    arrival_idx_within_cell=arrival_idx,
                    global_request_idx=global_request_offset + arrival_idx,
                    is_prewarm=is_prewarm,
                    prompt_cache_key_used=cell_prompt_cache_key,
                    usage_dict=usage_dict,
                    first_token_latency_ms=first_token_latency_ms,
                    total_latency_ms=total_latency_ms,
                    rate_limited=rate_limited,
                    headers_parsed=headers_parsed,
                    relative_time_s=relative_time_s,
                    deployment_used=deployment,
                    scheduled_dispatch_cell_elapsed_ms=scheduled_elapsed_ms,
                    admitted_dispatch_cell_elapsed_ms=admitted_elapsed_ms,
                    dispatch_backlog_ms=backlog_ms,
                    in_flight_at_dispatch=in_flight_snapshot,
                    arrival_rpm_at_request_time=arrival_rpm,
                    request_estimated_processed_tokens=est_tokens,
                    failed=failed,
                    failure_reason=failure_reason,
                    git_commit=git_commit,
                    dirty=dirty,
                    system_sha=system_sha,
                    user_prompts_source_sha=user_prompts_source_sha,
                    source_corpus_sha=source_corpus_sha,
                    pricing_snapshot_path=pricing_snapshot_path,
                    dry_run=dry_run,
                    run_id_short=run_id_short,
                    intended_dispatch_iso=intended_iso,
                    scheduled_dispatch_iso=scheduled_iso,
                    admitted_dispatch_iso=admitted_iso,
                    adaptive_step=adaptive_step,
                )
                return record
            finally:
                in_flight -= 1
        finally:
            sem.release()

    # Phase 1: pre-warm tasks. Pre-warm calls are NEVER early-stopped
    # (the early-stop event is checked under `is_prewarm=False` only).
    # v2.3 fix loop #5 — committed-cost increment happens at dispatch
    # time (before `asyncio.create_task`) so pre-warm calls contribute
    # to the deterministic cell_committed_usd counter the same way
    # ramp calls do; this keeps the committed counter consistent under
    # ANY response stream (success, 429, transport error, zero-usage).
    prewarm_tasks: list[asyncio.Task[dict[str, Any]]] = []
    for i in range(len(prewarm_times_s)):
        nonlocal_state["committed_usd"] += DETERMINISTIC_PER_CALL_USD
        prewarm_tasks.append(
            asyncio.create_task(
                _admit_and_call(
                    i, prewarm_times_s[i], True, intended_iso_prewarm[i],
                )
            )
        )
    prewarm_records_raw = (
        await asyncio.gather(*prewarm_tasks) if prewarm_tasks else []
    )
    # v2.3 microfix fix loop #4 — filter any None sentinels from
    # `_admit_and_call` (pre-warm should never emit None since the
    # early-stop guard skips when is_prewarm=True, but the filter is
    # defensive against future signature changes).
    prewarm_records = [r for r in prewarm_records_raw if r is not None]

    # Phase 2: ramp tasks (only AFTER pre-warm gather to enforce strict
    # phase ordering — the warm criterion is computed off pre-warm
    # records only and the cell summary distinguishes is_prewarm vs ramp).
    #
    # v2.3 Microfix B — REWRITE of the v2.2.1 probe_max_usd branch.
    # The v2.2.1 implementation used a forbidden sequential-await pattern
    # `for i in range(...): await _admit_and_call(...); if usd >= cap: break`
    # which collapsed effective dispatch rate to `~1 / per_call_wall_s`
    # regardless of `candidate_tps`. The v2.3 implementation dispatches
    # CONCURRENTLY via `asyncio.create_task` at every layer (including
    # under spend caps), enforcing the cap via:
    #   (a) non-blocking stop event (total_max_usd; set by separate
    #       accounting coroutine — see _run_calibration_async),
    #   (b) advisory O(1) admission check BEFORE create_task (probe_max_usd).
    # Already-in-flight tasks always run to completion (their cost is
    # already committed under the deterministic conservative estimator).
    ramp_offset = len(prewarm_times_s)
    ramp_tasks: list[asyncio.Task[dict[str, Any]]] = []
    n_dispatched_in_ramp = 0
    n_skipped_due_to_probe_max_usd = 0
    n_skipped_due_to_total_max_usd = 0
    n_skipped_due_to_probe_max_calls = 0
    n_skipped_due_to_first_429 = 0
    halt_reason: str | None = None
    for i in range(len(ramp_times_s)):
        # v2.3 microfix 2026-05-30 (auditor finding #4) — Advisory O(1)
        # admission check for probe_max_calls. Mirrors the probe_max_usd
        # pattern exactly: read-only counter check BEFORE
        # ``asyncio.create_task``; NEVER inserts an ``await call()``
        # into the dispatch loop body (FORBIDDEN by spec Microfix B).
        if (
            probe_max_calls is not None
            and n_dispatched_in_ramp >= probe_max_calls
        ):
            if n_skipped_due_to_probe_max_calls == 0:
                logger.warning(
                    "PROBE_CALLS_CAP_HIT cell_idx=%d n_dispatched=%d "
                    "cap=%d / %d ramp_calls",
                    cell_idx, n_dispatched_in_ramp,
                    probe_max_calls, len(ramp_times_s),
                )
                halt_reason = "probe_max_calls_hit"
            n_skipped_due_to_probe_max_calls += 1
            break
        # v2.3 (b) — Advisory O(1) admission check for probe_max_usd.
        # NEVER inserts an await; reads only in-process counters.
        # v2.3 fix loop #5 (auditor BLOCKER 1) — check uses
        # DETERMINISTIC COMMITTED cost, not realized billed cost. Every
        # dispatched call contributes the full per-call rate; 429s,
        # transport errors, and zero-usage stubs DO NOT discount the
        # committed counter (no 429-no-bill bypass; no zero-usage
        # bypass).
        if probe_max_usd is not None:
            projected_after = (
                nonlocal_state["committed_usd"]
                + DETERMINISTIC_PER_CALL_USD
            )
            if projected_after > probe_max_usd:
                if n_skipped_due_to_probe_max_usd == 0:
                    logger.warning(
                        "PROBE_USD_CAP_HIT cell_idx=%d "
                        "committed_usd=%.4f realized_usd=%.4f "
                        "cap=%.4f n_dispatched=%d / %d ramp_calls",
                        cell_idx, nonlocal_state["committed_usd"],
                        nonlocal_state["cell_usd"],
                        probe_max_usd, n_dispatched_in_ramp,
                        len(ramp_times_s),
                    )
                    if halt_reason is None:
                        halt_reason = "probe_max_usd_hit"
                n_skipped_due_to_probe_max_usd += 1
                break
        # v2.3 (a) — Non-blocking stop event for total_max_usd. Checked
        # AT EACH SCHEDULED-DISPATCH-TIME ARRIVAL (before create_task);
        # NOT after each call completion.
        if (
            total_max_usd_stop_event is not None
            and total_max_usd_stop_event.is_set()
        ):
            if n_skipped_due_to_total_max_usd == 0:
                logger.warning(
                    "TOTAL_USD_STOP_EVENT cell_idx=%d cell_usd=%.4f "
                    "n_dispatched=%d / %d ramp_calls",
                    cell_idx, nonlocal_state["cell_usd"],
                    n_dispatched_in_ramp, len(ramp_times_s),
                )
                if halt_reason is None:
                    halt_reason = "total_max_usd_stop_event_set"
            n_skipped_due_to_total_max_usd += 1
            break
        # v2.3 microfix 2026-05-30 fix loop #4 (auditor finding #3) —
        # Non-blocking early-stop event for the FIRST observed 429 on
        # calibration probes (largest/smallest, per
        # `calibration.early_stop_on_first_429_{largest,smallest}`).
        # Mirrors the `total_max_usd_stop_event` pattern verbatim: O(1)
        # read of an `asyncio.Event` BEFORE `asyncio.create_task`. The
        # event is set inside `_admit_and_call` the FIRST time a real
        # 429 (`rate_limited=True`) is observed. Sequential-await
        # dispatch is FORBIDDEN by spec Microfix B — the concurrency
        # invariant from `TestConcurrentDispatchInvariant` is preserved
        # because `create_task` remains the only dispatch path.
        if early_stop_on_first_429 and early_stop_429_event.is_set():
            if n_skipped_due_to_first_429 == 0:
                logger.warning(
                    "FIRST_429_EARLY_STOP cell_idx=%d cell_usd=%.4f "
                    "n_dispatched=%d / %d ramp_calls",
                    cell_idx, nonlocal_state["cell_usd"],
                    n_dispatched_in_ramp, len(ramp_times_s),
                )
                if halt_reason is None:
                    halt_reason = "first_429_observed"
            n_skipped_due_to_first_429 += 1
            break
        # v2.3 fix loop #5 — increment committed-cost counter BEFORE
        # asyncio.create_task so the next iteration's admission check
        # sees the committed cost of the call we're about to dispatch
        # (deterministic + race-free; never depends on _admit_and_call
        # completing first).
        nonlocal_state["committed_usd"] += DETERMINISTIC_PER_CALL_USD
        ramp_tasks.append(
            asyncio.create_task(
                _admit_and_call(
                    ramp_offset + i, ramp_times_s[i], False,
                    intended_iso_ramp[i],
                )
            )
        )
        n_dispatched_in_ramp += 1
    ramp_records_raw = (
        await asyncio.gather(*ramp_tasks) if ramp_tasks else []
    )
    # v2.3 microfix fix loop #4 — filter None sentinels emitted by
    # ramp `_admit_and_call` invocations that returned early due to
    # the first-429 stop event. The records list is the ground truth
    # for both the JSONL output and the aggregate computation, so
    # filtering here keeps the rest of the pipeline simple.
    ramp_records = [r for r in ramp_records_raw if r is not None]
    # v2.3 microfix fix loop #4 — set halt_reason from the early-stop
    # event when it fired during the cell (the dispatch loop's
    # synchronous create_task path may not see the event before all
    # tasks were already dispatched, so we set halt_reason here too —
    # idempotent with the dispatch-loop assignment above, which only
    # fires for the cross-iter case).
    if (
        halt_reason is None
        and early_stop_on_first_429
        and early_stop_429_event.is_set()
    ):
        halt_reason = "first_429_observed"
        logger.warning(
            "FIRST_429_EARLY_STOP_POST_GATHER cell_idx=%d cell_usd=%.4f "
            "n_dispatched=%d n_kept_ramp_records=%d / %d ramp_calls",
            cell_idx, nonlocal_state["cell_usd"], n_dispatched_in_ramp,
            len(ramp_records), len(ramp_times_s),
        )

    # Phase 3: cool-down.
    if cool_down_s > 0 and not dry_run:
        await asyncio.sleep(cool_down_s)

    all_records = list(prewarm_records) + list(ramp_records)
    admitted_sorted = sorted(
        all_records,
        key=lambda r: r.get("admitted_dispatch_cell_elapsed_ms", 0),
    )
    for rec in admitted_sorted:
        out_fh.write(
            json.dumps(rec, sort_keys=True, default=_json_default) + "\n"
        )
    out_fh.flush()

    return (
        admitted_sorted,
        nonlocal_state["cell_usd"],
        nonlocal_state["committed_usd"],
        max_in_flight,
        halt_reason,
    )


# ----------------------------------------------------------------------------
# Async measurement orchestrator
# ----------------------------------------------------------------------------


async def _run_measurement_async(
    *,
    cfg: ExperimentConfig,
    runs_dir: pathlib.Path,
    system_prompt: str,
    user_prompts: list[str],
    git_commit: str,
    dirty: bool,
    pricing: PaygPricing,
    pricing_snapshot_path: str,
    endpoint_value: str,
    deployment: str,
    dry_run: bool,
    stage: str,
    timestamp_label: str,
    run_id_short: str,
    today: datetime.date,
    run_lock_metadata: dict[str, Any] | None,
    source_corpus_sha: str,
    user_prompts_source_sha: str,
    calibration_result_path: str | None = None,
    calibration_result_sha256: str | None = None,
    calibration_run_id_short: str | None = None,
    selected_peak_tps_override: float | None = None,
    smoke_summary_path_for_evidence: str | None = None,
    calibration_selected_via: str | None = None,
    calibration_selected_at_phase: str | None = None,
    calibration_selected_at_bracket_depth: int | None = None,
    calibration_phase_b_concurrency: int | None = None,
    calibration_selected_bracket_root_phase: str | None = None,
    # v2.4 NEW (optional kwargs so v2.1/v2.3 call sites keep working).
    # When provided, the v2.4 empirical-promotion gate runs as §6
    # preflight chain step 5 (before the v2.1 TPM-feasibility gate).
    calibration_result_data: dict[str, Any] | None = None,
    smoke_summary_data: dict[str, Any] | None = None,
    smoke_summary_sha256_for_evidence: str | None = None,
    v24_now_provider: Callable[[], datetime.datetime] | None = None,
    v24_terminal_report_lists_calibration_sha_payg_not_ptu: bool = False,
    v24_repo_root: pathlib.Path | None = None,
    pricing_policy_provenance: dict[str, Any] | None = None,
) -> MeasurementResult:
    sweep_vals = list(cfg.sweep.max_output_tokens)
    if stage == "smoke":
        ramp_duration_s = float(cfg.runtime.smoke_ramp_duration_seconds)
        cool_down_s = float(cfg.runtime.smoke_cool_down_seconds)
        hard_ceiling = cfg.budget.smoke_hard_ceiling_usd
    else:
        ramp_duration_s = float(cfg.runtime.ramp_duration_seconds)
        cool_down_s = float(cfg.runtime.cool_down_seconds)
        hard_ceiling = cfg.budget.evidence_hard_ceiling_usd
    # v2.2.1 — selected_peak_tps from Stage 0.5 calibration supersedes
    # the YAML-pinned runtime.peak_ramp_tps. Dry-run keeps the YAML
    # value (no calibration required); smoke/evidence ALWAYS use the
    # calibration-supplied TPS when present.
    peak_ramp_tps = (
        float(selected_peak_tps_override)
        if selected_peak_tps_override is not None
        else cfg.runtime.peak_ramp_tps
    )
    prewarm_tps = cfg.runtime.prewarm_tps
    prewarm_calls = cfg.runtime.prewarm_calls_per_cell

    # ---- USD preflight gate (v2.3 — evaluated BEFORE the v2.1 TPM
    # feasibility gate for smoke/evidence so a high `selected_peak_tps`
    # from Phase B / bracket calibration surfaces the documented
    # stage-specific reason
    # (`smoke_preflight_over_ceiling_narrow_sweep_or_file_new_spec`,
    # `evidence_preflight_over_ceiling_narrow_sweep_or_file_new_spec`)
    # rather than the v2.1 generic TPM-feasibility abort. Dry-run keeps
    # the v2.1 ordering (TPM first) since it has no calibration-derived
    # TPS.) ----
    #
    # v2.3 microfix 2026-05-30 fix loop #4 (auditor finding #1) — for
    # smoke/evidence stages the preflight MUST use the deterministic
    # conservative estimator at $0.009/call (NO 429-no-bill discount,
    # NO cached-token discount, NO pricing assumptions). The pricing-
    # driven `compute_projected_usd` is preserved for the dry-run path
    # only (no calibration-supplied TPS in dry-run). The active
    # thresholds (smoke TPS≥12 aborts; evidence TPS≥5 aborts; lower
    # TPS values pass) are numerically reproducible from the estimator
    # alone — no monkeypatch required for the regression tests.
    avg_tps = (prewarm_tps + peak_ramp_tps) / 2.0
    ramp_calls_est = int(ramp_duration_s * avg_tps)
    calls_per_cell_est = prewarm_calls + ramp_calls_est
    if stage in ("smoke", "evidence"):
        projected_usd = deterministic_conservative_cost_estimator(
            stage=stage,
            peak_tps=peak_ramp_tps,
            n_cells=len(sweep_vals),
            prewarm_calls_per_cell=prewarm_calls,
            prewarm_tps=prewarm_tps,
            ramp_duration_s=ramp_duration_s,
            cool_down_seconds=int(cool_down_s),
        )
    else:
        projected_usd = compute_projected_usd(
            sweep=sweep_vals,
            calls_per_cell=calls_per_cell_est,
            pricing=pricing,
            model=cfg.deployment.family,
            input_tokens=float(BASE_PROMPT_TOKENS_FOR_GATE),
            output_tokens=float(EVIDENCE_PROJECTED_OUTPUT_TOKENS),
            cached_fraction=EVIDENCE_CACHED_FRACTION,
        )
    preflight_threshold = 0.9 * hard_ceiling
    midrun_threshold = 0.85 * hard_ceiling
    logger.info(
        "BUDGET_PREFLIGHT stage=%s cells=%d calls_per_cell~=%d "
        "projected_usd=%.4f hard_ceiling=%.4f preflight_threshold=%.4f "
        "midrun_threshold=%.4f estimator=%s",
        stage, len(sweep_vals), calls_per_cell_est,
        projected_usd, hard_ceiling, preflight_threshold, midrun_threshold,
        "deterministic_conservative" if stage in ("smoke", "evidence")
        else "pricing_driven",
    )
    if projected_usd > preflight_threshold and stage in ("smoke", "evidence"):
        stage_reason = (
            "smoke_preflight_over_ceiling_narrow_sweep_or_file_new_spec"
            if stage == "smoke"
            else "evidence_preflight_over_ceiling_narrow_sweep_or_file_new_spec"
        )
        raise PreflightBudgetAbortError(
            f"projected_usd ${projected_usd:.4f} > 0.9 × hard_ceiling "
            f"${preflight_threshold:.4f} (stage={stage}, "
            f"selected_peak_tps={peak_ramp_tps}, cells="
            f"{len(sweep_vals)}, calls_per_cell~={calls_per_cell_est}, "
            f"estimator=deterministic_conservative). "
            f"Narrow the sweep or file a new task spec — v2.3 does NOT "
            f"raise the cap from a flag.",
            reason=stage_reason,
        )

    # ---- v2.4 §6 preflight chain step 5 — empirical-promotion gate ----
    # Runs BEFORE the v2.1 TPM-feasibility preflight for smoke/evidence
    # stages that loaded a v2.3 calibration result. When the gate admits
    # (warm-cache OR mini-probe-revalidated path), the v2.1 cold-cache
    # preflight is SKIPPED. When the gate denies AND the v2.1 cold-cache
    # fallback also denies, the runner raises ``TpmFeasibilityAbortError``
    # carrying the §8 ``empirical_promotion_denied_reason`` so the outer
    # ``main()`` handler can write the §9.4 abort envelope. v2.1/v2.3 call
    # sites without ``calibration_result_data`` keep the legacy v2.1
    # cold-cache-only behaviour.
    v24_decision: EmpiricalPromotionDecision | None = None
    _v24_now = v24_now_provider or (
        lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    _v24_repo_root = v24_repo_root or pathlib.Path(__file__).resolve().parents[1]
    if (
        stage in ("smoke", "evidence")
        and not dry_run
        and calibration_result_data is not None
        and len(sweep_vals) >= 2
    ):
        smallest_cell_max_output_tokens = sweep_vals[0]
        largest_cell_max_output_tokens = sweep_vals[-1]
        smoke_yaml_metadata = dict(cfg.metadata or {})
        smoke_runner_resolved = {
            "deployment_used": deployment,
            "model": cfg.deployment.family,
            "api_version": cfg.client.api_version,
            "pricing_snapshot_path": pricing_snapshot_path,
            "pricing_accessed_date": (
                pricing.accessed_date
                if hasattr(pricing, "accessed_date")
                and pricing.accessed_date is not None
                else None
            ),
        }
        ep_config = cfg.runtime.empirical_promotion.to_config()
        payg_admissible = resolve_pricing_snapshot_payg_admissibility(
            calibration_pricing_snapshot_path=(
                calibration_result_data.get("pricing_snapshot_path")
            ),
            calibration_pricing_accessed_date=(
                calibration_result_data.get("pricing_accessed_date")
            ),
            repo_root=_v24_repo_root,
        )
        # ---- v2.4 §7 — mini-probe production surface (opt-in) ----
        # When the YAML enables the mini-probe AND the calibration may
        # be stale (computing age upfront so we don't waste a $1 probe
        # on fresh calibrations), eagerly execute one async mini-probe
        # BEFORE invoking the (sync) gate, then expose its cached result
        # via a single-shot closure. Defensive: the closure tracks
        # invocations and raises
        # ``MiniProbeAttemptedMoreThanOncePerRunError`` if the gate
        # somehow re-invokes it (the gate itself also enforces the cap,
        # so this is belt-and-suspenders).
        _mini_probe_callable: Callable[[], dict[str, Any]] | None = None
        _mini_probe_done_result: dict[str, Any] | None = None
        if (
            ep_config.mini_probe_enabled
            and calibration_result_data.get("completed_at_iso")
        ):
            cal_completed_iso = calibration_result_data["completed_at_iso"]
            cal_dt = _parse_iso8601(cal_completed_iso)
            if cal_dt.tzinfo is None:
                cal_dt = cal_dt.replace(tzinfo=datetime.timezone.utc)
            now_dt = _v24_now()
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=datetime.timezone.utc)
            age_h = (now_dt - cal_dt).total_seconds() / 3600.0
            if age_h > ep_config.calibration_max_age_hours:
                # Open the JSONL once for the mini-probe records (the
                # main measurement JSONL hasn't been opened yet — that
                # happens below inside the `try:` block).
                mp_jsonl = (
                    runs_dir
                    / f"{timestamp_label}_{cfg.experiment_id}_"
                      f"{'dry-run' if dry_run else stage}.mini_probe.jsonl"
                )
                runs_dir.mkdir(parents=True, exist_ok=True)
                mp_client, mp_credential = _build_live_client(
                    endpoint_value=endpoint_value,
                    max_retries=cfg.client.max_retries,
                )
                try:
                    await _preflight_reachability(
                        client=mp_client, deployment=deployment,
                    )
                    with mp_jsonl.open("w", encoding="utf-8") as mp_fh:
                        _mini_probe_done_result = await _run_mini_probe_async(
                            cfg=cfg,
                            runs_dir=runs_dir,
                            timestamp_label=timestamp_label,
                            stage=stage,
                            run_id_short=run_id_short,
                            client=mp_client,
                            deployment=deployment,
                            system_prompt=system_prompt,
                            user_prompts=user_prompts,
                            git_commit=git_commit,
                            dirty=dirty,
                            system_sha=_sha256_text(system_prompt),
                            source_corpus_sha=source_corpus_sha,
                            user_prompts_source_sha=user_prompts_source_sha,
                            pricing_snapshot_path=pricing_snapshot_path,
                            pricing=pricing,
                            selected_peak_tps=peak_ramp_tps,
                            calibration_result_sha256=(
                                calibration_result_sha256 or ""
                            ),
                            sim_started_mono=time.monotonic(),
                            out_fh=mp_fh,
                            global_request_offset=0,
                        )
                finally:
                    await _aclose_quiet(mp_client)
                    await _aclose_quiet(mp_credential)

                _mp_calls = {"n": 0}
                _captured = _mini_probe_done_result

                def _mp_callable() -> dict[str, Any]:
                    _mp_calls["n"] += 1
                    if _mp_calls["n"] > 1:
                        raise MiniProbeAttemptedMoreThanOncePerRunError(
                            "mini_probe_attempted_more_than_once_per_run"
                        )
                    return _captured  # already computed eagerly

                _mini_probe_callable = _mp_callable
        v24_decision = evaluate_empirical_promotion_gate(
            calibration_result=calibration_result_data,
            smoke_yaml_metadata=smoke_yaml_metadata,
            smoke_runner_resolved=smoke_runner_resolved,
            config=ep_config,
            deployment_tpm_quota=cfg.deployment_tpm_quota,
            base_prompt_tokens_for_gate=BASE_PROMPT_TOKENS_FOR_GATE,
            smallest_cell_max_output_tokens=smallest_cell_max_output_tokens,
            largest_cell_max_output_tokens=largest_cell_max_output_tokens,
            now_provider=_v24_now,
            pricing_snapshot_path_resolves_committed_payg=payg_admissible,
            terminal_report_lists_calibration_sha_payg_not_ptu=(
                v24_terminal_report_lists_calibration_sha_payg_not_ptu
            ),
            mini_probe_callable=_mini_probe_callable,
            mini_probe_attempts_so_far=0,
        )
        logger.info(
            "EMPIRICAL_PROMOTION_GATE stage=%s path=%s formula=%s "
            "denied_reason=%s admits=%s",
            stage,
            v24_decision.promotion_path,
            v24_decision.largest_cell_projection_formula,
            v24_decision.empirical_denied_reason,
            v24_decision.smoke_promotion_admits,
        )
        if (
            v24_decision.promotion_path
            != PROMOTION_PATH_COLD_CACHE_STRICT
            and v24_decision.smoke_promotion_admits
        ):
            # Empirical-promotion-aware admission supersedes the v2.1
            # cold-cache preflight; skip the legacy raise.
            pass
        elif not v24_decision.smoke_promotion_admits:
            raise TpmFeasibilityAbortError(
                f"v2.4 empirical-promotion gate denied "
                f"(empirical_promotion_denied_reason="
                f"{v24_decision.empirical_denied_reason!r}); cold-cache "
                f"fallback also denied "
                f"(smallest_tpm={v24_decision.cold_cache_smallest_tpm:.1f}, "
                f"largest_tpm={v24_decision.cold_cache_largest_tpm:.1f}, "
                f"failure_reason="
                f"{v24_decision.feasibility_failure_reason!r}).",
                v24_empirical_denied_reason=(
                    v24_decision.empirical_denied_reason
                    or EMPIRICAL_PROMOTION_DISABLED_OUTCOME_NOT_SELECTED
                ),
                v24_stage=stage,
            )
        # else: empirical denied, cold-cache admits → fall through to the
        # v2.1 cold-cache preflight which will admit; promotion_path
        # remains cold_cache_strict.

    # ---- TPM feasibility preflight (v2.1 blocker #4 canonical formula) ----
    projections: list[tuple[int, float]] = []
    for mo in sweep_vals:
        projected_tpm = compute_projected_tpm_cell(
            peak_ramp_tps=peak_ramp_tps,
            base_prompt_tokens_for_gate=BASE_PROMPT_TOKENS_FOR_GATE,
            max_output_tokens=mo,
        )
        projections.append((mo, projected_tpm))
    smallest_mo, smallest_tpm = projections[0]
    largest_mo, largest_tpm = projections[-1]
    lower_threshold = TPM_LOWER_GATE_FRACTION * cfg.deployment_tpm_quota
    upper_threshold = TPM_UPPER_GATE_FRACTION * cfg.deployment_tpm_quota
    logger.info(
        "TPM_FEASIBILITY_PREFLIGHT smallest_mo=%d smallest_tpm=%.1f "
        "lower_threshold=%.1f largest_mo=%d largest_tpm=%.1f "
        "upper_threshold=%.1f quota=%d peak_tps=%.3f base_tokens=%d",
        smallest_mo, smallest_tpm, lower_threshold,
        largest_mo, largest_tpm, upper_threshold,
        cfg.deployment_tpm_quota, peak_ramp_tps,
        BASE_PROMPT_TOKENS_FOR_GATE,
    )
    # v2.4 — when the gate has admitted under a warm/mini-probe path, the
    # v2.1 cold-cache preflight raise is bypassed (the empirical
    # observation supersedes the cold-cache pessimism for the smallest
    # cell; the largest-cell upper bound is preserved by the gate's own
    # warm-projection arithmetic). Cold-cache-strict path keeps the
    # legacy v2.1 raise verbatim.
    skip_cold_cache_raise = (
        v24_decision is not None
        and v24_decision.promotion_path != PROMOTION_PATH_COLD_CACHE_STRICT
        and v24_decision.smoke_promotion_admits
    )
    if smallest_tpm > lower_threshold and not skip_cold_cache_raise:
        raise TpmFeasibilityAbortError(
            f"smallest cell (max_output_tokens={smallest_mo}) projects "
            f"{smallest_tpm:.1f} TPM > "
            f"{TPM_LOWER_GATE_FRACTION:.2f} × quota="
            f"{cfg.deployment_tpm_quota} (= {lower_threshold:.1f}); the "
            f"smallest cell would 429 at peak ramp, eliminating signal "
            f"contrast. Reduce runtime.peak_ramp_tps OR raise the "
            f"smallest sweep cell. Task 019 v2.1 pinned peak=0.33."
        )
    if largest_tpm < upper_threshold and not skip_cold_cache_raise:
        raise TpmFeasibilityAbortError(
            f"largest cell (max_output_tokens={largest_mo}) projects "
            f"{largest_tpm:.1f} TPM < "
            f"{TPM_UPPER_GATE_FRACTION:.2f} × quota="
            f"{cfg.deployment_tpm_quota} (= {upper_threshold:.1f}); the "
            f"largest cell would NOT 429, eliminating signal contrast. "
            f"Raise runtime.peak_ramp_tps OR raise the largest sweep cell."
        )

    # ---- USD preflight gate (dry-run path; smoke/evidence handled above) ----
    if projected_usd > preflight_threshold:
        raise PreflightBudgetAbortError(
            f"projected_usd ${projected_usd:.4f} > 0.9 × hard_ceiling "
            f"${preflight_threshold:.4f} (stage={stage}, cells="
            f"{len(sweep_vals)}, calls_per_cell~={calls_per_cell_est}). "
            f"Narrow the sweep or raise the hard ceiling in the YAML "
            f"(but NEVER lower peak_ramp_tps, concurrency, reasoning.effort, "
            f"sdk max_retries, prewarm_calls_per_cell, or api_version — "
            f"those are Task 019 v2.1 pinned controls)."
        )

    system_sha = _sha256_text(system_prompt)
    file_stage = "dry-run" if dry_run else stage
    jsonl_path = (
        runs_dir / f"{timestamp_label}_{cfg.experiment_id}_{file_stage}.jsonl"
    )
    summary_path = pathlib.Path(str(jsonl_path) + ".summary.json")
    if jsonl_path.exists():
        raise FileExistsError(
            f"JSONL target already exists: {jsonl_path} (append-only)"
        )
    runs_dir.mkdir(parents=True, exist_ok=True)

    client: Any = None
    credential: Any = None
    if not dry_run:
        client, credential = _build_live_client(
            endpoint_value=endpoint_value,
            max_retries=cfg.client.max_retries,
        )
        await _preflight_reachability(client=client, deployment=deployment)

    sim_started_mono = time.monotonic()
    total_usd = 0.0
    total_committed_usd = 0.0
    cell_summaries: list[dict[str, Any]] = []
    halt_reason: str | None = None
    global_request_offset = 0

    try:
        with jsonl_path.open("w", encoding="utf-8") as out_fh:
            for cell_idx, cell_mo in enumerate(sweep_vals):
                logger.info(
                    "CELL_BEGIN cell_idx=%d max_output_tokens=%d "
                    "prewarm_calls=%d ramp_duration_s=%.0f peak_tps=%.3f",
                    cell_idx, cell_mo, prewarm_calls, ramp_duration_s,
                    peak_ramp_tps,
                )
                (
                    cell_records,
                    cell_usd,
                    cell_committed_usd,
                    cell_max_in_flight,
                    _cell_halt_reason,
                ) = await _run_cell(
                    cfg=cfg,
                    cell_idx=cell_idx,
                    cell_max_output_tokens=cell_mo,
                    prewarm_calls=prewarm_calls,
                    prewarm_tps=prewarm_tps,
                    ramp_duration_s=ramp_duration_s,
                    peak_ramp_tps=peak_ramp_tps,
                    cool_down_s=cool_down_s,
                    concurrency=cfg.runtime.concurrency,
                    client=client,
                    deployment=deployment,
                    system_prompt=system_prompt,
                    user_prompts=user_prompts,
                    git_commit=git_commit,
                    dirty=dirty,
                    system_sha=system_sha,
                    user_prompts_source_sha=user_prompts_source_sha,
                    source_corpus_sha=source_corpus_sha,
                    pricing_snapshot_path=pricing_snapshot_path,
                    pricing=pricing,
                    dry_run=dry_run,
                    out_fh=out_fh,
                    global_request_offset=global_request_offset,
                    sim_started_mono=sim_started_mono,
                    run_id_short=run_id_short,
                )
                global_request_offset += len(cell_records)
                total_usd += cell_usd
                total_committed_usd += cell_committed_usd
                agg = _aggregate_cell(
                    cell_records, cell_max_output_tokens=cell_mo
                )
                agg["max_in_flight_observed"] = cell_max_in_flight
                # v2.3 fix loop #5 (auditor BLOCKER 3) — propagate the
                # admitted-pressure result and the "first-429 metadata
                # present" flag into per-cell smoke/evidence summaries
                # so the v2.3 measurement_summary schema can echo them
                # without re-computing from the raw JSONL.
                ap_block = compute_admitted_pressure_block(
                    admitted_dispatch_iso_list=[
                        r.get("admitted_dispatch_iso")
                        for r in cell_records
                        if not r.get("is_prewarm", False)
                        and r.get("admitted_dispatch_iso")
                    ],
                    candidate_tps=peak_ramp_tps,
                    probe_window_end_iso=compute_probe_window_end_iso(
                        cell_records=cell_records,
                        fallback_now_iso=_iso8601_z(_utc_now()),
                    ),
                    window_s=ADMITTED_PRESSURE_WINDOW_S,
                    floor_ratio=ADMITTED_PRESSURE_FLOOR_RATIO,
                    observed_n_429=int(agg.get("n_429_records", 0) or 0),
                )
                agg["admitted_pressure"] = ap_block
                agg["admitted_pressure_passed"] = bool(
                    ap_block.get("admitted_pressure_passed", False)
                )
                agg["first_429_metadata_present"] = bool(
                    int(agg.get("n_429_records", 0) or 0) >= 1
                )
                cell_summary = {
                    "cell_idx": cell_idx,
                    "max_output_tokens": cell_mo,
                    "calls_in_cell": len(cell_records),
                    "cell_usd": round(cell_usd, 6),
                    "cell_committed_usd": round(cell_committed_usd, 6),
                    "projected_tpm": round(projections[cell_idx][1], 2),
                    "prompt_cache_key_namespace": build_prompt_cache_key(
                        run_id_short=run_id_short, max_output_tokens=cell_mo,
                    ),
                    **agg,
                }
                cell_summaries.append(cell_summary)
                logger.info(
                    "CELL_END cell_idx=%d max_output_tokens=%d records=%d "
                    "cell_usd=%.4f cell_committed_usd=%.4f "
                    "cumulative_usd=%.4f cumulative_committed_usd=%.4f "
                    "cache_hit_steady=%.4f ttft_p95_steady=%.1f "
                    "n_429=%d first_429_rpm=%s warm=%s backlog_excess=%s",
                    cell_idx, cell_mo, len(cell_records), cell_usd,
                    cell_committed_usd, total_usd, total_committed_usd,
                    agg["cache_hit_ratio_steady_state"],
                    agg["first_token_latency_ms_p95_steady_state"],
                    agg["n_429_records"],
                    agg["first_429_arrival_rpm"],
                    agg["warm_criterion_passed"],
                    agg["backlog_excessive"],
                )
                # ---- Mid-run budget gate ----
                # v2.3 fix loop #5 (auditor BLOCKER 1) — the mid-run
                # halt is keyed off DETERMINISTIC COMMITTED spend, not
                # realized billed cost. Committed cost ≥ realized for
                # every cell (429s/transport errors count for the full
                # per-call rate), so the committed counter is the
                # source-of-truth guardrail.
                if total_committed_usd > midrun_threshold:
                    halt_reason = "midrun_budget_gate"
                    logger.warning(
                        "MIDRUN_BUDGET_HALT committed_usd=%.4f "
                        "realized_usd=%.4f > 0.85 × hard_ceiling=%.4f; "
                        "halting cleanly after cell_idx=%d (next cell "
                        "NOT started)",
                        total_committed_usd, total_usd, midrun_threshold,
                        cell_idx,
                    )
                    break

        partial = (
            halt_reason is not None or len(cell_summaries) < len(sweep_vals)
        )

        # ---- Summary JSON ----
        citations = CitationsBuilder(
            pricing_path=cfg.pricing_snapshot_path,
            pricing_source_url=pricing.source_url,
            pricing_accessed_date=pricing.accessed_date,
        ).to_dict()
        pinned_confounds_echo = {
            "reasoning_effort": cfg.request_template.reasoning_effort,
            "api_version": cfg.client.api_version,
            "sdk_max_retries": cfg.client.max_retries,
            "concurrency": cfg.runtime.concurrency,
            "peak_ramp_tps": cfg.runtime.peak_ramp_tps,
            "prewarm_tps": cfg.runtime.prewarm_tps,
            "prewarm_calls_per_cell": cfg.runtime.prewarm_calls_per_cell,
            "dispatcher": cfg.runtime.dispatcher,
            "deployment_tpm_quota": cfg.deployment_tpm_quota,
            "base_prompt_tokens_for_gate": BASE_PROMPT_TOKENS_FOR_GATE,
            "assembled_system_prompt_sha256":
                EXPECTED_ASSEMBLED_SYSTEM_PROMPT_SHA256,
            "source_corpus_sha256": source_corpus_sha,
            "user_prompts_source_sha256": user_prompts_source_sha,
            "user_prompts_index_set": list(USER_PROMPTS_INDEX_SET),
            "sweep_max_output_tokens": list(sweep_vals),
            "ptu_evidence": False,
        }
        backlog_excessive_any = any(
            bool(c.get("backlog_excessive", False)) for c in cell_summaries
        )
        cache_not_warm_any = any(
            bool(c.get("cache_not_warm", False)) for c in cell_summaries
        )
        max_in_flight_observed_run = max(
            (int(c.get("max_in_flight_observed", 0) or 0) for c in cell_summaries),
            default=0,
        )
        onset_per_cell = {
            str(c.get("max_output_tokens")): c.get("first_429_arrival_rpm")
            for c in cell_summaries
        }
        # v2.1 protocol-correction: surface n_429 as a sibling of
        # first_429_arrival_rpm so downstream readers (analyze script,
        # smoke-gate evaluator) never confuse "no 429 yet" (rpm=None) with
        # "count missing" (also None). A count is always a non-negative int.
        n429_per_cell = {
            str(c.get("max_output_tokens")): int(c.get("n_429_records", 0) or 0)
            for c in cell_summaries
        }
        tpm_block = {
            "formula": (
                "60 * peak_ramp_tps * (base_prompt_tokens_for_gate + "
                "max_output_tokens)"
            ),
            "peak_ramp_tps": peak_ramp_tps,
            "base_prompt_tokens_for_gate": BASE_PROMPT_TOKENS_FOR_GATE,
            "deployment_tpm_quota": cfg.deployment_tpm_quota,
            "lower_gate_fraction": TPM_LOWER_GATE_FRACTION,
            "upper_gate_fraction": TPM_UPPER_GATE_FRACTION,
            "lower_threshold": round(lower_threshold, 2),
            "upper_threshold": round(upper_threshold, 2),
            "per_cell_projections": [
                {"max_output_tokens": mo, "projected_tpm": round(tpm, 2)}
                for mo, tpm in projections
            ],
            "passed": True,
        }
        summary: dict[str, Any] = {
            "experiment_id": cfg.experiment_id,
            "benchmark": cfg.benchmark,
            "stage": stage,
            "dry_run": dry_run,
            "partial": partial,
            "halt_reason": halt_reason,
            "git_commit": git_commit,
            "dirty": dirty,
            "system_prompt_sha256": system_sha,
            "user_prompts_source_sha256": user_prompts_source_sha,
            "source_corpus_sha256": source_corpus_sha,
            "deployment_used": deployment,
            "deployment_env": cfg.deployment.deployment_env,
            "endpoint_env": cfg.deployment.endpoint_env,
            "api_version": cfg.client.api_version,
            "sdk_max_retries": cfg.client.max_retries,
            "model": cfg.deployment.family,
            "pricing_snapshot_path": cfg.pricing_snapshot_path,
            "pricing_source_url": pricing.source_url,
            "pricing_accessed_date": pricing.accessed_date,
            "pricing_policy": pricing_policy_provenance or {},
            "projected_usd": round(projected_usd, 6),
            "hard_ceiling_usd": hard_ceiling,
            "preflight_threshold_usd": round(preflight_threshold, 6),
            "midrun_threshold_usd": round(midrun_threshold, 6),
            "total_usd": round(total_usd, 6),
            "cells_planned": len(sweep_vals),
            "cells_completed": len(cell_summaries),
            "calls_per_cell_estimated": calls_per_cell_est,
            "sweep_planned": sweep_vals,
            "run_id_short": run_id_short,
            "pinned_confounds_echo": pinned_confounds_echo,
            "metadata": dict(cfg.metadata),
            "tpm_feasibility": tpm_block,
            "backlog_excessive_any": backlog_excessive_any,
            "cache_not_warm_any": cache_not_warm_any,
            "max_in_flight_observed_run": max_in_flight_observed_run,
            "first_429_arrival_rpm_per_cell": onset_per_cell,
            "n_429_records_per_cell": n429_per_cell,
            "smoke_gate": (
                evaluate_smoke_gate_block(
                    cell_summaries=cell_summaries,
                    sweep_planned=list(sweep_vals),
                )
                if stage == "smoke"
                else None
            ),
            "evidence_429_contrast_gate": (
                evaluate_smoke_gate_block(
                    cell_summaries=cell_summaries,
                    sweep_planned=list(sweep_vals),
                )
                if stage == "evidence"
                else None
            ),
            "run_lock_metadata": run_lock_metadata,
            "citations": citations,
            "cell_summaries": cell_summaries,
            "jsonl_path": str(jsonl_path),
            # v2.2.1 durable linkage echo (NEVER includes the smoke
            # summary's own sha256 — that lives in the sibling .sha256
            # sidecar only).
            "selected_peak_tps": (
                float(selected_peak_tps_override)
                if selected_peak_tps_override is not None
                else None
            ),
            "calibration_result_path": calibration_result_path,
            "calibration_result_sha256": calibration_result_sha256,
            "calibration_run_id_short": calibration_run_id_short,
            "smoke_summary_path": smoke_summary_path_for_evidence,
            # v2.3 fix loop #5 (auditor BLOCKER 3) — propagate the
            # calibration's two-phase / bracket selection metadata into
            # smoke + evidence summaries so downstream linkage validation
            # (validate_smoke_summary) can enforce
            # `smoke_selected_at_phase_mismatches_calibration` and so
            # readers of the smoke/evidence summary can see which phase
            # / concurrency override / bracket depth produced the
            # selected_peak_tps without re-reading the calibration_result.
            "selected_via": calibration_selected_via,
            "selected_at_phase": calibration_selected_at_phase,
            # v2.3 fix loop #6 (auditor BLOCKER 1) — root phase for
            # bracket selections, propagated from calibration so the
            # smoke/evidence summary preserves the parent-grid lineage
            # without overloading selected_at_phase.
            "selected_bracket_root_phase": (
                calibration_selected_bracket_root_phase
            ),
            "selected_at_bracket_depth": (
                calibration_selected_at_bracket_depth
            ),
            # v2.3 fix loop #6 (auditor BLOCKER 2) — BOOL (per spec).
            # True when the selection path used the Phase B concurrency
            # override: either a Phase B grid selection
            # (selected_at_phase == "B") OR a bracket-search selection
            # rooted in Phase B (selected_at_phase == "bracket" AND
            # selected_bracket_root_phase == "B"). False for Phase A
            # selections and for bracket selections rooted in Phase A.
            # The actual integer concurrency value is recorded
            # separately under phase_b_concurrency_value (audit-only;
            # null when the override was not exercised).
            "phase_b_concurrency_used": bool(
                calibration_selected_at_phase == "B"
                or (
                    calibration_selected_at_phase == "bracket"
                    and calibration_selected_bracket_root_phase == "B"
                )
            ),
            "phase_b_concurrency_value": (
                calibration_phase_b_concurrency
                if (
                    calibration_selected_at_phase == "B"
                    or (
                        calibration_selected_at_phase == "bracket"
                        and (
                            calibration_selected_bracket_root_phase
                            == "B"
                        )
                    )
                )
                else None
            ),
            "total_committed_usd": round(total_committed_usd, 6),
            "completed_at_iso": _iso8601_z(_utc_now()),
            "schema_version": "task019.v2.3.measurement_summary",
        }
        # ---- v2.4 §9.1 / §9.3 — overlay admitted-summary fields ----
        # When the v2.4 empirical-promotion gate has been evaluated
        # (calibration_result_data was supplied AND stage is smoke or
        # evidence AND not dry_run) AND the gate admitted on any path
        # (warm / mini-probe / cold-cache-strict fallback), upgrade the
        # summary to the v2.4 admitted schema. The v2.4 builder writes
        # the path-conditional null discipline per §9.1; the v2.4
        # validators verify required-field presence and reject the
        # forbidden-field families. Existing v2.3 operational keys
        # (cell summaries, JSONL path, pinned-confounds echo, etc.) are
        # PRESERVED so downstream v2.3 consumers continue to work; the
        # bump is strictly additive at the schema-version literal +
        # named-field level.
        if (
            v24_decision is not None
            and v24_decision.smoke_promotion_admits
            and calibration_result_data is not None
            and calibration_result_path is not None
            and calibration_result_sha256 is not None
        ):
            completed_at_iso_for_age = (
                calibration_result_data.get("completed_at_iso") or ""
            )
            try:
                apply_v24_admitted_summary_fields(
                    base_summary=summary,
                    decision=v24_decision,
                    calibration_result=calibration_result_data,
                    calibration_result_path=calibration_result_path,
                    calibration_result_sha256=calibration_result_sha256,
                    completed_at_iso_for_age=completed_at_iso_for_age,
                    now_provider=_v24_now,
                    stage=stage,
                    smoke_summary_dict_for_evidence=(
                        smoke_summary_data if stage == "evidence" else None
                    ),
                    smoke_summary_path_for_evidence=(
                        smoke_summary_path_for_evidence
                        if stage == "evidence"
                        else None
                    ),
                    smoke_summary_sha256_for_evidence=(
                        smoke_summary_sha256_for_evidence
                        if stage == "evidence"
                        else None
                    ),
                )
                # Defensive validation BEFORE writing — catches bugs in
                # the overlay logic at runner integration time instead of
                # producing a malformed artifact on disk.
                if stage == "smoke":
                    validate_smoke_summary_v24(summary)
                else:
                    validate_evidence_summary_v24(summary)
                logger.info(
                    "V24_ADMITTED_SUMMARY_OVERLAY stage=%s schema=%s "
                    "path=%s formula=%s",
                    stage,
                    summary["schema_version"],
                    summary["tpm_feasibility_promotion_path"],
                    summary["largest_cell_projection_formula"],
                )
            except Exception as exc:
                # Hard-fail rather than silently writing a v2.3 summary
                # under what was supposed to be a v2.4 admit — that
                # would constitute a schema-mismatch audit defect.
                raise RuntimeError(
                    f"v2.4 admitted-summary overlay failed for stage="
                    f"{stage!r}: {exc!r}"
                ) from exc
        with summary_path.open("w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, sort_keys=True, default=_json_default)
        # v2.2.1 — smoke summary's own sha256 lives ONLY in a sibling
        # .sha256 sidecar (NEVER inside the smoke summary itself). The
        # evidence runner reads this sidecar via validate_smoke_summary().
        if stage == "smoke" and not dry_run:
            sidecar = write_smoke_summary_sidecar_sha256(summary_path)
            logger.info(
                "SMOKE_SUMMARY_SIDECAR_WRITTEN path=%s sha256=%s",
                sidecar, sidecar.read_text(encoding="utf-8").strip(),
            )
        if partial:
            partial_path = pathlib.Path(
                str(jsonl_path).replace(".jsonl", ".partial.summary.json")
            )
            with partial_path.open("w", encoding="utf-8") as fh:
                json.dump(
                    summary, fh, indent=2, sort_keys=True, default=_json_default
                )
            logger.info("PARTIAL_SUMMARY written=%s", partial_path)

        gate_block = (
            summary.get("smoke_gate")
            if stage == "smoke"
            else summary.get("evidence_429_contrast_gate")
        )
        if gate_block is not None:
            logger.info(
                "GATE_VERDICT stage=%s passed=%s reason=%s "
                "largest_cell=%s n_429_largest=%s smallest_cell=%s "
                "n_429_smallest=%s stage2_promotable=%s",
                stage,
                gate_block["passed"],
                gate_block["reason"],
                gate_block["largest_cell_max_output_tokens"],
                gate_block["largest_cell_n_429"],
                gate_block["smallest_cell_max_output_tokens"],
                gate_block["smallest_cell_n_429"],
                gate_block["stage2_promotable"],
            )

        return MeasurementResult(
            cells_completed=len(cell_summaries),
            cells_planned=len(sweep_vals),
            total_usd=total_usd,
            jsonl_path=jsonl_path,
            summary_path=summary_path,
            partial=partial,
            halt_reason=halt_reason,
            cell_summaries=cell_summaries,
            run_lock_metadata=run_lock_metadata,
            total_committed_usd=total_committed_usd,
        )
    finally:
        await _aclose_quiet(client)
        await _aclose_quiet(credential)



# ----------------------------------------------------------------------------
# Synchronous wrapper — orchestrates run-lock + prompt-identity + asyncio.run
# ----------------------------------------------------------------------------


def run_measurement(
    *,
    cfg: ExperimentConfig,
    benchmarks_root: pathlib.Path,
    dry_run: bool,
    stage: str,
    allow_dirty: bool,
    env: dict[str, str] | None = None,
    today: datetime.date | None = None,
    pricing_policy: str = LIVE_MEASUREMENT,
    run_id_short_override: str | None = None,
    timestamp_label_override: str | None = None,
    calibration_result_path: str | None = None,
    smoke_summary_path: str | None = None,
) -> MeasurementResult:
    """Synchronous wrapper. Acquires the run-lock, verifies prompt-
    identity, then dispatches to the async runner.

    v2.2.1 additions:

    - ``calibration_result_path``: REQUIRED for smoke + evidence (not
      dry-run). The CLI ``main`` already short-circuits to exit 9 when
      missing; this is a defensive second check that also runs in unit
      tests.
    - ``smoke_summary_path``: REQUIRED for evidence (not smoke).
    - Refuses dirty working tree unless ``allow_dirty=True`` (preserved
      v2.1 behaviour).

    The run-lock is held for the entire duration; the OS will release the
    flock when the fd is closed (including on hard crash). The lock
    metadata is echoed into ``runs/*.summary.json``.
    """
    src_env = env if env is not None else dict(os.environ)
    today_date = today if today is not None else _utc_now().date()

    verified_pricing = verify_campaign_pricing(
        snapshot_path=cfg.pricing_snapshot_path,
        model_family=cfg.deployment.family,
        model_version=cfg.deployment.version,
        policy_mode=pricing_policy,
        today=today_date,
    )
    verified_pricing.policy.require_offline_if_historical(dry_run=dry_run)
    pricing = verified_pricing.snapshot

    # Endpoint + deployment resolution. In dry-run mode the runner never
    # opens a network client, so missing Azure env vars are tolerated
    # (fall back to placeholder strings purely for logging) to enable
    # CI / Mac-Mini-without-creds Stage 0 dry-run validation.
    endpoint_template = f"${{{cfg.deployment.endpoint_env}}}"
    if dry_run:
        endpoint_value = src_env.get(cfg.deployment.endpoint_env, "")
        deployment_env_val = src_env.get(cfg.deployment.deployment_env, "")
        deployment = deployment_env_val or cfg.deployment.deployment_name
    else:
        endpoint_value = _resolve_env_value(endpoint_template, env=src_env)
        deployment = _resolve_env_value(
            cfg.deployment.deployment_template, env=src_env
        )

    git_commit = _resolve_git_commit()
    dirty = not _git_is_clean()
    if dirty and not allow_dirty:
        raise EndpointMisconfiguredError(
            "git working tree is dirty; commit changes first OR pass "
            "--allow-dirty (Task 019 v2.1 records git_commit + dirty=true "
            "into every record + summary for audit)"
        )

    benchmark_dir = benchmarks_root / cfg.benchmark
    benchmark_dir.mkdir(parents=True, exist_ok=True)

    # Prompt-identity verification (exit 7 on mismatch).
    corpus_p = pathlib.Path(cfg.source_corpus_path)
    if not corpus_p.is_absolute():
        corpus_p = pathlib.Path.cwd() / corpus_p
    user_prompts_p = pathlib.Path(cfg.user_prompts_source_path)
    if not user_prompts_p.is_absolute():
        user_prompts_p = pathlib.Path.cwd() / user_prompts_p
    assembled_prompt, _all_user_prompts, selected_user_prompts = (
        verify_prompt_identity_or_exit7(
            corpus_path=corpus_p,
            user_prompts_path=user_prompts_p,
            corpus_seed=cfg.corpus_seed,
            target_tokens=cfg.target_system_prompt_tokens,
        )
    )
    source_corpus_sha = _sha256_file(corpus_p)
    user_prompts_source_sha = _sha256_file(user_prompts_p)

    # v2.2.1 — durable inter-stage linkage validation.
    calibration_result_data: dict[str, Any] | None = None
    calibration_result_sha256_val: str | None = None
    calibration_run_id_short_val: str | None = None
    calibration_selected_via: str | None = None
    calibration_selected_at_phase: str | None = None
    calibration_selected_at_bracket_depth: int | None = None
    calibration_phase_b_concurrency: int | None = None
    calibration_selected_bracket_root_phase: str | None = None
    selected_peak_tps: float | None = None
    smoke_summary_data: dict[str, Any] | None = None
    smoke_summary_sha256_val: str | None = None
    if stage in ("smoke", "evidence") and not dry_run:
        if not calibration_result_path:
            raise LinkageValidationError(
                "v2.2.1: --calibration-result is REQUIRED for stage "
                f"{stage!r}; auto-discovery is FORBIDDEN",
                reason="calibration_result_missing",
            )
        calibration_result_data = validate_calibration_result(
            calibration_result_path,
            expected_source_corpus_sha256=source_corpus_sha,
            expected_assembled_prompt_sha256=(
                cfg.expected_assembled_system_prompt_sha256
            ),
            expected_user_prompts_source_sha256=user_prompts_source_sha,
            expected_user_prompts_index_set=tuple(
                cfg.user_prompts_index_set
            ),
            max_age_hours=(
                cfg.calibration.calibration_max_age_hours
                if cfg.calibration is not None
                else CALIBRATION_MAX_AGE_HOURS
            ),
            # v2.4 review-fix (REQUEST-CHANGES #1): when the v2.4
            # empirical-promotion block is present in the runtime config
            # (always true for v2.4 YAMLs; the dataclass default is the
            # §10 PIN), stale calibrations are no longer rejected at
            # this preflight step. The v2.4 gate downstream in
            # `_run_measurement_async` evaluates invariant 12 and routes
            # stale calibrations to either the `cold_cache_strict`
            # fallback (default; raises TPM_FEASIBILITY_ABORT with
            # `empirical_promotion_disabled_calibration_stale_and_mini_probe_disabled`
            # when the fallback also denies) or, when
            # `runtime.empirical_promotion.mini_probe_enabled: true`
            # AND the auditor-approved YAML comment is present, the
            # opt-in mini-probe revalidation path. Direct callers of
            # `validate_calibration_result()` that never reach the v2.4
            # gate keep `allow_stale=False` (the default) and retain
            # the v2.2.1/v2.3 legacy strict-freshness behaviour.
            allow_stale=(
                cfg.runtime.empirical_promotion is not None
            ),
        )
        calibration_result_sha256_val = compute_calibration_result_sha256(
            calibration_result_path
        )
        calibration_run_id_short_val = calibration_result_data.get(
            "run_id_short"
        )
        selected_peak_tps = float(
            calibration_result_data["selected_peak_tps"]
        )
        # v2.3 fix loop #5 (auditor BLOCKER 3) — capture the calibration
        # selection metadata so the downstream smoke/evidence summary
        # can echo it AND so validate_smoke_summary (for evidence) can
        # enforce `smoke_selected_at_phase_mismatches_calibration`.
        calibration_selected_via = calibration_result_data.get(
            "selected_via"
        )
        calibration_selected_at_phase = calibration_result_data.get(
            "selected_at_phase"
        )
        b_depth = calibration_result_data.get("selected_at_bracket_depth")
        calibration_selected_at_bracket_depth = (
            int(b_depth) if isinstance(b_depth, int) else None
        )
        calibration_phase_b_concurrency = calibration_result_data.get(
            "concurrency_phase_b"
        )
        # v2.3 fix loop #6 (auditor BLOCKER 1) — read the bracket root
        # phase echoed by the calibration runner so the downstream
        # summary can compute `phase_b_concurrency_used` as a BOOL
        # (auditor BLOCKER 2) and so audit consumers can see whether a
        # bracket selection descended from Phase A or Phase B.
        calibration_selected_bracket_root_phase = (
            calibration_result_data.get("selected_bracket_root_phase")
        )
        if stage == "evidence":
            if not smoke_summary_path:
                raise LinkageValidationError(
                    "v2.2.1: --smoke-summary is REQUIRED for evidence; "
                    "auto-discovery is FORBIDDEN",
                    reason="smoke_summary_missing",
                )
            smoke_summary_data = validate_smoke_summary(
                smoke_summary_path,
                calibration_result_path=calibration_result_path,
                calibration_result_sha256=calibration_result_sha256_val,
                calibration_run_id_short=calibration_run_id_short_val or "",
                selected_peak_tps=selected_peak_tps,
                expected_source_corpus_sha256=source_corpus_sha,
                expected_assembled_prompt_sha256=(
                    cfg.expected_assembled_system_prompt_sha256
                ),
                expected_user_prompts_source_sha256=user_prompts_source_sha,
                max_age_hours=(
                    cfg.calibration.calibration_max_age_hours
                    if cfg.calibration is not None
                    else CALIBRATION_MAX_AGE_HOURS
                ),
                expected_selected_at_phase=calibration_selected_at_phase,
            )
            # The validated smoke summary is consumed implicitly through
            # the side effect (raises on mismatch); the returned dict is
            # currently not propagated downstream but is logged for audit
            # so reviewers can confirm the linkage actually fired.
            logger.info(
                "EVIDENCE_LINKAGE_VALIDATED smoke_summary=%s "
                "smoke_gate_passed=%s smoke_selected_peak_tps=%s",
                smoke_summary_path,
                smoke_summary_data.get("smoke_gate", {}).get("passed"),
                smoke_summary_data.get("selected_peak_tps"),
            )
            # v2.4 §9.3 — capture the smoke summary's own content sha256
            # so the downstream evidence runner can echo it into the
            # `smoke_summary_reference` block. `validate_smoke_summary`
            # has already proven the sidecar matches the file content,
            # so a single recompute here is the audit-safe choice
            # (sidecar text is also acceptable and equal-by-contract).
            smoke_summary_sha256_val = _sha256_file(
                pathlib.Path(smoke_summary_path)
            )
            # v2.4 §9.3 / §11.21 — evidence-stage pre-promotion
            # echo-validation preflight. Fires BEFORE the v2.4
            # empirical-promotion gate / HTTP dispatch in
            # `_run_measurement_async`, so a malformed/missing echo
            # surface causes a clean pre-promotion abort (exit code 9
            # with `evidence_summary_missing_smoke_promotion_path_echo`
            # and a v2.4 §9.4 abort envelope) instead of a late
            # RuntimeError from the admitted-summary overlay.
            #
            # The intended echo block is constructed by copying the
            # three v2.4 promotion-path fields from the loaded smoke
            # summary verbatim — exactly what
            # `build_admitted_evidence_summary` /
            # `apply_v24_admitted_summary_fields` will write later. The
            # echo VALIDATOR (`validate_evidence_summary_smoke_promotion_path_echo`)
            # byte-compares each field against the source. When the
            # source smoke summary lacks the v2.4 fields (e.g., a
            # legacy v2.3-shaped smoke summary or a corrupted file
            # whose v2.4 keys were dropped), the proposed echo also
            # lacks them and the validator returns the mismatched
            # field name — converted here into a
            # `LinkageValidationError` whose `reason` is the §8 stable
            # identifier `evidence_summary_missing_smoke_promotion_path_echo`
            # (exit code 9; abort envelope is materialised by `main()`
            # per §9.4 with `empirical_promotion_denied_reason=null`).
            intended_smoke_reference = {
                "smoke_summary_path": smoke_summary_path,
                "smoke_summary_sha256": smoke_summary_sha256_val,
                "smoke_tpm_feasibility_promotion_path": (
                    smoke_summary_data.get(
                        "tpm_feasibility_promotion_path"
                    )
                ),
                "smoke_tpm_feasibility_promotion_decision_reason": (
                    smoke_summary_data.get(
                        "tpm_feasibility_promotion_decision_reason"
                    )
                ),
                "smoke_largest_cell_projection_formula": (
                    smoke_summary_data.get(
                        "largest_cell_projection_formula"
                    )
                ),
            }
            echo_mismatch = (
                validate_evidence_summary_smoke_promotion_path_echo(
                    evidence_smoke_reference=intended_smoke_reference,
                    source_smoke_summary=smoke_summary_data,
                )
            )
            if echo_mismatch is not None:
                logger.error(
                    "EVIDENCE_SUMMARY_MISSING_SMOKE_PROMOTION_PATH_ECHO "
                    "field=%s smoke_summary=%s",
                    echo_mismatch,
                    smoke_summary_path,
                )
                raise LinkageValidationError(
                    f"v2.4 §9.3 echo-validation preflight failed: "
                    f"smoke_summary_reference.{echo_mismatch} is "
                    f"absent / null / does not byte-equal the source "
                    f"smoke summary's corresponding field. The five "
                    f"`smoke_summary_reference.*` fields MUST byte-"
                    f"equal-echo the source smoke summary (§9.3); the "
                    f"evidence runner aborts BEFORE the v2.4 "
                    f"empirical-promotion gate / HTTP dispatch with "
                    f"exit code 9 (§8 stable identifier "
                    f"`evidence_summary_missing_smoke_promotion_path_echo`).",
                    reason=(
                        "evidence_summary_missing_smoke_promotion_path_echo"
                    ),
                )

    runs_dir = benchmark_dir / "runs"
    timestamp_label = (
        timestamp_label_override
        if timestamp_label_override is not None
        else _timestamp_label()
    )
    run_id_short = (
        run_id_short_override
        if run_id_short_override is not None
        else uuid.uuid4().hex[:8]
    )
    # Estimate expected duration (minutes) for the lock holder metadata.
    if stage == "smoke":
        per_cell_min = (
            cfg.runtime.smoke_ramp_duration_seconds
            + cfg.runtime.smoke_cool_down_seconds
            + cfg.runtime.prewarm_calls_per_cell / cfg.runtime.prewarm_tps
        ) / 60.0
    else:
        per_cell_min = (
            cfg.runtime.ramp_duration_seconds
            + cfg.runtime.cool_down_seconds
            + cfg.runtime.prewarm_calls_per_cell / cfg.runtime.prewarm_tps
        ) / 60.0
    expected_duration_min = max(
        5, int(per_cell_min * len(cfg.sweep.max_output_tokens)) + 5
    )

    # ---- Run-lock acquisition (exit 4 on conflict) ----
    runlock_path = benchmark_dir / ".runlock"
    if dry_run:
        # Dry-run never opens a network client; we still acquire the
        # lock so concurrent dry-runs don't trample each other's
        # JSONL artifacts.
        lock_fd, lock_metadata = acquire_runlock(
            runlock_path,
            experiment_id=cfg.experiment_id,
            expected_duration_min=2,
        )
    else:
        lock_fd, lock_metadata = acquire_runlock(
            runlock_path,
            experiment_id=cfg.experiment_id,
            expected_duration_min=expected_duration_min,
        )

    try:
        # v2.4 operational wiring fix — compute the §3.1 invariant 11
        # condition 5 named flag from the committed terminal report(s)
        # so the production smoke / evidence path no longer silently
        # defaults to ``False`` (which would force the v2.3 fixture
        # case to the
        # `empirical_promotion_disabled_ptu_evidence_field_missing_and_cannot_infer`
        # denial even when the calibration's sha IS enumerated by a
        # canonical terminal report). For dry-run and stages without a
        # calibration sha (legacy/v2.1 call sites), the flag is False.
        v24_repo_root = pathlib.Path(__file__).resolve().parents[1]
        v24_terminal_report_flag = (
            verify_terminal_report_lists_calibration_sha_payg_not_ptu(
                repo_root=v24_repo_root,
                calibration_result_sha256=calibration_result_sha256_val,
            )
            if calibration_result_sha256_val
            else False
        )
        return asyncio.run(
            _run_measurement_async(
                cfg=cfg,
                runs_dir=runs_dir,
                system_prompt=assembled_prompt,
                user_prompts=selected_user_prompts,
                git_commit=git_commit,
                dirty=dirty,
                pricing=pricing,
                pricing_snapshot_path=cfg.pricing_snapshot_path,
                endpoint_value=endpoint_value,
                deployment=deployment,
                dry_run=dry_run,
                stage=stage,
                timestamp_label=timestamp_label,
                run_id_short=run_id_short,
                today=today_date,
                pricing_policy_provenance=verified_pricing.provenance(),
                run_lock_metadata=lock_metadata,
                source_corpus_sha=source_corpus_sha,
                user_prompts_source_sha=user_prompts_source_sha,
                calibration_result_path=calibration_result_path,
                calibration_result_sha256=calibration_result_sha256_val,
                calibration_run_id_short=calibration_run_id_short_val,
                selected_peak_tps_override=selected_peak_tps,
                smoke_summary_path_for_evidence=(
                    smoke_summary_path if stage == "evidence" else None
                ),
                calibration_selected_via=calibration_selected_via,
                calibration_selected_at_phase=(
                    calibration_selected_at_phase
                ),
                calibration_selected_at_bracket_depth=(
                    calibration_selected_at_bracket_depth
                ),
                calibration_phase_b_concurrency=(
                    calibration_phase_b_concurrency
                ),
                calibration_selected_bracket_root_phase=(
                    calibration_selected_bracket_root_phase
                ),
                calibration_result_data=calibration_result_data,
                smoke_summary_data=smoke_summary_data,
                smoke_summary_sha256_for_evidence=(
                    smoke_summary_sha256_val if stage == "evidence" else None
                ),
                v24_terminal_report_lists_calibration_sha_payg_not_ptu=(
                    v24_terminal_report_flag
                ),
                v24_repo_root=v24_repo_root,
            )
        )
    finally:
        release_runlock(lock_fd)


# ----------------------------------------------------------------------------
# v2.2.1 — Stage 0.5 calibration runner
# ----------------------------------------------------------------------------


def compute_probe_window_end_iso(
    *,
    cell_records: list[dict[str, Any]],
    fallback_now_iso: str,
) -> str:
    """v2.3 microfix 2026-05-30 (auditor finding #2) — compute the
    probe window's RIGHT EDGE from the admitted dispatch schedule,
    NEVER from wall-clock NOW after the cell coroutine returns.

    Resolution order (descending priority):

    1. Latest ``admitted_dispatch_iso`` across all **probe-phase**
       (i.e. non-prewarm) records in the cell. This is the canonical
       admitted-burst tail; it's what the admitted-pressure floor and
       schedule-rate invariants must measure against.
    2. Latest ``intended_dispatch_iso`` across probe-phase records.
       Used only when no record carries an admitted timestamp — e.g.
       semaphore saturation prevented admission for the entire cell.
       The intended-dispatch end equals the SCHEDULE end (computed
       deterministically from the seed/TPS pacer), which is the
       correct fallback for asserting "the schedule itself intended
       to dispatch enough calls".
    3. ``fallback_now_iso`` — used only when the cell ran ZERO probe-
       phase records (truncated by ``probe_max_usd`` /
       ``probe_max_calls`` / ``total_max_usd_stop_event`` BEFORE the
       first dispatch). In this degenerate case the admitted-pressure
       block is meaningless and the caller should treat the resulting
       window as a no-op.

    Using ``_utc_now()`` after ``_run_cell`` returns is the v2.3 bug
    the auditor flagged: long HTTP TTFTs (Foundry-side queueing,
    reasoning-mode bodies, transport jitter) shift the right edge of
    the last-``admitted_pressure_window_s`` window forward past the
    actual dispatch burst, hiding admitted-pressure failures behind
    in-flight completions and silently inflating the admitted-RPM
    denominator.
    """
    admitted_iso_in_probe = [
        r.get("admitted_dispatch_iso")
        for r in cell_records
        if not r.get("is_prewarm", False)
        and r.get("admitted_dispatch_iso")
    ]
    if admitted_iso_in_probe:
        return max(admitted_iso_in_probe)
    intended_iso_in_probe = [
        r.get("intended_dispatch_iso")
        for r in cell_records
        if not r.get("is_prewarm", False)
        and r.get("intended_dispatch_iso")
    ]
    if intended_iso_in_probe:
        return max(intended_iso_in_probe)
    return fallback_now_iso


def _aggregate_calibration_probe(
    *,
    records: list[dict[str, Any]],
    cell_max_output_tokens: int,
    candidate_tps: float | None = None,
    probe_window_end_iso: str | datetime.datetime | None = None,
    admitted_pressure_window_s: int = ADMITTED_PRESSURE_WINDOW_S,
    admitted_pressure_floor_ratio: float = ADMITTED_PRESSURE_FLOOR_RATIO,
    probe_phase_label: str | None = None,
    phase_label: str | None = None,
    bracket_depth: int | None = None,
    prompt_cache_key: str | None = None,
    source_corpus_sha: str | None = None,
    system_sha: str | None = None,
    user_prompts_source_sha: str | None = None,
    run_id_short: str | None = None,
) -> dict[str, Any]:
    """Aggregate calibration-probe per-call records into the eligibility
    + selection signals required by the Stage 0.5 outcome enum.

    Reuses ``_aggregate_cell`` for the core stats then layers on the
    v2.2.1 ``backlog_p50_ms`` + ``visible_output_mean_per_probe`` +
    ``visible_output_n_records`` + ``all_empty_visible_output``
    diagnostic fields the calibration_result.json schema requires.

    v2.3 EXTENSION — when ``candidate_tps`` + ``probe_window_end_iso``
    are supplied, also computes the ``admitted_pressure`` block and (if
    any record carries ``429_observed=true``) the ``first_429_metadata``
    block per spec § "First-429 metadata block" and § "Admitted-pressure
    validation gate".
    """
    agg = _aggregate_cell(
        records=records,
        cell_max_output_tokens=cell_max_output_tokens,
    )
    # Pull non-prewarm "admitted" records to compute v2.2.1 extras.
    ramp_records = [
        r for r in records if not r.get("is_prewarm", False)
    ]
    visible_out = [
        float(r.get("visible_output_tokens", 0) or 0)
        for r in ramp_records
    ]
    backlogs = [
        float(r.get("dispatch_backlog_ms", 0) or 0)
        for r in ramp_records
        if not _is_pre_admission_failure(r)
    ]
    agg["backlog_p50_ms"] = _percentile(backlogs, 50.0) if backlogs else 0.0
    agg["backlog_p95_ms"] = agg.get("p95_dispatch_backlog_ms", 0.0)
    agg["backlog_max_ms"] = agg.get("max_dispatch_backlog_ms", 0.0)
    agg["visible_output_mean_per_probe"] = (
        (sum(visible_out) / len(visible_out)) if visible_out else 0.0
    )
    agg["visible_output_n_records"] = len(visible_out)
    # All-empty-visible-output sanity: a probe whose admitted-window
    # records ALL report visible_output_tokens=0 has no meaningful
    # admission/billing signal (per spec: folds into the cache_not_warm
    # outcome with inconclusive_reason_detail).
    agg["all_empty_visible_output"] = bool(
        ramp_records and all(v == 0 for v in visible_out)
    )
    # v2.3 NEW — admitted-pressure block + first_429_metadata.
    if candidate_tps is not None and probe_window_end_iso is not None:
        admitted_iso_list = [
            r.get("admitted_dispatch_iso")
            for r in ramp_records
            if r.get("admitted_dispatch_iso")
        ]
        n_429_records = int(agg.get("n_429_records", 0) or 0)
        agg["admitted_pressure"] = compute_admitted_pressure_block(
            admitted_dispatch_iso_list=admitted_iso_list,
            candidate_tps=candidate_tps,
            probe_window_end_iso=probe_window_end_iso,
            window_s=admitted_pressure_window_s,
            floor_ratio=admitted_pressure_floor_ratio,
            observed_n_429=n_429_records,
        )
        # First-429 metadata block (only when n_429 ≥ 1).
        if n_429_records >= 1:
            first_429 = next(
                (r for r in ramp_records if r.get("429_observed", False)),
                None,
            )
            if first_429 is not None:
                # Steady-state admitted RPM (last 30s ratio same as
                # admitted_pressure block).
                agg["first_429_metadata"] = {
                    "target_tps": float(candidate_tps),
                    "admitted_peak_rpm_observed_last_30s": agg[
                        "admitted_pressure"
                    ]["admitted_peak_rpm_observed_last_30s"],
                    "admitted_steady_state_rpm_observed_last_30s": agg[
                        "admitted_pressure"
                    ]["admitted_peak_rpm_observed_last_30s"],
                    "scheduled_dispatch_iso": first_429.get(
                        "scheduled_dispatch_iso"
                    ),
                    "admitted_dispatch_iso": first_429.get(
                        "admitted_dispatch_iso"
                    ),
                    "dispatch_backlog_ms_at_first_429": int(
                        first_429.get("dispatch_backlog_ms", 0) or 0
                    ),
                    "retry_after_ms": first_429.get("retry_after_ms"),
                    "retry_after": first_429.get("retry_after"),
                    "backlog_p50_ms_at_first_429": agg["backlog_p50_ms"],
                    "backlog_p95_ms_at_first_429": agg["backlog_p95_ms"],
                    "cache_hit_ratio_at_first_429": agg.get(
                        "cache_hit_ratio_steady_state", 0.0
                    ),
                    "visible_output_tokens_of_preceding_success": (
                        _first_429_preceding_visible_output(ramp_records)
                    ),
                    "prompt_cache_key_used": (
                        prompt_cache_key
                        or first_429.get("prompt_cache_key_used")
                    ),
                    "source_corpus_sha256": (
                        source_corpus_sha
                        or first_429.get("source_corpus_sha256")
                    ),
                    "system_prompt_sha256": (
                        system_sha
                        or first_429.get("system_prompt_sha256")
                    ),
                    "user_prompts_source_sha256": (
                        user_prompts_source_sha
                        or first_429.get("user_prompts_source_sha256")
                    ),
                    "run_id_short": run_id_short,
                    "candidate_tps": float(candidate_tps),
                    "probe_phase": probe_phase_label,
                    "phase": phase_label,
                    "bracket_depth": bracket_depth,
                }
                # Sanity — every required field must be populated.
                required = (
                    "target_tps", "admitted_peak_rpm_observed_last_30s",
                    "admitted_steady_state_rpm_observed_last_30s",
                    "scheduled_dispatch_iso", "admitted_dispatch_iso",
                    "dispatch_backlog_ms_at_first_429",
                    "backlog_p50_ms_at_first_429",
                    "backlog_p95_ms_at_first_429",
                    "cache_hit_ratio_at_first_429",
                    "prompt_cache_key_used",
                    "candidate_tps", "probe_phase", "phase",
                )
                missing = [
                    k for k in required
                    if agg["first_429_metadata"].get(k) in (None, "")
                    and k not in ("scheduled_dispatch_iso",
                                  "admitted_dispatch_iso")
                ]
                if missing:
                    raise RuntimeError(
                        f"first_429_metadata block missing required "
                        f"fields {missing}; refusing to write a partial "
                        f"block"
                    )
        else:
            agg["first_429_metadata"] = None
    return agg


def _first_429_preceding_visible_output(
    ramp_records: list[dict[str, Any]],
) -> int | None:
    """Return ``visible_output_tokens`` of the most recent successful
    record (no 429) BEFORE the first 429 observation, or None when no
    preceding success exists."""
    last_success: int | None = None
    for r in ramp_records:
        if r.get("429_observed", False):
            return last_success
        if not r.get("failed", False):
            v = r.get("visible_output_tokens")
            if v is not None:
                try:
                    last_success = int(v)
                except (TypeError, ValueError):
                    pass
    return last_success


def run_calibration(
    *,
    cfg: ExperimentConfig,
    benchmarks_root: pathlib.Path,
    allow_dirty: bool,
    env: dict[str, str] | None = None,
    today: datetime.date | None = None,
    pricing_policy: str = LIVE_MEASUREMENT,
    run_id_short_override: str | None = None,
    timestamp_label_override: str | None = None,
) -> MeasurementResult:
    """v2.2.1 — Stage 0.5 adaptive TPS calibration.

    Iterates ``cfg.calibration.candidate_tps_grid`` in ASCENDING order.
    For each candidate:

    1. Runs a largest-cell probe (constant-rate, 180 s, early-stop on
       first real 429 when enabled). Applies the three BLOCKING
       eligibility gates (warm criterion, backlog, all-empty-visible-
       output). On any gate failure → bounded retry ONCE with
       ``_retry1`` cache-key suffix.
    2. If still ineligible after the retry → terminate calibration with
       the spec's inconclusive outcome (exit 8).
    3. If eligible AND ``n_429_records >= 1`` → run a smallest-cell
       control probe at the same TPS with the same eligibility + retry
       semantics. If the control probe gives ``n_429_records == 0`` →
       SELECTED. If ≥1 → ascend.
    4. If the largest-cell probe gave ``n_429_records == 0`` → ascend.
    5. Inter-probe cooldown of ``inter_probe_cooldown_s`` between probes
       (suppressed in dry-run / test environments via ``today`` /
       ``timestamp_label_override``).

    Writes ``runs/{timestamp}_exp007_calibration.result.json`` (the
    machine-readable result; sha256 NOT inside) plus a sibling
    ``runs/{timestamp}_exp007_calibration.summary.json`` containing
    ``calibration_result_sha256`` (the durable linkage anchor referenced
    by smoke + evidence via ``--calibration-result <path>``).

    Raises:
        CalibrationTerminalError: every non-``selected`` outcome maps
            to exit 8. The calibration_result.json file IS still
            written before raise (partial result).
        LinkageValidationError: peak_ramp_tps override forbidden, etc.
    """
    if cfg.calibration is None:
        raise LinkageValidationError(
            "experiment YAML has no `calibration:` block; v2.2.1 requires "
            "an explicit calibration config",
            reason="calibration_result_invalid_schema",
        )
    src_env = env if env is not None else dict(os.environ)
    today_date = today if today is not None else _utc_now().date()
    verified_pricing = verify_campaign_pricing(
        snapshot_path=cfg.pricing_snapshot_path,
        model_family=cfg.deployment.family,
        model_version=cfg.deployment.version,
        policy_mode=pricing_policy,
        today=today_date,
    )
    verified_pricing.policy.require_offline_if_historical(dry_run=False)
    pricing = verified_pricing.snapshot

    # Resolve endpoint + deployment only after pricing is admitted.
    endpoint_value = _resolve_env_value(
        f"${{{cfg.deployment.endpoint_env}}}", env=src_env
    )
    deployment = _resolve_env_value(
        cfg.deployment.deployment_template, env=src_env
    )

    git_commit = _resolve_git_commit()
    dirty = not _git_is_clean()
    if dirty and not allow_dirty:
        raise EndpointMisconfiguredError(
            "git working tree is dirty; commit changes first OR pass "
            "--allow-dirty (Task 019 v2.2.1 records git_commit + "
            "dirty=true into every record + summary for audit)"
        )

    benchmark_dir = benchmarks_root / cfg.benchmark
    benchmark_dir.mkdir(parents=True, exist_ok=True)

    corpus_p = pathlib.Path(cfg.source_corpus_path)
    if not corpus_p.is_absolute():
        corpus_p = pathlib.Path.cwd() / corpus_p
    user_prompts_p = pathlib.Path(cfg.user_prompts_source_path)
    if not user_prompts_p.is_absolute():
        user_prompts_p = pathlib.Path.cwd() / user_prompts_p
    assembled_prompt, _all_user_prompts, selected_user_prompts = (
        verify_prompt_identity_or_exit7(
            corpus_path=corpus_p,
            user_prompts_path=user_prompts_p,
            corpus_seed=cfg.corpus_seed,
            target_tokens=cfg.target_system_prompt_tokens,
        )
    )
    source_corpus_sha = _sha256_file(corpus_p)
    user_prompts_source_sha = _sha256_file(user_prompts_p)

    runs_dir = benchmark_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    timestamp_label = (
        timestamp_label_override
        if timestamp_label_override is not None
        else _timestamp_label()
    )
    run_id_short = (
        run_id_short_override
        if run_id_short_override is not None
        else uuid.uuid4().hex[:8]
    )

    # ---- v2.2.1 USD preflight: deterministic conservative pessimistic
    # calibration projection vs 0.9 × calibration.total_max_usd. ----
    proj_usd = deterministic_conservative_cost_estimator(
        stage="calibration_pessimistic",
        peak_tps=0.0,
        candidate_tps_grid=cfg.calibration.candidate_tps_grid,
        probe_duration_s=cfg.calibration.probe_duration_s,
        calibration_prewarm_calls=cfg.calibration.prewarm_calls,
    )
    preflight_threshold = 0.9 * cfg.calibration.total_max_usd
    if proj_usd > preflight_threshold:
        raise PreflightBudgetAbortError(
            f"calibration deterministic conservative projection "
            f"${proj_usd:.2f} > 0.9 × calibration.total_max_usd = "
            f"${preflight_threshold:.2f}"
        )

    # Run-lock (ALWAYS — calibration must be exclusive against
    # smoke/evidence on the same deployment).
    runlock_path = benchmark_dir / ".runlock"
    expected_duration_min = max(
        16, int(2 * cfg.calibration.probe_duration_s / 60.0
                * len(cfg.calibration.candidate_tps_grid)) + 5
    )
    lock_fd, lock_metadata = acquire_runlock(
        runlock_path,
        experiment_id=cfg.experiment_id,
        expected_duration_min=expected_duration_min,
    )

    result_path = (
        runs_dir
        / f"{timestamp_label}_{cfg.experiment_id}_calibration.result.json"
    )
    summary_path = (
        runs_dir
        / f"{timestamp_label}_{cfg.experiment_id}_calibration.summary.json"
    )

    try:
        outcome_result = asyncio.run(
            _run_calibration_async(
                cfg=cfg,
                runs_dir=runs_dir,
                system_prompt=assembled_prompt,
                user_prompts=selected_user_prompts,
                git_commit=git_commit,
                dirty=dirty,
                pricing=pricing,
                pricing_snapshot_path=cfg.pricing_snapshot_path,
                endpoint_value=endpoint_value,
                deployment=deployment,
                timestamp_label=timestamp_label,
                run_id_short=run_id_short,
                today=today_date,
                pricing_policy_provenance=verified_pricing.provenance(),
                run_lock_metadata=lock_metadata,
                source_corpus_sha=source_corpus_sha,
                user_prompts_source_sha=user_prompts_source_sha,
                result_path=result_path,
                summary_path=summary_path,
            )
        )
    finally:
        release_runlock(lock_fd)
    return outcome_result


# ----------------------------------------------------------------------------
# Task 019 v2.6 — Stage 0.5.C adaptive calibration wiring
# ----------------------------------------------------------------------------
#
# v2.5 built the validity runway (helpers, schemas, lints, blocked-run
# evidence discipline). v2.6 turns that runway into the first legitimate
# chance at decision-relevant evidence by wiring the v2.5 helper surface
# into the production calibration runner.
#
# This module exposes three pure / dispatcher-injected helpers:
#
#   ``_v24_probe_to_v25_observation``
#       Translate a v2.4 probe-aggregate dict into the v2.5 observation
#       shape expected by ``compute_onset_eligibility``,
#       ``compute_role_onset_interval``, and
#       ``aggregate_observations_same_tps``. No admission logic; purely
#       a key-rename + eligibility-attached adapter.
#
#   ``_evaluate_adaptive_trigger``
#       Pure predicate that mirrors v2.5 §3.2. Returns
#       ``(should_run_adaptive, trigger_reason)``. The runner calls
#       this AFTER Phase A/B settles; a False verdict means no adaptive
#       artifacts are emitted and the v2.4 outcome is returned unchanged
#       (auditor microfix #1 item 7).
#
#   ``_run_adaptive_stage_0_5c``
#       Async orchestrator. Takes the parent's probe list and an injected
#       ``dispatch_probe`` async callable so unit tests can exercise the
#       full Step 1 / Step 2 / Step 3 / C1 / C2 / C3 flow without an
#       Azure client. Step 1 is observation-only and dispatches zero
#       HTTP calls (auditor microfix #1 item 1). 0.5.C dispatches only
#       fire for Step 2 expansion, Step 3 bracket, and C2 replicate
#       (auditor microfix #1 item 1). The adaptive USD cap is a separate
#       envelope from v2.4/task caps (auditor microfix #1 item 2).
#       ``min_remaining_usd_for_expansion`` gates Step 2 only (auditor
#       microfix #1 item 3). C2 sequencing is candidate detection →
#       exactly one replicate per required role → aggregate same-TPS →
#       evaluate_c2 (auditor microfix #1 item 5).


# Outcomes that v2.5 §3.2 trigger predicate 1 admits for Stage 0.5.C.
# Any "selected" or terminal-cap outcome already in the v2.4 calibration
# is NOT a trigger — 0.5.C is additive only.
_V25_TRIGGER_PREDICATE_1_OUTCOMES: frozenset[str] = frozenset({
    CALIBRATION_OUTCOME_NO_CONTRAST,
    CALIBRATION_OUTCOME_PHASE_B_ENDPOINT_NOT_THROTTLING,
    CALIBRATION_OUTCOME_PHASE_B_DRIVER_PRESSURE_INSUFFICIENT,
    CALIBRATION_OUTCOME_INCONCLUSIVE_ADMITTED_PRESSURE,
    CALIBRATION_OUTCOME_INCONCLUSIVE_CACHE,
    CALIBRATION_OUTCOME_INCONCLUSIVE_BACKLOG,
})


def _evaluate_adaptive_trigger(
    *,
    outcome: str | None,
    adaptive_enabled: bool,
    adaptive_total_committed_usd: float,
    v24_total_committed_usd: float,
    v24_calibration_total_max_usd: float,
) -> tuple[bool, str]:
    """Task 019 v2.6 / v2.5 §3.2 — pre-registered 0.5.C trigger predicate.

    Returns ``(should_run_adaptive, reason)``. ``reason`` is an
    enumerated, operator-readable token regardless of verdict so the
    runner can log it without revealing prompt bytes or secrets.

    Auditor microfix #1 item 2 — the adaptive entry gate compares the
    REMAINING budget against the SEPARATE 0.5.C envelope
    (``ADAPTIVE_CALIBRATION_MAX_USD``); the v2.4 calibration cap is
    checked INDEPENDENTLY (a 0.5.C entry that would already cross the
    v2.4 cap is also refused).
    """
    from scripts.task019_v25_adaptive import (
        ADAPTIVE_CALIBRATION_MAX_USD,
        MIN_REMAINING_USD_FOR_ADAPTIVE_ENTRY,
    )

    if not adaptive_enabled:
        return False, "adaptive_calibration_yaml_disabled"
    if outcome not in _V25_TRIGGER_PREDICATE_1_OUTCOMES:
        return False, "adaptive_trigger_not_matched_outcome_not_in_predicate_set"
    adaptive_remaining = (
        ADAPTIVE_CALIBRATION_MAX_USD - adaptive_total_committed_usd
    )
    if adaptive_remaining < MIN_REMAINING_USD_FOR_ADAPTIVE_ENTRY:
        return (
            False,
            "adaptive_calibration_budget_exhausted_at_entry",
        )
    v24_remaining = (
        v24_calibration_total_max_usd - v24_total_committed_usd
    )
    if v24_remaining < MIN_REMAINING_USD_FOR_ADAPTIVE_ENTRY:
        return (
            False,
            "v24_calibration_total_max_usd_exhausted_at_adaptive_entry",
        )
    return True, "adaptive_trigger_matched"


def _v24_probe_to_v25_observation(
    *,
    probe: dict[str, Any],
    prompt_identity_sha256: str,
    pricing_snapshot_path: str,
    cache_hit_floor_for_role: float,
    backlog_ceiling_seconds: float,
) -> dict[str, Any]:
    """v2.6 — translate a v2.4 probe-aggregate into a v2.5 observation.

    The v2.5 helpers (``compute_role_onset_interval``,
    ``aggregate_observations_same_tps``, ``compute_onset_eligibility``)
    expect keys ``role``, ``tps_dispatched``, ``n_429``, ``n_records``,
    ``cache_hit_ratio_steady_state``, ``admitted``,
    ``backlog_pre_dispatch_seconds``, ``prompt_identity_sha256``,
    ``pricing_snapshot_path``, ``terminal_status``, and ``eligible``.
    The v2.4 probe aggregate uses ``n_429_records`` and ``candidate_tps``
    and stores the admitted-pressure verdict under
    ``admitted_pressure.admitted_pressure_passed``. This adapter
    contains NO admission logic — it computes ``eligible`` exclusively
    via the v2.5 ``compute_onset_eligibility`` helper.
    """
    from scripts.task019_v25_adaptive import compute_onset_eligibility

    ap = probe.get("admitted_pressure") or {}
    admitted = bool(ap.get("admitted_pressure_passed", False))
    backlog_seconds = float(probe.get("backlog_p50_ms", 0.0)) / 1000.0
    # Terminal-status is derived from probe halt_reason. v2.4 probe halt
    # reasons map to no v2.5 network-error terminal status, so the
    # default is "completed". Only an explicit network error attached by
    # ``_run_cell`` (currently stored as ``halt_reason`` such as
    # "transport_error_terminal") would map to a v2.5 network-error
    # status; for this adapter we conservatively only mark
    # ``"openai.APIConnectionError"`` when the probe's halt_reason
    # explicitly carries that token.
    halt_reason = probe.get("halt_reason") or ""
    if "APIConnectionError" in str(halt_reason):
        terminal_status = "openai.APIConnectionError"
    else:
        terminal_status = "completed"
    obs: dict[str, Any] = {
        "role": probe.get("role"),
        "tps_dispatched": float(probe.get("candidate_tps", 0.0)),
        "n_429": int(probe.get("n_429_records", 0) or 0),
        "n_records": int(probe.get("n_records", 0) or 0),
        "cache_hit_ratio_steady_state": float(
            probe.get("cache_hit_ratio_steady_state", 0.0) or 0.0
        ),
        "admitted": admitted,
        "backlog_pre_dispatch_seconds": backlog_seconds,
        "prompt_identity_sha256": prompt_identity_sha256,
        "pricing_snapshot_path": pricing_snapshot_path,
        "terminal_status": terminal_status,
    }
    elig = compute_onset_eligibility(
        probe=obs,
        pinned_prompt_identity_sha256=prompt_identity_sha256,
        pinned_pricing_snapshot_path=pricing_snapshot_path,
        cache_hit_floor_for_role=cache_hit_floor_for_role,
        backlog_ceiling_seconds=backlog_ceiling_seconds,
    )
    obs["eligible"] = (elig.eligibility == "eligible")
    obs["onset_bound_eligibility"] = elig.eligibility
    obs["onset_bound_eligibility_reason"] = elig.reason
    return obs


def _v24_probes_to_v25_observations(
    *,
    probes: Sequence[dict[str, Any]],
    prompt_identity_sha256: str,
    pricing_snapshot_path: str,
    backlog_ceiling_seconds: float,
) -> list[dict[str, Any]]:
    """v2.6 — batch adapter for ``_v24_probe_to_v25_observation``.

    The v2.4 cache-hit floor depends on role (``largest`` vs
    ``smallest_control``); both v2.4 pinned floors are 0.80, sourced
    from ``task019_v25_adaptive`` so they cannot drift from v2.5.
    """
    from scripts.task019_v25_adaptive import (
        CACHE_HIT_FLOOR_LARGEST,
        CACHE_HIT_FLOOR_SMALLEST_CONTROL,
    )

    out: list[dict[str, Any]] = []
    for p in probes:
        role = p.get("role")
        if role == "largest":
            floor = CACHE_HIT_FLOOR_LARGEST
        elif role == "smallest_control":
            floor = CACHE_HIT_FLOOR_SMALLEST_CONTROL
        else:
            # v2.4 has no other roles; defensively skip.
            continue
        out.append(
            _v24_probe_to_v25_observation(
                probe=p,
                prompt_identity_sha256=prompt_identity_sha256,
                pricing_snapshot_path=pricing_snapshot_path,
                cache_hit_floor_for_role=floor,
                backlog_ceiling_seconds=backlog_ceiling_seconds,
            )
        )
    return out


def _build_adaptive_caps_state(
    *,
    adaptive_committed_usd: float,
    adaptive_started_mono: float,
    consecutive_apiconn_errors: int,
) -> list[dict[str, Any]]:
    """v2.6 — assemble the §0.3 / §4.4 adaptive cap state list.

    Each entry is ``{cap_name, observed, limit, halted_on_cap}``. The
    caller passes the live counters; this function performs only the
    comparison so the trace is self-describing.
    """
    from scripts.task019_v25_adaptive import (
        ADAPTIVE_APICONNECTIONERROR_CONSECUTIVE_MAX,
        ADAPTIVE_CALIBRATION_MAX_USD,
        ADAPTIVE_CALIBRATION_WALL_TIME_MAX_MINUTES,
    )

    elapsed_minutes = (time.monotonic() - adaptive_started_mono) / 60.0
    return [
        {
            "cap_name": "adaptive_calibration_max_usd",
            "observed": round(adaptive_committed_usd, 6),
            "limit": ADAPTIVE_CALIBRATION_MAX_USD,
            "halted_on_cap": (
                adaptive_committed_usd >= ADAPTIVE_CALIBRATION_MAX_USD
            ),
        },
        {
            "cap_name": "adaptive_calibration_wall_time_max_minutes",
            "observed": round(elapsed_minutes, 4),
            "limit": ADAPTIVE_CALIBRATION_WALL_TIME_MAX_MINUTES,
            "halted_on_cap": (
                elapsed_minutes
                >= ADAPTIVE_CALIBRATION_WALL_TIME_MAX_MINUTES
            ),
        },
        {
            "cap_name": "adaptive_apiconnectionerror_consecutive_max",
            "observed": consecutive_apiconn_errors,
            "limit": ADAPTIVE_APICONNECTIONERROR_CONSECUTIVE_MAX,
            "halted_on_cap": (
                consecutive_apiconn_errors
                >= ADAPTIVE_APICONNECTIONERROR_CONSECUTIVE_MAX
            ),
        },
    ]


def _adaptive_cap_terminal_outcome(
    caps_state: Sequence[dict[str, Any]],
) -> str | None:
    """v2.6 — return the §0.3 / §8.1 cap-terminal outcome string when any
    cap has ``halted_on_cap``. The first halted cap wins, in the
    canonical order: budget → wall-time → APIConnectionError.
    """
    by_name: dict[str, dict[str, Any]] = {
        c["cap_name"]: c for c in caps_state
    }
    ordered = [
        ("adaptive_calibration_max_usd", "adaptive_calibration_budget_exhausted"),
        (
            "adaptive_calibration_wall_time_max_minutes",
            "adaptive_calibration_wall_time_exhausted",
        ),
        (
            "adaptive_apiconnectionerror_consecutive_max",
            "adaptive_calibration_api_connection_unstable",
        ),
    ]
    for cap_name, terminal in ordered:
        cap = by_name.get(cap_name)
        if cap and cap.get("halted_on_cap"):
            return terminal
    return None


async def _run_adaptive_stage_0_5c(
    *,
    parent_probes: Sequence[dict[str, Any]],
    dispatch_probe: Any,
    prompt_identity_sha256: str,
    pricing_snapshot_path: str,
    backlog_ceiling_seconds: float,
    v24_base_cache_key: str,
    phase_a_grid_tps: Sequence[float],
    phase_b_grid_tps: Sequence[float],
    v24_total_committed_usd: float,
    v24_calibration_total_max_usd: float,
    largest_cell_max_output_tokens: int,
    smallest_cell_max_output_tokens: int,
) -> dict[str, Any]:
    """Task 019 v2.6 — Stage 0.5.C orchestrator (dispatcher-injected).

    Produces a result dict with keys: ``outcome``, ``selected_peak_tps``,
    ``selected_via``, ``adaptive_search_trace``,
    ``role_onset_intervals``, ``c1_evaluation_trace``,
    ``c2_evaluation_trace``, ``c3_evaluation_trace``,
    ``adaptive_caps_state``, ``adaptive_committed_usd``,
    ``adaptive_search_probes`` (only the probes dispatched IN 0.5.C),
    ``trigger_reason``.

    ``dispatch_probe`` is an ``async`` callable with signature::

        async def dispatch_probe(
            *,
            role: str,
            tps: float,
            cap: int,
            adaptive_step: str,
        ) -> dict

    The returned dict is the same v2.4 probe-aggregate shape produced
    by ``_probe_once`` PLUS three v2.5 additive fields: ``probe_usd``,
    ``probe_committed_usd``, and (optional) ``terminal_status`` set to
    ``"openai.APIConnectionError"`` when the dispatcher's underlying
    Azure client raised that exception. The orchestrator never opens a
    network connection; the dispatcher is the only side-effecting
    component.

    Auditor microfix #1 invariants preserved:
      - item 1: Step 1 is observation-only; zero HTTP calls.
      - item 2: ``$25`` cap is a separate envelope; v2.4 cap also
        consulted.
      - item 3: ``$3`` expansion gate is Step-2-only.
      - item 4: helper signatures from ``task019_v25_adaptive``.
      - item 5: C2 = candidate → replicate → aggregate → evaluate.
      - item 7: when no candidate exists OR cap halts, no admission is
        claimed.
    """
    from scripts.task019_v25_adaptive import (
        ADAPTIVE_BRACKET_DEPTH_MAX_PER_ROLE,
        ADAPTIVE_C2_REPLICATES_MAX_PER_ROLE,
        ADAPTIVE_CALIBRATION_MAX_USD,
        ADAPTIVE_EXPANSION_PROBES_MAX_PER_ROLE,
        ADAPTIVE_STEP_NAMES,
        C2_ONSET_SEPARATION_MARGIN_TPS,
        CACHE_HIT_FLOOR_LARGEST,
        CACHE_HIT_FLOOR_SMALLEST_CONTROL,
        MIN_REMAINING_USD_FOR_EXPANSION,
        aggregate_observations_same_tps,
        build_adaptive_cache_bucket_key,
        compute_role_onset_interval,
        evaluate_c1,
        evaluate_c2,
        evaluate_c3_terminal,
        plan_step2_expansion,
        plan_step3_bracket_midpoint,
    )

    started_mono = time.monotonic()
    adaptive_committed_usd = 0.0
    consecutive_apiconn_errors = 0
    adaptive_probes: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    expansion_count_per_role: dict[str, int] = {
        "largest": 0,
        "smallest_control": 0,
    }
    bracket_depth_per_role: dict[str, int] = {
        "largest": 0,
        "smallest_control": 0,
    }
    c2_replicates_per_role: dict[str, int] = {
        "largest": 0,
        "smallest_control": 0,
    }

    def _caps_state() -> list[dict[str, Any]]:
        return _build_adaptive_caps_state(
            adaptive_committed_usd=adaptive_committed_usd,
            adaptive_started_mono=started_mono,
            consecutive_apiconn_errors=consecutive_apiconn_errors,
        )

    def _all_observations_v25() -> list[dict[str, Any]]:
        parent_v25 = _v24_probes_to_v25_observations(
            probes=parent_probes,
            prompt_identity_sha256=prompt_identity_sha256,
            pricing_snapshot_path=pricing_snapshot_path,
            backlog_ceiling_seconds=backlog_ceiling_seconds,
        )
        adapt_v25 = _v24_probes_to_v25_observations(
            probes=adaptive_probes,
            prompt_identity_sha256=prompt_identity_sha256,
            pricing_snapshot_path=pricing_snapshot_path,
            backlog_ceiling_seconds=backlog_ceiling_seconds,
        )
        return parent_v25 + adapt_v25

    def _compute_intervals() -> dict[str, Any]:
        obs = _all_observations_v25()
        return {
            "largest": compute_role_onset_interval(
                probes=obs, role="largest",
            ),
            "smallest_control": compute_role_onset_interval(
                probes=obs, role="smallest_control",
            ),
        }

    def _role_cap_for(role: str) -> int:
        return (
            largest_cell_max_output_tokens
            if role == "largest"
            else smallest_cell_max_output_tokens
        )

    # ---- Step 1: observation-only onset intervals ----
    # No HTTP. We record ineligible-reason metadata for every parent
    # probe AS IT APPEARED IN 0.5.A / 0.5.B (auditor microfix #1 item
    # 1). The step label must be in ADAPTIVE_STEP_NAMES.
    step1_obs = _v24_probes_to_v25_observations(
        probes=parent_probes,
        prompt_identity_sha256=prompt_identity_sha256,
        pricing_snapshot_path=pricing_snapshot_path,
        backlog_ceiling_seconds=backlog_ceiling_seconds,
    )
    assert "step1_observation_only" in ADAPTIVE_STEP_NAMES
    trace.append({
        "step": "step1_observation_only",
        "n_observations_considered": len(step1_obs),
        "n_eligible": sum(1 for o in step1_obs if o["eligible"]),
        "n_ineligible": sum(1 for o in step1_obs if not o["eligible"]),
        "ineligibility_reasons": [
            {
                "role": o["role"],
                "tps_dispatched": o["tps_dispatched"],
                "reason": o["onset_bound_eligibility_reason"],
            }
            for o in step1_obs
            if not o["eligible"]
        ],
    })
    intervals = _compute_intervals()
    trace.append({
        "step": "step1_observation_only",
        "intervals_after_step1": {
            role: {
                "state": iv.state,
                "onset_lower_tps": iv.onset_lower_tps,
                "onset_upper_tps": iv.onset_upper_tps,
            }
            for role, iv in intervals.items()
        },
    })

    # ---- Step 2: expansion dispatches ----
    # Auditor microfix #1 item 3 — the $3 expansion gate is Step-2-only.
    for role in ("largest", "smallest_control"):
        while True:
            iv = intervals[role]
            plan = plan_step2_expansion(
                interval=iv,
                phase_a_grid_tps=phase_a_grid_tps,
                phase_b_grid_tps=phase_b_grid_tps,
            )
            if plan is None:
                break
            if (
                expansion_count_per_role[role]
                >= ADAPTIVE_EXPANSION_PROBES_MAX_PER_ROLE
            ):
                trace.append({
                    "step": "step2_expansion",
                    "role": role,
                    "action": "skipped",
                    "reason": "expansion_count_cap_reached",
                })
                break
            adaptive_remaining = (
                ADAPTIVE_CALIBRATION_MAX_USD - adaptive_committed_usd
            )
            # 90% no-new-expansion rule (§4.4): once we've committed
            # >=90% of the adaptive envelope, no NEW Step 2 expansion
            # may schedule. Step 3 / C2 still allowed.
            if (
                adaptive_committed_usd
                >= 0.90 * ADAPTIVE_CALIBRATION_MAX_USD
            ):
                trace.append({
                    "step": "step2_expansion",
                    "role": role,
                    "action": "skipped",
                    "reason": "adaptive_cap_90pct_no_new_expansion",
                })
                break
            if adaptive_remaining < MIN_REMAINING_USD_FOR_EXPANSION:
                trace.append({
                    "step": "step2_expansion",
                    "role": role,
                    "action": "skipped",
                    "reason": (
                        "min_remaining_usd_for_expansion_violated"
                    ),
                })
                break
            v24_remaining = (
                v24_calibration_total_max_usd - v24_total_committed_usd
            )
            if v24_remaining < MIN_REMAINING_USD_FOR_EXPANSION:
                trace.append({
                    "step": "step2_expansion",
                    "role": role,
                    "action": "skipped",
                    "reason": (
                        "v24_calibration_remaining_below_expansion_gate"
                    ),
                })
                break
            # Build the v2.5 cache bucket key (auditor microfix #1 item
            # 4 — helper signature is authoritative).
            cache_bucket = build_adaptive_cache_bucket_key(
                v24_base=v24_base_cache_key,
                step="step2_expansion",
                role=role,
                tps=plan.tps_next,
            )
            trace.append({
                "step": "step2_expansion",
                "role": role,
                "action": "dispatch",
                "tps_next": plan.tps_next,
                "side": plan.side,
                "clamped_to_cap": plan.clamped_to_cap,
                "cache_bucket_key": cache_bucket,
            })
            probe_agg = await dispatch_probe(
                role=role,
                tps=plan.tps_next,
                cap=_role_cap_for(role),
                adaptive_step="step2_expansion",
            )
            adaptive_probes.append(probe_agg)
            adaptive_committed_usd += float(
                probe_agg.get("probe_committed_usd", 0.0)
            )
            if (
                probe_agg.get("terminal_status")
                == "openai.APIConnectionError"
            ):
                consecutive_apiconn_errors += 1
            else:
                consecutive_apiconn_errors = 0
            caps = _caps_state()
            if any(c.get("halted_on_cap") for c in caps):
                trace.append({
                    "step": "step2_expansion",
                    "role": role,
                    "action": "halted",
                    "reason": "cap_halted_after_dispatch",
                    "caps_state": caps,
                })
                return _build_cap_terminal_result(
                    adaptive_committed_usd=adaptive_committed_usd,
                    adaptive_probes=adaptive_probes,
                    trace=trace,
                    intervals=_compute_intervals(),
                    caps_state=caps,
                )
            expansion_count_per_role[role] += 1
            intervals = _compute_intervals()

    # ---- Step 3: bracket dispatches ----
    # Auditor microfix #1 item 3 — Step 3 does NOT apply the $3 gate.
    for role in ("largest", "smallest_control"):
        while True:
            iv = intervals[role]
            mid = plan_step3_bracket_midpoint(iv)
            if mid is None:
                break
            if (
                bracket_depth_per_role[role]
                >= ADAPTIVE_BRACKET_DEPTH_MAX_PER_ROLE
            ):
                trace.append({
                    "step": "step3_bracket",
                    "role": role,
                    "action": "skipped",
                    "reason": "bracket_depth_cap_reached",
                })
                break
            adaptive_remaining = (
                ADAPTIVE_CALIBRATION_MAX_USD - adaptive_committed_usd
            )
            # Step 3 enforces only hard caps (no $3 expansion gate).
            if adaptive_remaining <= 0:
                trace.append({
                    "step": "step3_bracket",
                    "role": role,
                    "action": "skipped",
                    "reason": "adaptive_cap_exhausted",
                })
                break
            v24_remaining = (
                v24_calibration_total_max_usd - v24_total_committed_usd
            )
            if v24_remaining <= 0:
                trace.append({
                    "step": "step3_bracket",
                    "role": role,
                    "action": "skipped",
                    "reason": "v24_cap_exhausted",
                })
                break
            cache_bucket = build_adaptive_cache_bucket_key(
                v24_base=v24_base_cache_key,
                step="step3_bracket",
                role=role,
                tps=mid,
            )
            trace.append({
                "step": "step3_bracket",
                "role": role,
                "action": "dispatch",
                "tps_next": mid,
                "depth_about_to_increment": (
                    bracket_depth_per_role[role] + 1
                ),
                "cache_bucket_key": cache_bucket,
            })
            probe_agg = await dispatch_probe(
                role=role,
                tps=mid,
                cap=_role_cap_for(role),
                adaptive_step="step3_bracket",
            )
            adaptive_probes.append(probe_agg)
            adaptive_committed_usd += float(
                probe_agg.get("probe_committed_usd", 0.0)
            )
            if (
                probe_agg.get("terminal_status")
                == "openai.APIConnectionError"
            ):
                consecutive_apiconn_errors += 1
            else:
                consecutive_apiconn_errors = 0
            caps = _caps_state()
            if any(c.get("halted_on_cap") for c in caps):
                trace.append({
                    "step": "step3_bracket",
                    "role": role,
                    "action": "halted",
                    "reason": "cap_halted_after_dispatch",
                    "caps_state": caps,
                })
                return _build_cap_terminal_result(
                    adaptive_committed_usd=adaptive_committed_usd,
                    adaptive_probes=adaptive_probes,
                    trace=trace,
                    intervals=_compute_intervals(),
                    caps_state=caps,
                )
            bracket_depth_per_role[role] += 1
            intervals = _compute_intervals()

    # ---- C1 admission ----
    # §0.8 aggregation BEFORE C1 (auditor microfix #1 item 5 — same-TPS
    # aggregation precedes both C1 and C2 admissions).
    all_obs = _all_observations_v25()
    aggregated: list[Any] = []
    seen: set[tuple[str, float]] = set()
    for o in all_obs:
        if not o["eligible"]:
            continue
        key = (o["role"], float(o["tps_dispatched"]))
        if key in seen:
            continue
        seen.add(key)
        try:
            aggregated.append(
                aggregate_observations_same_tps(
                    probes=all_obs,
                    role=o["role"],
                    tps=o["tps_dispatched"],
                )
            )
        except ValueError:
            continue
    c1 = evaluate_c1(aggregated_observations=aggregated)
    trace.append({
        "step": "c1_evaluation",
        "decision": c1.decision,
        "reason": c1.reason,
        "selected_peak_tps": c1.selected_peak_tps,
    })
    if c1.decision == "ADMIT":
        caps = _caps_state()
        return {
            "outcome": CALIBRATION_OUTCOME_SELECTED,
            "selected_peak_tps": c1.selected_peak_tps,
            "selected_via": c1.selected_via,
            "selected_at_phase": "adaptive",
            "adaptive_search_trace": trace,
            "role_onset_intervals": _intervals_to_dict(intervals),
            "c1_evaluation_trace": _decision_to_dict(c1),
            "c2_evaluation_trace": None,
            "c3_evaluation_trace": None,
            "adaptive_caps_state": caps,
            "adaptive_committed_usd": adaptive_committed_usd,
            "adaptive_search_probes": adaptive_probes,
        }

    # ---- C2 candidate → replicate → aggregate → admission ----
    largest_iv = intervals["largest"]
    smallest_iv = intervals["smallest_control"]
    c2_candidate = (
        largest_iv.state == "bracketed"
        and smallest_iv.state == "bracketed"
        and smallest_iv.onset_lower_tps is not None
        and largest_iv.onset_upper_tps is not None
        and (
            smallest_iv.onset_lower_tps - largest_iv.onset_upper_tps
        )
        >= C2_ONSET_SEPARATION_MARGIN_TPS
    )
    c2: Any
    if not c2_candidate:
        # No candidate — record canonical denial via evaluate_c2 with
        # aggregated_observations_at_t_star=None (auditor microfix #1
        # item 5 — this call is NOT an admission decision).
        c2 = evaluate_c2(
            largest_interval=largest_iv,
            smallest_interval=smallest_iv,
            aggregated_observations_at_t_star=None,
        )
        trace.append({
            "step": "c2_candidate_detection",
            "candidate_exists": False,
            "decision": c2.decision,
            "reason": c2.reason,
        })
    else:
        t_star = math.sqrt(
            largest_iv.onset_upper_tps * smallest_iv.onset_lower_tps
        )
        trace.append({
            "step": "c2_candidate_detection",
            "candidate_exists": True,
            "t_star": t_star,
            "largest_onset_upper": largest_iv.onset_upper_tps,
            "smallest_onset_lower": smallest_iv.onset_lower_tps,
            "c2_onset_separation_margin_tps": (
                C2_ONSET_SEPARATION_MARGIN_TPS
            ),
        })
        # Dispatch exactly one replicate per required role at t*.
        for role in ("largest", "smallest_control"):
            if (
                c2_replicates_per_role[role]
                >= ADAPTIVE_C2_REPLICATES_MAX_PER_ROLE
            ):
                trace.append({
                    "step": "c2_replicate",
                    "role": role,
                    "action": "skipped",
                    "reason": "c2_replicate_cap_reached",
                })
                continue
            adaptive_remaining = (
                ADAPTIVE_CALIBRATION_MAX_USD - adaptive_committed_usd
            )
            if adaptive_remaining <= 0:
                trace.append({
                    "step": "c2_replicate",
                    "role": role,
                    "action": "skipped",
                    "reason": "adaptive_cap_exhausted",
                })
                break
            cache_bucket = build_adaptive_cache_bucket_key(
                v24_base=v24_base_cache_key,
                step="c2_replicate",
                role=role,
                tps=t_star,
            )
            trace.append({
                "step": "c2_replicate",
                "role": role,
                "action": "dispatch",
                "tps_next": t_star,
                "cache_bucket_key": cache_bucket,
            })
            probe_agg = await dispatch_probe(
                role=role,
                tps=t_star,
                cap=_role_cap_for(role),
                adaptive_step="c2_replicate",
            )
            adaptive_probes.append(probe_agg)
            adaptive_committed_usd += float(
                probe_agg.get("probe_committed_usd", 0.0)
            )
            if (
                probe_agg.get("terminal_status")
                == "openai.APIConnectionError"
            ):
                consecutive_apiconn_errors += 1
            else:
                consecutive_apiconn_errors = 0
            c2_replicates_per_role[role] += 1
            caps = _caps_state()
            if any(c.get("halted_on_cap") for c in caps):
                trace.append({
                    "step": "c2_replicate",
                    "role": role,
                    "action": "halted",
                    "reason": "cap_halted_after_dispatch",
                    "caps_state": caps,
                })
                return _build_cap_terminal_result(
                    adaptive_committed_usd=adaptive_committed_usd,
                    adaptive_probes=adaptive_probes,
                    trace=trace,
                    intervals=_compute_intervals(),
                    caps_state=caps,
                )
        # Aggregate same-TPS observations at t* for both roles, then
        # evaluate_c2.
        all_obs = _all_observations_v25()
        t_star_aggs: dict[str, Any] = {}
        for role in ("largest", "smallest_control"):
            try:
                t_star_aggs[role] = aggregate_observations_same_tps(
                    probes=all_obs, role=role, tps=t_star,
                )
            except ValueError:
                # No eligible observation at t* for this role — leave
                # out; evaluate_c2 will surface the right denial reason.
                pass
        c2 = evaluate_c2(
            largest_interval=largest_iv,
            smallest_interval=smallest_iv,
            aggregated_observations_at_t_star=(
                t_star_aggs if t_star_aggs else None
            ),
        )
        trace.append({
            "step": "c2_evaluation",
            "decision": c2.decision,
            "reason": c2.reason,
            "selected_peak_tps": c2.selected_peak_tps,
        })
    if c2.decision == "ADMIT":
        caps = _caps_state()
        return {
            "outcome": CALIBRATION_OUTCOME_SELECTED,
            "selected_peak_tps": c2.selected_peak_tps,
            "selected_via": c2.selected_via,
            "selected_at_phase": "adaptive",
            "adaptive_search_trace": trace,
            "role_onset_intervals": _intervals_to_dict(intervals),
            "c1_evaluation_trace": _decision_to_dict(c1),
            "c2_evaluation_trace": _decision_to_dict(c2),
            "c3_evaluation_trace": None,
            "adaptive_caps_state": caps,
            "adaptive_committed_usd": adaptive_committed_usd,
            "adaptive_search_probes": adaptive_probes,
        }

    # ---- C3 terminal ----
    caps = _caps_state()
    cap_terminal = _adaptive_cap_terminal_outcome(caps)
    if cap_terminal is not None:
        # A cap halted: emit the cap-terminal outcome, NEVER C3
        # (auditor microfix #1 / §0.3).
        trace.append({
            "step": "cap_terminal",
            "outcome": cap_terminal,
            "caps_state": caps,
        })
        return {
            "outcome": cap_terminal,
            "selected_peak_tps": None,
            "selected_via": None,
            "selected_at_phase": None,
            "adaptive_search_trace": trace,
            "role_onset_intervals": _intervals_to_dict(intervals),
            "c1_evaluation_trace": _decision_to_dict(c1),
            "c2_evaluation_trace": _decision_to_dict(c2),
            "c3_evaluation_trace": None,
            "adaptive_caps_state": caps,
            "adaptive_committed_usd": adaptive_committed_usd,
            "adaptive_search_probes": adaptive_probes,
        }
    c3 = evaluate_c3_terminal(
        c1=c1, c2=c2, adaptive_caps_state=caps,
    )
    trace.append({
        "step": "c3_evaluation",
        "decision": c3.decision,
        "reason": c3.reason,
    })
    return {
        "outcome": (
            "no_promotable_contrast_at_this_prompt_deployment"
            if c3.decision == "ADMIT"
            else CALIBRATION_OUTCOME_NO_CONTRAST
        ),
        "selected_peak_tps": None,
        "selected_via": None,
        "selected_at_phase": None,
        "adaptive_search_trace": trace,
        "role_onset_intervals": _intervals_to_dict(intervals),
        "c1_evaluation_trace": _decision_to_dict(c1),
        "c2_evaluation_trace": _decision_to_dict(c2),
        "c3_evaluation_trace": _decision_to_dict(c3),
        "adaptive_caps_state": caps,
        "adaptive_committed_usd": adaptive_committed_usd,
        "adaptive_search_probes": adaptive_probes,
    }


def _intervals_to_dict(intervals: dict[str, Any]) -> dict[str, Any]:
    return {
        role: {
            "state": iv.state,
            "onset_lower_tps": iv.onset_lower_tps,
            "onset_upper_tps": iv.onset_upper_tps,
        }
        for role, iv in intervals.items()
    }


def _decision_to_dict(d: Any | None) -> dict[str, Any] | None:
    if d is None:
        return None
    return {
        "criterion": d.criterion,
        "decision": d.decision,
        "reason": d.reason,
        "selected_peak_tps": d.selected_peak_tps,
        "selected_via": d.selected_via,
    }


def _build_cap_terminal_result(
    *,
    adaptive_committed_usd: float,
    adaptive_probes: list[dict[str, Any]],
    trace: list[dict[str, Any]],
    intervals: dict[str, Any],
    caps_state: list[dict[str, Any]],
) -> dict[str, Any]:
    """v2.6 — assemble a cap-terminal result dict mid-flight.

    Cap-terminal outcomes (§0.3) NEVER claim C3.
    """
    cap_terminal = _adaptive_cap_terminal_outcome(caps_state) or (
        "adaptive_calibration_budget_exhausted"
    )
    return {
        "outcome": cap_terminal,
        "selected_peak_tps": None,
        "selected_via": None,
        "selected_at_phase": None,
        "adaptive_search_trace": trace,
        "role_onset_intervals": _intervals_to_dict(intervals),
        "c1_evaluation_trace": None,
        "c2_evaluation_trace": None,
        "c3_evaluation_trace": None,
        "adaptive_caps_state": caps_state,
        "adaptive_committed_usd": adaptive_committed_usd,
        "adaptive_search_probes": adaptive_probes,
    }


def _write_adaptive_calibration_summary(
    *,
    runs_dir: pathlib.Path,
    timestamp_label: str,
    cfg: "ExperimentConfig",
    run_id_short: str,
    git_commit: str,
    dirty: bool,
    deployment: str,
    pricing: "PaygPricing",
    pricing_snapshot_path: str,
    prompt_identity_sha256: str,
    started_at: datetime.datetime,
    completed_at: datetime.datetime,
    result_path: pathlib.Path,
    summary_path: pathlib.Path,
    adaptive_result: dict[str, Any],
    adaptive_block: "_AdaptiveCalibrationBlock",
    phase_a_probes: list[dict[str, Any]],
    phase_b_probes: list[dict[str, Any]],
) -> pathlib.Path:
    """Task 019 v2.6 — write the v2.5 adaptive_calibration_summary.json.

    The file is hash-linked to the calibration result + summary so the
    v2.5 ``validate_adaptive_calibration_summary`` validator can verify
    integrity. The path follows the existing v2.4 naming convention
    (sibling of the calibration result).
    """
    from scripts.task019_v25_adaptive import (
        PAYG_NOT_PTU_CAVEAT_BANNER,
        SCHEMA_VERSION_ADAPTIVE_CALIBRATION_SUMMARY_V25,
        validate_adaptive_calibration_summary,
    )

    adaptive_summary_path = (
        runs_dir
        / f"{timestamp_label}_{cfg.experiment_id}"
        f"_adaptive_calibration_summary.json"
    )
    # Read prior calibrations disclosure payload (already validated by
    # the v2.5 YAML preflight). Empty list when no path was supplied.
    disclosed: list[dict[str, Any]] = []
    if adaptive_block.prior_calibrations_disclosure_path:
        try:
            disclosure_p = pathlib.Path(
                adaptive_block.prior_calibrations_disclosure_path
            )
            if not disclosure_p.is_absolute():
                # Resolve relative to the experiment YAML's repo root.
                disclosure_p = (
                    cfg.path.resolve().parents[2] / disclosure_p
                )
            payload = json.loads(
                disclosure_p.read_text(encoding="utf-8")
            )
            if isinstance(payload, list):
                disclosed = payload
        except (OSError, ValueError):
            disclosed = []

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION_ADAPTIVE_CALIBRATION_SUMMARY_V25,
        "git_commit": git_commit,
        "dirty": dirty,
        "run_id_short": run_id_short,
        "experiment_id": cfg.experiment_id,
        "started_at_iso": _iso8601_z(started_at),
        "completed_at_iso": _iso8601_z(completed_at),
        # Task019 v2.7 follow-up: `_DeploymentBlock` exposes `family`
        # (model family, e.g. "gpt-5.2") and `deployment_name`; it has
        # no `model` attribute. The convention elsewhere in this module
        # (lines using `cfg.deployment.family` for the result payload's
        # "model" key) is the correct one. Mirror it here so the
        # adaptive-summary sidecar writer no longer raises
        # AttributeError mid-write.
        "model": cfg.deployment.family,
        "deployment_used": deployment,
        "calibration_result_path": str(result_path),
        "calibration_result_sha256": _sha256_file(result_path),
        "calibration_summary_path": str(summary_path),
        "calibration_summary_sha256": _sha256_file(summary_path),
        "pricing_source_url": pricing.source_url,
        "pricing_accessed_date": pricing.accessed_date,
        "pricing_snapshot_path": pricing_snapshot_path,
        "payg_not_ptu_caveat": PAYG_NOT_PTU_CAVEAT_BANNER,
        "prompt_identity_sha256": prompt_identity_sha256,
        "phase_a_probe_observations": [
            _summarise_probe_for_adaptive_summary(p)
            for p in phase_a_probes
        ],
        "phase_b_probe_observations": [
            _summarise_probe_for_adaptive_summary(p)
            for p in phase_b_probes
        ],
        "adaptive_search_trace": adaptive_result.get(
            "adaptive_search_trace", []
        ),
        "role_onset_intervals": adaptive_result.get(
            "role_onset_intervals", {}
        ),
        "contrast_criterion_evaluation": {
            "c1": adaptive_result.get("c1_evaluation_trace"),
            "c2": adaptive_result.get("c2_evaluation_trace"),
            "c3": adaptive_result.get("c3_evaluation_trace"),
        },
        "adaptive_caps_state": adaptive_result.get(
            "adaptive_caps_state", []
        ),
        "adaptive_calibration_total_usd": round(
            adaptive_result.get("adaptive_committed_usd", 0.0), 6,
        ),
        "adaptive_calibration_total_committed_usd": round(
            adaptive_result.get("adaptive_committed_usd", 0.0), 6,
        ),
        "auditor_approval_comment_verbatim": (
            adaptive_block.auditor_approval_comment or ""
        ),
        "disclosed_prior_calibrations": disclosed,
    }
    with adaptive_summary_path.open("w", encoding="utf-8") as fh:
        json.dump(
            payload, fh, indent=2, sort_keys=True,
            default=_json_default,
        )
    # Validate after write so a missing required field surfaces as a
    # deterministic operator-facing error (v2.5 §9.1).
    validate_adaptive_calibration_summary(payload)
    return adaptive_summary_path


def _summarise_probe_for_adaptive_summary(
    probe: dict[str, Any],
) -> dict[str, Any]:
    """v2.6 — compact probe echo for the adaptive summary's
    ``phase_a_probe_observations`` / ``phase_b_probe_observations``
    fields. The v2.5 schema requires the keys to be present; this
    helper extracts the minimum fields needed for downstream readers
    without duplicating the full probe record (which already lives in
    the calibration result + JSONL).
    """
    return {
        "candidate_tps": probe.get("candidate_tps"),
        "role": probe.get("role"),
        "n_records": probe.get("n_records"),
        "n_429_records": probe.get("n_429_records"),
        "cache_hit_ratio_steady_state": probe.get(
            "cache_hit_ratio_steady_state"
        ),
        "phase": probe.get("phase"),
        "probe_phase": probe.get("probe_phase"),
        "bracket_depth": probe.get("bracket_depth"),
        "probe_usd": probe.get("probe_usd"),
        "probe_committed_usd": probe.get("probe_committed_usd"),
    }


async def _run_calibration_async(
    *,
    cfg: ExperimentConfig,
    runs_dir: pathlib.Path,
    system_prompt: str,
    user_prompts: list[str],
    git_commit: str,
    dirty: bool,
    pricing: PaygPricing,
    pricing_snapshot_path: str,
    endpoint_value: str,
    deployment: str,
    timestamp_label: str,
    run_id_short: str,
    today: datetime.date,
    run_lock_metadata: dict[str, Any] | None,
    source_corpus_sha: str,
    user_prompts_source_sha: str,
    result_path: pathlib.Path,
    summary_path: pathlib.Path,
    pricing_policy_provenance: dict[str, Any] | None = None,
) -> MeasurementResult:
    """v2.3 async implementation of Stage 0.5 two-phase calibration.

    Phase A (v2.2.1's grid; safe ramp): iterate
    ``cfg.calibration.candidate_tps_grid`` ascending. For each candidate:

    1. Largest-cell probe (constant-rate, 180 s, four eligibility gates
       including the v2.3 admitted-pressure floor).
    2. On 0 largest-cell 429s + admitted-pressure pass → ascend.
    3. On 0 largest-cell 429s + admitted-pressure FAIL after
       ``_retry1_admp`` → terminal
       ``calibration_probe_inconclusive_admitted_pressure_insufficient``.
    4. On ≥ 1 largest-cell 429 → smallest-cell control probe (same
       eligibility gates).
    5. On smallest-cell control probe 0 429s → SELECTED.
    6. On smallest-cell control probe ≥ 1 429 (no-usable-contrast
       trigger) → bracket search (geometric midpoint, depth 3, same
       phase) BEFORE accepting terminal
       ``no_usable_contrast_at_this_prompt_deployment``.

    Phase B (v2.3 NEW; escalate-until-429): ENTERED iff Phase A iterated
    the full grid without observing a largest-cell 429 AND every Phase A
    largest-cell probe passed the admitted-pressure gate. Same per-probe
    procedure as Phase A; ``runtime.concurrency_phase_b: 512``; Phase B
    grid pinned to ``[5.0, 8.0, 12.0, 16.0, 24.0, 32.0]``.

    Writes ``calibration_result.json`` with the v2.3 9-member outcome
    enum, ``selected_via``, ``selected_at_phase``, ``selected_at_bracket_depth``,
    ``bracket_trace``, ``first_429_metadata`` per-probe, etc.
    """
    if cfg.calibration is None:
        raise LinkageValidationError(
            "no calibration block",
            reason="calibration_result_invalid_schema",
        )
    calib = cfg.calibration
    started_at = _utc_now()
    sim_started_mono = time.monotonic()

    client, credential = _build_live_client(
        endpoint_value=endpoint_value,
        max_retries=cfg.client.max_retries,
    )
    await _preflight_reachability(client=client, deployment=deployment)
    # JSONL for raw probe records (audit-only; not the durable result).
    jsonl_path = (
        runs_dir
        / f"{timestamp_label}_{cfg.experiment_id}_calibration.jsonl"
    )
    out_fh = jsonl_path.open("w", encoding="utf-8")
    total_usd = 0.0
    total_committed_usd = 0.0
    probes: list[dict[str, Any]] = []
    bracket_trace: list[dict[str, Any]] = []
    selected_tps: float | None = None
    selected_via: str | None = None
    selected_at_phase: str | None = None
    selected_at_candidate_idx: int | None = None
    selected_at_bracket_depth: int | None = None
    # v2.3 fix loop #6 (auditor BLOCKER 1) — bracket-search selections
    # are serialized with ``selected_at_phase='bracket'`` (the bracket
    # *is* its own phase, distinct from the parent A/B grid that rooted
    # it). The parent phase the bracket descended from is recorded
    # separately under ``selected_bracket_root_phase`` so downstream
    # readers can still answer "did the bracket borrow Phase B
    # concurrency?" without conflating the bracket with the grid that
    # spawned it.
    selected_bracket_root_phase: str | None = None
    outcome: str | None = None
    inconclusive_probe_role: str | None = None
    inconclusive_at_candidate_tps: float | None = None
    inconclusive_reason_detail: str | None = None
    halt_reason: str | None = None
    largest_429_seen_at_any_tps = False
    # v2.3 — non-blocking total_max_usd stop event. Set by a separate
    # accounting check at probe boundaries; the _run_cell dispatcher
    # consults this AT EACH SCHEDULED-DISPATCH-TIME (NEVER inside the
    # await chain). At 0.85 × total_max_usd we begin halting dispatch.
    total_usd_stop_event = asyncio.Event()
    sys_sha = _sha256_text(system_prompt)
    system_sha_local = sys_sha
    phase_a_admitted_pressure_failed = False

    async def _probe_once(
        *,
        candidate_tps: float,
        cap: int,
        suffix: str | None,
        role: str,
        phase_label: str,
        probe_phase_label: str,
        concurrency_for_probe: int,
        probe_max_calls_for_probe: int,
        bracket_depth: int | None = None,
        adaptive_step: str | None = None,
    ) -> dict[str, Any]:
        """Run a single calibration probe at ``candidate_tps`` for the
        cell with ``max_output_tokens=cap``. ``role`` ∈ {"largest",
        "smallest_control"}. ``suffix`` ∈ {None, "_retry1",
        "_retry1_admp", "_bracket1..3", "_bracket1..3_retry1",
        "_bracket1..3_retry1_admp"}.

        Task 019 v2.6 — when ``adaptive_step`` is set (one of
        ``ADAPTIVE_STEP_NAMES``), the cache bucket key is composed via
        ``build_adaptive_cache_bucket_key`` on top of the v2.4 base
        key. The v2.4 prompt-identity contract is PRESERVED (no byte
        contributing to ``prompt_identity_sha256`` is changed).
        """
        nonlocal total_usd, total_committed_usd
        cache_key = build_calibration_cache_key(
            run_id_short=run_id_short,
            max_output_tokens=cap,
            tps=candidate_tps,
            suffix=suffix,
        )
        if adaptive_step is not None:
            from scripts.task019_v25_adaptive import (
                build_adaptive_cache_bucket_key as _build_adaptive_key,
            )
            cache_key = _build_adaptive_key(
                v24_base=cache_key,
                step=adaptive_step,
                role=role,
                tps=candidate_tps,
            )
        probe_start_iso = _iso8601_z(_utc_now())
        # v2.3 microfix fix loop #4 (auditor finding #3) — select the
        # early-stop-on-first-429 flag from the calibration YAML based
        # on probe role. Largest-cell probes early-stop iff
        # `early_stop_on_first_429_largest` is True (success criterion
        # for the largest probe is "saw a 429"; once seen, no need to
        # keep dispatching). Smallest-cell control probes early-stop
        # iff `early_stop_on_first_429_smallest` is True (a 429 on
        # the smallest cell at the SAME TPS as a largest-cell 429 is
        # the contrast-lost verdict; no point continuing). Bracket
        # probes inherit the role flag from their root probe (largest
        # or smallest_control).
        if role == "largest":
            early_stop_429 = bool(calib.early_stop_on_first_429_largest)
        elif role == "smallest_control":
            early_stop_429 = bool(calib.early_stop_on_first_429_smallest)
        else:
            early_stop_429 = False
        (
            cell_records,
            cell_usd,
            cell_committed_usd,
            _max_in_flight,
            probe_halt_reason,
        ) = await _run_cell(
            cfg=cfg,
            cell_idx=len(probes),
            cell_max_output_tokens=cap,
            prewarm_calls=calib.prewarm_calls,
            prewarm_tps=calib.prewarm_tps,
            ramp_duration_s=float(calib.probe_duration_s),
            peak_ramp_tps=candidate_tps,
            cool_down_s=0.0,
            concurrency=concurrency_for_probe,
            client=client,
            deployment=deployment,
            system_prompt=system_prompt,
            user_prompts=user_prompts,
            git_commit=git_commit,
            dirty=dirty,
            system_sha=sys_sha,
            user_prompts_source_sha=user_prompts_source_sha,
            source_corpus_sha=source_corpus_sha,
            pricing_snapshot_path=pricing_snapshot_path,
            pricing=pricing,
            dry_run=False,
            out_fh=out_fh,
            global_request_offset=sum(p.get("n_records", 0) for p in probes),
            sim_started_mono=sim_started_mono,
            run_id_short=run_id_short,
            cache_key_override=cache_key,
            constant_rate=True,
            probe_max_usd=calib.probe_max_usd,
            probe_max_calls=probe_max_calls_for_probe,
            total_max_usd_stop_event=total_usd_stop_event,
            early_stop_on_first_429=early_stop_429,
            adaptive_step=adaptive_step,
        )
        total_usd += cell_usd
        total_committed_usd += cell_committed_usd
        # v2.3 fix loop #5 (auditor BLOCKER 1) — re-evaluate the
        # total-spend stop event AT BOUNDARY between probes against
        # DETERMINISTIC COMMITTED spend. The committed total is the
        # source-of-truth guardrail; a fast 429 or transport-error
        # stream that produces zero realized cost cannot bypass the
        # cap because every dispatched call already added the full
        # per-call rate to total_committed_usd.
        if (
            not total_usd_stop_event.is_set()
            and total_committed_usd >= 0.85 * calib.total_max_usd
        ):
            total_usd_stop_event.set()
        # v2.3 runtime invariant: assert the schedule itself intended to
        # dispatch at sufficient rate. Raises
        # ProbeScheduleIntendedRateInsufficientError on failure.
        intended_iso_in_probe = [
            r.get("intended_dispatch_iso")
            for r in cell_records
            if not r.get("is_prewarm", False)
            and r.get("intended_dispatch_iso")
        ]
        # v2.3 (auditor microfix 2026-05-30, finding #2) — the
        # admitted-pressure / schedule-rate windows MUST anchor on the
        # ADMITTED-DISPATCH SCHEDULE end, NOT on wall-clock NOW after
        # `_run_cell` returns. Using `_utc_now()` here lets a slow HTTP
        # tail (high TTFT, large reasoning bodies, transport jitter)
        # shift the last-`admitted_pressure_window_s` boundary FORWARD
        # past the dispatch burst, hiding admitted-pressure failures
        # behind in-flight completions and silently inflating the
        # admitted-RPM denominator. Delegated to
        # ``compute_probe_window_end_iso`` for unit testability.
        probe_end_iso = compute_probe_window_end_iso(
            cell_records=cell_records,
            fallback_now_iso=_iso8601_z(_utc_now()),
        )
        # Only assert when we actually ran enough probe-phase records;
        # an early stop from probe_max_usd / stop_event can legitimately
        # leave few records — that's a CAP-INDUCED truncation, not a
        # schedule-generation bug. Skip the invariant when caps fired.
        if (
            intended_iso_in_probe
            and not total_usd_stop_event.is_set()
            and (cell_committed_usd + DETERMINISTIC_PER_CALL_USD)
            < calib.probe_max_usd
        ):
            try:
                assert_probe_schedule_intended_rate(
                    intended_dispatch_iso_list=intended_iso_in_probe,
                    candidate_tps=candidate_tps,
                    probe_window_end_iso=probe_end_iso,
                    window_s=calib.admitted_pressure_window_s,
                    floor_ratio=calib.admitted_pressure_floor_ratio,
                )
            except ProbeScheduleIntendedRateInsufficientError as exc:
                logger.error(
                    "PROBE_SCHEDULE_INTENDED_RATE_INSUFFICIENT "
                    "candidate_tps=%s intended_in_window=%d "
                    "required=%d", exc.candidate_tps,
                    exc.intended_in_window, exc.required_in_window,
                )
                raise
        agg = _aggregate_calibration_probe(
            records=cell_records,
            cell_max_output_tokens=cap,
            candidate_tps=candidate_tps,
            probe_window_end_iso=probe_end_iso,
            admitted_pressure_window_s=calib.admitted_pressure_window_s,
            admitted_pressure_floor_ratio=(
                calib.admitted_pressure_floor_ratio
            ),
            probe_phase_label=probe_phase_label,
            phase_label=phase_label,
            bracket_depth=bracket_depth,
            prompt_cache_key=cache_key,
            source_corpus_sha=source_corpus_sha,
            system_sha=system_sha_local,
            user_prompts_source_sha=user_prompts_source_sha,
            run_id_short=run_id_short,
        )
        agg["prompt_cache_key"] = cache_key
        agg["candidate_tps"] = candidate_tps
        agg["role"] = role
        agg["probe_usd"] = cell_usd
        agg["probe_committed_usd"] = cell_committed_usd
        agg["retry"] = suffix is not None
        agg["retry_suffix"] = suffix
        agg["probe_phase"] = probe_phase_label
        agg["phase"] = phase_label
        agg["bracket_depth"] = bracket_depth
        agg["probe_started_at_iso"] = probe_start_iso
        agg["probe_ended_at_iso"] = probe_end_iso
        # v2.3 microfix 2026-05-30 (finding #4) — surface _run_cell halt
        # reason ({"probe_max_calls_hit", "probe_max_usd_hit",
        # "total_max_usd_stop_event_set"} or None) into the probe summary
        # so downstream audit and the calibration_result.json can record
        # which advisory cap (if any) interrupted ramp dispatch.
        agg["halt_reason"] = probe_halt_reason
        return agg

    def _classify_eligibility(agg: dict[str, Any]) -> str | None:
        """Return None if eligible; else one of the inconclusive-reason
        details: ``warm_criterion_failed_on_initial_and_retry``,
        ``backlog_excessive_on_initial_and_retry``, or
        ``all_empty_visible_output_responses``.

        v2.3 extension — also returns
        ``admitted_pressure_floor_ratio_not_reached_on_initial_and_retry``
        when the admitted-pressure gate fails AND the probe observed
        zero 429s."""
        if not agg.get("warm_criterion_passed", False):
            return "warm_criterion_failed_on_initial_and_retry"
        if agg.get("backlog_excessive", False):
            return "backlog_excessive_on_initial_and_retry"
        if agg.get("all_empty_visible_output", False):
            return "all_empty_visible_output_responses"
        ap = agg.get("admitted_pressure") or {}
        if (
            ap.get("admitted_pressure_passed") is False
            and not ap.get("admitted_pressure_skipped_due_to_429", False)
        ):
            return "admitted_pressure_floor_ratio_not_reached_on_initial_and_retry"
        return None

    async def _do_iterate_grid(
        *,
        grid: tuple[float, ...],
        phase_label: str,
        concurrency_for_phase: int,
        probe_max_calls_for_phase: int,
    ) -> tuple[str | None, float | None, int | None, list[float]]:
        """Iterate one phase's grid. Returns
        ``(outcome_or_None, selected_tps, selected_idx, gates_passing_tps_list)``.

        ``outcome_or_None`` is None when the grid was exhausted without
        a selection or terminal failure — caller decides whether to
        enter Phase B / emit endpoint-not-throttling outcome.
        """
        nonlocal selected_via, selected_at_phase, selected_at_candidate_idx
        nonlocal selected_at_bracket_depth, selected_bracket_root_phase
        nonlocal inconclusive_probe_role, inconclusive_at_candidate_tps
        nonlocal inconclusive_reason_detail
        nonlocal largest_429_seen_at_any_tps
        nonlocal phase_a_admitted_pressure_failed
        gates_passing_tps: list[float] = []
        local_outcome: str | None = None
        local_selected_tps: float | None = None
        local_selected_idx: int | None = None
        for idx, tps in enumerate(grid):
            if total_committed_usd >= 0.85 * calib.total_max_usd:
                local_outcome = CALIBRATION_OUTCOME_TOTAL_USD_EXHAUSTED
                break
            largest_attempts: list[dict[str, Any]] = []
            agg = await _probe_once(
                candidate_tps=tps,
                cap=calib.largest_cell_max_output_tokens,
                suffix=None,
                role="largest",
                phase_label=phase_label,
                probe_phase_label="largest_probe_steady",
                concurrency_for_probe=concurrency_for_phase,
                probe_max_calls_for_probe=probe_max_calls_for_phase,
            )
            largest_attempts.append(agg)
            reason = _classify_eligibility(agg)
            if reason is not None:
                if total_committed_usd >= 0.85 * calib.total_max_usd:
                    local_outcome = (
                        CALIBRATION_OUTCOME_TOTAL_USD_EXHAUSTED
                    )
                    probes.extend(largest_attempts)
                    break
                # v2.3 — pick suffix based on the failed gate so the
                # artifact trail is unambiguous (warm/backlog → _retry1;
                # admitted-pressure → _retry1_admp).
                if reason == (
                    "admitted_pressure_floor_ratio_not_reached_on_initial_and_retry"
                ):
                    retry_suffix = "_retry1_admp"
                else:
                    retry_suffix = "_retry1"
                agg_retry = await _probe_once(
                    candidate_tps=tps,
                    cap=calib.largest_cell_max_output_tokens,
                    suffix=retry_suffix,
                    role="largest",
                    phase_label=phase_label,
                    probe_phase_label="largest_probe_steady",
                    concurrency_for_probe=concurrency_for_phase,
                    probe_max_calls_for_probe=probe_max_calls_for_phase,
                )
                largest_attempts.append(agg_retry)
                reason_retry = _classify_eligibility(agg_retry)
                if reason_retry is not None:
                    if reason_retry == "backlog_excessive_on_initial_and_retry":
                        local_outcome = (
                            CALIBRATION_OUTCOME_INCONCLUSIVE_BACKLOG
                        )
                    elif reason_retry == (
                        "admitted_pressure_floor_ratio_not_reached_on_initial_and_retry"
                    ):
                        local_outcome = (
                            CALIBRATION_OUTCOME_INCONCLUSIVE_ADMITTED_PRESSURE
                        )
                        if phase_label == "A":
                            phase_a_admitted_pressure_failed = True
                    else:
                        local_outcome = (
                            CALIBRATION_OUTCOME_INCONCLUSIVE_CACHE
                        )
                    inconclusive_probe_role = "largest"
                    inconclusive_at_candidate_tps = tps
                    inconclusive_reason_detail = reason_retry
                    probes.extend(largest_attempts)
                    break
                agg = agg_retry
            probes.extend(largest_attempts)
            # At this point the largest-cell probe is eligible.
            n_429_largest = int(agg.get("n_429_records", 0) or 0)
            if n_429_largest == 0:
                # Eligible AND 0 429s → record for Phase B precondition
                # and bracket T_low lookup; advance to next candidate.
                gates_passing_tps.append(float(tps))
                await asyncio.sleep(calib.inter_probe_cooldown_s)
                continue
            largest_429_seen_at_any_tps = True
            # ---- Smallest-cell control probe (with bounded retry) ----
            if total_committed_usd >= 0.85 * calib.total_max_usd:
                local_outcome = CALIBRATION_OUTCOME_TOTAL_USD_EXHAUSTED
                break
            await asyncio.sleep(calib.inter_probe_cooldown_s)
            control_attempts: list[dict[str, Any]] = []
            control = await _probe_once(
                candidate_tps=tps,
                cap=calib.smallest_cell_max_output_tokens,
                suffix=None,
                role="smallest_control",
                phase_label=phase_label,
                probe_phase_label="smallest_control_probe_steady",
                concurrency_for_probe=concurrency_for_phase,
                probe_max_calls_for_probe=probe_max_calls_for_phase,
            )
            control_attempts.append(control)
            control_reason = _classify_eligibility(control)
            if control_reason is not None:
                if total_committed_usd >= 0.85 * calib.total_max_usd:
                    local_outcome = (
                        CALIBRATION_OUTCOME_TOTAL_USD_EXHAUSTED
                    )
                    probes.extend(control_attempts)
                    break
                if control_reason == (
                    "admitted_pressure_floor_ratio_not_reached_on_initial_and_retry"
                ):
                    retry_suffix = "_retry1_admp"
                else:
                    retry_suffix = "_retry1"
                control_retry = await _probe_once(
                    candidate_tps=tps,
                    cap=calib.smallest_cell_max_output_tokens,
                    suffix=retry_suffix,
                    role="smallest_control",
                    phase_label=phase_label,
                    probe_phase_label="smallest_control_probe_steady",
                    concurrency_for_probe=concurrency_for_phase,
                    probe_max_calls_for_probe=probe_max_calls_for_phase,
                )
                control_attempts.append(control_retry)
                control_reason_retry = _classify_eligibility(control_retry)
                if control_reason_retry is not None:
                    if control_reason_retry == (
                        "backlog_excessive_on_initial_and_retry"
                    ):
                        local_outcome = (
                            CALIBRATION_OUTCOME_INCONCLUSIVE_BACKLOG
                        )
                    elif control_reason_retry == (
                        "admitted_pressure_floor_ratio_not_reached_on_initial_and_retry"
                    ):
                        local_outcome = (
                            CALIBRATION_OUTCOME_INCONCLUSIVE_ADMITTED_PRESSURE
                        )
                    else:
                        local_outcome = (
                            CALIBRATION_OUTCOME_INCONCLUSIVE_CACHE
                        )
                    inconclusive_probe_role = "smallest_control"
                    inconclusive_at_candidate_tps = tps
                    inconclusive_reason_detail = control_reason_retry
                    probes.extend(control_attempts)
                    break
                control = control_retry
            probes.extend(control_attempts)
            if int(control.get("n_429_records", 0) or 0) == 0:
                local_outcome = CALIBRATION_OUTCOME_SELECTED
                local_selected_tps = float(tps)
                local_selected_idx = idx
                selected_via = "grid_ascending"
                selected_at_phase = phase_label
                selected_at_candidate_idx = idx
                break
            # ---- v2.3 NEW bracket search before terminal no-contrast ----
            t_low = (
                gates_passing_tps[-1] if gates_passing_tps else None
            )
            if t_low is None:
                local_outcome = CALIBRATION_OUTCOME_NO_CONTRAST
                inconclusive_reason_detail = (
                    "bracket_precondition_no_t_low_in_same_phase"
                )
                break
            bracket_outcome, b_selected_tps, b_depth = (
                await _do_bracket_search(
                    t_low=t_low,
                    t_high=float(tps),
                    phase_label=phase_label,
                    concurrency_for_bracket=concurrency_for_phase,
                    probe_max_calls_for_bracket=probe_max_calls_for_phase,
                )
            )
            if bracket_outcome == CALIBRATION_OUTCOME_SELECTED:
                local_outcome = CALIBRATION_OUTCOME_SELECTED
                local_selected_tps = b_selected_tps
                selected_via = "bracket_search"
                # v2.3 fix loop #6 (auditor BLOCKER 1) — bracket
                # selections record selected_at_phase='bracket'; the
                # parent grid phase (A or B) lives in
                # selected_bracket_root_phase so concurrency / audit
                # consumers can still trace which Phase-B concurrency
                # override (if any) was active during the bracket.
                selected_at_phase = "bracket"
                selected_bracket_root_phase = phase_label
                selected_at_bracket_depth = b_depth
                selected_at_candidate_idx = None
            else:
                local_outcome = CALIBRATION_OUTCOME_NO_CONTRAST
            break
        return (
            local_outcome, local_selected_tps,
            local_selected_idx, gates_passing_tps,
        )

    async def _do_bracket_search(
        *,
        t_low: float,
        t_high: float,
        phase_label: str,
        concurrency_for_bracket: int,
        probe_max_calls_for_bracket: int,
    ) -> tuple[str, float | None, int | None]:
        """v2.3 NEW — bounded bracket search at geometric midpoints.

        Returns ``(outcome, selected_tps_or_None, depth_or_None)``.
        Updates ``bracket_trace`` and ``probes`` in-place.

        v2.3 fix loop #5 (auditor BLOCKER 2): bracket largest-cell and
        smallest-cell control probes carry the SAME bounded-retry
        semantics as parent calibration probes (one retry on warm /
        backlog / admitted-pressure eligibility failure before the
        bracket aborts). Retry suffix composes as
        ``_bracket{N}_retry1`` for warm/backlog failures and
        ``_bracket{N}_retry1_admp`` for admitted-pressure failures so
        the per-probe artifact trail unambiguously names the failed
        gate AND the bracket depth without collision. The maximum
        bracket depth (3) is preserved.
        """
        nonlocal inconclusive_reason_detail

        def _bracket_retry_suffix(depth: int, reason: str) -> str:
            if reason == (
                "admitted_pressure_floor_ratio_not_reached_on_initial_and_retry"
            ):
                return f"_bracket{depth}_retry1_admp"
            return f"_bracket{depth}_retry1"

        cur_low = t_low
        cur_high = t_high
        for depth in range(1, calib.bracket_max_depth + 1):
            if total_committed_usd >= 0.85 * calib.total_max_usd:
                bracket_trace.append({
                    "depth": depth,
                    "t_low": cur_low,
                    "t_high": cur_high,
                    "t_bracket": None,
                    "outcome": "halted_total_usd_exhausted",
                })
                return (
                    CALIBRATION_OUTCOME_TOTAL_USD_EXHAUSTED, None, None,
                )
            t_bracket = compute_bracket_geometric_midpoint(
                cur_low, cur_high,
            )
            largest_attempts: list[dict[str, Any]] = []
            agg = await _probe_once(
                candidate_tps=t_bracket,
                cap=calib.largest_cell_max_output_tokens,
                suffix=f"_bracket{depth}",
                role="largest",
                phase_label="bracket",
                probe_phase_label="largest_probe_steady",
                concurrency_for_probe=concurrency_for_bracket,
                probe_max_calls_for_probe=probe_max_calls_for_bracket,
                bracket_depth=depth,
            )
            largest_attempts.append(agg)
            reason = _classify_eligibility(agg)
            if reason is not None:
                # v2.3 fix loop #5 BLOCKER 2 — bounded retry within
                # bracket. Mirrors parent's per-cell retry shape.
                if total_committed_usd >= 0.85 * calib.total_max_usd:
                    probes.extend(largest_attempts)
                    bracket_trace.append({
                        "depth": depth,
                        "t_low": cur_low,
                        "t_high": cur_high,
                        "t_bracket": t_bracket,
                        "largest_n_429": int(
                            agg.get("n_429_records", 0) or 0
                        ),
                        "smallest_n_429": None,
                        "outcome": "halted_total_usd_exhausted",
                        "eligibility_reason": reason,
                    })
                    return (
                        CALIBRATION_OUTCOME_TOTAL_USD_EXHAUSTED,
                        None,
                        None,
                    )
                retry_suffix = _bracket_retry_suffix(depth, reason)
                agg_retry = await _probe_once(
                    candidate_tps=t_bracket,
                    cap=calib.largest_cell_max_output_tokens,
                    suffix=retry_suffix,
                    role="largest",
                    phase_label="bracket",
                    probe_phase_label="largest_probe_steady",
                    concurrency_for_probe=concurrency_for_bracket,
                    probe_max_calls_for_probe=probe_max_calls_for_bracket,
                    bracket_depth=depth,
                )
                largest_attempts.append(agg_retry)
                reason_retry = _classify_eligibility(agg_retry)
                if reason_retry is not None:
                    probes.extend(largest_attempts)
                    bracket_trace.append({
                        "depth": depth,
                        "t_low": cur_low,
                        "t_high": cur_high,
                        "t_bracket": t_bracket,
                        "largest_n_429": int(
                            agg_retry.get("n_429_records", 0) or 0
                        ),
                        "smallest_n_429": None,
                        "outcome": "aborted_eligibility_after_retry",
                        "eligibility_reason": reason_retry,
                        "retry_suffix": retry_suffix,
                    })
                    inconclusive_reason_detail = (
                        f"bracket_aborted_at_depth_{depth}"
                        f"_eligibility_fail_after_retry"
                    )
                    return CALIBRATION_OUTCOME_NO_CONTRAST, None, None
                # Retry passed eligibility — adopt the retry agg as the
                # bracket largest probe result for subsequent logic.
                agg = agg_retry
            n_429_largest = int(agg.get("n_429_records", 0) or 0)
            if n_429_largest == 0:
                # No 429 at the bracket point → contrast (if any) is at
                # HIGHER TPS. Shrink to (T_bracket, T_high) and recurse.
                probes.extend(largest_attempts)
                bracket_trace.append({
                    "depth": depth,
                    "t_low": cur_low,
                    "t_high": cur_high,
                    "t_bracket": t_bracket,
                    "largest_n_429": 0,
                    "smallest_n_429": None,
                    "outcome": "recurse_upper",
                })
                cur_low = t_bracket
                continue
            # Largest-cell 429 at the bracket point → smallest-cell
            # control probe at the same bracket TPS.
            await asyncio.sleep(calib.inter_probe_cooldown_s)
            control_attempts: list[dict[str, Any]] = []
            control = await _probe_once(
                candidate_tps=t_bracket,
                cap=calib.smallest_cell_max_output_tokens,
                suffix=f"_bracket{depth}",
                role="smallest_control",
                phase_label="bracket",
                probe_phase_label="smallest_control_probe_steady",
                concurrency_for_probe=concurrency_for_bracket,
                probe_max_calls_for_probe=probe_max_calls_for_bracket,
                bracket_depth=depth,
            )
            control_attempts.append(control)
            control_reason = _classify_eligibility(control)
            if control_reason is not None:
                # v2.3 fix loop #5 BLOCKER 2 — bounded retry on control
                # probe inside bracket (mirrors parent retry shape).
                if total_committed_usd >= 0.85 * calib.total_max_usd:
                    probes.extend(largest_attempts + control_attempts)
                    bracket_trace.append({
                        "depth": depth,
                        "t_low": cur_low,
                        "t_high": cur_high,
                        "t_bracket": t_bracket,
                        "largest_n_429": n_429_largest,
                        "smallest_n_429": int(
                            control.get("n_429_records", 0) or 0
                        ),
                        "outcome": "halted_total_usd_exhausted",
                        "eligibility_reason": control_reason,
                    })
                    return (
                        CALIBRATION_OUTCOME_TOTAL_USD_EXHAUSTED,
                        None,
                        None,
                    )
                control_retry_suffix = _bracket_retry_suffix(
                    depth, control_reason,
                )
                control_retry = await _probe_once(
                    candidate_tps=t_bracket,
                    cap=calib.smallest_cell_max_output_tokens,
                    suffix=control_retry_suffix,
                    role="smallest_control",
                    phase_label="bracket",
                    probe_phase_label="smallest_control_probe_steady",
                    concurrency_for_probe=concurrency_for_bracket,
                    probe_max_calls_for_probe=probe_max_calls_for_bracket,
                    bracket_depth=depth,
                )
                control_attempts.append(control_retry)
                control_reason_retry = _classify_eligibility(control_retry)
                if control_reason_retry is not None:
                    probes.extend(largest_attempts + control_attempts)
                    bracket_trace.append({
                        "depth": depth,
                        "t_low": cur_low,
                        "t_high": cur_high,
                        "t_bracket": t_bracket,
                        "largest_n_429": n_429_largest,
                        "smallest_n_429": int(
                            control_retry.get("n_429_records", 0) or 0
                        ),
                        "outcome": "aborted_eligibility_after_retry",
                        "eligibility_reason": control_reason_retry,
                        "retry_suffix": control_retry_suffix,
                    })
                    inconclusive_reason_detail = (
                        f"bracket_aborted_at_depth_{depth}"
                        f"_eligibility_fail_after_retry"
                    )
                    return CALIBRATION_OUTCOME_NO_CONTRAST, None, None
                control = control_retry
            probes.extend(largest_attempts + control_attempts)
            n_429_small = int(control.get("n_429_records", 0) or 0)
            if n_429_small == 0:
                # SELECTED at the bracket point.
                bracket_trace.append({
                    "depth": depth,
                    "t_low": cur_low,
                    "t_high": cur_high,
                    "t_bracket": t_bracket,
                    "largest_n_429": n_429_largest,
                    "smallest_n_429": 0,
                    "outcome": "selected",
                })
                return (
                    CALIBRATION_OUTCOME_SELECTED, float(t_bracket), depth,
                )
            # Both probes 429 → contrast (if any) is at LOWER TPS.
            bracket_trace.append({
                "depth": depth,
                "t_low": cur_low,
                "t_high": cur_high,
                "t_bracket": t_bracket,
                "largest_n_429": n_429_largest,
                "smallest_n_429": n_429_small,
                "outcome": "recurse_lower",
            })
            cur_high = t_bracket
        # Bracket exhausted — terminal no-usable-contrast.
        inconclusive_reason_detail = (
            f"bracket_exhausted_at_depth_{calib.bracket_max_depth}"
        )
        return CALIBRATION_OUTCOME_NO_CONTRAST, None, None

    try:
        # ---- Phase A — safe ramp (v2.2.1 grid, kept verbatim) ----
        (
            outcome_a, selected_a, sel_idx_a, phase_a_passing,
        ) = await _do_iterate_grid(
            grid=calib.candidate_tps_grid,
            phase_label="A",
            concurrency_for_phase=cfg.runtime.concurrency,
            probe_max_calls_for_phase=calib.probe_max_calls,
        )
        if outcome_a is not None and outcome_a != CALIBRATION_OUTCOME_SELECTED:
            # Terminal in Phase A — record outcome (admitted-pressure
            # failure also blocks Phase B per spec).
            outcome = outcome_a
            if outcome == CALIBRATION_OUTCOME_TOTAL_USD_EXHAUSTED:
                halt_reason = "total_usd_exhausted_in_phase_a"
        elif outcome_a == CALIBRATION_OUTCOME_SELECTED:
            outcome = CALIBRATION_OUTCOME_SELECTED
            selected_tps = selected_a
        else:
            # Phase A exhausted with 0 largest 429s.
            # Phase B entry preconditions:
            #   (1) at least one Phase A probe must have happened — so
            #       gates_passing_tps may be non-empty;
            #   (2) no Phase A largest probe failed admitted-pressure
            #       gate (phase_a_admitted_pressure_failed is False —
            #       set inside _do_iterate_grid on admitted-pressure
            #       terminal failure, but that path actually sets
            #       outcome_a, so when we reach here this should be
            #       False by construction). For belt-and-braces guard
            #       we check it again.
            if phase_a_admitted_pressure_failed:
                outcome = (
                    CALIBRATION_OUTCOME_INCONCLUSIVE_ADMITTED_PRESSURE
                )
            else:
                # phase_a_passing collected here is the per-Phase-A
                # gates_passing_tps list (informational; bracket search
                # uses its own per-grid gates_passing_tps list).
                _ = phase_a_passing  # kept for future cross-phase use
                # ---- Phase B — escalate-until-429 ----
                phase_b_admitted_pressure_failed_any = False
                (
                    outcome_b, selected_b, sel_idx_b, phase_b_passing,
                ) = await _do_iterate_grid(
                    grid=calib.candidate_tps_grid_phase_b,
                    phase_label="B",
                    concurrency_for_phase=cfg.runtime.concurrency_phase_b,
                    probe_max_calls_for_phase=calib.probe_max_calls_phase_b,
                )
                # Check if any Phase B probe terminated with admitted-
                # pressure failure (recorded into local outcome_b above
                # via CALIBRATION_OUTCOME_INCONCLUSIVE_ADMITTED_PRESSURE).
                if outcome_b == (
                    CALIBRATION_OUTCOME_INCONCLUSIVE_ADMITTED_PRESSURE
                ):
                    phase_b_admitted_pressure_failed_any = True
                if outcome_b == CALIBRATION_OUTCOME_SELECTED:
                    outcome = CALIBRATION_OUTCOME_SELECTED
                    selected_tps = selected_b
                elif outcome_b is not None:
                    outcome = outcome_b
                    if outcome == CALIBRATION_OUTCOME_TOTAL_USD_EXHAUSTED:
                        halt_reason = "total_usd_exhausted_in_phase_b"
                else:
                    # Phase B grid exhausted with 0 largest 429s and no
                    # admitted-pressure terminal failure → emit one of
                    # the two v2.3 NEW terminal outcomes.
                    if phase_b_admitted_pressure_failed_any:
                        outcome = (
                            CALIBRATION_OUTCOME_PHASE_B_DRIVER_PRESSURE_INSUFFICIENT
                        )
                    else:
                        outcome = (
                            CALIBRATION_OUTCOME_PHASE_B_ENDPOINT_NOT_THROTTLING
                        )

        # ---- Task 019 v2.6 — Stage 0.5.C adaptive dispatcher wiring ----
        # The dispatcher runs only when both (a) the YAML enables
        # adaptive calibration AND (b) the §3.2 trigger predicate
        # matches the v2.4 outcome AND (c) the v2.5 entry gates
        # allow. Otherwise the v2.4 outcome is returned UNCHANGED
        # (auditor microfix #1 item 7 — no adaptive artifacts on
        # trigger-false).
        adaptive_result: dict[str, Any] | None = None
        adaptive_trigger_reason: str = "adaptive_calibration_yaml_disabled"
        adaptive_block = cfg.runtime.adaptive_calibration
        _adaptive_should_run, adaptive_trigger_reason = (
            _evaluate_adaptive_trigger(
                outcome=outcome,
                adaptive_enabled=adaptive_block.enabled,
                adaptive_total_committed_usd=0.0,
                v24_total_committed_usd=total_committed_usd,
                v24_calibration_total_max_usd=calib.total_max_usd,
            )
        )
        if _adaptive_should_run:
            logger.info(
                "ADAPTIVE_STAGE_0_5_C_ENTER "
                "v24_outcome=%s reason=%s", outcome, adaptive_trigger_reason,
            )

            async def _adaptive_dispatch_probe(
                *, role: str, tps: float, cap: int,
                adaptive_step: str,
            ) -> dict[str, Any]:
                """v2.6 adaptive 0.5.C dispatcher closure.

                Re-uses ``_probe_once`` (which routes through the same
                async scheduled dispatcher + ``_run_cell`` HTTP client
                policy as v2.4) but tags the cache bucket via
                ``adaptive_step`` so the v2.5 cache-bucket-key suffix
                applies. The probe aggregate is appended to ``probes``
                here so it joins both JSONL persistence and adaptive
                accounting.
                """
                phase_label = "adaptive"
                probe_phase_label = (
                    f"adaptive_{adaptive_step}_{role}"
                )
                agg = await _probe_once(
                    candidate_tps=tps,
                    cap=cap,
                    suffix=None,
                    role=role,
                    phase_label=phase_label,
                    probe_phase_label=probe_phase_label,
                    concurrency_for_probe=(
                        cfg.runtime.concurrency_phase_b
                    ),
                    probe_max_calls_for_probe=(
                        calib.probe_max_calls_phase_b
                    ),
                    adaptive_step=adaptive_step,
                )
                # Tag for downstream provenance in adaptive trace and
                # adaptive summary; persisted to JSONL with the v2.4
                # additive-fields contract.
                agg["adaptive_step"] = adaptive_step
                probes.append(agg)
                return agg

            # Task 019 v2.6 fix — adaptive Stage 0.5.C base cache key
            # must carry a POSITIVE TPS namespace token because
            # `build_calibration_cache_key` enforces `tps > 0` as a
            # helper invariant. The base key is a namespace prefix
            # only: every adaptive probe re-composes its own per-probe
            # bucket key via `build_adaptive_cache_bucket_key(v24_base=
            # base, step=..., role=..., tps=plan.tps_next)` which
            # encodes the *actual* probe TPS. The semantically
            # correct namespace value for the v2.4-to-adaptive bridge
            # is the pinned `runtime.peak_ramp_tps` (v2.1 pin = 0.33)
            # — i.e., the TPS that anchored the v2.4 leg whose
            # outcome triggered Stage 0.5.C entry. This preserves the
            # helper invariant without changing measurement semantics.
            v24_base_cache_key = build_calibration_cache_key(
                run_id_short=run_id_short,
                max_output_tokens=calib.largest_cell_max_output_tokens,
                tps=cfg.runtime.peak_ramp_tps,
                suffix=None,
            )
            try:
                adaptive_result = await _run_adaptive_stage_0_5c(
                    parent_probes=list(probes),
                    dispatch_probe=_adaptive_dispatch_probe,
                    prompt_identity_sha256=sys_sha,
                    pricing_snapshot_path=pricing_snapshot_path,
                    backlog_ceiling_seconds=float(
                        calib.admitted_pressure_window_s
                    ),
                    v24_base_cache_key=v24_base_cache_key,
                    phase_a_grid_tps=calib.candidate_tps_grid,
                    phase_b_grid_tps=calib.candidate_tps_grid_phase_b,
                    v24_total_committed_usd=total_committed_usd,
                    v24_calibration_total_max_usd=calib.total_max_usd,
                    largest_cell_max_output_tokens=(
                        calib.largest_cell_max_output_tokens
                    ),
                    smallest_cell_max_output_tokens=(
                        calib.smallest_cell_max_output_tokens
                    ),
                )
            except Exception as adapt_exc:  # noqa: BLE001
                logger.error(
                    "ADAPTIVE_STAGE_0_5_C_EXCEPTION %s: %s",
                    type(adapt_exc).__name__, adapt_exc,
                )
                adaptive_result = None
            else:
                # v2.6 wiring — adaptive result OVERRIDES the v2.4
                # outcome only when it ADMITs or emits a v2.5
                # cap-terminal outcome / C3 verdict. The v2.4 outcome
                # NEVER overrides a v2.5 selected outcome.
                if adaptive_result.get("outcome") == CALIBRATION_OUTCOME_SELECTED:
                    outcome = CALIBRATION_OUTCOME_SELECTED
                    selected_tps = adaptive_result["selected_peak_tps"]
                    selected_via = adaptive_result["selected_via"]
                    selected_at_phase = "adaptive"
                else:
                    # Non-selected adaptive outcome (C3 or cap-terminal)
                    # supersedes the v2.4 terminal because v2.5 promised
                    # additional search; the v2.4 trigger-matched outcome
                    # is preserved in the adaptive trace.
                    new_outcome = adaptive_result.get("outcome")
                    if new_outcome:
                        outcome = new_outcome
        else:
            logger.info(
                "ADAPTIVE_STAGE_0_5_C_SKIPPED reason=%s",
                adaptive_trigger_reason,
            )
    finally:
        out_fh.close()
        await _aclose_quiet(client)

    completed_at = _utc_now()
    # Task 019 v2.6 — schema_version bumps to v2.5 when adaptive ran,
    # carries the v2.5-approved extended outcome/selected_via enum and
    # ``selected_at_phase == 'adaptive'`` for C1/C2 selections.
    _schema_version = "task019.v2.3.calibration_result"
    if adaptive_result is not None:
        _schema_version = "task019.v2.5.calibration_result"
    result_doc: dict[str, Any] = {
        "schema_version": _schema_version,
        "experiment_id": cfg.experiment_id,
        "run_id_short": run_id_short,
        "outcome": outcome,
        "selected_peak_tps": selected_tps,
        "selected_via": selected_via,
        "selected_at_phase": selected_at_phase,
        # v2.3 fix loop #6 (auditor BLOCKER 1) — parent phase that
        # rooted a bracket-search selection (null for grid-ascending
        # selections, "A" or "B" for bracket-search selections).
        "selected_bracket_root_phase": selected_bracket_root_phase,
        "selected_at_candidate_idx": selected_at_candidate_idx,
        "selected_at_bracket_depth": selected_at_bracket_depth,
        "candidate_tps_grid": list(calib.candidate_tps_grid),
        "candidate_tps_grid_phase_b": list(
            calib.candidate_tps_grid_phase_b
        ),
        "candidate_tps_grid_pinned": list(CALIBRATION_CANDIDATE_TPS_GRID),
        "candidate_tps_grid_phase_b_pinned": list(
            CALIBRATION_CANDIDATE_TPS_GRID_PHASE_B
        ),
        "startup_abort_reason": None,
        "inconclusive_probe_role": inconclusive_probe_role,
        "inconclusive_at_candidate_tps": inconclusive_at_candidate_tps,
        "inconclusive_reason_detail": inconclusive_reason_detail,
        "bracket_trace": bracket_trace,
        "prompt_identity": {
            "source_corpus_sha256": source_corpus_sha,
            "assembled_system_prompt_sha256": sys_sha,
            "system_prompt_sha256": sys_sha,
            "user_prompts_source_sha256": user_prompts_source_sha,
            "user_prompts_index_set": list(cfg.user_prompts_index_set),
        },
        "deployment_used": deployment,
        "deployment_env": cfg.deployment.deployment_env,
        "endpoint_env": cfg.deployment.endpoint_env,
        "api_version": cfg.client.api_version,
        "sdk_max_retries": cfg.client.max_retries,
        "model": cfg.deployment.family,
        "pricing_snapshot_path": pricing_snapshot_path,
        "pricing_source_url": pricing.source_url,
        "pricing_accessed_date": pricing.accessed_date,
        "pricing_policy": pricing_policy_provenance or {},
        "started_at_iso": _iso8601_z(started_at),
        "completed_at_iso": _iso8601_z(completed_at),
        "total_usd": round(total_usd, 6),
        "total_committed_usd": round(total_committed_usd, 6),
        "calibration_total_max_usd": calib.total_max_usd,
        "calibration_probe_max_usd": calib.probe_max_usd,
        # v2.3 microfix 2026-05-30 fix loop #2 (auditor finding #3) —
        # echo BOTH phase call-count caps at top-level so the
        # calibration result is self-describing: the spend cap was
        # already echoed; per-phase call caps (probe_max_calls for
        # Phase A, probe_max_calls_phase_b for Phase B) were not. They
        # are now first-class top-level fields, matching the spend cap.
        "calibration_probe_max_calls_phase_a": calib.probe_max_calls,
        "calibration_probe_max_calls_phase_b": calib.probe_max_calls_phase_b,
        "concurrency_phase_a": cfg.runtime.concurrency,
        "concurrency_phase_b": cfg.runtime.concurrency_phase_b,
        "admitted_pressure_floor_ratio": (
            calib.admitted_pressure_floor_ratio
        ),
        "admitted_pressure_window_s": calib.admitted_pressure_window_s,
        "bracket_max_depth": calib.bracket_max_depth,
        "probes": [
            {
                "candidate_tps": p["candidate_tps"],
                "role": p["role"],
                "retry": p["retry"],
                "retry_suffix": p.get("retry_suffix"),
                "phase": p.get("phase"),
                "probe_phase": p.get("probe_phase"),
                "bracket_depth": p.get("bracket_depth"),
                "prompt_cache_key": p["prompt_cache_key"],
                "n_records": p.get("n_records"),
                "n_429_records": p.get("n_429_records"),
                "warm_criterion_passed": p.get("warm_criterion_passed"),
                "warm_criterion_hits": p.get("warm_criterion_hits"),
                "warm_criterion_considered": p.get(
                    "warm_criterion_considered"
                ),
                "backlog_p50_ms": p.get("backlog_p50_ms"),
                "backlog_p95_ms": p.get("backlog_p95_ms"),
                "backlog_max_ms": p.get("backlog_max_ms"),
                "backlog_excessive": p.get("backlog_excessive"),
                "all_empty_visible_output": p.get(
                    "all_empty_visible_output"
                ),
                "visible_output_mean_per_probe": p.get(
                    "visible_output_mean_per_probe"
                ),
                "visible_output_n_records": p.get(
                    "visible_output_n_records"
                ),
                "first_429_arrival_rpm": p.get("first_429_arrival_rpm"),
                "cache_hit_ratio_steady_state": p.get(
                    "cache_hit_ratio_steady_state"
                ),
                "admitted_pressure": p.get("admitted_pressure"),
                "first_429_metadata": p.get("first_429_metadata"),
                "probe_usd": round(p.get("probe_usd", 0.0), 6),
                "probe_committed_usd": round(
                    p.get("probe_committed_usd", 0.0), 6,
                ),
                # v2.3 microfix 2026-05-30 fix loop #2 (auditor
                # finding #3) — _probe_once stores the _run_cell halt
                # reason on each agg (one of
                # {"probe_max_calls_hit", "probe_max_usd_hit",
                # "total_max_usd_stop_event_set", None}); echo it into
                # the serialized probe dict so downstream audit can
                # see which advisory cap (if any) truncated the probe.
                "halt_reason": p.get("halt_reason"),
                "eligibility_outcome": (
                    _classify_eligibility(p) or "eligible"
                ),
            }
            for p in probes
        ],
        "retry_attempts": [
            {
                "candidate_tps": p["candidate_tps"],
                "role": p["role"],
                "retry_suffix": p.get("retry_suffix"),
                "prompt_cache_key": p["prompt_cache_key"],
            }
            for p in probes if p.get("retry")
        ],
        "halt_reason": halt_reason,
        "run_lock_metadata": run_lock_metadata,
    }
    with result_path.open("w", encoding="utf-8") as fh:
        json.dump(
            result_doc, fh, indent=2, sort_keys=True,
            default=_json_default,
        )

    # Sibling summary (the durable linkage anchor — contains the
    # calibration_result_sha256 referenced by smoke/evidence).
    calibration_sha = _sha256_file(result_path)
    summary_doc: dict[str, Any] = {
        "schema_version": "task019.v2.3.calibration_summary",
        "experiment_id": cfg.experiment_id,
        "run_id_short": run_id_short,
        "calibration_result_path": str(result_path),
        "calibration_result_sha256": calibration_sha,
        "outcome": outcome,
        "selected_peak_tps": selected_tps,
        "selected_via": selected_via,
        "selected_at_phase": selected_at_phase,
        # v2.3 fix loop #6 (auditor BLOCKER 1) — bracket-root parent
        # phase echo (null for grid-ascending selections).
        "selected_bracket_root_phase": selected_bracket_root_phase,
        "candidate_tps_grid": list(calib.candidate_tps_grid),
        "candidate_tps_grid_phase_b": list(
            calib.candidate_tps_grid_phase_b
        ),
        "total_usd": round(total_usd, 6),
        "total_committed_usd": round(total_committed_usd, 6),
        "started_at_iso": _iso8601_z(started_at),
        "completed_at_iso": _iso8601_z(completed_at),
        "n_probes": len(probes),
        "n_bracket_points_evaluated": len(bracket_trace),
        "halt_reason": halt_reason,
        "guardrail": V23_GUARDRAIL_STRING,
    }
    with summary_path.open("w", encoding="utf-8") as fh:
        json.dump(
            summary_doc, fh, indent=2, sort_keys=True,
            default=_json_default,
        )

    # Task 019 v2.6 — Stage 0.5.C adaptive summary writer. When the
    # §3.2 trigger matched and the orchestrator returned a result,
    # persist the v2.5 adaptive_calibration_summary alongside the
    # calibration result. Auditor microfix #1 item 7: trigger-false
    # MUST NOT write any adaptive artifact.
    if adaptive_result is not None:
        try:
            _write_adaptive_calibration_summary(
                runs_dir=runs_dir,
                timestamp_label=timestamp_label,
                cfg=cfg,
                run_id_short=run_id_short,
                git_commit=git_commit,
                dirty=dirty,
                deployment=deployment,
                pricing=pricing,
                pricing_snapshot_path=pricing_snapshot_path,
                prompt_identity_sha256=sys_sha,
                started_at=started_at,
                completed_at=completed_at,
                result_path=result_path,
                summary_path=summary_path,
                adaptive_result=adaptive_result,
                adaptive_block=cfg.runtime.adaptive_calibration,
                phase_a_probes=[
                    p for p in probes if p.get("phase") == "A"
                ],
                phase_b_probes=[
                    p for p in probes if p.get("phase") == "B"
                ],
            )
        except Exception as adapt_write_exc:  # noqa: BLE001
            # Adaptive summary writing must never destabilise the v2.4
            # primary result path. Log and continue; the calibration
            # result remains authoritative.
            logger.error(
                "ADAPTIVE_SUMMARY_WRITE_FAILED %s: %s",
                type(adapt_write_exc).__name__, adapt_write_exc,
            )

    # Raise CalibrationTerminalError for every non-selected outcome — the
    # caller maps it to exit 8 in main().
    if outcome != CALIBRATION_OUTCOME_SELECTED:
        raise CalibrationTerminalError(
            f"calibration outcome={outcome} (result written to "
            f"{result_path})",
            outcome=outcome or CALIBRATION_OUTCOME_NO_CONTRAST,
            inconclusive_probe_role=inconclusive_probe_role,
            inconclusive_at_candidate_tps=inconclusive_at_candidate_tps,
            inconclusive_reason_detail=inconclusive_reason_detail,
        )

    return MeasurementResult(
        cells_completed=len(probes),
        cells_planned=2 * (
            len(calib.candidate_tps_grid)
            + len(calib.candidate_tps_grid_phase_b)
        ),
        total_usd=total_usd,
        jsonl_path=jsonl_path,
        summary_path=summary_path,
        partial=False,
        halt_reason=halt_reason,
        cell_summaries=probes,
        run_lock_metadata=run_lock_metadata,
        total_committed_usd=total_committed_usd,
    )


# ----------------------------------------------------------------------------
# Task 019 v2.4 — Empirical-calibration-aware smoke / evidence promotion
# (spec: .internal/tasks/019-v2.4-empirical-calibration-aware-promotion.md)
# ----------------------------------------------------------------------------
#
# v2.4 adds an empirical-promotion gate that runs as §6 chain step 5, BEFORE
# the v2.1 TPM-feasibility gate (§6 chain step 6). When every invariant in
# §3.1 holds, the gate replaces the cold-cache projection's smallest-cell
# input with the calibration's already-paid-for warm-cache observation;
# numeric thresholds (0.85 × quota / 1.25 × quota) and selected_peak_tps are
# UNCHANGED. When any invariant fails, the runner evaluates the v2.1
# cold-cache feasibility AND either writes an admitted summary on cold-cache
# admit or an abort envelope on cold-cache deny. v2.3 pins are PRESERVED.
#
# All §10 RFC values are PINNED at module load. None of them is a runtime
# YAML knob; the YAML may CARRY them for explicit operator readback per §16
# DoD, but the loader enforces equality against the pinned constants.

# §10 microfix #1 pinned RFC values
EMPIRICAL_PROMOTION_CACHE_HIT_FLOOR_SMALLEST_CONTROL = 0.80
"""§10 microfix #1 PIN. v2.3 measured 0.8517 (smallest-cell control,
sha 92126b46…b81); margin +0.0517. NOT chosen to marginally include /
exclude any single observation. Loosening requires a fresh spec
revision."""

EMPIRICAL_PROMOTION_CACHE_HIT_FLOOR_LARGEST = 0.80
"""§10 microfix #1 PIN. v2.3 measured 0.8829 (largest-cell, sha
92126b46…b81); margin +0.0829. Symmetric with the smallest-cell floor
by design — the contrast contract is symmetric on
``cache_hit_ratio_steady_state``."""

EMPIRICAL_PROMOTION_CALIBRATION_MAX_AGE_HOURS = 24
"""§10 microfix #1 PIN. Matches the v2.2.1 `calibration_max_age_hours`
contract verbatim — no new operator surface."""

EMPIRICAL_PROMOTION_MINIMUM_RECORDS_AT_SELECTED_TPS = 30
"""§10 microfix #1 PIN. v2.3 produced 71 / 97 records; 30 is the
minimum window for which `n_429 == 0` is statistically meaningful at
`selected_peak_tps ≈ 0.475` (≥ 63 s of observation)."""

EMPIRICAL_PROMOTION_MINI_PROBE_ENABLED_DEFAULT = False
"""§10 microfix #1 PIN. Safest default; mini-probe is OPT-IN only with
auditor-approved YAML comment per §7."""

EMPIRICAL_PROMOTION_MINI_PROBE_MAX_USD = 1.00
"""§7 + §12 PIN. Per-mini-probe spend cap."""

EMPIRICAL_PROMOTION_MINI_PROBE_MAX_ATTEMPTS_PER_RUN = 1
"""§7 PIN. At most one mini-probe per smoke run + one per evidence run."""

WARM_PROJECTION_FLOOR_UNCACHED_TOKENS = 100
"""§4 PIN. ``effective_uncached_prompt_tokens`` floor — cache is never
100% effective; never project zero uncached input tokens. Used verbatim
in §11.16 pure-function test."""

# §7 — auditor-approved YAML comment shape for `mini_probe_enabled: true`.
_AUDITOR_APPROVED_COMMENT_RE = re.compile(
    r"^\s*#\s*auditor-approved-(\d{4}-\d{2}-\d{2}):\s*(\S+)\s*$"
)
"""v2.4 §7 — pattern for the operator-acknowledged comment above the
`mini_probe_enabled: true` YAML key. Matches `# auditor-approved-YYYY-MM-DD:
<auditor-handle>`."""

# §9 schema version literals — five distinct schemas
SCHEMA_VERSION_SMOKE_SUMMARY = "task019.v2.4.smoke_summary"
SCHEMA_VERSION_EVIDENCE_SUMMARY = "task019.v2.4.evidence_summary"
SCHEMA_VERSION_MINI_PROBE_RESULT = "task019.v2.4.mini_probe_result"
SCHEMA_VERSION_MINI_PROBE_SUMMARY = "task019.v2.4.mini_probe_summary"
SCHEMA_VERSION_ABORT_ENVELOPE = "task019.v2.4.abort_envelope"

# §3 promotion-path identifiers (three; never any other)
PROMOTION_PATH_COLD_CACHE_STRICT = "cold_cache_strict"
PROMOTION_PATH_EMPIRICAL_CALIBRATION_AWARE = "empirical_calibration_aware"
PROMOTION_PATH_MINI_PROBE_REVALIDATED = "mini_probe_revalidated"

PROMOTION_PATHS: frozenset[str] = frozenset({
    PROMOTION_PATH_COLD_CACHE_STRICT,
    PROMOTION_PATH_EMPIRICAL_CALIBRATION_AWARE,
    PROMOTION_PATH_MINI_PROBE_REVALIDATED,
})

# §4 / §4.2 — `largest_cell_projection_formula` literal values
LARGEST_CELL_PROJECTION_FORMULA_WARM = "v2.4_warm_projection"
LARGEST_CELL_PROJECTION_FORMULA_COLD = "v2.1_cold_cache_strict"

# §8 — §3.1 invariants 1–12 empirical-denial stable identifiers
EMPIRICAL_PROMOTION_DISABLED_OUTCOME_NOT_SELECTED = (
    "empirical_promotion_disabled_outcome_not_selected"
)
EMPIRICAL_PROMOTION_DISABLED_UNKNOWN_SELECTION_PROVENANCE = (
    "empirical_promotion_disabled_unknown_selection_provenance"
)
EMPIRICAL_PROMOTION_DISABLED_NO_LARGEST_429_AT_SELECTED_TPS = (
    "empirical_promotion_disabled_no_largest_429_at_selected_tps"
)
EMPIRICAL_PROMOTION_DISABLED_SMALLEST_CONTROL_OBSERVED_429 = (
    "empirical_promotion_disabled_smallest_control_observed_429"
)
EMPIRICAL_PROMOTION_DISABLED_SMALLEST_CONTROL_TOO_FEW_RECORDS = (
    "empirical_promotion_disabled_smallest_control_too_few_records"
)
EMPIRICAL_PROMOTION_DISABLED_CACHE_HIT_BELOW_FLOOR = (
    "empirical_promotion_disabled_cache_hit_below_floor"
)
EMPIRICAL_PROMOTION_DISABLED_CACHE_HIT_BELOW_FLOOR_LARGEST = (
    "empirical_promotion_disabled_cache_hit_below_floor_largest"
)
EMPIRICAL_PROMOTION_DISABLED_ADMITTED_PRESSURE_NOT_PASSED = (
    "empirical_promotion_disabled_admitted_pressure_not_passed"
)
EMPIRICAL_PROMOTION_DISABLED_DEPLOYMENT_IDENTITY_MISMATCH = (
    "empirical_promotion_disabled_deployment_identity_mismatch"
)
EMPIRICAL_PROMOTION_DISABLED_PRICING_SNAPSHOT_MISMATCH = (
    "empirical_promotion_disabled_pricing_snapshot_mismatch"
)
EMPIRICAL_PROMOTION_DISABLED_PTU_EVIDENCE_TRUE_OUT_OF_SCOPE = (
    "empirical_promotion_disabled_ptu_evidence_true_out_of_scope"
)
EMPIRICAL_PROMOTION_DISABLED_PTU_EVIDENCE_FIELD_MISSING_AND_CANNOT_INFER = (
    "empirical_promotion_disabled_ptu_evidence_field_missing_and_cannot_infer"
)
EMPIRICAL_PROMOTION_DISABLED_CALIBRATION_STALE_AND_MINI_PROBE_DISABLED = (
    "empirical_promotion_disabled_calibration_stale_and_mini_probe_disabled"
)
EMPIRICAL_PROMOTION_DISABLED_MINI_PROBE_FAILED_AND_COLD_CACHE_FAILS = (
    "empirical_promotion_disabled_mini_probe_failed_and_cold_cache_fails"
)
EMPIRICAL_PROMOTION_ENABLED_BUT_WARM_PROJECTION_STILL_OUTSIDE_CONTRACT = (
    "empirical_promotion_enabled_but_warm_projection_still_outside_contract"
)
EVIDENCE_SUMMARY_MISSING_SMOKE_PROMOTION_PATH_ECHO = (
    "evidence_summary_missing_smoke_promotion_path_echo"
)

# §8 — `mini_probe_failed_*` stable diagnostic identifiers (NEVER surface
# in the abort envelope's `empirical_promotion_denied_reason` per microfix
# #6 blocker 2; appear only in mini_probe_result / stderr / operator log /
# admitted-summary free-text suffix).
MINI_PROBE_FAILED_CACHE_NOT_WARM = "mini_probe_failed_cache_not_warm"
MINI_PROBE_FAILED_BACKLOG_EXCESSIVE = "mini_probe_failed_backlog_excessive"
MINI_PROBE_FAILED_ALL_EMPTY_VISIBLE_OUTPUT = (
    "mini_probe_failed_all_empty_visible_output"
)
MINI_PROBE_FAILED_ADMITTED_PRESSURE_INSUFFICIENT = (
    "mini_probe_failed_admitted_pressure_insufficient"
)
MINI_PROBE_FAILED_OBSERVED_429_ON_SMALLEST_CONTROL = (
    "mini_probe_failed_observed_429_on_smallest_control"
)
MINI_PROBE_FAILED_CACHE_HIT_BELOW_FLOOR = (
    "mini_probe_failed_cache_hit_below_floor"
)
MINI_PROBE_FAILED_TOO_FEW_RECORDS = "mini_probe_failed_too_few_records"

MINI_PROBE_FAILED_REASONS: frozenset[str] = frozenset({
    MINI_PROBE_FAILED_CACHE_NOT_WARM,
    MINI_PROBE_FAILED_BACKLOG_EXCESSIVE,
    MINI_PROBE_FAILED_ALL_EMPTY_VISIBLE_OUTPUT,
    MINI_PROBE_FAILED_ADMITTED_PRESSURE_INSUFFICIENT,
    MINI_PROBE_FAILED_OBSERVED_429_ON_SMALLEST_CONTROL,
    MINI_PROBE_FAILED_CACHE_HIT_BELOW_FLOOR,
    MINI_PROBE_FAILED_TOO_FEW_RECORDS,
})

# §8 — other v2.4 hard-exit identifiers (exit code 9, terminal)
MINI_PROBE_YAML_ENABLED_WITHOUT_AUDITOR_APPROVED_COMMENT = (
    "mini_probe_yaml_enabled_without_auditor_approved_comment"
)
MINI_PROBE_ATTEMPTED_MORE_THAN_ONCE_PER_RUN = (
    "mini_probe_attempted_more_than_once_per_run"
)

# §9.4 abort-envelope `exit_reason` — for the empirical-denial-followed-by-
# cold-cache-denial terminal case this is ALWAYS `TPM_FEASIBILITY_ABORT`
# (exit code 1, v2.1 PRESERVED — microfix #5 blocker 1 pin).
TPM_FEASIBILITY_ABORT_EXIT_REASON = "TPM_FEASIBILITY_ABORT"

# §9.4 abort-envelope forbidden fields (admitted-summary block fields that
# MUST NOT appear in the abort envelope; schema validator rejects on
# presence)
ABORT_ENVELOPE_FORBIDDEN_FIELDS: frozenset[str] = frozenset({
    "tpm_feasibility_promotion_inputs",
    "tpm_feasibility_promotion_path",
    "tpm_feasibility_promotion_decision_reason",
    "empirical_warm_projection_inputs",
    "mini_probe_result",
    "mini_probe_result_sha256",
    "ptu_evidence_inferred",
    "ptu_evidence_inference_basis",
    "largest_cell_projection_formula",
    "smoke_summary_reference",
})

# §11.18 v2.3 fixture identifiers (microfix #2 / #3 / #4 corrected)
V23_FIXTURE_DEPLOYMENT_USED = "ptu-deploy-throttled"
V23_FIXTURE_DEPLOYMENT_ENV = "AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED"
V23_FIXTURE_EXPERIMENT_ID = "exp007_max_output_tokens_sweep"
V23_FIXTURE_PRICING_SNAPSHOT_PATH = "pricing/azure-openai-payg-2026-05.yaml"
V23_FIXTURE_CALIBRATION_RESULT_SHA256 = (
    "92126b46ab4320ba38566229292b3b89922d7d58e42a97c43224d67e6a75db81"
)


@dataclass(frozen=True)
class EmpiricalPromotionConfig:
    """v2.4 §10 PINNED RFC values — runtime YAML may CARRY these keys
    under `runtime.empirical_promotion.*` for operator readback, but
    the loader enforces equality against the pinned module constants.

    No field on this dataclass is a runtime knob. Operators cannot
    loosen any pinned value per-run; spec revision is the only path.
    """

    cache_hit_floor_smallest_control: float = (
        EMPIRICAL_PROMOTION_CACHE_HIT_FLOOR_SMALLEST_CONTROL
    )
    cache_hit_floor_largest: float = EMPIRICAL_PROMOTION_CACHE_HIT_FLOOR_LARGEST
    calibration_max_age_hours: int = EMPIRICAL_PROMOTION_CALIBRATION_MAX_AGE_HOURS
    minimum_records_at_selected_tps: int = (
        EMPIRICAL_PROMOTION_MINIMUM_RECORDS_AT_SELECTED_TPS
    )
    mini_probe_enabled: bool = EMPIRICAL_PROMOTION_MINI_PROBE_ENABLED_DEFAULT
    mini_probe_max_usd: float = EMPIRICAL_PROMOTION_MINI_PROBE_MAX_USD
    mini_probe_max_attempts_per_run: int = (
        EMPIRICAL_PROMOTION_MINI_PROBE_MAX_ATTEMPTS_PER_RUN
    )


@dataclass(frozen=True)
class EmpiricalPromotionDecision:
    """v2.4 promotion-gate decision. Pure data — no I/O.

    Returned by `evaluate_empirical_promotion_gate()`. Consumers compose
    this with `build_admitted_smoke_summary()` /
    `build_admitted_evidence_summary()` / `build_abort_envelope()` to
    materialise the §9 artifact.
    """

    promotion_path: str  # one of PROMOTION_PATHS
    largest_cell_projection_formula: str  # warm | cold
    empirical_denied_reason: str | None
    decision_reason_suffix: str  # short identifier + free-text suffix
    cold_cache_admits: bool
    cold_cache_smallest_tpm: float
    cold_cache_largest_tpm: float
    warm_projection_smallest_tpm: float | None
    warm_projection_largest_tpm: float | None
    warm_projection_inputs: dict[str, Any] | None
    ptu_evidence_inferred: bool | None
    ptu_evidence_inference_basis: dict[str, bool] | None
    mini_probe_result: dict[str, Any] | None
    mini_probe_result_sha256: str | None
    smoke_promotion_admits: bool
    feasibility_failure_reason: str | None


# §4 / §4.2 — pure projection helpers


def compute_effective_uncached_prompt_tokens(
    *,
    base_prompt_tokens_for_gate: int,
    cache_hit_ratio_steady_state: float,
    floor: int = WARM_PROJECTION_FLOOR_UNCACHED_TOKENS,
) -> float:
    """§4 — ``effective_uncached_prompt_tokens = max(base * (1 - r), 100)``.

    Cache is never 100% effective; the ``floor`` (default 100) keeps the
    projection conservative even when measured cache-hit ratio is very
    high.

    Args:
        base_prompt_tokens_for_gate: Cold-cache base from the canonical
            ``max(assembled, target) + 100`` formula (= 2158 at v2.1 pins).
        cache_hit_ratio_steady_state: Measured warm-cache fraction in
            ``[0, 1]``.
        floor: Conservative floor on the uncached-token result. Default
            is the §4 PIN of 100.

    Returns:
        The effective uncached prompt-token count.

    Raises:
        ValueError: any input out of range.
    """
    if base_prompt_tokens_for_gate <= 0:
        raise ValueError(
            f"base_prompt_tokens_for_gate must be > 0; got "
            f"{base_prompt_tokens_for_gate}"
        )
    if not 0.0 <= cache_hit_ratio_steady_state <= 1.0:
        raise ValueError(
            f"cache_hit_ratio_steady_state must be in [0, 1]; got "
            f"{cache_hit_ratio_steady_state}"
        )
    if floor <= 0:
        raise ValueError(f"floor must be > 0; got {floor}")
    return max(
        float(base_prompt_tokens_for_gate)
        * (1.0 - float(cache_hit_ratio_steady_state)),
        float(floor),
    )


def compute_warm_projection_tpm(
    *,
    selected_peak_tps: float,
    effective_uncached_prompt_tokens: float,
    max_output_tokens: int,
) -> float:
    """§4 warm-projection formula.

    ``projected_tpm_warm = 60 × selected_peak_tps ×
        (effective_uncached_prompt_tokens + max_output_tokens)``
    """
    if selected_peak_tps <= 0:
        raise ValueError(
            f"selected_peak_tps must be > 0; got {selected_peak_tps}"
        )
    if effective_uncached_prompt_tokens <= 0:
        raise ValueError(
            f"effective_uncached_prompt_tokens must be > 0; got "
            f"{effective_uncached_prompt_tokens}"
        )
    if max_output_tokens <= 0:
        raise ValueError(
            f"max_output_tokens must be > 0; got {max_output_tokens}"
        )
    return (
        60.0
        * float(selected_peak_tps)
        * (float(effective_uncached_prompt_tokens) + float(max_output_tokens))
    )


# §3.1 invariant 11 — backward-compatibility PTU inference


def evaluate_ptu_evidence_inference_basis(
    *,
    calibration_result: dict[str, Any],
    smoke_yaml_metadata: dict[str, Any],
    pricing_snapshot_path_resolves_committed_payg: bool,
    terminal_report_lists_calibration_sha_payg_not_ptu: bool,
) -> tuple[bool, dict[str, bool]]:
    """§3.1 invariant 11 five-condition PAYG-not-PTU inference.

    When ``calibration_result.metadata.ptu_evidence`` is ABSENT (the v2.3
    fixture case), the runner admits the calibration as PAYG-not-PTU IFF
    every one of FIVE named conditions holds. This function returns the
    individual condition values plus the overall admit flag.

    Args:
        calibration_result: Loaded `calibration_result.json` dict.
        smoke_yaml_metadata: The smoke / evidence YAML's `metadata` block.
        pricing_snapshot_path_resolves_committed_payg: Caller-supplied
            truthy IFF the calibration's `pricing_snapshot_path` resolves
            to an existing committed PAYG pricing snapshot under
            `pricing/` AND the snapshot YAML's top-level `source_url`
            (microfix #4) is a non-empty HTTPS URL AND
            `calibration_result.pricing_accessed_date` is present and
            non-empty. Caller computes this from the snapshot YAML.
        terminal_report_lists_calibration_sha_payg_not_ptu: Caller-
            supplied truthy IFF the committed v2.3 terminal report
            enumerates the calibration's sha256 with explicit
            PAYG-not-PTU classification AND the smoke / evidence YAML's
            `metadata.ptu_evidence == false`.

    Returns:
        Tuple ``(admit, basis)`` where ``basis`` is a five-key dict
        per the §9.1 `ptu_evidence_inference_basis` block. The five
        keys are the literal names enumerated in the schema body.
    """
    cond_deployment = (
        calibration_result.get("deployment_used")
        == V23_FIXTURE_DEPLOYMENT_USED
    )
    cond_env = (
        calibration_result.get("deployment_env")
        == V23_FIXTURE_DEPLOYMENT_ENV
    )
    cond_experiment = (
        calibration_result.get("experiment_id")
        == V23_FIXTURE_EXPERIMENT_ID
    )
    cond_pricing = bool(pricing_snapshot_path_resolves_committed_payg)
    cond_yaml_and_terminal_report = (
        bool(smoke_yaml_metadata.get("ptu_evidence") is False)
        and bool(terminal_report_lists_calibration_sha_payg_not_ptu)
    )
    basis = {
        "deployment_used_eq_gpt_5_2_throttled": bool(cond_deployment),
        "deployment_env_eq_AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED": (
            bool(cond_env)
        ),
        "experiment_id_eq_exp007_max_output_tokens_sweep": (
            bool(cond_experiment)
        ),
        "pricing_snapshot_path_resolves_committed_payg": cond_pricing,
        "yaml_metadata_ptu_evidence_false_and_terminal_report_lists_sha": (
            bool(cond_yaml_and_terminal_report)
        ),
    }
    admit = all(basis.values())
    return admit, basis


def resolve_pricing_snapshot_payg_admissibility(
    *,
    calibration_pricing_snapshot_path: str | None,
    calibration_pricing_accessed_date: str | None,
    repo_root: pathlib.Path,
) -> bool:
    """§3.1 invariant 11 condition 4 (microfix #3 + #4).

    Returns True IFF:
      (a) ``calibration_pricing_accessed_date`` is present and non-empty,
      (b) the resolved snapshot YAML carries a top-level ``source_url``
          (microfix #4 corrected the live rule from the prior ``source``
          wording) field whose value is a non-empty HTTPS URL,
      (c) the snapshot path resolves under ``pricing/`` (microfix #3
          corrected the resolution directory from the prior
          ``data/pricing-snapshots/`` wording).

    Args:
        calibration_pricing_snapshot_path: Calibration result's
            `pricing_snapshot_path` (e.g.
            ``pricing/azure-openai-payg-2026-05.yaml``).
        calibration_pricing_accessed_date: Calibration result's
            `pricing_accessed_date` (e.g. ``2026-05-19``).
        repo_root: Repository root used to resolve the snapshot path.

    Returns:
        Bool — see semantics above.
    """
    if not isinstance(calibration_pricing_snapshot_path, str) or not (
        calibration_pricing_snapshot_path.strip()
    ):
        return False
    if not isinstance(calibration_pricing_accessed_date, str) or not (
        calibration_pricing_accessed_date.strip()
    ):
        return False
    snapshot_path = pathlib.Path(calibration_pricing_snapshot_path)
    if not snapshot_path.is_absolute():
        snapshot_path = repo_root / snapshot_path
    # Microfix #3 — must resolve under `pricing/`.
    try:
        resolved = snapshot_path.resolve()
    except OSError:
        return False
    try:
        resolved.relative_to((repo_root / "pricing").resolve())
    except ValueError:
        return False
    if not resolved.is_file():
        return False
    try:
        snapshot_text = resolved.read_text(encoding="utf-8")
        snapshot_doc = yaml.safe_load(snapshot_text) or {}
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(snapshot_doc, dict):
        return False
    # Microfix #4 — top-level key is `source_url` (not `source`).
    source_url = snapshot_doc.get("source_url")
    if not isinstance(source_url, str) or not source_url.strip():
        return False
    if not source_url.startswith("https://"):
        return False
    return True


# §3.1 invariant 11 condition 5 — committed-terminal-report wiring.
#
# The v2.3 backward-compatibility inference admits a calibration as
# PAYG-not-PTU only when the calibration's sha256 is enumerated by a
# committed terminal report under explicit PAYG-not-PTU classification
# AND a Task 019 v2.3/v2.4 context marker. This helper is the SINGLE
# production wiring point that computes the named condition flag the
# CLI passes through `_run_measurement_async` ->
# `evaluate_empirical_promotion_gate(..., terminal_report_lists_
# calibration_sha_payg_not_ptu=...)`.
#
# Canonical sources (checked in order; the first file that satisfies
# ALL THREE coexistence markers wins):
#
#   1. ``benchmarks/07-max-output-tokens-reservation/
#       live-calibration-smoke-evidence-final.md`` — the human-curated
#       Task 019 v2.3 live terminal report referenced by the v2.4 spec
#       §1 and §17 as the authoritative live-run record.
#   2. ``CHANGELOG.md`` — append-only project changelog whose Task 019
#       v2.3 and v2.4 entries enumerate the same calibration sha256
#       with the same PAYG-not-PTU classification (verified at the
#       call site by string coexistence in the same file body).

TERMINAL_REPORT_CANONICAL_PATHS: tuple[str, ...] = (
    "benchmarks/07-max-output-tokens-reservation/"
    "live-calibration-smoke-evidence-final.md",
    "CHANGELOG.md",
)

_TERMINAL_REPORT_PAYG_NOT_PTU_MARKERS: tuple[str, ...] = (
    "PAYG-not-PTU",
    "PAYG, not PTU",
)

_TERMINAL_REPORT_TASK_CONTEXT_MARKERS: tuple[str, ...] = (
    "Task 019 v2.3",
    "Task 019 v2.4",
)


def verify_terminal_report_lists_calibration_sha_payg_not_ptu(
    *,
    repo_root: pathlib.Path,
    calibration_result_sha256: str | None,
    candidate_paths: tuple[str, ...] | None = None,
) -> bool:
    """v2.4 operational wiring — §3.1 invariant 11 condition 5.

    Returns True IFF at least one committed canonical terminal report
    under ``repo_root`` simultaneously contains ALL THREE markers in
    the same file body:

      (a) the calibration result sha256 (lowercase hex literal),
      (b) explicit PAYG-not-PTU classification phrasing
          (``PAYG-not-PTU`` or ``PAYG, not PTU``),
      (c) a Task 019 v2.3 or v2.4 context marker
          (``Task 019 v2.3`` or ``Task 019 v2.4``).

    All three markers MUST coexist in the same file so that the
    inference cannot promote off a stale or unrelated mention of the
    sha in some other artefact. Per §3.1 the YAML
    ``metadata.ptu_evidence == false`` check is enforced separately
    inside ``evaluate_ptu_evidence_inference_basis``; this helper
    deliberately does NOT inspect the smoke YAML — it answers only
    the "is the sha listed in the committed terminal report" half.

    Args:
        repo_root: Repository root used to resolve canonical paths.
        calibration_result_sha256: Lowercase hex sha256 of the
            calibration result file; pass ``None`` (or an empty
            string / malformed value) to short-circuit ``False``.
        candidate_paths: Override list for tests; defaults to
            ``TERMINAL_REPORT_CANONICAL_PATHS``.

    Returns:
        Bool — see semantics above. Never raises; missing files,
        permission errors, or non-UTF-8 decode errors are silently
        treated as a non-match for the affected candidate path so
        that the gate degrades closed.
    """
    if not isinstance(calibration_result_sha256, str):
        return False
    sha_lower = calibration_result_sha256.strip().lower()
    if len(sha_lower) != 64 or any(
        c not in "0123456789abcdef" for c in sha_lower
    ):
        return False
    paths = (
        candidate_paths
        if candidate_paths is not None
        else TERMINAL_REPORT_CANONICAL_PATHS
    )
    for rel in paths:
        candidate = repo_root / rel
        try:
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if sha_lower not in text.lower():
            continue
        if not any(m in text for m in _TERMINAL_REPORT_PAYG_NOT_PTU_MARKERS):
            continue
        if not any(m in text for m in _TERMINAL_REPORT_TASK_CONTEXT_MARKERS):
            continue
        return True
    return False


# §3.1 — core gate evaluation


def _parse_iso8601(s: str) -> datetime.datetime:
    """Parse a UTC ISO-8601 timestamp; supports trailing ``Z`` and
    sub-second precision. Naive — caller controls semantics."""
    return _parse_iso8601_z(s)


def _select_probe_at_selection_point(
    calibration_result: dict[str, Any], *, role: str
) -> dict[str, Any] | None:
    """Return the calibration probe matching the selection point's
    (selected_at_phase, selected_at_bracket_depth, selected_peak_tps,
    role) tuple, or ``None`` if absent.

    The selection point is fully determined by the calibration result's
    own metadata; v2.4 does NOT permit overriding any of the four keys.
    """
    selected_peak_tps = calibration_result.get("selected_peak_tps")
    selected_at_phase = calibration_result.get("selected_at_phase")
    selected_at_bracket_depth = calibration_result.get(
        "selected_at_bracket_depth"
    )
    if selected_peak_tps is None:
        return None
    for probe in calibration_result.get("probes", []) or []:
        if probe.get("role") != role:
            continue
        if probe.get("candidate_tps") != selected_peak_tps:
            continue
        if probe.get("phase") != selected_at_phase:
            continue
        if probe.get("bracket_depth") != selected_at_bracket_depth:
            continue
        return probe
    return None


def evaluate_empirical_promotion_gate(  # noqa: PLR0911, PLR0912, PLR0915
    *,
    calibration_result: dict[str, Any],
    smoke_yaml_metadata: dict[str, Any],
    smoke_runner_resolved: dict[str, Any],
    config: EmpiricalPromotionConfig,
    deployment_tpm_quota: int,
    base_prompt_tokens_for_gate: int,
    smallest_cell_max_output_tokens: int,
    largest_cell_max_output_tokens: int,
    now_provider: Callable[[], datetime.datetime],
    pricing_snapshot_path_resolves_committed_payg: bool,
    terminal_report_lists_calibration_sha_payg_not_ptu: bool,
    mini_probe_callable: Callable[[], dict[str, Any]] | None = None,
    mini_probe_attempts_so_far: int = 0,
) -> EmpiricalPromotionDecision:
    """§3.1 invariants 1–12 evaluator. Returns an
    `EmpiricalPromotionDecision`; raises ONLY ``ValueError`` on malformed
    inputs. All denial-paths return a decision whose
    ``empirical_denied_reason`` names the §8 stable identifier and
    whose ``promotion_path`` falls back to ``cold_cache_strict`` (or
    ``mini_probe_revalidated`` when invariant 12 fails with the mini-probe
    enabled and passing).

    Caller (the runner) is responsible for materialising the §9 artifact
    (admitted summary on cold-cache-admit, abort envelope on cold-cache-
    deny). This function does NOT touch disk and does NOT depend on the
    wall clock — ``now_provider`` is injected for §11.18 frozen-clock
    tests.

    Args:
        calibration_result: Loaded calibration_result.json dict.
        smoke_yaml_metadata: The smoke / evidence YAML's `metadata` block.
        smoke_runner_resolved: Dict with keys ``deployment_used``,
            ``model``, ``api_version``, ``pricing_snapshot_path``,
            ``pricing_accessed_date`` — the smoke/evidence runner's
            resolved values for identity checks (invariants 9–10).
        config: Empirical-promotion config; v2.4 pins enforced upstream.
        deployment_tpm_quota: e.g. 60000.
        base_prompt_tokens_for_gate: v2.1 PIN = 2158.
        smallest_cell_max_output_tokens: e.g. 256.
        largest_cell_max_output_tokens: e.g. 16384.
        now_provider: Returns the current UTC datetime for freshness
            evaluation. Production binds ``lambda:
            datetime.datetime.now(datetime.timezone.utc)``; §11.18
            sub-tests inject a frozen value.
        pricing_snapshot_path_resolves_committed_payg: §3.1 invariant 11
            condition 4 truth value, caller-computed via
            ``resolve_pricing_snapshot_payg_admissibility``.
        terminal_report_lists_calibration_sha_payg_not_ptu: §3.1
            invariant 11 condition 5's terminal-report half, caller-
            supplied (the runner reads the v2.3 terminal report once).
        mini_probe_callable: When invariant 12 is the ONLY denial AND
            ``config.mini_probe_enabled`` is True, the gate calls this
            zero-arg callable. The callable's return value is the
            mini-probe result dict per §9.2; if it raises, the gate
            treats this as a mini-probe gate failure. Set to ``None`` to
            skip the mini-probe path (caller controls eligibility).
        mini_probe_attempts_so_far: Defensive counter; raises if
            ``mini_probe_max_attempts_per_run`` would be exceeded.

    Returns:
        EmpiricalPromotionDecision.
    """
    selected_peak_tps = calibration_result.get("selected_peak_tps")
    if selected_peak_tps is None:
        raise ValueError(
            "calibration_result.selected_peak_tps is required for v2.4 "
            "promotion-gate evaluation"
        )
    selected_peak_tps_f = float(selected_peak_tps)
    # Cold-cache feasibility is always evaluated as the fallback baseline.
    cold_smallest = compute_projected_tpm_cell(
        peak_ramp_tps=selected_peak_tps_f,
        base_prompt_tokens_for_gate=base_prompt_tokens_for_gate,
        max_output_tokens=smallest_cell_max_output_tokens,
    )
    cold_largest = compute_projected_tpm_cell(
        peak_ramp_tps=selected_peak_tps_f,
        base_prompt_tokens_for_gate=base_prompt_tokens_for_gate,
        max_output_tokens=largest_cell_max_output_tokens,
    )
    lower_threshold = TPM_LOWER_GATE_FRACTION * float(deployment_tpm_quota)
    upper_threshold = TPM_UPPER_GATE_FRACTION * float(deployment_tpm_quota)
    cold_admits = (
        cold_smallest <= lower_threshold and cold_largest >= upper_threshold
    )

    def _fallback(
        reason: str, suffix_note: str = ""
    ) -> EmpiricalPromotionDecision:
        suffix = (
            f"{reason}{(' ' + suffix_note) if suffix_note else ''}"
        )
        return EmpiricalPromotionDecision(
            promotion_path=PROMOTION_PATH_COLD_CACHE_STRICT,
            largest_cell_projection_formula=(
                LARGEST_CELL_PROJECTION_FORMULA_COLD
            ),
            empirical_denied_reason=reason,
            decision_reason_suffix=suffix,
            cold_cache_admits=cold_admits,
            cold_cache_smallest_tpm=cold_smallest,
            cold_cache_largest_tpm=cold_largest,
            warm_projection_smallest_tpm=None,
            warm_projection_largest_tpm=None,
            warm_projection_inputs=None,
            ptu_evidence_inferred=None,
            ptu_evidence_inference_basis=None,
            mini_probe_result=None,
            mini_probe_result_sha256=None,
            smoke_promotion_admits=cold_admits,
            feasibility_failure_reason=(
                None if cold_admits else _classify_feasibility_failure(
                    smallest_tpm=cold_smallest,
                    largest_tpm=cold_largest,
                    lower_threshold=lower_threshold,
                    upper_threshold=upper_threshold,
                )
            ),
        )

    # Invariant 1 — outcome is "selected"
    if calibration_result.get("outcome") != CALIBRATION_OUTCOME_SELECTED:
        return _fallback(EMPIRICAL_PROMOTION_DISABLED_OUTCOME_NOT_SELECTED)

    # Invariant 2 — selection provenance is from the known set
    selected_via = calibration_result.get("selected_via")
    selected_at_phase = calibration_result.get("selected_at_phase")
    bracket_depth = calibration_result.get("selected_at_bracket_depth")
    # Task 019 v2.5 (spec §6 item 1): the v2.4 known set is EXTENDED
    # to include the two adaptive selected_via values and the
    # ``"adaptive"`` selected_at_phase. v2.5 adaptive C1 / C2 results
    # are admitted through this preflight unchanged; only OTHER /
    # unknown provenance still falls back through
    # EMPIRICAL_PROMOTION_DISABLED_UNKNOWN_SELECTION_PROVENANCE.
    allowed_via = {
        "phase_a",
        "phase_b",
        "bracket_search",
        "adaptive_strict_separating_tps",
        "adaptive_onset_separation_replicate_confirmed",
    }
    allowed_phase = {"A", "B", "bracket", "adaptive"}
    if (
        selected_via not in allowed_via
        or selected_at_phase not in allowed_phase
        or (
            selected_at_phase == "bracket"
            and isinstance(bracket_depth, int)
            and bracket_depth > BRACKET_MAX_DEPTH
        )
    ):
        return _fallback(
            EMPIRICAL_PROMOTION_DISABLED_UNKNOWN_SELECTION_PROVENANCE
        )

    largest_probe = _select_probe_at_selection_point(
        calibration_result, role="largest"
    )
    smallest_probe = _select_probe_at_selection_point(
        calibration_result, role="smallest_control"
    )

    # Invariant 3 — largest-cell contrast is real
    if largest_probe is None or int(
        largest_probe.get("n_429_records", 0) or 0
    ) < 1:
        return _fallback(
            EMPIRICAL_PROMOTION_DISABLED_NO_LARGEST_429_AT_SELECTED_TPS
        )

    # Invariant 4 — smallest-cell control observed zero 429s with ≥ 30 records
    if smallest_probe is None:
        return _fallback(
            EMPIRICAL_PROMOTION_DISABLED_SMALLEST_CONTROL_OBSERVED_429
        )
    if int(smallest_probe.get("n_429_records", 0) or 0) >= 1:
        return _fallback(
            EMPIRICAL_PROMOTION_DISABLED_SMALLEST_CONTROL_OBSERVED_429
        )
    if int(smallest_probe.get("n_records", 0) or 0) < int(
        config.minimum_records_at_selected_tps
    ):
        return _fallback(
            EMPIRICAL_PROMOTION_DISABLED_SMALLEST_CONTROL_TOO_FEW_RECORDS
        )

    # Invariant 5 — smallest-cell cache-hit floor
    measured_chr_smallest = float(
        smallest_probe.get("cache_hit_ratio_steady_state", 0.0) or 0.0
    )
    if measured_chr_smallest < config.cache_hit_floor_smallest_control:
        delta = (
            measured_chr_smallest - config.cache_hit_floor_smallest_control
        )
        return _fallback(
            EMPIRICAL_PROMOTION_DISABLED_CACHE_HIT_BELOW_FLOOR,
            f"measured={measured_chr_smallest:.4f} "
            f"floor={config.cache_hit_floor_smallest_control:.4f} "
            f"delta={delta:+.4f}",
        )

    # Invariant 6 — largest-cell cache-hit floor
    measured_chr_largest = float(
        largest_probe.get("cache_hit_ratio_steady_state", 0.0) or 0.0
    )
    if measured_chr_largest < config.cache_hit_floor_largest:
        delta = measured_chr_largest - config.cache_hit_floor_largest
        return _fallback(
            EMPIRICAL_PROMOTION_DISABLED_CACHE_HIT_BELOW_FLOOR_LARGEST,
            f"measured={measured_chr_largest:.4f} "
            f"floor={config.cache_hit_floor_largest:.4f} "
            f"delta={delta:+.4f}",
        )

    # Invariant 7 — admitted-pressure gate passed on both selected probes
    def _admp_passed(probe: dict[str, Any]) -> bool:
        block = probe.get("admitted_pressure") or {}
        return bool(block.get("admitted_pressure_passed", False))

    if not (_admp_passed(smallest_probe) and _admp_passed(largest_probe)):
        return _fallback(
            EMPIRICAL_PROMOTION_DISABLED_ADMITTED_PRESSURE_NOT_PASSED
        )

    # Invariant 9 — deployment / model / api_version identity match
    # (invariant 8 — prompt identity — is enforced upstream by
    # validate_calibration_result; v2.4 keeps the v2.3 exit verbatim.)
    for k in ("deployment_used", "model", "api_version"):
        if calibration_result.get(k) != smoke_runner_resolved.get(k):
            return _fallback(
                EMPIRICAL_PROMOTION_DISABLED_DEPLOYMENT_IDENTITY_MISMATCH,
                f"mismatched_field={k}",
            )

    # Invariant 10 — pricing snapshot identity match
    if (
        calibration_result.get("pricing_snapshot_path")
        != smoke_runner_resolved.get("pricing_snapshot_path")
        or calibration_result.get("pricing_accessed_date")
        != smoke_runner_resolved.get("pricing_accessed_date")
    ):
        return _fallback(
            EMPIRICAL_PROMOTION_DISABLED_PRICING_SNAPSHOT_MISMATCH
        )

    # Invariant 11 — PTU evidence: false (with backward-compatibility
    # inference rule for the v2.3 fixture case where metadata.ptu_evidence
    # is absent).
    ptu_meta = (calibration_result.get("metadata") or {}).get("ptu_evidence")
    yaml_ptu = smoke_yaml_metadata.get("ptu_evidence")
    inferred_basis: dict[str, bool] | None = None
    inferred: bool | None = None
    if ptu_meta is not None:
        # Primary rule (field present).
        if ptu_meta is True or yaml_ptu is True:
            return _fallback(
                EMPIRICAL_PROMOTION_DISABLED_PTU_EVIDENCE_TRUE_OUT_OF_SCOPE
            )
        inferred = None
        inferred_basis = None
    else:
        # Backward-compatibility inference rule.
        admit, basis = evaluate_ptu_evidence_inference_basis(
            calibration_result=calibration_result,
            smoke_yaml_metadata=smoke_yaml_metadata,
            pricing_snapshot_path_resolves_committed_payg=(
                pricing_snapshot_path_resolves_committed_payg
            ),
            terminal_report_lists_calibration_sha_payg_not_ptu=(
                terminal_report_lists_calibration_sha_payg_not_ptu
            ),
        )
        if not admit:
            decision = _fallback(
                EMPIRICAL_PROMOTION_DISABLED_PTU_EVIDENCE_FIELD_MISSING_AND_CANNOT_INFER  # noqa: E501
            )
            # Preserve the basis on the decision so caller diagnostics
            # (decision_reason_suffix / stderr / operator log) can name
            # the specific failing condition.
            return EmpiricalPromotionDecision(
                promotion_path=decision.promotion_path,
                largest_cell_projection_formula=(
                    decision.largest_cell_projection_formula
                ),
                empirical_denied_reason=decision.empirical_denied_reason,
                decision_reason_suffix=(
                    f"{decision.decision_reason_suffix} "
                    f"failing_conditions="
                    f"{sorted(k for k, v in basis.items() if not v)}"
                ),
                cold_cache_admits=decision.cold_cache_admits,
                cold_cache_smallest_tpm=decision.cold_cache_smallest_tpm,
                cold_cache_largest_tpm=decision.cold_cache_largest_tpm,
                warm_projection_smallest_tpm=None,
                warm_projection_largest_tpm=None,
                warm_projection_inputs=None,
                ptu_evidence_inferred=None,
                ptu_evidence_inference_basis=None,
                mini_probe_result=None,
                mini_probe_result_sha256=None,
                smoke_promotion_admits=decision.smoke_promotion_admits,
                feasibility_failure_reason=(
                    decision.feasibility_failure_reason
                ),
            )
        inferred = True
        inferred_basis = basis

    # Invariant 12 — calibration freshness
    completed_at_iso = calibration_result.get("completed_at_iso")
    if not isinstance(completed_at_iso, str) or not completed_at_iso:
        return _fallback(
            EMPIRICAL_PROMOTION_DISABLED_CALIBRATION_STALE_AND_MINI_PROBE_DISABLED  # noqa: E501
        )
    try:
        completed_dt = _parse_iso8601(completed_at_iso)
    except ValueError:
        return _fallback(
            EMPIRICAL_PROMOTION_DISABLED_CALIBRATION_STALE_AND_MINI_PROBE_DISABLED  # noqa: E501
        )
    now_dt = now_provider()
    if completed_dt.tzinfo is None:
        completed_dt = completed_dt.replace(tzinfo=datetime.timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=datetime.timezone.utc)
    age_hours = (now_dt - completed_dt).total_seconds() / 3600.0
    if age_hours > config.calibration_max_age_hours:
        # Invariant 12 sole denial — consider mini-probe revalidation.
        if not config.mini_probe_enabled or mini_probe_callable is None:
            return _fallback(
                EMPIRICAL_PROMOTION_DISABLED_CALIBRATION_STALE_AND_MINI_PROBE_DISABLED  # noqa: E501
            )
        if mini_probe_attempts_so_far >= config.mini_probe_max_attempts_per_run:
            return _fallback(
                EMPIRICAL_PROMOTION_DISABLED_MINI_PROBE_FAILED_AND_COLD_CACHE_FAILS  # noqa: E501
            )
        try:
            mini_probe_result = mini_probe_callable()
        except Exception:  # noqa: BLE001 — defensive: treat any failure as a
            # mini-probe gate failure; raw reason is in stderr / mini-probe
            # artifact, NOT in abort-envelope `empirical_promotion_denied_reason`
            # (microfix #6 blocker 2).
            return _fallback(
                EMPIRICAL_PROMOTION_DISABLED_MINI_PROBE_FAILED_AND_COLD_CACHE_FAILS  # noqa: E501
            )
        outcome = (mini_probe_result or {}).get("mini_probe_outcome")
        if not isinstance(outcome, str) or not outcome.startswith("passed"):
            return _fallback(
                EMPIRICAL_PROMOTION_DISABLED_MINI_PROBE_FAILED_AND_COLD_CACHE_FAILS  # noqa: E501
            )
        # Mini-probe passed → promotion path is mini_probe_revalidated.
        chr_smallest_for_projection = float(
            mini_probe_result.get(
                "mini_probe_cache_hit_ratio_steady_state", 0.0
            )
            or 0.0
        )
        mp_sha = mini_probe_result.get("mini_probe_result_sha256")
        # §4.2 — largest cell uses v2.1 cold-cache formula in this path.
        eff_uncached_smallest = (
            compute_effective_uncached_prompt_tokens(
                base_prompt_tokens_for_gate=base_prompt_tokens_for_gate,
                cache_hit_ratio_steady_state=chr_smallest_for_projection,
            )
        )
        warm_smallest = compute_warm_projection_tpm(
            selected_peak_tps=selected_peak_tps_f,
            effective_uncached_prompt_tokens=eff_uncached_smallest,
            max_output_tokens=smallest_cell_max_output_tokens,
        )
        # Largest cell — cold-cache projection (NOT warm).
        cold_largest_mp = compute_projected_tpm_cell(
            peak_ramp_tps=selected_peak_tps_f,
            base_prompt_tokens_for_gate=base_prompt_tokens_for_gate,
            max_output_tokens=largest_cell_max_output_tokens,
        )
        warm_inputs = {
            "base_prompt_tokens_for_gate": base_prompt_tokens_for_gate,
            "selected_peak_tps": selected_peak_tps_f,
            "smallest_cell_max_output_tokens": smallest_cell_max_output_tokens,
            "largest_cell_max_output_tokens": largest_cell_max_output_tokens,
            "cache_hit_ratio_steady_state_smallest": (
                chr_smallest_for_projection
            ),
            "cache_hit_ratio_steady_state_largest_or_null_on_mini_probe_path": (
                None
            ),
            "effective_uncached_prompt_tokens_smallest": eff_uncached_smallest,
            "effective_uncached_prompt_tokens_largest_or_null_on_mini_probe_path": (  # noqa: E501
                None
            ),
            "projected_tpm_warm_smallest": warm_smallest,
            "projected_tpm_largest_under_chosen_formula": cold_largest_mp,
            "lower_threshold_tpm": lower_threshold,
            "upper_threshold_tpm": upper_threshold,
        }
        admits = (
            warm_smallest <= lower_threshold
            and cold_largest_mp >= upper_threshold
        )
        return EmpiricalPromotionDecision(
            promotion_path=PROMOTION_PATH_MINI_PROBE_REVALIDATED,
            largest_cell_projection_formula=(
                LARGEST_CELL_PROJECTION_FORMULA_COLD
            ),
            empirical_denied_reason=None,
            decision_reason_suffix=(
                "mini_probe_revalidated; "
                "largest_cell_projection_formula=v2.1_cold_cache_strict"
            ),
            cold_cache_admits=cold_admits,
            cold_cache_smallest_tpm=cold_smallest,
            cold_cache_largest_tpm=cold_largest,
            warm_projection_smallest_tpm=warm_smallest,
            warm_projection_largest_tpm=cold_largest_mp,
            warm_projection_inputs=warm_inputs,
            ptu_evidence_inferred=inferred,
            ptu_evidence_inference_basis=inferred_basis,
            mini_probe_result=mini_probe_result,
            mini_probe_result_sha256=(
                mp_sha if isinstance(mp_sha, str) else None
            ),
            smoke_promotion_admits=admits,
            feasibility_failure_reason=(
                None if admits else _classify_feasibility_failure(
                    smallest_tpm=warm_smallest,
                    largest_tpm=cold_largest_mp,
                    lower_threshold=lower_threshold,
                    upper_threshold=upper_threshold,
                )
            ),
        )

    # All twelve invariants hold → empirical_calibration_aware path.
    eff_uncached_smallest = compute_effective_uncached_prompt_tokens(
        base_prompt_tokens_for_gate=base_prompt_tokens_for_gate,
        cache_hit_ratio_steady_state=measured_chr_smallest,
    )
    eff_uncached_largest = compute_effective_uncached_prompt_tokens(
        base_prompt_tokens_for_gate=base_prompt_tokens_for_gate,
        cache_hit_ratio_steady_state=measured_chr_largest,
    )
    warm_smallest = compute_warm_projection_tpm(
        selected_peak_tps=selected_peak_tps_f,
        effective_uncached_prompt_tokens=eff_uncached_smallest,
        max_output_tokens=smallest_cell_max_output_tokens,
    )
    warm_largest = compute_warm_projection_tpm(
        selected_peak_tps=selected_peak_tps_f,
        effective_uncached_prompt_tokens=eff_uncached_largest,
        max_output_tokens=largest_cell_max_output_tokens,
    )
    admits = (
        warm_smallest <= lower_threshold and warm_largest >= upper_threshold
    )
    warm_inputs = {
        "base_prompt_tokens_for_gate": base_prompt_tokens_for_gate,
        "selected_peak_tps": selected_peak_tps_f,
        "smallest_cell_max_output_tokens": smallest_cell_max_output_tokens,
        "largest_cell_max_output_tokens": largest_cell_max_output_tokens,
        "cache_hit_ratio_steady_state_smallest": measured_chr_smallest,
        "cache_hit_ratio_steady_state_largest_or_null_on_mini_probe_path": (
            measured_chr_largest
        ),
        "effective_uncached_prompt_tokens_smallest": eff_uncached_smallest,
        "effective_uncached_prompt_tokens_largest_or_null_on_mini_probe_path": (
            eff_uncached_largest
        ),
        "projected_tpm_warm_smallest": warm_smallest,
        "projected_tpm_largest_under_chosen_formula": warm_largest,
        "lower_threshold_tpm": lower_threshold,
        "upper_threshold_tpm": upper_threshold,
    }
    return EmpiricalPromotionDecision(
        promotion_path=PROMOTION_PATH_EMPIRICAL_CALIBRATION_AWARE,
        largest_cell_projection_formula=LARGEST_CELL_PROJECTION_FORMULA_WARM,
        empirical_denied_reason=None,
        decision_reason_suffix="empirical_calibration_aware",
        cold_cache_admits=cold_admits,
        cold_cache_smallest_tpm=cold_smallest,
        cold_cache_largest_tpm=cold_largest,
        warm_projection_smallest_tpm=warm_smallest,
        warm_projection_largest_tpm=warm_largest,
        warm_projection_inputs=warm_inputs,
        ptu_evidence_inferred=inferred,
        ptu_evidence_inference_basis=inferred_basis,
        mini_probe_result=None,
        mini_probe_result_sha256=None,
        smoke_promotion_admits=admits,
        feasibility_failure_reason=(
            None if admits else _classify_feasibility_failure(
                smallest_tpm=warm_smallest,
                largest_tpm=warm_largest,
                lower_threshold=lower_threshold,
                upper_threshold=upper_threshold,
            )
        ),
    )


def _classify_feasibility_failure(
    *,
    smallest_tpm: float,
    largest_tpm: float,
    lower_threshold: float,
    upper_threshold: float,
) -> str:
    if smallest_tpm > lower_threshold:
        return "smallest_overshoots_lower_threshold"
    if largest_tpm < upper_threshold:
        return "largest_undershoots_upper_threshold"
    return "ok"


# §9.1 / §9.3 / §9.4 — artifact builders


def build_admitted_smoke_summary(
    *,
    decision: EmpiricalPromotionDecision,
    calibration_result: dict[str, Any],
    calibration_result_path: str,
    calibration_result_sha256: str,
    completed_at_iso_for_age: str,
    now_provider: Callable[[], datetime.datetime],
) -> dict[str, Any]:
    """Build a `task019.v2.4.smoke_summary` admitted-summary dict per §9.1.

    Caller writes this dict (json.dumps) to
    `runs/<ts>_<exp>_smoke.summary.json`.
    """
    completed_dt = _parse_iso8601(completed_at_iso_for_age)
    if completed_dt.tzinfo is None:
        completed_dt = completed_dt.replace(tzinfo=datetime.timezone.utc)
    now_dt = now_provider()
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=datetime.timezone.utc)
    age_hours = (now_dt - completed_dt).total_seconds() / 3600.0
    smallest_probe = (
        _select_probe_at_selection_point(
            calibration_result, role="smallest_control"
        )
        or {}
    )
    largest_probe = (
        _select_probe_at_selection_point(calibration_result, role="largest")
        or {}
    )

    def _probe_block(p: dict[str, Any]) -> dict[str, Any]:
        admp = p.get("admitted_pressure") or {}
        return {
            "n_records": int(p.get("n_records", 0) or 0),
            "n_429_records": int(p.get("n_429_records", 0) or 0),
            "cache_hit_ratio_steady_state": float(
                p.get("cache_hit_ratio_steady_state", 0.0) or 0.0
            ),
            "admitted_pressure_passed": bool(
                admp.get("admitted_pressure_passed", False)
            ),
            "admitted_peak_rpm_observed_last_30s": float(
                admp.get("admitted_peak_rpm_observed_last_30s", 0.0) or 0.0
            ),
        }

    return {
        "schema_version": SCHEMA_VERSION_SMOKE_SUMMARY,
        "tpm_feasibility_promotion_path": decision.promotion_path,
        "tpm_feasibility_promotion_decision_reason": (
            decision.decision_reason_suffix
        ),
        "tpm_feasibility_promotion_inputs": {
            "calibration_result_path": calibration_result_path,
            "calibration_result_sha256": calibration_result_sha256,
            "calibration_outcome": calibration_result.get("outcome"),
            "calibration_selected_via": calibration_result.get(
                "selected_via"
            ),
            "calibration_selected_at_phase": calibration_result.get(
                "selected_at_phase"
            ),
            "calibration_selected_at_bracket_depth": calibration_result.get(
                "selected_at_bracket_depth"
            ),
            "calibration_selected_peak_tps": calibration_result.get(
                "selected_peak_tps"
            ),
            "calibration_deployment_used": calibration_result.get(
                "deployment_used"
            ),
            "calibration_model": calibration_result.get("model"),
            "calibration_api_version": calibration_result.get("api_version"),
            "calibration_pricing_snapshot_path": calibration_result.get(
                "pricing_snapshot_path"
            ),
            "calibration_pricing_accessed_date": calibration_result.get(
                "pricing_accessed_date"
            ),
            "calibration_ptu_evidence": (
                (calibration_result.get("metadata") or {}).get(
                    "ptu_evidence"
                )
                if calibration_result.get("metadata") is not None
                else None
            ),
            "calibration_completed_at_iso": calibration_result.get(
                "completed_at_iso"
            ),
            "calibration_age_hours_at_promotion_check": age_hours,
            "smallest_control_probe": _probe_block(smallest_probe),
            "largest_probe": _probe_block(largest_probe),
            "cache_hit_floor_smallest_control": (
                EMPIRICAL_PROMOTION_CACHE_HIT_FLOOR_SMALLEST_CONTROL
            ),
            "cache_hit_floor_largest": EMPIRICAL_PROMOTION_CACHE_HIT_FLOOR_LARGEST,
        },
        "ptu_evidence_inferred": (
            decision.ptu_evidence_inferred
            if decision.promotion_path != PROMOTION_PATH_COLD_CACHE_STRICT
            else None
        ),
        "ptu_evidence_inference_basis": (
            decision.ptu_evidence_inference_basis
            if decision.promotion_path != PROMOTION_PATH_COLD_CACHE_STRICT
            else None
        ),
        "largest_cell_projection_formula": (
            decision.largest_cell_projection_formula
        ),
        "empirical_warm_projection_inputs": (
            decision.warm_projection_inputs
            if decision.promotion_path != PROMOTION_PATH_COLD_CACHE_STRICT
            else None
        ),
        "mini_probe_result": (
            decision.mini_probe_result
            if decision.promotion_path == PROMOTION_PATH_MINI_PROBE_REVALIDATED
            else None
        ),
        "mini_probe_result_sha256": (
            decision.mini_probe_result_sha256
            if decision.promotion_path == PROMOTION_PATH_MINI_PROBE_REVALIDATED
            else None
        ),
    }


def build_admitted_evidence_summary(
    *,
    decision: EmpiricalPromotionDecision,
    calibration_result: dict[str, Any],
    calibration_result_path: str,
    calibration_result_sha256: str,
    completed_at_iso_for_age: str,
    now_provider: Callable[[], datetime.datetime],
    smoke_summary_dict: dict[str, Any],
    smoke_summary_path: str,
    smoke_summary_sha256: str,
) -> dict[str, Any]:
    """Build a `task019.v2.4.evidence_summary` admitted-summary dict per §9.3.

    Reuses every field of the smoke schema verbatim AND adds the
    `smoke_summary_reference` block whose five fields byte-equal-echo
    the source smoke summary.
    """
    base = build_admitted_smoke_summary(
        decision=decision,
        calibration_result=calibration_result,
        calibration_result_path=calibration_result_path,
        calibration_result_sha256=calibration_result_sha256,
        completed_at_iso_for_age=completed_at_iso_for_age,
        now_provider=now_provider,
    )
    base["schema_version"] = SCHEMA_VERSION_EVIDENCE_SUMMARY
    base["smoke_summary_reference"] = {
        "smoke_summary_path": smoke_summary_path,
        "smoke_summary_sha256": smoke_summary_sha256,
        "smoke_tpm_feasibility_promotion_path": smoke_summary_dict.get(
            "tpm_feasibility_promotion_path"
        ),
        "smoke_tpm_feasibility_promotion_decision_reason": (
            smoke_summary_dict.get("tpm_feasibility_promotion_decision_reason")
        ),
        "smoke_largest_cell_projection_formula": smoke_summary_dict.get(
            "largest_cell_projection_formula"
        ),
    }
    return base


def apply_v24_admitted_summary_fields(
    *,
    base_summary: dict[str, Any],
    decision: EmpiricalPromotionDecision,
    calibration_result: dict[str, Any],
    calibration_result_path: str,
    calibration_result_sha256: str,
    completed_at_iso_for_age: str,
    now_provider: Callable[[], datetime.datetime],
    stage: str,
    smoke_summary_dict_for_evidence: dict[str, Any] | None = None,
    smoke_summary_path_for_evidence: str | None = None,
    smoke_summary_sha256_for_evidence: str | None = None,
) -> dict[str, Any]:
    """Overlay v2.4 §9.1 / §9.3 admitted-summary fields onto an existing
    v2.3 measurement-summary dict (in place AND returned).

    Bumps ``schema_version`` to ``task019.v2.4.smoke_summary`` (stage =
    ``smoke``) or ``task019.v2.4.evidence_summary`` (stage = ``evidence``).
    The original v2.3 operational fields (cell summaries, JSONL path,
    pinned-confounds echo, run-lock metadata, etc.) are PRESERVED — the
    v2.4 schema validators only enforce required-field presence + null
    discipline on the v2.4-named keys and do not reject extras, so the
    extended dict is BOTH (a) a strictly conforming
    ``task019.v2.4.{smoke,evidence}_summary`` artifact AND (b) byte-
    superset-compatible with audit consumers that still look for v2.3
    operational fields.

    For ``stage == "evidence"``, the caller MUST supply
    ``smoke_summary_dict_for_evidence`` + path + sha256 so the
    ``smoke_summary_reference`` block can byte-equal-echo per §9.3.

    Args:
        base_summary: The v2.3 measurement_summary dict (mutated in place).
        decision: The v2.4 promotion gate's decision.
        calibration_result: Loaded ``calibration_result.json`` dict.
        calibration_result_path: Path to the calibration result file.
        calibration_result_sha256: Recomputed sha256 of the calibration
            result file.
        completed_at_iso_for_age: The calibration's ``completed_at_iso``
            (used to compute age at promotion check).
        now_provider: ``lambda: datetime.datetime.now(...)`` (injected
            for frozen-clock test reproducibility).
        stage: ``"smoke"`` or ``"evidence"``.
        smoke_summary_dict_for_evidence: Required when stage == evidence;
            the source smoke summary's dict (already parsed).
        smoke_summary_path_for_evidence: Required when stage == evidence.
        smoke_summary_sha256_for_evidence: Required when stage == evidence;
            the smoke summary file's content sha256 (from the sidecar).

    Returns:
        The mutated ``base_summary`` (same object as input).

    Raises:
        ValueError: stage is not smoke / evidence, or evidence is
            missing required smoke-summary-reference inputs.
    """
    if stage not in ("smoke", "evidence"):
        raise ValueError(
            f"apply_v24_admitted_summary_fields: stage must be "
            f"'smoke' or 'evidence'; got {stage!r}"
        )
    overlay = build_admitted_smoke_summary(
        decision=decision,
        calibration_result=calibration_result,
        calibration_result_path=calibration_result_path,
        calibration_result_sha256=calibration_result_sha256,
        completed_at_iso_for_age=completed_at_iso_for_age,
        now_provider=now_provider,
    )
    # build_admitted_smoke_summary already encodes the per-path null
    # discipline; overlay verbatim then optionally promote to evidence.
    for k, v in overlay.items():
        base_summary[k] = v
    if stage == "evidence":
        if (
            smoke_summary_dict_for_evidence is None
            or smoke_summary_path_for_evidence is None
            or smoke_summary_sha256_for_evidence is None
        ):
            raise ValueError(
                "apply_v24_admitted_summary_fields: stage='evidence' "
                "requires smoke_summary_dict_for_evidence, "
                "smoke_summary_path_for_evidence, AND "
                "smoke_summary_sha256_for_evidence (§9.3 echo contract)"
            )
        base_summary["schema_version"] = SCHEMA_VERSION_EVIDENCE_SUMMARY
        base_summary["smoke_summary_reference"] = {
            "smoke_summary_path": smoke_summary_path_for_evidence,
            "smoke_summary_sha256": smoke_summary_sha256_for_evidence,
            "smoke_tpm_feasibility_promotion_path": (
                smoke_summary_dict_for_evidence.get(
                    "tpm_feasibility_promotion_path"
                )
            ),
            "smoke_tpm_feasibility_promotion_decision_reason": (
                smoke_summary_dict_for_evidence.get(
                    "tpm_feasibility_promotion_decision_reason"
                )
            ),
            "smoke_largest_cell_projection_formula": (
                smoke_summary_dict_for_evidence.get(
                    "largest_cell_projection_formula"
                )
            ),
        }
    return base_summary


def build_abort_envelope(
    *,
    stage: str,
    exit_reason: str,
    empirical_promotion_denied_reason: str | None,
) -> dict[str, Any]:
    """Build a `task019.v2.4.abort_envelope` dict per §9.4.

    The envelope is intentionally minimal — four required fields, zero
    admitted-summary fields. The schema validator
    (`validate_abort_envelope_v24`) enforces both rules.
    """
    if stage not in ("smoke", "evidence"):
        raise ValueError(
            f"stage must be 'smoke' or 'evidence'; got {stage!r}"
        )
    if not isinstance(exit_reason, str) or not exit_reason:
        raise ValueError("exit_reason must be a non-empty string")
    # microfix #5 blocker 1 — `empirical_promotion_disabled_*` strings
    # are STABLE empirical-denial identifiers; they MUST NOT appear in
    # `exit_reason`.
    if exit_reason.startswith("empirical_promotion_disabled_"):
        raise ValueError(
            "exit_reason MUST NOT be an empirical_promotion_disabled_* "
            "stable identifier (microfix #5 blocker 1); those surface in "
            "empirical_promotion_denied_reason — for the empirical-"
            "denial-followed-by-cold-cache-denial terminal case "
            "exit_reason is TPM_FEASIBILITY_ABORT"
        )
    # microfix #6 blocker 2 — `mini_probe_failed_*` strings NEVER appear
    # in any abort-envelope field.
    if empirical_promotion_denied_reason is not None:
        if not isinstance(empirical_promotion_denied_reason, str):
            raise ValueError(
                "empirical_promotion_denied_reason must be a string or None"
            )
        if empirical_promotion_denied_reason in MINI_PROBE_FAILED_REASONS:
            raise ValueError(
                "empirical_promotion_denied_reason MUST NOT be a raw "
                "mini_probe_failed_* identifier (microfix #6 blocker 2); "
                "the mini-probe-attempted-and-failed terminal case "
                "surfaces as the §3.1 invariant-12 composite "
                "empirical_promotion_disabled_mini_probe_failed_and_cold_cache_fails"
            )
    return {
        "schema_version": SCHEMA_VERSION_ABORT_ENVELOPE,
        "stage": stage,
        "exit_reason": exit_reason,
        "empirical_promotion_denied_reason": empirical_promotion_denied_reason,
    }


def write_abort_envelope_artifact(
    *,
    runs_dir: pathlib.Path,
    experiment_id: str,
    timestamp_label: str,
    stage: str,
    exit_reason: str,
    empirical_promotion_denied_reason: str | None,
) -> pathlib.Path:
    """Materialise a v2.4 §9.4 abort envelope to disk at
    ``runs_dir/<timestamp>_<exp>_<stage>.summary.json``.

    The path is deliberately ``.summary.json`` to satisfy §9.4's
    "mutually exclusive with admitted summaries on the same path"
    contract — downstream audit infrastructure can detect an abort
    envelope by parsing the file and inspecting ``schema_version``.

    Raises:
        OSError: write failure.
        ValueError: envelope failed `validate_abort_envelope_v24`.
    """
    envelope = build_abort_envelope(
        stage=stage,
        exit_reason=exit_reason,
        empirical_promotion_denied_reason=(
            empirical_promotion_denied_reason
        ),
    )
    validate_abort_envelope_v24(envelope)
    runs_dir.mkdir(parents=True, exist_ok=True)
    out_path = runs_dir / (
        f"{timestamp_label}_{experiment_id}_{stage}.summary.json"
    )
    out_path.write_text(
        json.dumps(envelope, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out_path


# §9 schema validators (raise ``ValueError`` with a short reason)


def validate_smoke_summary_v24(d: dict[str, Any]) -> None:
    """v2.4 §9.1 schema validator (smoke admitted summary).

    Raises:
        ValueError: on missing / null required-non-null field OR
            present-and-non-null required-null field (per the §9.1
            path-conditional rules).
    """
    _validate_admitted_summary_v24(
        d, expected_schema_version=SCHEMA_VERSION_SMOKE_SUMMARY
    )


def validate_evidence_summary_v24(d: dict[str, Any]) -> None:
    """v2.4 §9.3 schema validator (evidence admitted summary)."""
    _validate_admitted_summary_v24(
        d, expected_schema_version=SCHEMA_VERSION_EVIDENCE_SUMMARY
    )
    # Evidence-specific: smoke_summary_reference is required-non-null
    # with all five echo fields present.
    ssr = d.get("smoke_summary_reference")
    if not isinstance(ssr, dict):
        raise ValueError(
            "evidence summary requires smoke_summary_reference block"
        )
    required_echo_keys = (
        "smoke_summary_path",
        "smoke_summary_sha256",
        "smoke_tpm_feasibility_promotion_path",
        "smoke_tpm_feasibility_promotion_decision_reason",
        "smoke_largest_cell_projection_formula",
    )
    for k in required_echo_keys:
        v = ssr.get(k)
        if v is None or (isinstance(v, str) and not v):
            raise ValueError(
                f"evidence summary smoke_summary_reference.{k} must be "
                f"non-null and non-empty; got {v!r}"
            )


def _validate_admitted_summary_v24(
    d: dict[str, Any], *, expected_schema_version: str
) -> None:
    if d.get("schema_version") != expected_schema_version:
        raise ValueError(
            f"schema_version must be {expected_schema_version!r}; got "
            f"{d.get('schema_version')!r}"
        )
    path = d.get("tpm_feasibility_promotion_path")
    if path not in PROMOTION_PATHS:
        raise ValueError(
            f"tpm_feasibility_promotion_path must be in {sorted(PROMOTION_PATHS)}; "
            f"got {path!r}"
        )
    if not isinstance(d.get("tpm_feasibility_promotion_decision_reason"), str):
        raise ValueError(
            "tpm_feasibility_promotion_decision_reason must be a string"
        )
    if not isinstance(d.get("tpm_feasibility_promotion_inputs"), dict):
        raise ValueError(
            "tpm_feasibility_promotion_inputs must be a dict"
        )
    largest_formula = d.get("largest_cell_projection_formula")
    if largest_formula not in (
        LARGEST_CELL_PROJECTION_FORMULA_WARM,
        LARGEST_CELL_PROJECTION_FORMULA_COLD,
    ):
        raise ValueError(
            "largest_cell_projection_formula must be one of "
            f"{LARGEST_CELL_PROJECTION_FORMULA_WARM!r}|"
            f"{LARGEST_CELL_PROJECTION_FORMULA_COLD!r}; got "
            f"{largest_formula!r}"
        )
    # Path-conditional null discipline.
    warm_inputs = d.get("empirical_warm_projection_inputs")
    mp_result = d.get("mini_probe_result")
    mp_sha = d.get("mini_probe_result_sha256")
    ptu_inferred = d.get("ptu_evidence_inferred")
    ptu_basis = d.get("ptu_evidence_inference_basis")
    if path == PROMOTION_PATH_COLD_CACHE_STRICT:
        if largest_formula != LARGEST_CELL_PROJECTION_FORMULA_COLD:
            raise ValueError(
                "on cold_cache_strict path, largest_cell_projection_formula "
                "MUST be v2.1_cold_cache_strict"
            )
        for k, v in (
            ("empirical_warm_projection_inputs", warm_inputs),
            ("mini_probe_result", mp_result),
            ("mini_probe_result_sha256", mp_sha),
            ("ptu_evidence_inferred", ptu_inferred),
            ("ptu_evidence_inference_basis", ptu_basis),
        ):
            if v is not None:
                raise ValueError(
                    f"on cold_cache_strict path, {k} MUST be null; "
                    f"got {v!r}"
                )
    elif path == PROMOTION_PATH_EMPIRICAL_CALIBRATION_AWARE:
        if largest_formula != LARGEST_CELL_PROJECTION_FORMULA_WARM:
            raise ValueError(
                "on empirical_calibration_aware path, "
                "largest_cell_projection_formula MUST be v2.4_warm_projection"
            )
        if warm_inputs is None:
            raise ValueError(
                "empirical_warm_projection_inputs MUST be non-null on "
                "empirical_calibration_aware path"
            )
        if mp_result is not None or mp_sha is not None:
            raise ValueError(
                "mini_probe_result / mini_probe_result_sha256 MUST be null "
                "on empirical_calibration_aware path"
            )
    elif path == PROMOTION_PATH_MINI_PROBE_REVALIDATED:
        if largest_formula != LARGEST_CELL_PROJECTION_FORMULA_COLD:
            raise ValueError(
                "on mini_probe_revalidated path, "
                "largest_cell_projection_formula MUST be v2.1_cold_cache_strict"
            )
        if warm_inputs is None:
            raise ValueError(
                "empirical_warm_projection_inputs MUST be non-null on "
                "mini_probe_revalidated path"
            )
        if mp_result is None or mp_sha is None:
            raise ValueError(
                "mini_probe_result / mini_probe_result_sha256 MUST be "
                "non-null on mini_probe_revalidated path"
            )


def validate_abort_envelope_v24(d: dict[str, Any]) -> None:
    """v2.4 §9.4 schema validator (abort envelope).

    Enforces:
      - schema_version literal
      - stage ∈ {smoke, evidence}
      - exit_reason is a non-empty string AND is NOT an
        `empirical_promotion_disabled_*` stable identifier (microfix
        #5 blocker 1).
      - empirical_promotion_denied_reason is either a stable
        `empirical_promotion_disabled_*` string OR `null` (microfix #5
        blocker 1; microfix #6 blocker 2 — never a raw
        `mini_probe_failed_*` identifier).
      - explicitly-forbidden admitted-summary fields per §9.4 are absent.
    """
    if d.get("schema_version") != SCHEMA_VERSION_ABORT_ENVELOPE:
        raise ValueError(
            f"schema_version must be {SCHEMA_VERSION_ABORT_ENVELOPE!r}; got "
            f"{d.get('schema_version')!r}"
        )
    if d.get("stage") not in ("smoke", "evidence"):
        raise ValueError(
            f"stage must be 'smoke' or 'evidence'; got {d.get('stage')!r}"
        )
    exit_reason = d.get("exit_reason")
    if not isinstance(exit_reason, str) or not exit_reason:
        raise ValueError("exit_reason must be a non-empty string")
    if exit_reason.startswith("empirical_promotion_disabled_"):
        raise ValueError(
            "exit_reason MUST NOT be an empirical_promotion_disabled_* "
            "identifier (microfix #5 blocker 1)"
        )
    denied = d.get("empirical_promotion_denied_reason")
    if denied is not None:
        if not isinstance(denied, str):
            raise ValueError(
                "empirical_promotion_denied_reason must be a string or null"
            )
        if denied in MINI_PROBE_FAILED_REASONS:
            raise ValueError(
                "empirical_promotion_denied_reason MUST NOT be a raw "
                "mini_probe_failed_* identifier (microfix #6 blocker 2)"
            )
    for k in ABORT_ENVELOPE_FORBIDDEN_FIELDS:
        if k in d:
            raise ValueError(
                f"abort envelope MUST NOT carry the admitted-summary "
                f"field {k!r} (§9.4 forbidden list)"
            )


def validate_evidence_summary_smoke_promotion_path_echo(
    *,
    evidence_smoke_reference: dict[str, Any],
    source_smoke_summary: dict[str, Any],
) -> str | None:
    """v2.4 §9.3 echo-validation preflight (the evidence runner's
    pre-promotion preflight check). Returns the mismatched echo field
    name on failure, or ``None`` on success.

    The check byte-compares each of the five `smoke_summary_reference.*`
    fields against the source smoke summary. ``smoke_summary_path`` and
    ``smoke_summary_sha256`` are validated only for presence (the source
    is the loaded smoke summary; the runner already knows its own path
    + sha256). The other three are byte-equal-echo of the source.
    """
    if not isinstance(evidence_smoke_reference, dict):
        return "smoke_summary_path"
    for k in (
        "smoke_summary_path",
        "smoke_summary_sha256",
    ):
        v = evidence_smoke_reference.get(k)
        if v is None or (isinstance(v, str) and not v):
            return k
    for k in (
        "smoke_tpm_feasibility_promotion_path",
        "smoke_tpm_feasibility_promotion_decision_reason",
        "smoke_largest_cell_projection_formula",
    ):
        v = evidence_smoke_reference.get(k)
        source_key = k.removeprefix("smoke_")
        if v is None or v != source_smoke_summary.get(source_key):
            return k
    return None


# §7 — YAML-load helpers for the auditor-approved comment + mini_probe knob.


def yaml_mini_probe_enabled_with_auditor_comment(
    yaml_text: str,
) -> tuple[bool, bool]:
    """Inspect raw YAML text and return ``(mini_probe_enabled,
    has_auditor_comment)``.

    The YAML validator uses this helper to refuse
    ``mini_probe_enabled: true`` without an
    ``# auditor-approved-YYYY-MM-DD: <handle>`` comment in the lines
    immediately above the key. ``mini_probe_enabled: false`` does NOT
    require a comment.

    Args:
        yaml_text: Raw YAML file contents (caller already read).

    Returns:
        Tuple ``(mini_probe_enabled, has_auditor_comment)``.
        ``mini_probe_enabled`` defaults to False if the key is absent.
        ``has_auditor_comment`` is True IFF the comment is present in
        the chunk of consecutive non-blank lines immediately above the
        key line.
    """
    lines = yaml_text.splitlines()
    enabled = False
    enabled_line_idx: int | None = None
    pat = re.compile(
        r"^\s*mini_probe_enabled\s*:\s*(true|false)\s*(#.*)?$",
        re.IGNORECASE,
    )
    for idx, line in enumerate(lines):
        m = pat.match(line)
        if m:
            enabled = m.group(1).strip().lower() == "true"
            enabled_line_idx = idx
            # Continue scanning (later occurrence wins).
    if not enabled:
        return False, False
    has_comment = False
    if enabled_line_idx is not None:
        # Scan upward for the comment in the contiguous comment / blank
        # block immediately above the key.
        i = enabled_line_idx - 1
        while i >= 0:
            stripped = lines[i].strip()
            if not stripped:
                # Blank line — keep scanning until we exit the
                # contiguous block above the key.
                i -= 1
                continue
            if stripped.startswith("#"):
                if _AUDITOR_APPROVED_COMMENT_RE.match(lines[i]):
                    has_comment = True
                    break
                i -= 1
                continue
            break
    return True, has_comment


# ----------------------------------------------------------------------------
# v2.4 §7 — Mini-probe production runner
# ----------------------------------------------------------------------------


def build_mini_probe_cache_key(
    *,
    run_id_short: str,
    tps: float,
    suffix: str | None = None,
) -> str:
    """Construct the mini-probe ``prompt_cache_key``.

    Format: ``task019_minip_{run_id_short}_cell00256_tps{tps_int:04d}``
    (TPS values < 10 keep the 4-digit ``tps_int`` formatter; ≥ 10 widen
    to 5 digits — same width-policy as ``build_calibration_cache_key``).

    The mini-probe is FIXED to cap=256 (`smallest_control` cell role),
    so ``cell00256`` is hard-coded. ``suffix`` ∈ {None, ``"_retry1"``}
    matches the §7 bounded-retry contract verbatim. The ``minip``
    namespace is DISTINCT from ``card1`` (smoke/evidence) and ``calib``
    (Stage 0.5) so mini-probe traffic CAN'T accidentally collide with
    real-measurement OR calibration cache buckets.

    Raises:
        ValueError: malformed ``run_id_short``, non-positive ``tps``,
            or an unknown ``suffix``.
    """
    if not re.fullmatch(r"[a-f0-9]{8}", run_id_short):
        raise ValueError(
            f"build_mini_probe_cache_key: run_id_short must be 8 "
            f"lowercase hex chars; got {run_id_short!r}"
        )
    if tps <= 0:
        raise ValueError(
            f"build_mini_probe_cache_key: tps must be > 0; got {tps!r}"
        )
    tps_int = int(round(tps * 1000))
    if tps_int <= 0 or tps_int > 99999:
        raise ValueError(
            f"build_mini_probe_cache_key: tps_int must be in 1..99999; "
            f"got {tps_int} from tps={tps!r}"
        )
    tps_str = f"{tps_int:04d}" if tps_int <= 9999 else str(tps_int)
    if suffix is None:
        suffix_part = ""
    elif suffix == "_retry1":
        suffix_part = "_retry1"
    else:
        raise ValueError(
            f"build_mini_probe_cache_key: unsupported suffix {suffix!r} "
            f"(allowed: None, '_retry1')"
        )
    return (
        f"task019_minip_{run_id_short}_cell00256_tps{tps_str}{suffix_part}"
    )


# §7 PIN — mini-probe duration is HALF v2.3 calibration probe duration
# (90s vs 180s). The pinned-constant default lives here; YAML cannot
# override — the §10 PIN block governs operator-visible knobs only.
MINI_PROBE_DURATION_S = 90.0
MINI_PROBE_PREWARM_CALLS = 12
MINI_PROBE_PREWARM_TPS = 0.05
MINI_PROBE_MAX_OUTPUT_TOKENS = 256
MINI_PROBE_GUARDRAIL_STRING = (
    "task019.v2.4.mini_probe — single smallest_control probe at the "
    "calibration's selected_peak_tps; pre-warm 12 calls @ 0.05 TPS; "
    "constant-rate probe 90 s @ selected_peak_tps; bounded retry once "
    "on any gate failure; pass criteria: n_records >= 30, n_429 == 0, "
    "cache_hit_ratio_steady_state >= 0.80, admitted_pressure_passed."
)


def _mini_probe_failed_reason_from_agg(agg: dict[str, Any]) -> str | None:
    """Inspect a mini-probe aggregate and return the §8
    `mini_probe_failed_*` stable identifier on gate failure, OR None
    when all four gates pass."""
    # Gate 1 — warm criterion (§7 + §8).
    if not agg.get("warm_criterion_passed", False):
        return MINI_PROBE_FAILED_CACHE_NOT_WARM
    # Gate 2 — backlog excessive.
    if agg.get("backlog_excessive", False):
        return MINI_PROBE_FAILED_BACKLOG_EXCESSIVE
    # Gate 3 — all-empty-visible-output.
    if agg.get("all_empty_visible_output", False):
        return MINI_PROBE_FAILED_ALL_EMPTY_VISIBLE_OUTPUT
    # Gate 4 — admitted-pressure (skipped on 429 by design — but a 429
    # itself is a separate failure mode for the smallest-control probe).
    if int(agg.get("n_429_records", 0) or 0) >= 1:
        return MINI_PROBE_FAILED_OBSERVED_429_ON_SMALLEST_CONTROL
    ap = agg.get("admitted_pressure") or {}
    if (
        ap.get("admitted_pressure_passed") is False
        and not ap.get("admitted_pressure_skipped_due_to_429", False)
    ):
        return MINI_PROBE_FAILED_ADMITTED_PRESSURE_INSUFFICIENT
    # Pass criteria — n_records + cache_hit floor.
    n_records = int(agg.get("n_records", 0) or 0)
    if n_records < EMPIRICAL_PROMOTION_MINIMUM_RECORDS_AT_SELECTED_TPS:
        return MINI_PROBE_FAILED_TOO_FEW_RECORDS
    chr_ = float(agg.get("cache_hit_ratio_steady_state", 0.0) or 0.0)
    if chr_ < EMPIRICAL_PROMOTION_CACHE_HIT_FLOOR_SMALLEST_CONTROL:
        return MINI_PROBE_FAILED_CACHE_HIT_BELOW_FLOOR
    return None


def build_mini_probe_result(
    *,
    outcome: str,
    selected_peak_tps: float,
    aggregate: dict[str, Any],
    cache_key: str,
    calibration_result_sha256: str,
    deployment_used: str,
    model: str,
    api_version: str,
    pricing_snapshot_path: str,
    pricing_accessed_date: str | None,
    prompt_identity: dict[str, Any],
    started_at_iso: str,
    completed_at_iso: str,
    total_usd: float,
    total_committed_usd: float,
) -> dict[str, Any]:
    """Materialise the §9.2 ``task019.v2.4.mini_probe_result`` dict from
    a probe aggregate."""
    ap = aggregate.get("admitted_pressure") or {}
    return {
        "schema_version": SCHEMA_VERSION_MINI_PROBE_RESULT,
        "mini_probe_outcome": outcome,
        "mini_probe_role": "smallest_control",
        "mini_probe_cell_max_output_tokens": MINI_PROBE_MAX_OUTPUT_TOKENS,
        "mini_probe_candidate_tps": float(selected_peak_tps),
        "mini_probe_n_records": int(aggregate.get("n_records", 0) or 0),
        "mini_probe_n_429_records": int(
            aggregate.get("n_429_records", 0) or 0
        ),
        "mini_probe_cache_hit_ratio_steady_state": float(
            aggregate.get("cache_hit_ratio_steady_state", 0.0) or 0.0
        ),
        "mini_probe_admitted_pressure": dict(ap),
        "mini_probe_warm_criterion_passed": bool(
            aggregate.get("warm_criterion_passed", False)
        ),
        "mini_probe_backlog_p95_ms": float(
            aggregate.get("backlog_p95_ms", 0.0) or 0.0
        ),
        "mini_probe_backlog_max_ms": float(
            aggregate.get("backlog_max_ms", 0.0) or 0.0
        ),
        "mini_probe_visible_output_mean_per_probe": float(
            aggregate.get("visible_output_mean_per_probe", 0.0) or 0.0
        ),
        "mini_probe_prompt_cache_key": cache_key,
        "calibration_result_sha256": calibration_result_sha256,
        "prompt_identity": dict(prompt_identity),
        "deployment_used": deployment_used,
        "model": model,
        "api_version": api_version,
        "pricing_snapshot_path": pricing_snapshot_path,
        "pricing_accessed_date": pricing_accessed_date,
        "ptu_evidence": False,
        "started_at_iso": started_at_iso,
        "completed_at_iso": completed_at_iso,
        "total_usd": round(float(total_usd), 6),
        "total_committed_usd": round(float(total_committed_usd), 6),
        "mini_probe_max_usd": EMPIRICAL_PROMOTION_MINI_PROBE_MAX_USD,
        "guardrail": MINI_PROBE_GUARDRAIL_STRING,
    }


def write_mini_probe_artifacts(
    *,
    runs_dir: pathlib.Path,
    experiment_id: str,
    timestamp_label: str,
    stage: str,
    result_doc: dict[str, Any],
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, str]:
    """Write the §9.2 mini-probe artifact triplet:
    ``runs/<ts>_<exp>_<stage>.mini_probe.result.json``,
    ``runs/<ts>_<exp>_<stage>.mini_probe.summary.json``,
    and a ``.sha256`` sidecar over the result file.

    Returns ``(result_path, summary_path, sidecar_path, result_sha256)``.
    """
    if stage not in ("smoke", "evidence"):
        raise ValueError(
            f"write_mini_probe_artifacts: stage must be 'smoke' or "
            f"'evidence'; got {stage!r}"
        )
    runs_dir.mkdir(parents=True, exist_ok=True)
    result_path = (
        runs_dir
        / f"{timestamp_label}_{experiment_id}_{stage}.mini_probe.result.json"
    )
    summary_path = (
        runs_dir
        / f"{timestamp_label}_{experiment_id}_{stage}.mini_probe.summary.json"
    )
    sidecar_path = pathlib.Path(str(result_path) + ".sha256")
    with result_path.open("w", encoding="utf-8") as fh:
        json.dump(
            result_doc, fh, indent=2, sort_keys=True, default=_json_default,
        )
    summary_doc = {
        "schema_version": SCHEMA_VERSION_MINI_PROBE_SUMMARY,
        "mini_probe_result_path": str(result_path),
        "mini_probe_outcome": result_doc.get("mini_probe_outcome"),
        "mini_probe_candidate_tps": result_doc.get("mini_probe_candidate_tps"),
        "mini_probe_n_records": result_doc.get("mini_probe_n_records"),
        "mini_probe_cache_hit_ratio_steady_state": result_doc.get(
            "mini_probe_cache_hit_ratio_steady_state"
        ),
        "calibration_result_sha256": result_doc.get(
            "calibration_result_sha256"
        ),
        "guardrail": MINI_PROBE_GUARDRAIL_STRING,
    }
    with summary_path.open("w", encoding="utf-8") as fh:
        json.dump(
            summary_doc, fh, indent=2, sort_keys=True, default=_json_default,
        )
    sha = _sha256_file(result_path)
    sidecar_path.write_text(sha + "\n", encoding="utf-8")
    return result_path, summary_path, sidecar_path, sha


async def _run_mini_probe_async(
    *,
    cfg: ExperimentConfig,
    runs_dir: pathlib.Path,
    timestamp_label: str,
    stage: str,
    run_id_short: str,
    client: Any,
    deployment: str,
    system_prompt: str,
    user_prompts: list[str],
    git_commit: str,
    dirty: bool,
    system_sha: str,
    source_corpus_sha: str,
    user_prompts_source_sha: str,
    pricing_snapshot_path: str,
    pricing: PaygPricing,
    selected_peak_tps: float,
    calibration_result_sha256: str,
    sim_started_mono: float,
    out_fh: Any,
    global_request_offset: int,
) -> dict[str, Any]:
    """v2.4 §7 — execute ONE mini-probe and return a §9.2-conformant
    result dict.

    Reuses :func:`_run_cell` (constant-rate probe phase + pre-warm with
    bounded retry once on gate failure). The smallest-cell control
    semantics are enforced by hard-coding
    ``cell_max_output_tokens=MINI_PROBE_MAX_OUTPUT_TOKENS`` and
    overriding the cache key with the ``task019_minip_*`` namespace.

    On any gate failure, retries ONCE with a ``_retry1`` cache-key
    suffix; a second failure returns
    ``mini_probe_outcome="failed_<gate>"`` (NEVER the raw retry-suffixed
    cache key — that lives only in the JSONL and the result-dict's
    ``mini_probe_prompt_cache_key`` field).

    Writes the §9.2 artifact triplet to ``runs_dir`` before returning
    so audit / abort-envelope consumers can reference the result by
    path + sha256 even on the failed path.
    """
    started_at_iso = _iso8601_z(_utc_now())

    async def _one_attempt(suffix: str | None) -> dict[str, Any]:
        cache_key = build_mini_probe_cache_key(
            run_id_short=run_id_short,
            tps=selected_peak_tps,
            suffix=suffix,
        )
        (
            records,
            cell_usd,
            cell_committed_usd,
            _max_in_flight,
            _halt_reason,
        ) = await _run_cell(
            cfg=cfg,
            cell_idx=0,
            cell_max_output_tokens=MINI_PROBE_MAX_OUTPUT_TOKENS,
            prewarm_calls=MINI_PROBE_PREWARM_CALLS,
            prewarm_tps=MINI_PROBE_PREWARM_TPS,
            ramp_duration_s=MINI_PROBE_DURATION_S,
            peak_ramp_tps=float(selected_peak_tps),
            cool_down_s=0.0,
            concurrency=cfg.runtime.concurrency,
            client=client,
            deployment=deployment,
            system_prompt=system_prompt,
            user_prompts=user_prompts,
            git_commit=git_commit,
            dirty=dirty,
            system_sha=system_sha,
            user_prompts_source_sha=user_prompts_source_sha,
            source_corpus_sha=source_corpus_sha,
            pricing_snapshot_path=pricing_snapshot_path,
            pricing=pricing,
            dry_run=False,
            out_fh=out_fh,
            global_request_offset=global_request_offset,
            sim_started_mono=sim_started_mono,
            run_id_short=run_id_short,
            cache_key_override=cache_key,
            constant_rate=True,
            probe_max_usd=EMPIRICAL_PROMOTION_MINI_PROBE_MAX_USD,
            early_stop_on_first_429=False,
        )
        probe_end_iso = compute_probe_window_end_iso(
            cell_records=records,
            fallback_now_iso=_iso8601_z(_utc_now()),
        )
        agg = _aggregate_calibration_probe(
            records=records,
            cell_max_output_tokens=MINI_PROBE_MAX_OUTPUT_TOKENS,
            candidate_tps=float(selected_peak_tps),
            probe_window_end_iso=probe_end_iso,
            prompt_cache_key=cache_key,
            source_corpus_sha=source_corpus_sha,
            system_sha=system_sha,
            user_prompts_source_sha=user_prompts_source_sha,
            run_id_short=run_id_short,
        )
        return {
            "agg": agg,
            "cache_key": cache_key,
            "cell_usd": cell_usd,
            "cell_committed_usd": cell_committed_usd,
            "n_records": len(records),
        }

    # Attempt #1 (no suffix).
    a1 = await _one_attempt(suffix=None)
    failure_reason_1 = _mini_probe_failed_reason_from_agg(a1["agg"])
    final = a1
    if failure_reason_1 is None:
        outcome = "passed"
        total_usd = float(a1["cell_usd"])
        total_committed_usd = float(a1["cell_committed_usd"])
    else:
        # Bounded retry ONCE with `_retry1` suffix.
        a2 = await _one_attempt(suffix="_retry1")
        failure_reason_2 = _mini_probe_failed_reason_from_agg(a2["agg"])
        total_usd = float(a1["cell_usd"]) + float(a2["cell_usd"])
        total_committed_usd = (
            float(a1["cell_committed_usd"]) + float(a2["cell_committed_usd"])
        )
        final = a2
        if failure_reason_2 is None:
            outcome = "passed"
        else:
            outcome = f"failed_{failure_reason_2.removeprefix('mini_probe_failed_')}"

    completed_at_iso = _iso8601_z(_utc_now())
    prompt_identity = {
        "system_sha256": system_sha,
        "source_corpus_sha256": source_corpus_sha,
        "user_prompts_source_sha256": user_prompts_source_sha,
    }
    result_doc = build_mini_probe_result(
        outcome=outcome,
        selected_peak_tps=float(selected_peak_tps),
        aggregate=final["agg"],
        cache_key=final["cache_key"],
        calibration_result_sha256=calibration_result_sha256,
        deployment_used=deployment,
        model=cfg.deployment.family,
        api_version=cfg.client.api_version,
        pricing_snapshot_path=pricing_snapshot_path,
        pricing_accessed_date=(
            pricing.accessed_date
            if hasattr(pricing, "accessed_date") else None
        ),
        prompt_identity=prompt_identity,
        started_at_iso=started_at_iso,
        completed_at_iso=completed_at_iso,
        total_usd=total_usd,
        total_committed_usd=total_committed_usd,
    )
    # Persist artifacts so downstream audit + abort envelope can
    # reference them by path/sha. The sha256 is folded back into the
    # in-memory dict so the v2.4 gate (mini_probe_revalidated path)
    # picks it up via `mini_probe_result_sha256`.
    _result_path, _summary_path, _sidecar_path, result_sha = (
        write_mini_probe_artifacts(
            runs_dir=runs_dir,
            experiment_id=cfg.experiment_id,
            timestamp_label=timestamp_label,
            stage=stage,
            result_doc=result_doc,
        )
    )
    result_doc["mini_probe_result_sha256"] = result_sha
    logger.info(
        "MINI_PROBE_COMPLETED stage=%s outcome=%s n_records=%d n_429=%d "
        "cache_hit=%.4f result=%s",
        stage, outcome, result_doc["mini_probe_n_records"],
        result_doc["mini_probe_n_429_records"],
        result_doc["mini_probe_cache_hit_ratio_steady_state"],
        _result_path,
    )
    return result_doc


def _assert_empirical_promotion_pins_match_defaults() -> None:
    """§13(c) auditor checklist — single source of truth.

    The `_EmpiricalPromotionBlock` dataclass defaults are literal mirrors
    of the §10 PINNED CONSTANTS. This assertion runs once at module
    import to guarantee the two stay in lockstep. If the constants are
    ever bumped (spec revision), this assertion will fail loudly until
    the dataclass defaults are bumped too.
    """
    sentinel = _EmpiricalPromotionBlock()
    pairs = (
        (
            "cache_hit_floor_smallest_control",
            EMPIRICAL_PROMOTION_CACHE_HIT_FLOOR_SMALLEST_CONTROL,
            sentinel.cache_hit_floor_smallest_control,
        ),
        (
            "cache_hit_floor_largest",
            EMPIRICAL_PROMOTION_CACHE_HIT_FLOOR_LARGEST,
            sentinel.cache_hit_floor_largest,
        ),
        (
            "calibration_max_age_hours",
            EMPIRICAL_PROMOTION_CALIBRATION_MAX_AGE_HOURS,
            sentinel.calibration_max_age_hours,
        ),
        (
            "minimum_records_at_selected_tps",
            EMPIRICAL_PROMOTION_MINIMUM_RECORDS_AT_SELECTED_TPS,
            sentinel.minimum_records_at_selected_tps,
        ),
        (
            "mini_probe_enabled",
            EMPIRICAL_PROMOTION_MINI_PROBE_ENABLED_DEFAULT,
            sentinel.mini_probe_enabled,
        ),
        (
            "mini_probe_max_usd",
            EMPIRICAL_PROMOTION_MINI_PROBE_MAX_USD,
            sentinel.mini_probe_max_usd,
        ),
        (
            "mini_probe_max_attempts_per_run",
            EMPIRICAL_PROMOTION_MINI_PROBE_MAX_ATTEMPTS_PER_RUN,
            sentinel.mini_probe_max_attempts_per_run,
        ),
    )
    for field_name, pinned, defaulted in pairs:
        if pinned != defaulted:
            raise AssertionError(
                f"v2.4 §13(c): _EmpiricalPromotionBlock.{field_name} "
                f"default {defaulted!r} drifted from §10 PIN {pinned!r}"
            )


_assert_empirical_promotion_pins_match_defaults()


# ----------------------------------------------------------------------------
# CLI plumbing
# ----------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scripts.measure_max_output_tokens_sweep",
        description=(
            "Task 019 v2.2.1 max_output_tokens admission-reservation proxy "
            "benchmark. Sweeps max_output_tokens ∈ [256..16384] against "
            "the throttled ptu-deploy-throttled deployment (PAYG GlobalStandard "
            "60K TPM / 600 RPM) with per-cell pre-warm → linear-ramp → "
            "cool-down via the async_scheduled dispatcher (concurrency=96, "
            "selected_peak_tps from Stage 0.5 calibration, sdk max_retries=0). "
            "Records the first 429-onset RPM per cell — PAYG throttled-quota / "
            "Azure admission-reservation PROXY for Hypothesis I."
        ),
    )
    p.add_argument("--experiment", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--smoke", action="store_true")
    p.add_argument(
        "--pricing-policy",
        choices=PRICING_POLICY_MODES,
        default=LIVE_MEASUREMENT,
        help=(
            "Versioned pricing semantics. live-measurement (default) requires "
            "fresh pricing; historical-replay is offline-only."
        ),
    )
    p.add_argument(
        "--stage",
        choices=("dry-run", "calibration", "smoke", "evidence"),
        default=None,
        help=(
            "Explicit stage selector. --dry-run is equivalent to "
            "--stage dry-run; --smoke is equivalent to --stage smoke. "
            "Default (no flag) is --stage evidence. v2.2.1 adds "
            "--stage calibration."
        ),
    )
    p.add_argument(
        "--calibration-result",
        default=None,
        help=(
            "v2.2.1 — REQUIRED for --stage smoke and --stage evidence. "
            "Path to a calibration_result.json with outcome=='selected' "
            "produced by --stage calibration. NO auto-discovery."
        ),
    )
    p.add_argument(
        "--smoke-summary",
        default=None,
        help=(
            "v2.2.1 — REQUIRED for --stage evidence (alongside "
            "--calibration-result). Path to the runs/*.summary.json "
            "produced by the smoke run whose calibration linkage and "
            "sidecar sha256 should be validated."
        ),
    )
    p.add_argument("--allow-dirty", action="store_true")
    p.add_argument("--benchmarks-root", default="benchmarks")
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return p


def _configure_logging(level: str) -> None:
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler(stream=sys.stderr)
        h.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%SZ",
            )
        )
        root.addHandler(h)
    root.setLevel(getattr(logging, level))


def _resolve_stage(args: argparse.Namespace) -> tuple[str, bool]:
    """Map flag combinations to ``(stage, dry_run)``."""
    flags_set = sum(
        1 for x in (args.dry_run, args.smoke, args.stage is not None) if x
    )
    if flags_set > 1:
        raise SystemExit(
            "--dry-run, --smoke, --stage are mutually exclusive"
        )
    if args.dry_run or args.stage == "dry-run":
        return ("dry-run", True)
    if args.smoke or args.stage == "smoke":
        return ("smoke", False)
    if args.stage == "calibration":
        return ("calibration", False)
    return ("evidence", False)


def _detect_forbidden_peak_ramp_tps_override(
    argv: list[str] | None,
) -> bool:
    """v2.2.1 — detect any literal ``--peak-ramp-tps`` argument.

    The CLI never DEFINES this option (so argparse would raise
    SystemExit on unknown arg), but we want a deterministic exit 9 (not
    SystemExit / 2) with the spec's pinned reason. Scan the raw argv
    BEFORE invoking argparse."""
    src = argv if argv is not None else sys.argv[1:]
    for tok in src:
        if tok == "--peak-ramp-tps" or tok.startswith("--peak-ramp-tps="):
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    # v2.2.1 — forbid --peak-ramp-tps override BEFORE argparse can
    # SystemExit(2) on unknown-arg.
    if _detect_forbidden_peak_ramp_tps_override(argv):
        logger.error(
            "LINKAGE_VALIDATION_FAILED reason="
            "peak_ramp_tps_override_forbidden_use_calibration_result "
            "(v2.2.1 — peak_ramp_tps flows from Stage 0.5 calibration; "
            "--peak-ramp-tps CLI override is FORBIDDEN, exit 9)"
        )
        return EXIT_LINKAGE_FAIL
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.log_level)
    stage, dry_run = _resolve_stage(args)
    # v2.2.1 — dispatch dry-run to its inner stage. Smoke/evidence keep
    # their natural inner stage; calibration is its own.
    if stage == "dry-run":
        inner_stage = "evidence"
    else:
        inner_stage = stage

    try:
        cfg = load_experiment(args.experiment)
    except FileNotFoundError as exc:
        logger.error("EXPERIMENT_YAML_MISSING %s", exc)
        return EXIT_DATASET
    except yaml.YAMLError as exc:
        # v2.3 microfix 2026-05-30 fix loop #2 (auditor finding #2):
        # malformed YAML must abort deterministically (exit 9) AND emit
        # the ``startup_abort_reason`` artifact so downstream audit can
        # detect the abort without scraping stderr. The stack trace is
        # SUPPRESSED — the artifact + the one-line stderr summary are
        # the durable evidence (per spec: "no stack trace").
        #
        # The artifact lives at ``{benchmarks_root}/unknown_experiment/``
        # because the YAML is unparseable (no ``experiment_id`` can be
        # recovered) — the artifact path itself is deterministic
        # (timestamp + ``unknown_experiment`` + standard suffix).
        short_msg = str(exc).splitlines()[0] if str(exc) else "yaml parse error"
        logger.error(
            "EXPERIMENT_YAML_MALFORMED reason=%s path=%s detail=%s",
            EXPERIMENT_YAML_MALFORMED_REASON,
            args.experiment,
            short_msg,
        )
        experiment_id_fallback = "unknown_experiment"
        _runs_dir = (
            pathlib.Path(args.benchmarks_root) / experiment_id_fallback
        )
        try:
            emit_startup_abort_artifact(
                _runs_dir,
                experiment_id=experiment_id_fallback,
                startup_abort_reason=EXPERIMENT_YAML_MALFORMED_REASON,
                message=(
                    f"experiment YAML at {args.experiment!r} could not "
                    f"be parsed by yaml.safe_load: {short_msg}"
                ),
            )
        except OSError as artifact_exc:
            logger.error(
                "STARTUP_ABORT_ARTIFACT_WRITE_FAILED %s", artifact_exc
            )
        return EXIT_LINKAGE_FAIL
    except LinkageValidationError as exc:
        logger.error("LINKAGE_VALIDATION_FAILED reason=%s %s", exc.reason, exc)
        # v2.3 microfix 2026-05-30 (finding #3) — emit a deterministic
        # ``startup_abort_reason`` artifact for the four pinned Phase B
        # grid mutations so downstream audit can detect the abort
        # without scraping stderr. Wider YAML-load failures fall back
        # to the bare logger.error above (spec says "or an equivalent
        # deterministic artifact per spec" — for non-Phase-B reasons
        # the deterministic artifact is the stderr line itself).
        if exc.reason in STARTUP_ABORT_PHASE_B_REASONS:
            experiment_id_fallback = "unknown_experiment"
            try:
                _raw_yaml_text = pathlib.Path(args.experiment).read_text(
                    encoding="utf-8"
                )
                _raw_yaml_doc = yaml.safe_load(_raw_yaml_text) or {}
                _raw_exp_id = _raw_yaml_doc.get("experiment_id")
                if isinstance(_raw_exp_id, str) and _raw_exp_id.strip():
                    experiment_id_fallback = _raw_exp_id.strip()
            except (OSError, yaml.YAMLError):
                pass
            _runs_dir = (
                pathlib.Path(args.benchmarks_root) / experiment_id_fallback
            )
            try:
                emit_startup_abort_artifact(
                    _runs_dir,
                    experiment_id=experiment_id_fallback,
                    startup_abort_reason=exc.reason or "unknown",
                    message=str(exc),
                )
            except OSError as artifact_exc:
                logger.error(
                    "STARTUP_ABORT_ARTIFACT_WRITE_FAILED %s", artifact_exc
                )
        return EXIT_LINKAGE_FAIL
    except ValueError as exc:
        logger.error("EXPERIMENT_YAML_INVALID %s", exc)
        return EXIT_RUNTIME

    # v2.2.1 — early linkage refusal for smoke/evidence with no
    # --calibration-result (auto-discovery FORBIDDEN, exit 9).
    if stage in ("smoke", "evidence") and not args.calibration_result:
        logger.error(
            "LINKAGE_VALIDATION_FAILED reason=calibration_result_missing "
            "(v2.2.1 — --stage %s requires --calibration-result <path>; "
            "no auto-discovery)",
            stage,
        )
        return EXIT_LINKAGE_FAIL
    if stage == "evidence" and not args.smoke_summary:
        logger.error(
            "LINKAGE_VALIDATION_FAILED reason=smoke_summary_missing "
            "(v2.2.1 — --stage evidence requires --smoke-summary <path>; "
            "no auto-discovery)",
        )
        return EXIT_LINKAGE_FAIL

    try:
        if stage == "calibration":
            result = run_calibration(
                cfg=cfg,
                benchmarks_root=pathlib.Path(args.benchmarks_root),
                allow_dirty=args.allow_dirty,
                pricing_policy=args.pricing_policy,
            )
        else:
            result = run_measurement(
                cfg=cfg,
                benchmarks_root=pathlib.Path(args.benchmarks_root),
                dry_run=dry_run,
                stage=inner_stage,
                allow_dirty=args.allow_dirty,
                calibration_result_path=args.calibration_result,
                smoke_summary_path=args.smoke_summary,
                pricing_policy=args.pricing_policy,
            )
    except CalibrationTerminalError as exc:
        logger.error(
            "CALIBRATION_TERMINAL outcome=%s %s", exc.outcome, exc
        )
        return EXIT_CALIBRATION_TERMINAL
    except LinkageValidationError as exc:
        logger.error("LINKAGE_VALIDATION_FAILED reason=%s %s", exc.reason, exc)
        # v2.4 §6 + §11.20 — pre-promotion abort envelope for the
        # prompt-identity-mismatch case (exit_reason verbatim,
        # empirical_promotion_denied_reason=null per §6).
        if exc.reason == "calibration_prompt_identity_mismatch":
            try:
                _v24_runs_dir = (
                    pathlib.Path(args.benchmarks_root) / cfg.experiment_id
                    / "runs"
                )
                write_abort_envelope_artifact(
                    runs_dir=_v24_runs_dir,
                    experiment_id=cfg.experiment_id,
                    timestamp_label=datetime.datetime.now(
                        datetime.timezone.utc
                    ).strftime("%Y%m%dT%H%M%SZ"),
                    stage=stage if stage in ("smoke", "evidence") else "smoke",
                    exit_reason="calibration_prompt_identity_mismatch",
                    empirical_promotion_denied_reason=None,
                )
            except (OSError, ValueError) as artifact_exc:
                logger.error(
                    "V24_ABORT_ENVELOPE_WRITE_FAILED %s", artifact_exc
                )
        # v2.4 §9.3 / §11.21 — pre-promotion abort envelope for the
        # evidence-stage echo-validation preflight failure. The
        # `evidence_summary_missing_smoke_promotion_path_echo` reason
        # fires BEFORE the §6 step 5 promotion gate is reached for the
        # evidence stage, so `empirical_promotion_denied_reason` is
        # `null` (microfix #5 blocker 1 / microfix #6 blocker 1 null-
        # discipline for pre-promotion preflight aborts). The envelope
        # MUST NOT carry any §9.4-forbidden admitted-summary field.
        elif exc.reason == (
            "evidence_summary_missing_smoke_promotion_path_echo"
        ):
            try:
                _v24_runs_dir = (
                    pathlib.Path(args.benchmarks_root) / cfg.experiment_id
                    / "runs"
                )
                write_abort_envelope_artifact(
                    runs_dir=_v24_runs_dir,
                    experiment_id=cfg.experiment_id,
                    timestamp_label=datetime.datetime.now(
                        datetime.timezone.utc
                    ).strftime("%Y%m%dT%H%M%SZ"),
                    stage="evidence",
                    exit_reason=(
                        "evidence_summary_missing_smoke_promotion_path_echo"
                    ),
                    empirical_promotion_denied_reason=None,
                )
            except (OSError, ValueError) as artifact_exc:
                logger.error(
                    "V24_ABORT_ENVELOPE_WRITE_FAILED %s", artifact_exc
                )
        return EXIT_LINKAGE_FAIL
    except PromptIdentitySHAMismatchError as exc:
        logger.error("PROMPT_IDENTITY_SHA_MISMATCH %s", exc)
        return EXIT_SHA_MISMATCH
    except RunLockHeldError as exc:
        logger.error("RUN_LOCK_HELD %s", exc)
        return EXIT_RUNLOCK
    except (PricingStaleError, PricingPolicyError) as exc:
        logger.error("PRICING_STALE %s", exc)
        return EXIT_PRICING
    except TpmFeasibilityAbortError as exc:
        logger.error("TPM_FEASIBILITY_ABORT %s", exc)
        # v2.4 §9.4 — when the abort fires downstream of an empirical-
        # promotion denial (cold-cache fallback ALSO denied), write the
        # abort envelope artifact. `exit_reason` is the v2.1-PRESERVED
        # `TPM_FEASIBILITY_ABORT` (microfix #5 blocker 1); the §8 stable
        # `empirical_promotion_disabled_*` identifier surfaces in
        # `empirical_promotion_denied_reason`.
        v24_reason = getattr(exc, "v24_empirical_denied_reason", None)
        v24_stage = getattr(exc, "v24_stage", None) or stage
        if v24_reason and v24_stage in ("smoke", "evidence"):
            try:
                _v24_runs_dir = (
                    pathlib.Path(args.benchmarks_root) / cfg.experiment_id
                    / "runs"
                )
                write_abort_envelope_artifact(
                    runs_dir=_v24_runs_dir,
                    experiment_id=cfg.experiment_id,
                    timestamp_label=datetime.datetime.now(
                        datetime.timezone.utc
                    ).strftime("%Y%m%dT%H%M%SZ"),
                    stage=v24_stage,
                    exit_reason=TPM_FEASIBILITY_ABORT_EXIT_REASON,
                    empirical_promotion_denied_reason=v24_reason,
                )
            except (OSError, ValueError) as artifact_exc:
                logger.error(
                    "V24_ABORT_ENVELOPE_WRITE_FAILED %s", artifact_exc
                )
        return EXIT_RUNTIME
    except PreflightBudgetAbortError as exc:
        reason_note = f" reason={exc.reason}" if exc.reason else ""
        logger.error("PREFLIGHT_BUDGET_ABORT%s %s", reason_note, exc)
        return EXIT_USD_PREFLIGHT
    except PreflightReachabilityError as exc:
        logger.error("PREFLIGHT_REACHABILITY_FAILED %s", exc)
        return EXIT_AUTH
    except EndpointMisconfiguredError as exc:
        logger.error("ENDPOINT_MISCONFIGURED %s", exc)
        return EXIT_AUTH
    except CorpusMissingError as exc:
        logger.error("DATASET_MISSING %s", exc)
        return EXIT_DATASET
    except BudgetHaltError as exc:
        logger.error("BUDGET_HALT %s", exc)
        return EXIT_RUNTIME

    halt_note = (
        f" (halt_reason={result.halt_reason})"
        if result.halt_reason else ""
    )
    summary_line = (
        f"\n=== measure_max_output_tokens_sweep summary ===\n"
        f"experiment_id        : {cfg.experiment_id}\n"
        f"stage                : {stage}{halt_note}\n"
        f"dry_run              : {dry_run}\n"
        f"cells_completed      : {result.cells_completed}/{result.cells_planned}\n"
        f"total_usd            : ${result.total_usd:.4f}\n"
        f"partial              : {result.partial}\n"
        f"jsonl                : {result.jsonl_path}\n"
        f"summary_json         : {result.summary_path}\n"
        f"==============================================="
    )
    print(summary_line)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
