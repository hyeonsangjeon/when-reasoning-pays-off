"""scripts/measure_cache_key_bucketing.py — Task 018 v2.4 prompt_cache_key
bucketing benchmark.

This module sweeps the cardinality of ``prompt_cache_key`` buckets against the
docs-stated ~15 req/min per-bucket overflow threshold described in Azure
OpenAI / AI Foundry's prompt-caching documentation
(https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/prompt-caching).

Design (verbatim from .internal/tasks/018-cache-key-bucketing-benchmark.md
v2.4 hotfix banner — raises runtime.concurrency from 8 → 96 to absorb the
observed live gpt-5.2 P95 TTFT ≈ 128 s; all other v2.3 pins unchanged):

* **Pure routing function.** ``select_bucket(arrival_idx, cardinality,
  namespace)`` is the only routing primitive. It is deterministic, stateless,
  and round-robin: ``f"{namespace}_bucket_{arrival_idx % cardinality:03d}"``.
* **Per-cell namespace.** Constructed once per cell as
  ``benchmark06_{retention_tag}_card{NN}_{run_id_short}`` so cell N never
  prewarms cell N+1's bucket entries (critical under ``24h`` retention).
* **Pinned v2.4 controls.** ``max_output_tokens=512``,
  ``reasoning.effort="low"``, ``client.api_version="preview"`` (Foundry v1),
  ``runtime.concurrency=96`` (v2.4 — was 8 in v2.3),
  ``runtime.sustain_tps=0.5``,
  ``runtime.dispatcher="async_scheduled"``,
  ``request_template.estimated_processed_tokens_max=11000``,
  ``metadata.deployment_tpm_quota=500000``. Echoed into every per-request
  record and every summary so the analysis can re-verify the regime.
* **Single deployment.** The unthrottled 500K-TPM ``gpt-5.2`` deployment.
  Task 013's throttled deployment is rejected at YAML load time so the
  Hypothesis G saturation signal does not confound this benchmark's bucketing
  signal.
* **async_scheduled dispatcher.** A wall-clock pacer schedules arrivals at
  ``sustain_tps`` cadence; an asyncio.Semaphore(concurrency=96) bounds the
  number of in-flight HTTP calls. The v2.4 size 96 gives ~50 % headroom over
  the Little's-Law steady-state in-flight count (0.5 TPS × ~128 s observed
  live P95 TTFT ≈ 64 in-flight); v2.3's sem=8 saturated under the same
  workload and tripped the backlog_excessive cell fail condition. Each record carries
  ``scheduled_dispatch_cell_elapsed_ms`` (captured immediately after the
  pacer sleep returns, BEFORE ``sem.acquire``) and
  ``admitted_dispatch_cell_elapsed_ms`` (captured AFTER ``sem.acquire``,
  IMMEDIATELY before the HTTP send). ``dispatch_backlog_ms`` is the
  difference. ``in_flight_at_dispatch`` is captured post-acquire (before
  increment). All RPM bookkeeping uses the admitted timestamps; the
  scheduled timestamps are diagnostic only.
* **Backlog-excessive cell.** A cell with
  ``P95(dispatch_backlog_ms) > 1500`` OR
  ``max(dispatch_backlog_ms) > 5000`` is flagged ``backlog_excessive=true``,
  excluded from the analysis aggregates, and counted as a Stage 1/2 failure.
* **TPM feasibility preflight gate (NEW in v2.3).** Before any client
  construction or network call the runner verifies
  ``60 × sustain_tps × estimated_processed_tokens_max <=
  0.70 × metadata.deployment_tpm_quota`` and aborts otherwise. For the v2.3
  pins this is ``60 × 0.5 × 11000 = 330000 <= 0.70 × 500000 = 350000``.
* **Per-request token cap (NEW in v2.3).** The runner refuses to construct
  any request whose estimated input tokens + ``max_output_tokens`` exceed
  ``estimated_processed_tokens_max``; the offending arrival is recorded
  as a failed sentinel record (no network call) and the cell continues.
* **Preflight USD budget gate.** Before the first network call the runner
  reads ``pricing/azure-openai-payg-2026-05.yaml`` (or whichever snapshot
  is configured), computes a projected USD cost from
  ``cells × calls_per_cell × per_call_cost`` (under a conservative
  85%-cached steady-state assumption and the YAML's pinned
  ``max_output_tokens``), and aborts with non-zero exit when
  ``projected_usd > 0.9 × hard_ceiling_usd``. The snapshot must exist and
  its ``accessed_date`` must be within 90 days of the run date.
* **Mid-run USD budget gate.** After each completed cell the runner sums
  actual USD spend; if it exceeds ``0.85 × hard_ceiling_usd`` the runner
  halts cleanly, writes a ``runs/<timestamp>.partial.summary.json`` with
  the completed-cell list, and exits 0 (partial run is legitimate).
* **Staged protocol.** ``--dry-run`` (Stage 0) issues zero network calls.
  ``--smoke`` (Stage 1) runs the smoke profile (``cells_smoke`` cells,
  ``calls_per_cell_smoke`` per cell). ``--stage evidence`` runs the full
  sweep. The default invocation (no stage flag) runs evidence.
* **PAYG-not-PTU framing.** Each YAML's ``metadata`` block declares
  ``consumption_model_context: paygo_standard``,
  ``runtime_mode: live_azure_single_deployment``,
  ``deployment_kind: GlobalStandard_PAYG``, ``simulation: false``,
  ``ptu_evidence: false``, ``deployment_tpm_quota: 500000``. The same
  block is echoed into every summary.

CLI contract::

    python -m scripts.measure_cache_key_bucketing \\
        --experiment experiments/exp006_cache_key_bucketing_inmemory.yaml \\
        [--dry-run | --smoke | --stage {dry-run,smoke,evidence}]

Exit codes:
    0 = success OR mid-run gate halt (partial run is a legitimate outcome)
    1 = preflight budget gate aborted, TPM gate aborted, OR unrecoverable
        runtime error
    2 = endpoint misconfiguration / auth / preflight reachability failed
    3 = corpus / user-prompts file missing or malformed
    4 = experiment YAML invalid
    5 = pricing snapshot missing or stale (> 90 days)
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import dataclasses
import datetime
import hashlib
import inspect
import json
import logging
import os
import pathlib
import random
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import yaml

from scripts._pricing_types import PaygPricing, TokenUsage
from scripts.cost_calculator import (
    load_payg_pricing,
    payg_cost_per_call,
)


__all__ = [
    "BUCKET_KEY_RE",
    "BudgetHaltError",
    "CitationsBuilder",
    "CorpusMissingError",
    "EndpointMisconfiguredError",
    "ExperimentConfig",
    "MeasurementResult",
    "PreflightBudgetAbortError",
    "PreflightReachabilityError",
    "PricingStaleError",
    "RpmTracker",
    "TokenCapAbortError",
    "TpmFeasibilityAbortError",
    "build_namespace",
    "compute_projected_tpm",
    "compute_projected_usd",
    "load_experiment",
    "main",
    "select_bucket",
]

logger = logging.getLogger("scripts.measure_cache_key_bucketing")


# ----------------------------------------------------------------------------
# Constants — Foundry v1 + Task 018 v2.3 pinned controls
# ----------------------------------------------------------------------------

FOUNDRY_API_VERSION = "preview"
"""Foundry v1 API version literal. Copied verbatim from Task 013's
``scripts/measure_dual_spillover.py`` so this benchmark inherits the same
api-version pin (see Task 018 v2.3 spec, Control / Varying Variables)."""

DEFAULT_TOKEN_ESTIMATE_DIVISOR = 4

DEFAULT_TOKEN_MAX_RETRIES = 5
DEFAULT_TOKEN_BASE_BACKOFF_SECONDS = 1.0
DEFAULT_TOKEN_MAX_BACKOFF_SECONDS = 30.0

PER_BUCKET_RPM_WINDOW_S = 60.0
"""Rolling window for per-bucket and common-prefix RPM bookkeeping."""

WARMUP_EXCLUSION_SECONDS = 180.0
"""Drop the first 3 minutes of every evidence-stage cell when computing
steady-state stats. See Task 018 v2.3 Implementation Notes "Cell duration
rationale"."""

SMOKE_CALLS_PER_CELL = 60
"""Stage 1 smoke per-cell call count. Smoke is pipeline validation, NOT
steady-state measurement, so no warm-up exclusion applies."""

SMOKE_CARDINALITIES = (1, 8)
"""Stage 1 smoke cells: card=1 above the 15 RPM threshold and card=8 below.
At v2.3 sustain_tps=0.5 (30 RPM common-prefix), per-bucket RPM is 30 and
3.75 respectively."""

PRICING_SNAPSHOT_MAX_AGE_DAYS = 90
"""Reject a pricing snapshot whose ``accessed_date`` is older than this."""

EVIDENCE_PROJECTED_OUTPUT_TOKENS = 500
"""Conservative per-call output-token assumption for the preflight projection
(visible ~300 + reasoning headroom at ``effort=low``)."""

EVIDENCE_CACHED_FRACTION = 0.85
"""Conservative steady-state cached-token fraction for the preflight
projection."""

# v2.3 pinned controls (single source of truth for load_experiment validators
# and for the TPM feasibility preflight math).

CONCURRENCY_PINNED = 96
"""v2.4 pinned ``runtime.concurrency`` (was 8 in v2.3). Sized for ~50 %
headroom over the Little's-Law steady-state in-flight count under the
observed live gpt-5.2 P95 TTFT ≈ 128 s at TPS=0.5
(``0.5 × 128 ≈ 64`` in-flight at steady state, plus margin for sporadic
100 s+ retry-induced outliers). v2.3's sem=8 was sized against an assumed
~9 s TTFT that did not survive contact with the live deployment; sem=8
saturated and tripped ``backlog_excessive`` on both YAMLs in Stage 1 smoke
(inmemory P95 backlog=2398 ms, 24h P95 backlog=111,238 ms; both
``max_in_flight_observed_card1 = 8``). The v2.4 fix is exclusively the
semaphore-resize remediation permitted by the per-cell backlog fail
condition; TPM math is unchanged (Azure debits TPM against admitted
arrival rate × per-request tokens, NOT against client semaphore capacity)."""

SUSTAIN_TPS_PINNED = 0.5
"""v2.3 pinned ``runtime.sustain_tps``. 0.5 TPS × 60 = 30 RPM common-prefix;
combined with ``CONCURRENCY_PINNED`` and ``ESTIMATED_PROCESSED_TOKENS_MAX``,
this is the highest TPS the deployment's 500K-TPM quota tolerates with
30 % headroom (see ``compute_projected_tpm`` and the v2.3 hotfix banner
in .internal/tasks/018-cache-key-bucketing-benchmark.md)."""

DISPATCHER_PINNED = "async_scheduled"
"""v2.3 pinned ``runtime.dispatcher``. The runner ONLY supports the
async_scheduled dispatcher (wall-clock pacer + asyncio.Semaphore-guarded
in-flight workers)."""

ESTIMATED_PROCESSED_TOKENS_MAX = 11000
"""v2.3 pinned ``request_template.estimated_processed_tokens_max``. Per-
request hard ceiling on (input_tokens + max_output_tokens). The runner
refuses to construct any prompt that exceeds this. Also the multiplicand
in the TPM feasibility preflight gate."""

DEPLOYMENT_TPM_QUOTA_DEFAULT = 500000
"""v2.3 pinned ``metadata.deployment_tpm_quota`` for the unthrottled gpt-5.2
deployment. Denominator of the TPM feasibility preflight gate."""

TPM_HEADROOM_FRACTION = 0.70
"""v2.3 TPM feasibility preflight headroom fraction:
``60 × sustain_tps × estimated_processed_tokens_max <=
TPM_HEADROOM_FRACTION × deployment_tpm_quota``. 0.70 absorbs occasional
single-request input spikes (the Azure OpenAI estimated-processed-token
sliding window includes BOTH input and output tokens; the runner cannot
control output tokens precisely beyond ``max_output_tokens``)."""

BACKLOG_P95_FAIL_MS = 1500.0
"""v2.3 cell-level backlog regression threshold (P95 dispatch backlog).
A cell with P95(dispatch_backlog_ms) > 1500 is flagged
``backlog_excessive=true``, excluded from analysis, and counted as a
Stage 1/2 failure (cell where the wall-clock pacer's intended cadence
diverged materially from the realized post-acquire cadence). Tune by
LOWERING sustain_tps or RAISING concurrency (and re-passing the TPM
preflight); NEVER by silently substituting scheduled timestamps for
admitted timestamps."""

BACKLOG_MAX_FAIL_MS = 5000.0
"""v2.3 cell-level backlog regression threshold (max dispatch backlog).
A cell with max(dispatch_backlog_ms) > 5000 is flagged
``backlog_excessive=true`` regardless of P95 (a single ~5 s stall is
operationally meaningful even if P95 is fine)."""

PRE_ADMISSION_FAILURE_REASONS: frozenset[str] = frozenset({
    "token_cap_exceeded",
})
"""Failure reasons whose records never issued a meaningful HTTP call.

Records flagged ``failed=True`` with one of these ``failure_reason`` values
are PRE-admission: by the v2.4 admitted-timestamp authoritative rule they
MUST NOT contribute to admitted dispatch backlog, admitted RPM, or
in-flight aggregates.

Every other failed record (e.g. ``transport_exception:<ExcName>`` raised
after the HTTP call left the process, or ``rate_limited_after_retries``
returned by ``_call_with_retry`` after the retry budget was exhausted) is
POST-admission — it passed ``sem.acquire`` and the dispatcher actually
invoked Azure — and therefore DOES contribute to admitted dispatch /
admitted RPM / in-flight aggregates. Only the cache-hit-ratio and
model-latency aggregates continue to exclude post-admission failures (their
``usage`` / ``first_token_latency_ms`` payloads are absent or meaningless).
"""

# Concrete Microsoft Learn URL for the Citations block. v2.3 spec moves
# the canonical URL under the ``ai-foundry`` path (Microsoft renamed
# the documentation root from /azure/foundry/ to /azure/ai-foundry/
# during the gpt-5.x ramp). The same content; the new URL is required
# so the Citations block lands on a non-redirected page.
AZURE_DOC_PROMPT_CACHING_URL = (
    "https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/prompt-caching"
)
AZURE_DOC_ACCESSED_DATE = "2026-05-29"
AZURE_DOC_CLAIMS_CITED: tuple[str, ...] = (
    "prompt_cache_key combined with prefix hash influences routing",
    "~15 req/min per (prefix_hash, prompt_cache_key) overflow threshold",
    "24h retention when prompt_cache_retention='24h'",
    "in_memory retention is the default",
)

# NEW in v2.3: separate Microsoft Learn citation for the Azure OpenAI
# rate-limit / TPM quota semantics that the TPM feasibility preflight
# depends on.
AZURE_RATE_LIMIT_DOC_URL = (
    "https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/quota"
)
AZURE_RATE_LIMIT_DOC_ACCESSED_DATE = "2026-05-29"
AZURE_RATE_LIMIT_DOC_CLAIMS_CITED: tuple[str, ...] = (
    "deployment TPM quota is enforced as a sliding window over "
    "estimated processed tokens (input + max_output)",
    "exceeding the sliding window returns HTTP 429 with retry-after",
    "operator must keep projected TPM safely below quota to avoid "
    "throttling that would confound per-bucket RPM observation",
)

# Anonymization audit regex — matches the cell-namespaced bucket keys this
# script emits. The 24h-retention YAML uses ``24h`` and the in_memory uses
# ``inmemory``. Run-id short is an 8-character lowercase hex slice; the
# audit regex permits 4-8 to be lenient about future shorter slices.
BUCKET_KEY_RE = re.compile(
    r"^benchmark06_(inmemory|24h)_card\d{2}_[a-f0-9]{4,8}_bucket_\d{3}$"
)


EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_AUTH = 2
EXIT_DATASET = 3
EXIT_CONFIG = 4
EXIT_PRICING = 5


# ----------------------------------------------------------------------------
# Typed errors
# ----------------------------------------------------------------------------


class PreflightBudgetAbortError(RuntimeError):
    """``projected_usd > 0.9 × hard_ceiling_usd`` — runner aborts before the
    first network call."""


class BudgetHaltError(RuntimeError):
    """Catastrophic budget violation. The mid-run gate halts CLEANLY via
    ``MeasurementResult.halt_reason``; this exception is reserved for paths
    that cannot continue (e.g. an unexpected per-call cost spike that
    crosses the hard ceiling within a single cell)."""


class EndpointMisconfiguredError(RuntimeError):
    """Required Azure env vars missing, empty, or pointing at the throttled
    deployment instead of the unthrottled one."""


class CorpusMissingError(FileNotFoundError):
    """Corpus or user-prompts file cannot be found / parsed."""


class PreflightReachabilityError(RuntimeError):
    """The single-deployment reachability ping failed; abort the run."""


class PricingStaleError(RuntimeError):
    """Pricing snapshot missing OR ``accessed_date`` older than
    ``PRICING_SNAPSHOT_MAX_AGE_DAYS``."""


class TpmFeasibilityAbortError(RuntimeError):
    """v2.3 TPM feasibility preflight gate: the projected sliding-window
    TPM (``60 × sustain_tps × estimated_processed_tokens_max``) exceeds
    ``TPM_HEADROOM_FRACTION × deployment_tpm_quota``. The runner aborts
    BEFORE any client construction or network call so the offending
    regime never reaches the deployment."""


class TokenCapAbortError(RuntimeError):
    """v2.3 per-request token cap: a constructed request's estimated
    (input_tokens + max_output_tokens) exceeds
    ``request_template.estimated_processed_tokens_max``. The runner
    raises BEFORE the HTTP send; the offending arrival is recorded as a
    failed sentinel record and the cell continues."""


# ----------------------------------------------------------------------------
# Pure helpers — routing, namespace, projection, RPM
# ----------------------------------------------------------------------------


def select_bucket(arrival_idx: int, cardinality: int, namespace: str) -> str:
    """Round-robin bucket selection with cell-unique namespace.

    Pure, deterministic, no state. The ``namespace`` argument carries the
    per-cell prefix (see ``build_namespace``) so the same ``arrival_idx``
    across different cells produces non-overlapping ``prompt_cache_key``
    strings — the v2 fix for the cross-cell cache-prewarm confound.

    Args:
        arrival_idx: Zero-based arrival index within the cell.
        cardinality: Number of distinct buckets to round-robin across.
        namespace: Per-cell namespace string from ``build_namespace``.

    Returns:
        The ``prompt_cache_key`` string to forward to the Responses API.

    Raises:
        ValueError: ``cardinality < 1`` or ``arrival_idx < 0`` or
            ``namespace`` empty.
    """
    if cardinality < 1:
        raise ValueError(f"cardinality must be >= 1; got {cardinality}")
    if arrival_idx < 0:
        raise ValueError(f"arrival_idx must be >= 0; got {arrival_idx}")
    if not namespace:
        raise ValueError("namespace must be a non-empty string")
    return f"{namespace}_bucket_{arrival_idx % cardinality:03d}"


def build_namespace(
    retention_tag: str, cardinality: int, run_id_short: str
) -> str:
    """Construct the per-cell namespace prefix.

    Per Task 018 v2.3 Implementation Notes "Key namespace": the namespace
    encodes the retention mode (so the two YAMLs never collide), the
    cardinality (so cells within one YAML never collide), and an 8-char
    lowercase-hex slice of a UUID-v4 generated at process start (so
    different runs never collide, even on the same deployment under
    24h retention).

    Args:
        retention_tag: ``"inmemory"`` or ``"24h"``.
        cardinality: Cell cardinality (1-99).
        run_id_short: 4-8 lowercase hex characters identifying the run.

    Returns:
        e.g. ``"benchmark06_inmemory_card08_a3f2bcd1"``.

    Raises:
        ValueError: On invalid retention tag, cardinality, or run_id format.
    """
    if retention_tag not in ("inmemory", "24h"):
        raise ValueError(
            f"retention_tag must be 'inmemory' or '24h'; got {retention_tag!r}"
        )
    if not (1 <= cardinality <= 99):
        raise ValueError(
            f"cardinality must be in 1..99 to fit the namespace format; "
            f"got {cardinality}"
        )
    if not re.fullmatch(r"[a-f0-9]{4,8}", run_id_short):
        raise ValueError(
            f"run_id_short must be 4-8 lowercase hex chars; got {run_id_short!r}"
        )
    return f"benchmark06_{retention_tag}_card{cardinality:02d}_{run_id_short}"


def compute_projected_usd(
    *,
    cardinalities: list[int],
    calls_per_cell: int,
    pricing: PaygPricing,
    model: str,
    input_tokens: float,
    output_tokens: float,
    cached_fraction: float,
) -> float:
    """Estimate the maximum-spend USD for a full evidence sweep.

    Conservative formula used by the preflight gate. Cached fraction is
    set high (default 0.85) so the projection underestimates first-cell
    rebuild cost slightly; the hard-ceiling headroom (15%) absorbs that.

    Args:
        cardinalities: List of cell cardinalities to sweep.
        calls_per_cell: Calls per cell.
        pricing: Loaded PAYG pricing snapshot.
        model: PAYG model key (e.g. ``"gpt-5.2"``).
        input_tokens: Per-call input token count.
        output_tokens: Per-call output token count (visible + reasoning).
        cached_fraction: Steady-state cached-token fraction (0.0-1.0).

    Returns:
        Projected total USD across all cells × calls_per_cell.

    Raises:
        ValueError: ``cached_fraction`` not in ``[0.0, 1.0]``.
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
    n_calls = len(cardinalities) * calls_per_cell
    return per_call * n_calls


def compute_projected_tpm(
    *,
    sustain_tps: float,
    estimated_processed_tokens_max: int,
) -> float:
    """v2.3 TPM feasibility preflight numerator.

    Returns ``60 × sustain_tps × estimated_processed_tokens_max`` — the
    upper bound on the deployment's Azure OpenAI estimated-processed-token
    sliding-window contribution from this benchmark's traffic, assuming
    every request lands at the per-request cap. The feasibility gate
    (see ``_tpm_feasibility_gate`` below) aborts when this number exceeds
    ``TPM_HEADROOM_FRACTION × deployment_tpm_quota``.

    Args:
        sustain_tps: Wall-clock arrival rate (requests/second).
        estimated_processed_tokens_max: Per-request hard cap
            (input + max_output).

    Returns:
        Projected TPM (tokens-per-minute).

    Raises:
        ValueError: Either argument is non-positive.
    """
    if sustain_tps <= 0:
        raise ValueError(
            f"sustain_tps must be > 0; got {sustain_tps}"
        )
    if estimated_processed_tokens_max <= 0:
        raise ValueError(
            f"estimated_processed_tokens_max must be > 0; got "
            f"{estimated_processed_tokens_max}"
        )
    return 60.0 * float(sustain_tps) * float(estimated_processed_tokens_max)


class RpmTracker:
    """Rolling 60-second arrival-rate tracker.

    Pure-Python deque-backed. ``record(timestamp)`` appends; ``count(now)``
    returns the number of arrivals in the trailing ``window_s`` window,
    discarding stale entries.

    Args:
        window_s: Rolling window length in seconds (default 60).
    """

    def __init__(self, window_s: float = PER_BUCKET_RPM_WINDOW_S) -> None:
        self.window_s = float(window_s)
        self._dq: collections.deque[float] = collections.deque()

    def record(self, ts: float) -> None:
        """Append an arrival timestamp."""
        self._dq.append(float(ts))

    def count(self, now: float) -> int:
        """Return arrival count in the trailing window ending at ``now``."""
        cutoff = float(now) - self.window_s
        while self._dq and self._dq[0] < cutoff:
            self._dq.popleft()
        return len(self._dq)


# ----------------------------------------------------------------------------
# Citations block
# ----------------------------------------------------------------------------


class CitationsBuilder:
    """Builds the Task 018 v2.3 Citations block for summaries and analysis."""

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
        """Return the Citations block as a plain dict (JSON-safe)."""
        return {
            "azure_doc": {
                "url": AZURE_DOC_PROMPT_CACHING_URL,
                "accessed_date": AZURE_DOC_ACCESSED_DATE,
                "claims_cited": list(AZURE_DOC_CLAIMS_CITED),
            },
            "azure_rate_limit_doc": {
                "url": AZURE_RATE_LIMIT_DOC_URL,
                "accessed_date": AZURE_RATE_LIMIT_DOC_ACCESSED_DATE,
                "claims_cited": list(AZURE_RATE_LIMIT_DOC_CLAIMS_CITED),
            },
            "pricing": {
                "path": self.pricing_path,
                "source_url": self.pricing_source_url,
                "accessed_date": self.pricing_accessed_date,
            },
        }


# ----------------------------------------------------------------------------
# Experiment YAML
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
class _RuntimeBlock:
    concurrency: int
    sustain_tps: float
    dispatcher: str
    cell_duration_seconds: int
    smoke_calls_per_cell: int
    washout_seconds: int


@dataclass(frozen=True)
class _SweepBlock:
    bucket_cardinality: list[int]


@dataclass(frozen=True)
class _RequestTemplate:
    max_output_tokens: int
    reasoning_effort: str
    prompt_cache_retention: str  # "in_memory" or "24h"
    estimated_processed_tokens_max: int


@dataclass(frozen=True)
class _BudgetBlock:
    evidence_estimated_usd: float
    evidence_hard_ceiling_usd: float
    smoke_hard_ceiling_usd: float
    confirmed: bool


@dataclass(frozen=True)
class _ClientBlock:
    api_version: str


@dataclass(frozen=True)
class ExperimentConfig:
    """Parsed Task 018 v2.3 cache-key-bucketing experiment YAML."""

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
    user_prompts_path: str
    system_prompt_corpus_path: str
    pricing_snapshot_path: str
    metadata: dict[str, Any]
    deployment_tpm_quota: int


def _require(d: dict, key: str, where: str) -> Any:
    if key not in d:
        raise ValueError(f"{where}: missing required key {key!r}")
    return d[key]


_ENV_TEMPLATE_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _extract_env_name(template: str) -> str | None:
    """Return the env-var NAME from a ``${VAR}`` template, or None."""
    m = _ENV_TEMPLATE_RE.fullmatch(template.strip())
    return m.group(1) if m else None


def load_experiment(path: str | pathlib.Path) -> ExperimentConfig:
    """Load + validate the Task 018 v2.3 experiment YAML.

    Raises:
        FileNotFoundError: ``path`` does not exist.
        ValueError: schema violation.
    """
    p = pathlib.Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"experiment YAML not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: experiment YAML must be a mapping at top level")

    where = str(p)
    exp_id = _require(raw, "experiment_id", where)
    description = _require(raw, "description", where)
    benchmark = _require(raw, "benchmark", where)
    if benchmark != "06-cache-key-bucketing":
        raise ValueError(
            f"{where}: benchmark must be '06-cache-key-bucketing'; got {benchmark!r}"
        )
    parent = raw.get("parent_experiment")

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
            f"{where}: deployment.auth_mode must be 'entra'; got {auth_mode!r}"
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
    # Throttled deployment is REJECTED — Task 018 isolates bucketing from
    # saturation by running on the unthrottled gpt-5.2 deployment only.
    if "THROTTLED" in deployment_env.upper():
        raise ValueError(
            f"{where}: deployment.deployment env-var name {deployment_env!r} "
            f"contains 'THROTTLED'; Task 018 v2.3 requires the unthrottled "
            f"gpt-5.2 deployment (saturation would confound the bucketing "
            f"signal)"
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

    req_raw = _require(raw, "request_template", where)
    if not isinstance(req_raw, dict):
        raise ValueError(f"{where}: request_template must be a mapping")
    max_out = int(_require(req_raw, "max_output_tokens", f"{where}.request_template"))
    if max_out != 512:
        raise ValueError(
            f"{where}: request_template.max_output_tokens must be 512 "
            f"(Task 018 v2.3 pinned control); got {max_out}"
        )
    reasoning_raw = _require(req_raw, "reasoning", f"{where}.request_template")
    if not isinstance(reasoning_raw, dict):
        raise ValueError(
            f"{where}: request_template.reasoning must be a mapping"
        )
    effort = str(_require(reasoning_raw, "effort", f"{where}.request_template.reasoning"))
    if effort != "low":
        raise ValueError(
            f"{where}: request_template.reasoning.effort must be 'low' "
            f"(Task 018 v2.3 pinned control; 'minimal' was rejected by "
            f"prior repo hotfixes as unsupported by gpt-5.2); got {effort!r}"
        )
    retention = str(
        _require(req_raw, "prompt_cache_retention", f"{where}.request_template")
    )
    if retention not in ("in_memory", "24h"):
        raise ValueError(
            f"{where}: request_template.prompt_cache_retention must be "
            f"'in_memory' or '24h'; got {retention!r}"
        )
    est_max = int(
        _require(
            req_raw,
            "estimated_processed_tokens_max",
            f"{where}.request_template",
        )
    )
    if est_max != ESTIMATED_PROCESSED_TOKENS_MAX:
        raise ValueError(
            f"{where}: request_template.estimated_processed_tokens_max must "
            f"be {ESTIMATED_PROCESSED_TOKENS_MAX} (Task 018 v2.3 pinned "
            f"control; sized so the TPM feasibility preflight passes at "
            f"60 × sustain_tps × est_max <= 0.70 × deployment_tpm_quota); "
            f"got {est_max}"
        )
    request_template = _RequestTemplate(
        max_output_tokens=max_out,
        reasoning_effort=effort,
        prompt_cache_retention=retention,
        estimated_processed_tokens_max=est_max,
    )

    client_raw = _require(raw, "client", where)
    if not isinstance(client_raw, dict):
        raise ValueError(f"{where}: client must be a mapping")
    api_version = str(_require(client_raw, "api_version", f"{where}.client"))
    if api_version != FOUNDRY_API_VERSION:
        raise ValueError(
            f"{where}: client.api_version must be {FOUNDRY_API_VERSION!r} "
            f"(Foundry v1 endpoint pin, copied verbatim from Task 013's "
            f"measure_dual_spillover.py); got {api_version!r}"
        )
    client = _ClientBlock(api_version=api_version)

    runtime_raw = _require(raw, "runtime", where)
    if not isinstance(runtime_raw, dict):
        raise ValueError(f"{where}: runtime must be a mapping")
    concurrency = int(_require(runtime_raw, "concurrency", f"{where}.runtime"))
    if concurrency != CONCURRENCY_PINNED:
        raise ValueError(
            f"{where}: runtime.concurrency must be {CONCURRENCY_PINNED} "
            f"(Task 018 v2.4 pinned control — async_scheduled dispatcher "
            f"uses an asyncio.Semaphore({CONCURRENCY_PINNED}) sized for "
            f"~50 % headroom over the Little's-Law steady-state in-flight "
            f"count under observed live gpt-5.2 P95 TTFT ≈ 128 s at "
            f"TPS=0.5; v2.3's pin of 8 saturated and tripped "
            f"backlog_excessive on both YAMLs in Stage 1 smoke); "
            f"got {concurrency}"
        )
    sustain_tps = float(_require(runtime_raw, "sustain_tps", f"{where}.runtime"))
    if abs(sustain_tps - SUSTAIN_TPS_PINNED) > 1e-9:
        raise ValueError(
            f"{where}: runtime.sustain_tps must be {SUSTAIN_TPS_PINNED} "
            f"(Task 018 v2.4 pinned control — 30 RPM common-prefix places "
            f"card=1 at 2× the 15 RPM threshold and card=8 well below it, "
            f"with TPM feasibility preflight passing at "
            f"60 × 0.5 × 11000 = 330000 <= 0.70 × 500000 = 350000); "
            f"got {sustain_tps}"
        )
    dispatcher = str(
        _require(runtime_raw, "dispatcher", f"{where}.runtime")
    )
    if dispatcher != DISPATCHER_PINNED:
        raise ValueError(
            f"{where}: runtime.dispatcher must be {DISPATCHER_PINNED!r} "
            f"(Task 018 v2.3 pinned control — the runner only supports "
            f"the async_scheduled dispatcher; v2.1 serial dispatch and "
            f"v2.2 pre-acquire-only dispatch are both rejected because "
            f"they cannot honor the admitted-timestamp authoritative "
            f"RPM contract); got {dispatcher!r}"
        )
    cell_dur = int(
        _require(runtime_raw, "cell_duration_seconds", f"{where}.runtime")
    )
    if cell_dur < 60:
        raise ValueError(
            f"{where}: runtime.cell_duration_seconds must be >= 60; got {cell_dur}"
        )
    smoke_calls = int(
        runtime_raw.get("smoke_calls_per_cell", SMOKE_CALLS_PER_CELL)
    )
    washout = int(runtime_raw.get("washout_seconds", 0))
    if washout < 0:
        raise ValueError(
            f"{where}: runtime.washout_seconds must be >= 0; got {washout}"
        )
    runtime = _RuntimeBlock(
        concurrency=concurrency,
        sustain_tps=sustain_tps,
        dispatcher=dispatcher,
        cell_duration_seconds=cell_dur,
        smoke_calls_per_cell=smoke_calls,
        washout_seconds=washout,
    )

    sweep_raw = _require(raw, "sweep", where)
    if not isinstance(sweep_raw, dict):
        raise ValueError(f"{where}: sweep must be a mapping")
    bc_raw = _require(sweep_raw, "bucket_cardinality", f"{where}.sweep")
    if not isinstance(bc_raw, list) or not bc_raw:
        raise ValueError(
            f"{where}: sweep.bucket_cardinality must be a non-empty list of ints"
        )
    cards: list[int] = []
    for v in bc_raw:
        iv = int(v)
        if iv < 1 or iv > 99:
            raise ValueError(
                f"{where}: sweep.bucket_cardinality entries must be in 1..99; "
                f"got {v!r}"
            )
        cards.append(iv)
    sweep = _SweepBlock(bucket_cardinality=cards)

    budget_raw = _require(raw, "budget", where)
    if not isinstance(budget_raw, dict):
        raise ValueError(f"{where}: budget must be a mapping")
    ev_est = float(
        _require(budget_raw, "evidence_estimated_usd", f"{where}.budget")
    )
    ev_hard = float(
        _require(budget_raw, "evidence_hard_ceiling_usd", f"{where}.budget")
    )
    sm_hard = float(
        _require(budget_raw, "smoke_hard_ceiling_usd", f"{where}.budget")
    )
    confirmed = bool(budget_raw.get("confirmed", False))
    if ev_hard <= 0 or sm_hard <= 0:
        raise ValueError(
            f"{where}: budget hard ceilings must be > 0"
        )
    budget = _BudgetBlock(
        evidence_estimated_usd=ev_est,
        evidence_hard_ceiling_usd=ev_hard,
        smoke_hard_ceiling_usd=sm_hard,
        confirmed=confirmed,
    )

    corpus_seed = int(_require(raw, "corpus_seed", where))
    target_tokens = int(raw.get("target_system_prompt_tokens", 10000))
    user_prompts_path = str(_require(raw, "user_prompts_path", where))
    corpus_path = str(_require(raw, "system_prompt_corpus_path", where))
    pricing_snap = str(
        _require(raw, "pricing_snapshot_path", where)
    )

    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError(f"{where}: metadata must be a mapping")
    md_required = {
        "consumption_model_context": "paygo_standard",
        "runtime_mode": "live_azure_single_deployment",
        "deployment_kind": "GlobalStandard_PAYG",
        "simulation": False,
        "ptu_evidence": False,
    }
    for k, expected in md_required.items():
        if k not in metadata:
            raise ValueError(
                f"{where}: metadata.{k} is required (Task 018 v2.3 PAYG/PTU "
                f"declaration); expected {expected!r}"
            )
        if metadata[k] != expected:
            raise ValueError(
                f"{where}: metadata.{k} must be {expected!r}; got {metadata[k]!r}"
            )
    # v2.3 adds deployment_tpm_quota as a pinned metadata field. The TPM
    # feasibility preflight depends on it; the runner aborts if it is
    # missing OR mismatched.
    if "deployment_tpm_quota" not in metadata:
        raise ValueError(
            f"{where}: metadata.deployment_tpm_quota is required "
            f"(Task 018 v2.3 pinned control — denominator of the TPM "
            f"feasibility preflight gate); expected "
            f"{DEPLOYMENT_TPM_QUOTA_DEFAULT}"
        )
    dep_quota = int(metadata["deployment_tpm_quota"])
    if dep_quota != DEPLOYMENT_TPM_QUOTA_DEFAULT:
        raise ValueError(
            f"{where}: metadata.deployment_tpm_quota must be "
            f"{DEPLOYMENT_TPM_QUOTA_DEFAULT} (Task 018 v2.3 pinned control "
            f"— the unthrottled gpt-5.2 deployment is provisioned with "
            f"this quota; a different value would invalidate the TPM "
            f"feasibility preflight math); got {dep_quota}"
        )

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
        user_prompts_path=user_prompts_path,
        system_prompt_corpus_path=corpus_path,
        pricing_snapshot_path=pricing_snap,
        metadata=dict(metadata),
        deployment_tpm_quota=dep_quota,
    )


# ----------------------------------------------------------------------------
# Helpers — env, git, time, hash
# ----------------------------------------------------------------------------


def _resolve_env_template(value: str, *, env: dict[str, str] | None = None) -> str:
    src = env if env is not None else os.environ

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        v = src.get(name)
        if not v:
            raise EndpointMisconfiguredError(
                f"environment variable {name} is not set; required by experiment YAML"
            )
        return v

    return _ENV_TEMPLATE_RE.sub(_sub, value)


def _require_env(name: str, *, env: dict[str, str] | None = None) -> str:
    src = env if env is not None else os.environ
    v = src.get(name)
    if not v:
        raise EndpointMisconfiguredError(
            f"required environment variable {name} is not set"
        )
    return v


def _resolve_git_commit(allow_dirty: bool) -> tuple[str, bool]:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        if not allow_dirty:
            raise RuntimeError(
                "git rev-parse HEAD failed; pass --allow-dirty to embed "
                "git_commit='unknown'."
            ) from None
        logger.warning("GIT_NO_REPO falling_back_to_unknown")
        return ("unknown", True)
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except subprocess.CalledProcessError:
        status = ""
    dirty = bool(status.strip())
    if dirty and not allow_dirty:
        raise RuntimeError("git worktree is dirty; commit or pass --allow-dirty.")
    return (sha, dirty)


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.timezone.utc)


def _iso8601_z(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _json_default(obj: Any) -> Any:
    """JSON serializer for ``date`` / ``datetime`` / ``pathlib.Path``."""
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    if isinstance(obj, pathlib.PurePath):
        return str(obj)
    raise TypeError(
        f"Object of type {type(obj).__name__} is not JSON serializable"
    )


def _zero_usage_dict() -> dict[str, Any]:
    """Foundry-v1-shaped zero usage dict for dry runs."""
    return {
        "input_tokens": 0,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 0,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 0,
    }


def _usage_to_token_usage(usage_dict: dict[str, Any]) -> TokenUsage:
    input_tokens = float(usage_dict.get("input_tokens", 0) or 0)
    output_tokens = float(usage_dict.get("output_tokens", 0) or 0)
    in_details = usage_dict.get("input_tokens_details") or {}
    cached_tokens = float(
        in_details.get("cached_tokens", 0) if isinstance(in_details, dict) else 0
    ) or 0.0
    out_details = usage_dict.get("output_tokens_details") or {}
    reasoning_tokens = float(
        out_details.get("reasoning_tokens", 0)
        if isinstance(out_details, dict)
        else 0
    ) or 0.0
    return TokenUsage(
        input_tokens=input_tokens,
        cached_tokens=cached_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
    )


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return float(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac)


def _load_user_prompts(p: pathlib.Path) -> list[str]:
    if not p.is_file():
        raise CorpusMissingError(f"user_prompts file not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        try:
            arr = json.load(fh)
        except json.JSONDecodeError as exc:
            raise CorpusMissingError(
                f"failed to parse user_prompts {p}: {exc}"
            ) from exc
    if not isinstance(arr, list) or not arr:
        raise CorpusMissingError(
            f"user_prompts {p} must be a non-empty JSON list of strings"
        )
    if not all(isinstance(s, str) and s for s in arr):
        raise CorpusMissingError(
            f"user_prompts {p} entries must all be non-empty strings"
        )
    return arr


def _build_system_prompt(
    corpus_path: pathlib.Path,
    corpus_seed: int,
    target_tokens: int,
) -> str:
    """Deterministic prompt construction matching Task 012's recipe."""
    if not corpus_path.is_file():
        raise CorpusMissingError(f"corpus file not found: {corpus_path}")
    try:
        with corpus_path.open("r", encoding="utf-8") as fh:
            arr = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusMissingError(
            f"failed to parse corpus {corpus_path}: {exc}"
        ) from exc
    if not isinstance(arr, list) or not arr:
        raise CorpusMissingError(
            f"corpus {corpus_path} must be a non-empty JSON list of strings"
        )
    for i, item in enumerate(arr):
        if not isinstance(item, str):
            raise CorpusMissingError(
                f"corpus {corpus_path} item #{i} must be a string"
            )
    rng = random.Random(corpus_seed)
    shuffled = list(arr)
    rng.shuffle(shuffled)
    parts: list[str] = []
    total_chars = 0
    target_chars = target_tokens * DEFAULT_TOKEN_ESTIMATE_DIVISOR
    idx = 0
    while total_chars < target_chars:
        snip = shuffled[idx % len(shuffled)]
        parts.append(snip)
        total_chars += len(snip) + 2
        idx += 1
    return "\n\n".join(parts)


# ----------------------------------------------------------------------------
# Pricing snapshot freshness guard
# ----------------------------------------------------------------------------


def _check_pricing_freshness(
    pricing: PaygPricing, today: datetime.date
) -> None:
    """Reject the run if the pricing snapshot is older than the max age."""
    try:
        accessed = datetime.date.fromisoformat(pricing.accessed_date)
    except ValueError as exc:
        raise PricingStaleError(
            f"pricing snapshot has unparseable accessed_date "
            f"{pricing.accessed_date!r}: {exc}"
        ) from exc
    age = (today - accessed).days
    if age > PRICING_SNAPSHOT_MAX_AGE_DAYS:
        raise PricingStaleError(
            f"pricing snapshot accessed_date={pricing.accessed_date} is "
            f"{age} days old (> {PRICING_SNAPSHOT_MAX_AGE_DAYS}); add a new "
            f"dated snapshot under pricing/ and update the YAML reference"
        )


# ----------------------------------------------------------------------------
# Live client construction (Entra ID, Foundry v1)
# ----------------------------------------------------------------------------


def _make_robust_token_provider(
    underlying: Callable[[], Awaitable[str]],
    *,
    max_retries: int = DEFAULT_TOKEN_MAX_RETRIES,
    base_backoff_seconds: float = DEFAULT_TOKEN_BASE_BACKOFF_SECONDS,
    max_backoff_seconds: float = DEFAULT_TOKEN_MAX_BACKOFF_SECONDS,
    sleeper: Callable[[float], Awaitable[None]] | None = None,
) -> Callable[[], Awaitable[str]]:
    """Mirror of Task 013's robust token provider (transient retry + lock)."""
    _sleeper = sleeper or asyncio.sleep
    retries = int(max_retries)
    base_backoff = float(base_backoff_seconds)
    max_backoff = float(max_backoff_seconds)

    try:
        from azure.identity import (  # noqa: PLC0415
            CredentialUnavailableError as _CredentialUnavailableError,
        )
    except ImportError:  # pragma: no cover
        class _CredentialUnavailableError(Exception):
            pass

    retryable_excs: tuple[type[BaseException], ...] = (
        _CredentialUnavailableError,
        asyncio.TimeoutError,
        TimeoutError,
    )
    lock = asyncio.Lock()

    async def _provider() -> str:
        async with lock:
            attempt = 0
            while True:
                try:
                    token = await underlying()
                except retryable_excs as exc:
                    if attempt >= retries:
                        logger.error(
                            "TOKEN_PROVIDER_EXHAUSTED attempts=%d exc=%s",
                            attempt + 1,
                            type(exc).__name__,
                        )
                        raise
                    backoff = min(max_backoff, base_backoff * (2 ** attempt))
                    logger.warning(
                        "TOKEN_PROVIDER_TRANSIENT_FAILURE attempt=%d/%d "
                        "exc=%s backoff_seconds=%.2f",
                        attempt + 1,
                        retries + 1,
                        type(exc).__name__,
                        backoff,
                    )
                    await _sleeper(backoff)
                    attempt += 1
                    continue
                if attempt > 0:
                    logger.info(
                        "TOKEN_PROVIDER_RECOVERED attempts=%d", attempt + 1
                    )
                return token

    return _provider


def _build_live_client(*, endpoint_value: str) -> Any:
    """Construct one Foundry v1 ``AsyncOpenAI`` client (Entra ID)."""
    from azure.identity.aio import (  # noqa: PLC0415
        DefaultAzureCredential,
        get_bearer_token_provider,
    )
    from openai import AsyncOpenAI  # noqa: PLC0415

    raw_provider = get_bearer_token_provider(
        DefaultAzureCredential(),
        "https://ai.azure.com/.default",
    )
    token_provider = _make_robust_token_provider(raw_provider)
    base_url = endpoint_value.rstrip("/") + "/openai/v1/"
    return AsyncOpenAI(base_url=base_url, api_key=token_provider)


def _parse_response_headers(headers: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "retry_after_ms": None,
        "retry_after_seconds": None,
        "x_ms_deployment_name": None,
    }
    if headers is None:
        return out
    try:
        getter = headers.get
    except AttributeError:
        return out
    ms_raw = getter("retry-after-ms")
    s_raw = getter("retry-after")
    dep_name = getter("x-ms-deployment-name")
    if ms_raw is not None:
        try:
            out["retry_after_ms"] = float(ms_raw)
        except (TypeError, ValueError):
            pass
    if s_raw is not None:
        try:
            out["retry_after_seconds"] = float(s_raw)
        except (TypeError, ValueError):
            pass
    if dep_name is not None:
        out["x_ms_deployment_name"] = str(dep_name)
    return out


# ----------------------------------------------------------------------------
# Per-call execution (single deployment, retry on 429)
# ----------------------------------------------------------------------------


SINGLE_429_MAX_RETRIES = 3
SINGLE_429_BASE_DELAY_S = 1.0


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


async def _call_with_retry(
    *, client: Any, call_kwargs: dict, request_idx: int
) -> dict[str, Any]:
    """One Responses API call with bounded exponential backoff on 429.

    Rate-limit responses are retried (with respect to ``retry-after-ms`` if
    present), not treated as failed measurements. After
    ``SINGLE_429_MAX_RETRIES`` consecutive 429s the call returns with
    ``rate_limited=True`` and ``usage=None`` so the cell can still account
    for the dropped observation.
    """
    started = time.monotonic()
    rate_limited_count = 0
    last_headers = _parse_response_headers(None)
    for attempt in range(SINGLE_429_MAX_RETRIES + 1):
        try:
            response, raw_headers = await _create_with_raw_response(
                client=client, call_kwargs=call_kwargs
            )
        except Exception as exc:
            status = getattr(exc, "status_code", None) or getattr(
                exc, "status", None
            )
            resp_obj = getattr(exc, "response", None)
            raw_headers = getattr(resp_obj, "headers", None) if resp_obj else None
            last_headers = _parse_response_headers(raw_headers)
            if status == 429:
                rate_limited_count += 1
                logger.warning(
                    "RATE_LIMITED request_idx=%d attempt=%d retry_after_ms=%s",
                    request_idx,
                    attempt,
                    last_headers.get("retry_after_ms"),
                )
                if attempt < SINGLE_429_MAX_RETRIES:
                    ms = last_headers.get("retry_after_ms")
                    s = last_headers.get("retry_after_seconds")
                    if ms is not None:
                        delay = ms / 1000.0
                    elif s is not None:
                        delay = s
                    else:
                        delay = SINGLE_429_BASE_DELAY_S * (2 ** attempt)
                    await asyncio.sleep(delay)
                    continue
                # Final retry also 429; record as rate_limited observation.
                elapsed = (time.monotonic() - started) * 1000.0
                return {
                    "usage": None,
                    "first_token_latency_ms": elapsed,
                    "total_latency_ms": elapsed,
                    "rate_limited": True,
                    "rate_limited_count": rate_limited_count,
                    "headers": last_headers,
                    "raised": None,
                }
            # Non-429 exception is fatal for this request.
            elapsed = (time.monotonic() - started) * 1000.0
            return {
                "usage": None,
                "first_token_latency_ms": elapsed,
                "total_latency_ms": elapsed,
                "rate_limited": False,
                "rate_limited_count": rate_limited_count,
                "headers": last_headers,
                "raised": exc,
            }
        elapsed = (time.monotonic() - started) * 1000.0
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
            "first_token_latency_ms": elapsed,
            "total_latency_ms": elapsed,
            "rate_limited": False,
            "rate_limited_count": rate_limited_count,
            "headers": headers_parsed,
            "raised": None,
        }
    # Unreachable: the for-loop always returns.
    raise RuntimeError("unreachable: _call_with_retry exited the loop")


# ----------------------------------------------------------------------------
# Preflight reachability
# ----------------------------------------------------------------------------


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
            f"{type(exc).__name__}; check auth + endpoint + deployment name"
        ) from exc
    usage_obj = getattr(resp, "usage", None)
    out_tok = 0
    if usage_obj is not None:
        out_tok = int(getattr(usage_obj, "output_tokens", 0) or 0)
    if out_tok <= 0:
        raise PreflightReachabilityError(
            f"pre-flight reachability for deployment={deployment} returned "
            f"zero output_tokens; expected > 0"
        )
    logger.info(
        "PREFLIGHT_OK deployment=%s output_tokens=%d", deployment, out_tok
    )
    return {"deployment": deployment, "reachable": True, "output_tokens": out_tok}


# ----------------------------------------------------------------------------
# Per-request record assembly
# ----------------------------------------------------------------------------


def _assemble_record(
    *,
    cfg: ExperimentConfig,
    cell_idx: int,
    arrival_idx_within_cell: int,
    global_request_idx: int,
    cardinality: int,
    bucket_index: int,
    bucket_namespace: str,
    prompt_cache_key_used: str,
    usage_dict: dict[str, Any],
    first_token_latency_ms: float,
    total_latency_ms: float,
    rate_limited: bool,
    rate_limited_count: int,
    headers_parsed: dict[str, Any],
    relative_time_s: float,
    deployment_used: str,
    per_bucket_running_rpm: int,
    common_prefix_running_rpm: int,
    scheduled_dispatch_cell_elapsed_ms: int,
    admitted_dispatch_cell_elapsed_ms: int,
    dispatch_backlog_ms: int,
    in_flight_at_dispatch: int,
    request_estimated_processed_tokens: int,
    failed: bool,
    failure_reason: str | None,
    git_commit: str,
    dirty: bool,
    system_sha: str,
    pricing_snapshot_path: str,
    dry_run: bool,
    run_id_short: str,
) -> dict[str, Any]:
    in_det = usage_dict.get("input_tokens_details") or {}
    cached_tokens_canonical = (
        int(in_det.get("cached_tokens", 0) or 0)
        if isinstance(in_det, dict)
        else 0
    )
    input_tokens_canonical = int(usage_dict.get("input_tokens", 0) or 0)
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
        # Pinned-confounds echo — every record carries the exact values the
        # YAML pinned, so the analysis can re-verify they were held constant.
        "request_max_output_tokens": cfg.request_template.max_output_tokens,
        "request_reasoning_effort": cfg.request_template.reasoning_effort,
        "request_api_version": cfg.client.api_version,
        "request_concurrency": cfg.runtime.concurrency,
        "request_sustain_tps": cfg.runtime.sustain_tps,
        "request_estimated_processed_tokens": request_estimated_processed_tokens,
        "request_estimated_processed_tokens_max":
            cfg.request_template.estimated_processed_tokens_max,
        "dispatcher_kind": cfg.runtime.dispatcher,
        # Bucketing fields.
        "bucket_cardinality": cardinality,
        "bucket_index": bucket_index,
        "bucket_namespace": bucket_namespace,
        "prompt_cache_key_used": prompt_cache_key_used,
        "prompt_cache_retention": cfg.request_template.prompt_cache_retention,
        "per_bucket_running_rpm": per_bucket_running_rpm,
        "common_prefix_running_rpm": common_prefix_running_rpm,
        # v2.3 admitted/scheduled dispatch telemetry.
        "scheduled_dispatch_cell_elapsed_ms": scheduled_dispatch_cell_elapsed_ms,
        "admitted_dispatch_cell_elapsed_ms": admitted_dispatch_cell_elapsed_ms,
        "dispatch_backlog_ms": dispatch_backlog_ms,
        "in_flight_at_dispatch": in_flight_at_dispatch,
        # Cell / arrival indexing.
        "cell_idx": cell_idx,
        "arrival_idx_within_cell": arrival_idx_within_cell,
        "request_idx": global_request_idx,
        "relative_time_s": relative_time_s,
        # Latency + token usage.
        "first_token_latency_ms": first_token_latency_ms,
        "total_latency_ms": total_latency_ms,
        "rate_limited": rate_limited,
        "rate_limited_count": rate_limited_count,
        "usage": usage_dict,
        "canonical_cached_tokens": cached_tokens_canonical,
        "canonical_input_tokens": input_tokens_canonical,
        # Per-record failure marker (set true for token-cap rejections and
        # for non-429 transport exceptions in async_scheduled mode so the
        # analysis can exclude them without abandoning the cell).
        "failed": failed,
        "failure_reason": failure_reason,
        # Response-side headers ONLY (no request-side auth headers).
        "retry_after_ms": headers_parsed.get("retry_after_ms"),
        "retry_after_seconds": headers_parsed.get("retry_after_seconds"),
        "x_ms_deployment_name": headers_parsed.get("x_ms_deployment_name"),
        # Provenance.
        "dry_run": dry_run,
        "cell_metadata": {
            "system_prompt_sha256": system_sha,
            "corpus_seed": cfg.corpus_seed,
            "run_id_short": run_id_short,
        },
        "pricing_snapshot_path": pricing_snapshot_path,
    }


# ----------------------------------------------------------------------------
# Cell summary helpers — steady-state aggregation
# ----------------------------------------------------------------------------


def _is_pre_admission_failure(record: dict[str, Any]) -> bool:
    """True iff ``record`` is a failed record that never crossed admission.

    Pre-admission failures (currently only ``token_cap_exceeded``) are
    rejected before any meaningful HTTP call is issued; they MUST be
    excluded from admitted dispatch backlog, admitted RPM, and in-flight
    aggregates. Post-admission failures (``transport_exception:*``,
    ``rate_limited_after_retries``) passed ``sem.acquire`` and the
    dispatcher actually invoked Azure — they are still
    ``failed=True`` (excluded from cache-hit and model-latency
    aggregates because their usage / latency payloads are absent or
    untrusted) but DO count toward admitted dispatch / RPM / in-flight
    aggregates per the v2.4 admitted-timestamp authoritative rule.

    See ``PRE_ADMISSION_FAILURE_REASONS``.
    """
    if not record.get("failed", False):
        return False
    reason = record.get("failure_reason") or ""
    # Default policy: a ``failed=True`` record with a missing, empty, or
    # unknown ``failure_reason`` is treated as POST-admission (i.e. it
    # contributes to admitted dispatch / RPM / in-flight aggregates).
    # Only reasons explicitly listed in ``PRE_ADMISSION_FAILURE_REASONS``
    # are classified as pre-admission. This default is conservative: it
    # avoids silently understating dispatcher cadence when a new failure
    # reason is introduced upstream before this allowlist is updated.
    return reason in PRE_ADMISSION_FAILURE_REASONS


def _aggregate_cell(
    records: list[dict[str, Any]],
    *,
    warmup_exclusion_s: float,
    sustain_tps: float,
) -> dict[str, Any]:
    """Aggregate per-cell stats; drop the first ``warmup_exclusion_s`` seconds.

    v2.4 admitted-timestamp authoritative rule (see
    ``_is_pre_admission_failure`` and ``PRE_ADMISSION_FAILURE_REASONS``):

    - **Cache-hit ratio + model-latency aggregates** are computed over
      ``non_failed`` records only (any ``failed=True`` record is excluded —
      its token usage or first-token latency payload is absent or
      untrusted).
    - **Admitted dispatch backlog, admitted RPM, and in-flight aggregates**
      are computed over ``admitted`` records — successes PLUS post-admission
      failures (``transport_exception:*``, ``rate_limited_after_retries``)
      because those records passed ``sem.acquire`` and the dispatcher
      actually invoked Azure. Only pre-admission failures (currently
      ``token_cap_exceeded``) are excluded from the admission aggregates;
      they never issued a meaningful HTTP call and so do not belong in
      the admitted-cadence bookkeeping.

    ``failed_count`` continues to count every ``failed=True`` record so
    cell-level operational health remains visible. The new
    ``n_pre_admission_failed_records`` and
    ``n_post_admission_failed_records`` fields surface the split so the
    analysis can audit the partition without re-deriving it.
    """
    if not records:
        return {
            "n_records": 0,
            "n_steady_state_records": 0,
            "n_failed_records": 0,
            "n_pre_admission_failed_records": 0,
            "n_post_admission_failed_records": 0,
            "n_admitted_records": 0,
            "cache_hit_ratio_steady_state": 0.0,
            "first_token_latency_ms_p50_steady_state": 0.0,
            "first_token_latency_ms_p95_steady_state": 0.0,
            "rate_limited_count": 0,
            "per_bucket_rpm_mean_steady_state": 0.0,
            "common_prefix_rpm_mean_steady_state": 0.0,
            "realized_admitted_per_bucket_rpm": 0.0,
            "realized_admitted_common_prefix_rpm": 0.0,
            "p95_dispatch_backlog_ms": 0.0,
            "max_dispatch_backlog_ms": 0.0,
            "max_in_flight_observed": 0,
            "backlog_excessive": False,
            "warmup_exclusion_s": warmup_exclusion_s,
            "scheduled_per_bucket_rpm_expected": 0.0,
            "scheduled_common_prefix_rpm_expected": 0.0,
        }
    cell_t0 = records[0]["relative_time_s"]
    failed_count = sum(1 for r in records if r.get("failed", False))
    pre_admission_failed_count = sum(
        1 for r in records if _is_pre_admission_failure(r)
    )
    post_admission_failed_count = failed_count - pre_admission_failed_count

    # Cache-hit + model-latency aggregates: success records only.
    non_failed = [r for r in records if not r.get("failed", False)]
    non_failed_steady = [
        r for r in non_failed
        if (r["relative_time_s"] - cell_t0) >= warmup_exclusion_s
    ]
    cache_target = non_failed_steady if non_failed_steady else non_failed

    # Admission-level aggregates (dispatch backlog, in-flight, admitted
    # RPM): success records PLUS post-admission failures. v2.4 rule —
    # excluding post-admission failures here would silently underreport
    # the dispatcher's true admitted cadence and hide queue buildup that
    # the v2.3 admitted-timestamp telemetry exists to expose.
    admitted = [r for r in records if not _is_pre_admission_failure(r)]
    admitted_steady = [
        r for r in admitted
        if (r["relative_time_s"] - cell_t0) >= warmup_exclusion_s
    ]
    rpm_target = admitted_steady if admitted_steady else admitted

    in_sum = sum(
        int(r.get("canonical_input_tokens", 0) or 0) for r in cache_target
    )
    cached_sum = sum(
        int(r.get("canonical_cached_tokens", 0) or 0) for r in cache_target
    )
    cache_hit = (cached_sum / in_sum) if in_sum > 0 else 0.0
    latencies = [
        float(r["first_token_latency_ms"])
        for r in cache_target
        if isinstance(r.get("first_token_latency_ms"), (int, float))
        and not r.get("rate_limited", False)
    ]
    per_bucket_rpms = [
        float(r.get("per_bucket_running_rpm", 0) or 0) for r in rpm_target
    ]
    common_rpms = [
        float(r.get("common_prefix_running_rpm", 0) or 0) for r in rpm_target
    ]
    rate_limited_n = sum(1 for r in records if r.get("rate_limited", False))

    # v2.4 admitted-timestamp realized-RPM computation. Use the SAME
    # admitted-elapsed-ms stream that the per-record per_bucket_running_rpm
    # was computed from (RpmTracker counts in the trailing 60s window at
    # each admission), then mean over the admitted target slice (which
    # includes post-admission failures per the v2.4 rule).
    realized_per_bucket_rpm = (
        sum(per_bucket_rpms) / len(per_bucket_rpms)
        if per_bucket_rpms else 0.0
    )
    realized_common_prefix_rpm = (
        sum(common_rpms) / len(common_rpms) if common_rpms else 0.0
    )

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

    # Scheduled RPM expectation under the wall-clock pacer (for sanity
    # comparison). At sustain_tps, common-prefix scheduled RPM = 60 × tps;
    # per-bucket scheduled RPM = (60 × tps) / cardinality. Read cardinality
    # from an admitted record if any (fall back to any record if every
    # record was a pre-admission failure — pathological-edge case).
    cardinality_src = (
        admitted[0] if admitted else (records[0] if records else None)
    )
    cardinality = (
        int(cardinality_src["bucket_cardinality"]) if cardinality_src else 1
    )
    scheduled_common_rpm = 60.0 * float(sustain_tps)
    scheduled_per_bucket_rpm = scheduled_common_rpm / max(1, cardinality)

    return {
        "n_records": len(records),
        "n_steady_state_records": len(non_failed_steady),
        "n_failed_records": failed_count,
        "n_pre_admission_failed_records": pre_admission_failed_count,
        "n_post_admission_failed_records": post_admission_failed_count,
        "n_admitted_records": len(admitted),
        "cache_hit_ratio_steady_state": cache_hit,
        "first_token_latency_ms_p50_steady_state": _percentile(latencies, 50.0),
        "first_token_latency_ms_p95_steady_state": _percentile(latencies, 95.0),
        "rate_limited_count": rate_limited_n,
        "per_bucket_rpm_mean_steady_state": realized_per_bucket_rpm,
        "common_prefix_rpm_mean_steady_state": realized_common_prefix_rpm,
        "realized_admitted_per_bucket_rpm": realized_per_bucket_rpm,
        "realized_admitted_common_prefix_rpm": realized_common_prefix_rpm,
        "p95_dispatch_backlog_ms": p95_backlog,
        "max_dispatch_backlog_ms": max_backlog,
        "max_in_flight_observed": max_in_flight,
        "backlog_excessive": backlog_excessive,
        "warmup_exclusion_s": warmup_exclusion_s,
        "scheduled_per_bucket_rpm_expected": scheduled_per_bucket_rpm,
        "scheduled_common_prefix_rpm_expected": scheduled_common_rpm,
    }


# ----------------------------------------------------------------------------
# Result dataclass
# ----------------------------------------------------------------------------


@dataclass
class MeasurementResult:
    """Top-level result of one Task 018 measurement run."""

    cells_completed: int
    cells_planned: int
    total_usd: float
    jsonl_path: pathlib.Path
    summary_path: pathlib.Path
    partial: bool
    halt_reason: str | None
    cell_summaries: list[dict[str, Any]] = field(default_factory=list)


# ----------------------------------------------------------------------------
# Core async runner
# ----------------------------------------------------------------------------


async def _run_cell(
    *,
    cfg: ExperimentConfig,
    cell_idx: int,
    cardinality: int,
    calls_in_cell: int,
    sustain_tps: float,
    concurrency: int,
    namespace: str,
    client: Any,
    deployment: str,
    system_prompt: str,
    user_prompts: list[str],
    git_commit: str,
    dirty: bool,
    system_sha: str,
    pricing_snapshot_path: str,
    pricing: PaygPricing,
    dry_run: bool,
    out_fh: Any,
    global_request_offset: int,
    sim_started_mono: float,
    run_id_short: str,
) -> tuple[list[dict[str, Any]], float, int]:
    """Run one cardinality cell via the v2.3 async_scheduled dispatcher.

    Pacing: wall-clock pacer schedules arrival ``i`` at ``cell_t0 + i / tps``.
    Concurrency: bounded by ``asyncio.Semaphore(concurrency)``.
    Telemetry: per-record ``scheduled_dispatch_cell_elapsed_ms`` is captured
    pre-acquire (immediately after the pacer sleep returns); per-record
    ``admitted_dispatch_cell_elapsed_ms`` is captured post-acquire, immediately
    before the HTTP send; ``dispatch_backlog_ms`` is the difference;
    ``in_flight_at_dispatch`` is captured post-acquire BEFORE the in-flight
    counter is incremented. The per-bucket and common-prefix running RPMs
    are recomputed AFTER the cell completes, in ADMITTED order, against the
    admitted-elapsed timeline (the authoritative timestamp).

    Per-request token cap: requests whose estimated prompt+output token count
    exceeds ``estimated_processed_tokens_max`` are recorded with
    ``failed=True, failure_reason='token_cap_exceeded'`` and skipped at the
    HTTP layer — no Azure call is issued.

    Returns ``(records, cell_usd, max_in_flight_observed)``.
    """
    sem = asyncio.Semaphore(concurrency)
    cell_start_mono = time.monotonic()
    in_flight = 0
    max_in_flight = 0
    cost_lock = asyncio.Lock()
    nonlocal_state = {"cell_usd": 0.0}

    est_max = cfg.request_template.estimated_processed_tokens_max
    sys_chars = len(system_prompt)

    async def _admit_and_call(
        arrival_idx: int,
    ) -> dict[str, Any]:
        nonlocal in_flight, max_in_flight
        scheduled_relative_s = arrival_idx / sustain_tps if sustain_tps > 0 else 0.0
        # Pre-acquire: wall-clock pacer sleeps until the scheduled instant.
        now_offset = time.monotonic() - cell_start_mono
        delay = scheduled_relative_s - now_offset
        if delay > 0 and not dry_run:
            await asyncio.sleep(delay)
        scheduled_elapsed_ms = int(
            round((time.monotonic() - cell_start_mono) * 1000.0)
        )
        await sem.acquire()
        try:
            admitted_mono = time.monotonic()
            admitted_elapsed_ms = int(
                round((admitted_mono - cell_start_mono) * 1000.0)
            )
            backlog_ms = admitted_elapsed_ms - scheduled_elapsed_ms
            # Snapshot in_flight BEFORE incrementing (spec: "in_flight at
            # the moment of admission, exclusive of the just-admitted call").
            in_flight_snapshot = in_flight
            in_flight += 1
            if in_flight > max_in_flight:
                max_in_flight = in_flight
            try:
                bucket_index = arrival_idx % cardinality
                prompt_cache_key_used = select_bucket(
                    arrival_idx, cardinality, namespace
                )
                user_text = user_prompts[
                    (global_request_offset + arrival_idx) % len(user_prompts)
                ]
                # Per-request token cap enforcement (v2.3). Uses the same
                # 4-chars-per-token heuristic the preflight pricing model
                # uses; a real tokenizer is unnecessary because the
                # estimated_processed_tokens_max field is also a
                # heuristic upper bound.
                est_tokens = (
                    int((sys_chars + len(user_text)) / 4)
                    + cfg.request_template.max_output_tokens
                )
                relative_time_s = admitted_mono - sim_started_mono
                if est_tokens > est_max:
                    logger.warning(
                        "TOKEN_CAP_REJECTED cell_idx=%d arrival_idx=%d "
                        "est_tokens=%d cap=%d",
                        cell_idx, arrival_idx, est_tokens, est_max,
                    )
                    record = _assemble_record(
                        cfg=cfg,
                        cell_idx=cell_idx,
                        arrival_idx_within_cell=arrival_idx,
                        global_request_idx=global_request_offset + arrival_idx,
                        cardinality=cardinality,
                        bucket_index=bucket_index,
                        bucket_namespace=namespace,
                        prompt_cache_key_used=prompt_cache_key_used,
                        usage_dict=_zero_usage_dict(),
                        first_token_latency_ms=0.0,
                        total_latency_ms=0.0,
                        rate_limited=False,
                        rate_limited_count=0,
                        headers_parsed=_parse_response_headers(None),
                        relative_time_s=relative_time_s,
                        deployment_used=deployment,
                        per_bucket_running_rpm=0,
                        common_prefix_running_rpm=0,
                        scheduled_dispatch_cell_elapsed_ms=scheduled_elapsed_ms,
                        admitted_dispatch_cell_elapsed_ms=admitted_elapsed_ms,
                        dispatch_backlog_ms=backlog_ms,
                        in_flight_at_dispatch=in_flight_snapshot,
                        request_estimated_processed_tokens=est_tokens,
                        failed=True,
                        failure_reason="token_cap_exceeded",
                        git_commit=git_commit,
                        dirty=dirty,
                        system_sha=system_sha,
                        pricing_snapshot_path=pricing_snapshot_path,
                        dry_run=dry_run,
                        run_id_short=run_id_short,
                    )
                    return record

                call_kwargs: dict[str, Any] = {
                    "model": deployment,
                    "input": system_prompt + "\n\n" + user_text,
                    "reasoning": {
                        "effort": cfg.request_template.reasoning_effort
                    },
                    "max_output_tokens": cfg.request_template.max_output_tokens,
                    "prompt_cache_key": prompt_cache_key_used,
                    "prompt_cache_retention":
                        cfg.request_template.prompt_cache_retention,
                }
                if dry_run:
                    usage_dict = _zero_usage_dict()
                    first_token_latency_ms = 0.0
                    total_latency_ms = 0.0
                    rate_limited = False
                    rate_limited_count = 0
                    headers_parsed = _parse_response_headers(None)
                    per_call_usd = 0.0
                    failed = False
                    failure_reason: str | None = None
                else:
                    res = await _call_with_retry(
                        client=client,
                        call_kwargs=call_kwargs,
                        request_idx=arrival_idx,
                    )
                    if res["raised"] is not None:
                        logger.warning(
                            "REQUEST_FAILED cell_idx=%d arrival=%d "
                            "exc_type=%s; recording as failed and continuing",
                            cell_idx,
                            arrival_idx,
                            type(res["raised"]).__name__,
                        )
                        usage_dict = _zero_usage_dict()
                        first_token_latency_ms = res["first_token_latency_ms"]
                        total_latency_ms = res["total_latency_ms"]
                        rate_limited = False
                        rate_limited_count = res["rate_limited_count"]
                        headers_parsed = res["headers"]
                        per_call_usd = 0.0
                        failed = True
                        failure_reason = (
                            f"transport_exception:{type(res['raised']).__name__}"
                        )
                    else:
                        usage_dict = res["usage"] or _zero_usage_dict()
                        first_token_latency_ms = res["first_token_latency_ms"]
                        total_latency_ms = res["total_latency_ms"]
                        rate_limited = res["rate_limited"]
                        rate_limited_count = res["rate_limited_count"]
                        headers_parsed = res["headers"]
                        if not rate_limited:
                            tu = _usage_to_token_usage(usage_dict)
                            per_call_usd = payg_cost_per_call(
                                tu, pricing, model=cfg.deployment.family
                            ).usd_per_request
                            failed = False
                            failure_reason = None
                        else:
                            per_call_usd = 0.0
                            failed = True
                            failure_reason = "rate_limited_after_retries"

                async with cost_lock:
                    nonlocal_state["cell_usd"] += per_call_usd

                record = _assemble_record(
                    cfg=cfg,
                    cell_idx=cell_idx,
                    arrival_idx_within_cell=arrival_idx,
                    global_request_idx=global_request_offset + arrival_idx,
                    cardinality=cardinality,
                    bucket_index=bucket_index,
                    bucket_namespace=namespace,
                    prompt_cache_key_used=prompt_cache_key_used,
                    usage_dict=usage_dict,
                    first_token_latency_ms=first_token_latency_ms,
                    total_latency_ms=total_latency_ms,
                    rate_limited=rate_limited,
                    rate_limited_count=rate_limited_count,
                    headers_parsed=headers_parsed,
                    relative_time_s=relative_time_s,
                    deployment_used=deployment,
                    # per_bucket/common_prefix running RPM are placeholders
                    # here; recomputed post-cell in admitted order below.
                    per_bucket_running_rpm=0,
                    common_prefix_running_rpm=0,
                    scheduled_dispatch_cell_elapsed_ms=scheduled_elapsed_ms,
                    admitted_dispatch_cell_elapsed_ms=admitted_elapsed_ms,
                    dispatch_backlog_ms=backlog_ms,
                    in_flight_at_dispatch=in_flight_snapshot,
                    request_estimated_processed_tokens=est_tokens,
                    failed=failed,
                    failure_reason=failure_reason,
                    git_commit=git_commit,
                    dirty=dirty,
                    system_sha=system_sha,
                    pricing_snapshot_path=pricing_snapshot_path,
                    dry_run=dry_run,
                    run_id_short=run_id_short,
                )
                return record
            finally:
                in_flight -= 1
        finally:
            sem.release()

    tasks = [
        asyncio.create_task(_admit_and_call(i))
        for i in range(calls_in_cell)
    ]
    cell_records: list[dict[str, Any]] = await asyncio.gather(*tasks)

    # v2.4: Per-bucket and common-prefix RPMs are recomputed in ADMITTED
    # order, against the admitted-elapsed-ms timeline. The admitted
    # timestamp is the authoritative RPM input — the scheduled timestamp
    # only tells us when the pacer woke up the task. Post-admission
    # failures (transport_exception:*, rate_limited_after_retries) DO
    # contribute to admitted RPM: they passed sem.acquire and actually
    # invoked Azure, so they consumed the same dispatcher capacity that
    # successful calls do. Only pre-admission failures (token_cap_exceeded)
    # are skipped — those records never issued a meaningful HTTP call.
    admitted_sorted = sorted(
        cell_records,
        key=lambda r: r.get("admitted_dispatch_cell_elapsed_ms", 0),
    )
    bucket_rpm: dict[int, RpmTracker] = {
        i: RpmTracker() for i in range(cardinality)
    }
    common_rpm = RpmTracker()
    for rec in admitted_sorted:
        if _is_pre_admission_failure(rec):
            # Pre-admission failures (currently only token_cap_exceeded)
            # never crossed the HTTP admission boundary and so do not
            # count toward the cache-key admitted arrival rate.
            continue
        admitted_s = rec["admitted_dispatch_cell_elapsed_ms"] / 1000.0
        bucket_idx = rec["bucket_index"]
        # Use cell_start-relative seconds as a monotonic-equivalent clock
        # for the RpmTracker.
        common_rpm.record(admitted_s)
        bucket_rpm[bucket_idx].record(admitted_s)
        rec["per_bucket_running_rpm"] = bucket_rpm[bucket_idx].count(
            admitted_s
        )
        rec["common_prefix_running_rpm"] = common_rpm.count(admitted_s)

    # Write JSONL in admitted order (matches the RPM rebuild ordering).
    for rec in admitted_sorted:
        out_fh.write(
            json.dumps(rec, sort_keys=True, default=_json_default) + "\n"
        )
    out_fh.flush()

    return admitted_sorted, nonlocal_state["cell_usd"], max_in_flight


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
) -> MeasurementResult:
    cardinalities = (
        list(SMOKE_CARDINALITIES) if stage == "smoke"
        else list(cfg.sweep.bucket_cardinality)
    )
    if stage == "smoke":
        calls_per_cell = cfg.runtime.smoke_calls_per_cell
        hard_ceiling = cfg.budget.smoke_hard_ceiling_usd
    else:
        sustain = cfg.runtime.sustain_tps
        calls_per_cell = int(cfg.runtime.cell_duration_seconds * sustain)
        hard_ceiling = cfg.budget.evidence_hard_ceiling_usd
    sustain_tps = cfg.runtime.sustain_tps

    # ---- Pricing freshness gate ----
    _check_pricing_freshness(pricing, today)

    # ---- v2.3 TPM feasibility preflight gate ----
    # Computed BEFORE the live client is constructed so a misconfigured
    # YAML never opens an HTTP client. ``60 × tps × est_tokens_max`` is the
    # worst-case projected TPM (every request fully utilizes its token cap);
    # the headroom fraction (0.70) preserves margin for cached-input bursts
    # and Azure-side metering jitter.
    projected_tpm = compute_projected_tpm(
        sustain_tps=sustain_tps,
        estimated_processed_tokens_max=cfg.request_template.estimated_processed_tokens_max,
    )
    tpm_quota_ceiling = TPM_HEADROOM_FRACTION * cfg.deployment_tpm_quota
    logger.info(
        "TPM_FEASIBILITY_PREFLIGHT projected_tpm=%.1f quota=%d "
        "headroom_fraction=%.2f ceiling=%.1f",
        projected_tpm, cfg.deployment_tpm_quota,
        TPM_HEADROOM_FRACTION, tpm_quota_ceiling,
    )
    if projected_tpm > tpm_quota_ceiling:
        raise TpmFeasibilityAbortError(
            f"projected TPM {projected_tpm:.0f} > "
            f"{TPM_HEADROOM_FRACTION:.2f} × deployment_tpm_quota="
            f"{cfg.deployment_tpm_quota} (= {tpm_quota_ceiling:.0f}); "
            f"reduce runtime.sustain_tps OR reduce "
            f"request_template.estimated_processed_tokens_max OR raise "
            f"metadata.deployment_tpm_quota (only if the deployment was "
            f"actually re-provisioned with higher TPM). NEVER bypass this "
            f"gate by reducing concurrency — concurrency is orthogonal to "
            f"TPM admission control."
        )

    # ---- Preflight budget gate ----
    projected_usd = compute_projected_usd(
        cardinalities=cardinalities,
        calls_per_cell=calls_per_cell,
        pricing=pricing,
        model=cfg.deployment.family,
        input_tokens=float(cfg.target_system_prompt_tokens),
        output_tokens=float(EVIDENCE_PROJECTED_OUTPUT_TOKENS),
        cached_fraction=EVIDENCE_CACHED_FRACTION,
    )
    preflight_threshold = 0.9 * hard_ceiling
    midrun_threshold = 0.85 * hard_ceiling
    logger.info(
        "BUDGET_PREFLIGHT stage=%s cells=%d calls_per_cell=%d "
        "projected_usd=%.4f hard_ceiling=%.4f preflight_threshold=%.4f "
        "midrun_threshold=%.4f",
        stage, len(cardinalities), calls_per_cell,
        projected_usd, hard_ceiling, preflight_threshold, midrun_threshold,
    )
    if projected_usd > preflight_threshold:
        raise PreflightBudgetAbortError(
            f"projected_usd ${projected_usd:.4f} > 0.9 × hard_ceiling "
            f"${preflight_threshold:.4f} (stage={stage}, cells="
            f"{len(cardinalities)}, calls_per_cell={calls_per_cell}). "
            f"Narrow the sweep or raise the hard ceiling in the YAML "
            f"(but NEVER lower sustain_tps, concurrency, max_output_tokens, "
            f"reasoning.effort, or api_version — those are pinned controls)."
        )

    system_sha = _sha256_text(system_prompt)
    # When ``dry_run`` is True, label the file with ``dry-run`` (not the
    # ``inner_stage`` "evidence") so reviewers can tell at a glance which
    # artifacts came from network-free dry-runs vs live runs.
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
    if not dry_run:
        client = _build_live_client(endpoint_value=endpoint_value)
        await _preflight_reachability(client=client, deployment=deployment)

    sim_started_mono = time.monotonic()
    total_usd = 0.0
    cell_summaries: list[dict[str, Any]] = []
    halt_reason: str | None = None
    all_records: list[dict[str, Any]] = []
    global_request_offset = 0
    retention_tag = (
        "inmemory" if cfg.request_template.prompt_cache_retention == "in_memory"
        else "24h"
    )

    with jsonl_path.open("w", encoding="utf-8") as out_fh:
        for cell_idx, cardinality in enumerate(cardinalities):
            # Inter-cell washout (in_memory YAML only). Spec permits 60-120s;
            # we use the YAML's washout_seconds verbatim, skipping on the
            # first cell.
            if (
                cell_idx > 0
                and cfg.runtime.washout_seconds > 0
                and not dry_run
            ):
                logger.info(
                    "WASHOUT cell_idx=%d sleep_seconds=%d",
                    cell_idx, cfg.runtime.washout_seconds,
                )
                await asyncio.sleep(cfg.runtime.washout_seconds)

            namespace = build_namespace(retention_tag, cardinality, run_id_short)
            logger.info(
                "CELL_BEGIN cell_idx=%d cardinality=%d calls_in_cell=%d "
                "namespace=%s",
                cell_idx, cardinality, calls_per_cell, namespace,
            )
            cell_records, cell_usd, cell_max_in_flight = await _run_cell(
                cfg=cfg,
                cell_idx=cell_idx,
                cardinality=cardinality,
                calls_in_cell=calls_per_cell,
                sustain_tps=sustain_tps,
                concurrency=cfg.runtime.concurrency,
                namespace=namespace,
                client=client,
                deployment=deployment,
                system_prompt=system_prompt,
                user_prompts=user_prompts,
                git_commit=git_commit,
                dirty=dirty,
                system_sha=system_sha,
                pricing_snapshot_path=pricing_snapshot_path,
                pricing=pricing,
                dry_run=dry_run,
                out_fh=out_fh,
                global_request_offset=global_request_offset,
                sim_started_mono=sim_started_mono,
                run_id_short=run_id_short,
            )
            global_request_offset += calls_per_cell
            total_usd += cell_usd
            all_records.extend(cell_records)
            warmup_excl = (
                WARMUP_EXCLUSION_SECONDS if stage == "evidence" else 0.0
            )
            agg = _aggregate_cell(
                cell_records,
                warmup_exclusion_s=warmup_excl,
                sustain_tps=sustain_tps,
            )
            # _aggregate_cell synthesizes max_in_flight_observed from
            # per-record in_flight_at_dispatch (BEFORE-increment snapshot).
            # Override with the dispatcher-side tracker which records the
            # post-increment peak; this is the authoritative concurrency
            # observation for the cell.
            agg["max_in_flight_observed"] = cell_max_in_flight
            cell_summary = {
                "cell_idx": cell_idx,
                "cardinality": cardinality,
                "namespace": namespace,
                "calls_in_cell": calls_per_cell,
                "cell_usd": round(cell_usd, 6),
                **agg,
            }
            cell_summaries.append(cell_summary)
            logger.info(
                "CELL_END cell_idx=%d cardinality=%d records=%d "
                "cell_usd=%.4f cumulative_usd=%.4f "
                "cache_hit_steady=%.4f ttft_p95_steady=%.1f",
                cell_idx, cardinality, len(cell_records), cell_usd, total_usd,
                agg["cache_hit_ratio_steady_state"],
                agg["first_token_latency_ms_p95_steady_state"],
            )
            # ---- Mid-run budget gate (per Task 018 v2.3) ----
            if total_usd > midrun_threshold:
                halt_reason = "midrun_budget_gate"
                logger.warning(
                    "MIDRUN_BUDGET_HALT cumulative_usd=%.4f > "
                    "0.85 × hard_ceiling=%.4f; halting cleanly after "
                    "cell_idx=%d (next cell NOT started)",
                    total_usd, midrun_threshold, cell_idx,
                )
                break

    partial = halt_reason is not None or len(cell_summaries) < len(cardinalities)

    # ---- Summary JSON ----
    citations = CitationsBuilder(
        pricing_path=cfg.pricing_snapshot_path,
        pricing_source_url=pricing.source_url,
        pricing_accessed_date=pricing.accessed_date,
    ).to_dict()
    pinned_confounds_echo = {
        "max_output_tokens": cfg.request_template.max_output_tokens,
        "reasoning_effort": cfg.request_template.reasoning_effort,
        "api_version": cfg.client.api_version,
        "concurrency": cfg.runtime.concurrency,
        "sustain_tps": cfg.runtime.sustain_tps,
        "dispatcher": cfg.runtime.dispatcher,
        "estimated_processed_tokens_max":
            cfg.request_template.estimated_processed_tokens_max,
        "deployment_tpm_quota": cfg.deployment_tpm_quota,
        "prompt_cache_retention": cfg.request_template.prompt_cache_retention,
    }
    # v2.3 cell-level cross-cuts surfaced at the top of the summary so
    # reviewers and Stage 1 / Stage 2 gates can read them without
    # spelunking into cell_summaries[]. v2.4 adds the run-level
    # max_in_flight_observed rollup so semaphore-saturation regressions
    # of the v2.3 sem=8 kind are visible without inspecting per-cell rows.
    backlog_excessive_any = any(
        bool(c.get("backlog_excessive", False)) for c in cell_summaries
    )
    max_in_flight_observed_run = max(
        (int(c.get("max_in_flight_observed", 0) or 0) for c in cell_summaries),
        default=0,
    )
    tpm_block = {
        "projected_tpm": round(projected_tpm, 2),
        "deployment_tpm_quota": cfg.deployment_tpm_quota,
        "headroom_fraction": TPM_HEADROOM_FRACTION,
        "ceiling": round(tpm_quota_ceiling, 2),
        "passed": True,  # by construction; we'd have raised otherwise
    }
    # Hoist card=1 cell stats as first-class smoke-summary fields so the
    # Stage 1 gate (admitted per-bucket RPM >= 15, backlog_excessive=false)
    # is a one-liner against the summary.
    smoke_card1: dict[str, Any] = {}
    if stage == "smoke" and cell_summaries:
        card1_summary = next(
            (c for c in cell_summaries if c.get("cardinality") == 1),
            None,
        )
        if card1_summary is not None:
            smoke_card1 = {
                "realized_admitted_per_bucket_rpm_card1": card1_summary.get(
                    "realized_admitted_per_bucket_rpm", 0.0
                ),
                "realized_admitted_common_prefix_rpm_card1": card1_summary.get(
                    "realized_admitted_common_prefix_rpm", 0.0
                ),
                "max_in_flight_observed_card1": card1_summary.get(
                    "max_in_flight_observed", 0
                ),
                "p95_dispatch_backlog_ms_card1": card1_summary.get(
                    "p95_dispatch_backlog_ms", 0.0
                ),
                "max_dispatch_backlog_ms_card1": card1_summary.get(
                    "max_dispatch_backlog_ms", 0.0
                ),
                "backlog_excessive_card1": bool(
                    card1_summary.get("backlog_excessive", False)
                ),
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
        "deployment_used": deployment,
        "deployment_env": cfg.deployment.deployment_env,
        "endpoint_env": cfg.deployment.endpoint_env,
        "api_version": cfg.client.api_version,
        "model": cfg.deployment.family,
        "pricing_snapshot_path": cfg.pricing_snapshot_path,
        "pricing_source_url": pricing.source_url,
        "pricing_accessed_date": pricing.accessed_date,
        "projected_usd": round(projected_usd, 6),
        "hard_ceiling_usd": hard_ceiling,
        "preflight_threshold_usd": round(preflight_threshold, 6),
        "midrun_threshold_usd": round(midrun_threshold, 6),
        "total_usd": round(total_usd, 6),
        "cells_planned": len(cardinalities),
        "cells_completed": len(cell_summaries),
        "calls_per_cell": calls_per_cell,
        "cardinalities_planned": cardinalities,
        "run_id_short": run_id_short,
        "pinned_confounds_echo": pinned_confounds_echo,
        "metadata": dict(cfg.metadata),
        "tpm_feasibility": tpm_block,
        "backlog_excessive_any": backlog_excessive_any,
        "max_in_flight_observed_run": max_in_flight_observed_run,
        "citations": citations,
        "cell_summaries": cell_summaries,
        "jsonl_path": str(jsonl_path),
        **smoke_card1,
    }
    # Partial-run summary uses a distinct filename so reviewers can grep for
    # it. The canonical summary file is written either way (atomic source of
    # truth); the *.partial.summary.json is a convenience alias.
    with summary_path.open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True, default=_json_default)
    if partial:
        partial_path = pathlib.Path(
            str(jsonl_path).replace(".jsonl", ".partial.summary.json")
        )
        with partial_path.open("w", encoding="utf-8") as fh:
            json.dump(
                summary, fh, indent=2, sort_keys=True, default=_json_default
            )
        logger.info("PARTIAL_SUMMARY written=%s", partial_path)

    return MeasurementResult(
        cells_completed=len(cell_summaries),
        cells_planned=len(cardinalities),
        total_usd=total_usd,
        jsonl_path=jsonl_path,
        summary_path=summary_path,
        partial=partial,
        halt_reason=halt_reason,
        cell_summaries=cell_summaries,
    )


# ----------------------------------------------------------------------------
# Synchronous wrapper
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
    run_id_short_override: str | None = None,
    timestamp_label_override: str | None = None,
) -> MeasurementResult:
    """Synchronous wrapper around the async runner."""
    src_env = env if env is not None else dict(os.environ)

    endpoint_value = _require_env(
        cfg.deployment.endpoint_env, env=src_env
    )
    deployment = _resolve_env_template(
        cfg.deployment.deployment_template, env=src_env
    )
    if not deployment:
        raise EndpointMisconfiguredError(
            f"deployment env-var {cfg.deployment.deployment_env} resolved empty"
        )

    pricing_path = pathlib.Path(cfg.pricing_snapshot_path)
    if not pricing_path.is_file():
        raise PricingStaleError(
            f"pricing snapshot file does not exist: {pricing_path}"
        )
    pricing = load_payg_pricing(pricing_path)

    git_commit, dirty = _resolve_git_commit(allow_dirty=allow_dirty)

    benchmark_dir = benchmarks_root / cfg.benchmark
    if not benchmark_dir.is_dir():
        raise CorpusMissingError(
            f"benchmark directory missing: {benchmark_dir}"
        )
    corpus_p = pathlib.Path(cfg.system_prompt_corpus_path)
    if not corpus_p.is_absolute():
        corpus_p = pathlib.Path.cwd() / corpus_p
    user_prompts_p = pathlib.Path(cfg.user_prompts_path)
    if not user_prompts_p.is_absolute():
        user_prompts_p = pathlib.Path.cwd() / user_prompts_p

    system_prompt = _build_system_prompt(
        corpus_p,
        corpus_seed=cfg.corpus_seed,
        target_tokens=cfg.target_system_prompt_tokens,
    )
    user_prompts = _load_user_prompts(user_prompts_p)

    runs_dir = benchmark_dir / "runs"
    timestamp_label = (
        timestamp_label_override
        if timestamp_label_override is not None
        else _utc_now().strftime("%Y%m%dT%H%M%SZ")
    )
    run_id_short = (
        run_id_short_override
        if run_id_short_override is not None
        else uuid.uuid4().hex[:8]
    )
    today_date = today if today is not None else _utc_now().date()

    return asyncio.run(
        _run_measurement_async(
            cfg=cfg,
            runs_dir=runs_dir,
            system_prompt=system_prompt,
            user_prompts=user_prompts,
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
        )
    )


# ----------------------------------------------------------------------------
# CLI plumbing
# ----------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scripts.measure_cache_key_bucketing",
        description=(
            "Task 018 v2.4 prompt_cache_key bucketing benchmark. Sweeps "
            "cardinality of cache-key buckets against the docs-stated "
            "~15 req/min overflow threshold on a single unthrottled "
            "gpt-5.2 PAYG deployment via the async_scheduled dispatcher "
            "(concurrency=96, sustain_tps=0.5, "
            "estimated_processed_tokens_max=11000) with TPM feasibility "
            "preflight + admitted-timestamp authoritative RPM. v2.4 raises "
            "the v2.3 sem=8 pin to 96 to absorb live gpt-5.2 P95 TTFT "
            "≈ 128 s without saturating the dispatcher semaphore."
        ),
    )
    p.add_argument("--experiment", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--smoke", action="store_true")
    p.add_argument(
        "--stage",
        choices=("dry-run", "smoke", "evidence"),
        default=None,
        help=(
            "Explicit stage selector. --dry-run is equivalent to "
            "--stage dry-run; --smoke is equivalent to --stage smoke. "
            "Default (no flag) is --stage evidence."
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
    # Default: full evidence stage.
    return ("evidence", False)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.log_level)
    stage, dry_run = _resolve_stage(args)
    # ``stage="dry-run"`` is informational; the underlying runner accepts
    # ``evidence`` or ``smoke`` for cell counts. Dry-run inherits whichever
    # the YAML defaults the operator wants exercised — we default to
    # ``evidence`` (full sweep) so dry-run validates the largest schema.
    inner_stage = "evidence" if stage == "dry-run" else stage

    try:
        cfg = load_experiment(args.experiment)
    except FileNotFoundError as exc:
        logger.error("EXPERIMENT_YAML_MISSING %s", exc)
        return EXIT_DATASET
    except ValueError as exc:
        logger.error("EXPERIMENT_YAML_INVALID %s", exc)
        return EXIT_CONFIG

    try:
        result = run_measurement(
            cfg=cfg,
            benchmarks_root=pathlib.Path(args.benchmarks_root),
            dry_run=dry_run,
            stage=inner_stage,
            allow_dirty=args.allow_dirty,
        )
    except PricingStaleError as exc:
        logger.error("PRICING_STALE %s", exc)
        return EXIT_PRICING
    except TpmFeasibilityAbortError as exc:
        logger.error("TPM_FEASIBILITY_ABORT %s", exc)
        return EXIT_RUNTIME
    except TokenCapAbortError as exc:
        logger.error("TOKEN_CAP_ABORT %s", exc)
        return EXIT_RUNTIME
    except PreflightBudgetAbortError as exc:
        logger.error("PREFLIGHT_BUDGET_ABORT %s", exc)
        return EXIT_RUNTIME
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
        f"\n=== measure_cache_key_bucketing summary ===\n"
        f"experiment_id        : {cfg.experiment_id}\n"
        f"stage                : {stage}{halt_note}\n"
        f"dry_run              : {dry_run}\n"
        f"cells_completed      : {result.cells_completed}/{result.cells_planned}\n"
        f"total_usd            : ${result.total_usd:.4f}\n"
        f"partial              : {result.partial}\n"
        f"jsonl                : {result.jsonl_path}\n"
        f"summary_json         : {result.summary_path}\n"
        f"==========================================="
    )
    print(summary_line)
    # Partial run is a legitimate outcome (exit 0).
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
