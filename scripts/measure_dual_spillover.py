"""scripts/measure_dual_spillover.py — Phase 2 dual-endpoint spillover measurement.

This module implements the Phase 2 (two-deployment, real-429) spillover
measurement called for by Task 013. It is a sibling to
``scripts/simulate_spillover.py`` (Task 012 Phase 1) and never modifies
it. The pure-function policy primitives (``reactive_decide``,
``proactive_decide``, the dataclasses, ``_percentile``,
``_build_arrival_schedule``, ``build_system_prompt``, ``sha256_text``)
are imported from Task 012 unchanged so per-policy behavior is
byte-identical to Phase 1. The differences are at the orchestration
layer:

* Two ``AsyncOpenAI`` clients (one per deployment). Same endpoint base,
  same Entra ID auth, distinguished by the ``model`` field at request
  time. Azure caches per deployment name, so different ``model`` →
  different cache pool. This is the property Phase 1 could not model.
* Throttle state is NEVER simulated. Real 429s from the primary
  deployment are the trigger this measurement exists to observe.
* Real 429s on the primary deployment are **not silently retried**.
  Reactive routes the failing request and the next
  ``stay_on_spillover_min_requests`` to spillover; proactive routes the
  failing request and the next ``real_429_followup_requests`` to
  spillover. The 429 is counted as its own observation either way.
* Real 429s on the spillover deployment are anomalies. They are
  retried once with exponential backoff. Aggregate rate > 1% halts
  the run with non-zero exit (signal that spillover TPM is
  misconfigured).
* Per-request record additionally captures: ``deployment_used`` (the
  actual ``model`` field), ``cache_pool`` (= ``deployment_used``),
  ``real_429_observed``, ``primary_429_count_running_total``,
  ``primary_health_check_state`` (reactive only), ``prompt_cache_key``
  / ``prompt_cache_retention`` (passed values, if any),
  ``retry_after_ms`` / ``retry_after_seconds`` (from 429 headers), and
  three ``x-ms-*`` headers that reveal whether the request passed
  through any Azure-side native spillover (``x_ms_spillover_from_deployment``,
  ``x_ms_deployment_name``, ``x_ms_spillover_error``).
* The Phase 1 field ``simulated_primary_throttle_state`` is omitted
  in Phase 2 records — it would be misleading next to real
  measurements.

CLI contract::

    python -m scripts.measure_dual_spillover \\
        --experiment experiments/exp005_dual_spillover_reactive.yaml \\
        [--dry-run] [--smoke]

Exit codes:
    0 = success
    1 = budget violation OR spillover real-429 rate > 1% halt
    2 = auth / endpoint misconfiguration OR pre-flight reachability failure
    3 = dataset / corpus / prompt files missing
    4 = experiment YAML invalid
    5 = live --smoke success criteria not met (primary_real_429_count < 1
        OR spillover_real_429_count != 0)
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import csv
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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import yaml

from scripts._azure_pricing import (
    CANONICAL_PAYG_SNAPSHOT_PATH,
    LIVE_MEASUREMENT,
    PRICING_POLICY_MODES,
    PricingPolicyError,
    verify_campaign_pricing,
)
from scripts._pricing_types import PaygPricing, TokenUsage
from scripts.cost_calculator import (
    payg_cost_per_call,
)
from scripts.simulate_spillover import (
    ProactiveObservation,
    ProactivePolicyParams,
    ProactiveState,
    ReactiveObservation,
    ReactivePolicyParams,
    ReactiveState,
    _build_arrival_schedule,
    _percentile,
    build_system_prompt,
    proactive_decide,
    reactive_decide,
    sha256_text,
)

__all__ = [
    "BudgetHaltError",
    "CorpusMissingError",
    "DualExperimentConfig",
    "DualPrimary429HaltError",
    "EndpointMisconfiguredError",
    "MeasurementResult",
    "PhasePolicyParams",
    "PreflightReachabilityError",
    "SmokeCriteriaError",
    "SpilloverHalt429Error",
    "_append_preflight_log_record",
    "_make_robust_token_provider",
    "load_experiment",
    "main",
    "preflight_reachability",
    "run_measurement",
]

logger = logging.getLogger("scripts.measure_dual_spillover")

# Foundry v1 API version literal (recorded verbatim in JSONL).
FOUNDRY_API_VERSION = "preview"

# Token-count heuristic divisor (chars / 4 ≈ tokens).
DEFAULT_TOKEN_ESTIMATE_DIVISOR = 4

# Long-run auth hardening (Task 015 Phase 2 blocker hotfix). The
# OpenAI SDK awaits our token provider before EVERY Responses API
# call (1000s of times per long run), and the underlying
# `azure.identity.aio` provider can spawn `az account
# get-access-token` whenever its internal cache misses or nears
# expiry. On a busy host the subprocess can transiently exceed the
# SDK default 10s process timeout and raise
# `CredentialUnavailableError("Timed out waiting for Azure CLI")` —
# observed in a Phase 2 reactive live run that died at
# request_idx≈1006 after ~2h of wall clock. These constants drive a
# minimal robust wrapper that retries transient timeouts with
# bounded exponential backoff before re-raising and serialises
# concurrent refreshes with an `asyncio.Lock` to prevent stampedes.
# The wrapper intentionally does NOT cache bearer strings on its own
# (a fixed-window outer cache would risk re-caching an aged token
# that `azure.identity` returned from its internal cache while still
# valid, then extending the wrapper's perceived freshness past the
# real Azure-side expiry → 401). Token reuse + refresh-near-expiry
# is delegated entirely to `azure.identity.aio`'s own internal
# credential cache, which knows the real expiry.
DEFAULT_TOKEN_MAX_RETRIES = 5
DEFAULT_TOKEN_BASE_BACKOFF_SECONDS = 1.0
DEFAULT_TOKEN_MAX_BACKOFF_SECONDS = 30.0

# Halt threshold for real (Azure-emitted) 429 responses on the
# SPILLOVER deployment as a fraction of completed spillover requests.
# Exceeding this is a signal that spillover TPM is misconfigured.
SPILLOVER_REAL_429_HALT_FRACTION = 0.01

# Minimum number of spillover requests before the spillover 429
# guard fires. Prevents a single early flake from halting the run.
SPILLOVER_REAL_429_MIN_REQUESTS = 20

# Smoke-mode overrides for total + sustain duration.
SMOKE_DURATION_SECONDS = 180
SMOKE_SUSTAIN_DURATION_SECONDS = 60

# Rolling-window size (seconds) for the cache-hit-ratio chart.
CACHE_ROLLING_WINDOW_S = 60.0

# Retry/backoff for spillover-side 429s. Primary 429s are NEVER
# silently retried — they are the measurement signal.
SPILLOVER_429_MAX_RETRIES = 1
SPILLOVER_429_BASE_DELAY_S = 1.0

EXIT_OK = 0
EXIT_BUDGET = 1
EXIT_AUTH = 2
EXIT_DATASET = 3
EXIT_CONFIG = 4
EXIT_PREFLIGHT = 2
EXIT_SMOKE = 5
EXIT_PRICING = 6


# ----------------------------------------------------------------------------
# Typed errors
# ----------------------------------------------------------------------------


class BudgetHaltError(RuntimeError):
    """Running USD total crossed ``budget.hard_ceiling_usd``."""


class SpilloverHalt429Error(RuntimeError):
    """Spillover-side real 429 rate exceeded the 1% halt threshold."""


class DualPrimary429HaltError(RuntimeError):
    """Catastrophic primary-side failure (kept for symmetry).

    Primary 429s are NOT an error condition by design — they are the
    signal this benchmark measures. This exception is reserved for a
    primary-side condition that prevents measurement entirely (e.g.,
    100% of primary requests fail in a way the runner cannot classify
    as 429).
    """


class EndpointMisconfiguredError(RuntimeError):
    """Required Azure env vars are missing or empty."""


class CorpusMissingError(FileNotFoundError):
    """Corpus or user-prompts file cannot be found / parsed."""


class PreflightReachabilityError(RuntimeError):
    """A pre-flight reachability check failed for one of the two deployments."""


class SmokeCriteriaError(RuntimeError):
    """Live smoke run did not meet its success criteria.

    A smoke run (``--smoke`` + live calls) MUST observe:

    * ``primary_real_429_count >= 1`` — the throttled primary deployment
      must actually throttle. If it doesn't, either the primary TPM is
      misconfigured (too high) or the workload shape is too light to
      exercise the rebuild-cost mechanism. Either way, downstream
      Phase 2 conclusions would be invalid.
    * ``spillover_real_429_count == 0`` — the spillover deployment must
      NOT throttle at all during the smoke window. If it does, the
      spillover TPM is misconfigured (too low) and the experiment has
      no clean spillover pool. The runtime ``> 1%`` halt guard is for
      full runs; the smoke gate is strictly zero so the misconfiguration
      is caught before a full run is launched.

    Raised post-run, before ``MeasurementResult`` is returned. The CLI
    maps this to a non-zero exit code (``EXIT_SMOKE``) with a clear
    diagnostic message naming which side of the criterion failed.
    """


# ----------------------------------------------------------------------------
# Phase-2 policy parameter container
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PhasePolicyParams:
    """Phase 2 policy parameters.

    Wraps the Task 012 ``ReactivePolicyParams`` and ``ProactivePolicyParams``
    plus Phase 2-specific knobs:

    Attributes:
        reactive_params: Verbatim Task 012 reactive params.
        proactive_params: Verbatim Task 012 proactive params.
        treat_real_429_as_trigger: Reactive only — whether a real 429
            from the primary is itself a trigger (default True; the
            policy contract requires True for this measurement).
        real_429_observed_action: Proactive only — action taken on a
            real 429 from primary. Currently only ``route_to_spillover``
            is implemented.
        real_429_followup_requests: Proactive only — count of subsequent
            requests (after the failing one) routed to spillover when a
            real 429 is observed.
    """

    reactive_params: ReactivePolicyParams
    proactive_params: ProactivePolicyParams
    treat_real_429_as_trigger: bool = True
    real_429_observed_action: str = "route_to_spillover"
    real_429_followup_requests: int = 5


# ----------------------------------------------------------------------------
# Experiment YAML loader (Phase 2 schema — distinct from Phase 1)
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class _DeploymentBlock:
    """One deployment's resolved config (primary or spillover)."""

    deployment_template: str
    deployment_name: str
    family: str
    version: str
    endpoint_env: str
    auth_mode: str
    tpm: int | None
    rpm: int | None


@dataclass(frozen=True)
class SimulationLoadConfig:
    """Parsed simulation load-shape block (warmup + ramp + sustain).

    Mirrors the Task 012 shape exactly so the imported
    ``_build_arrival_schedule`` consumes it without translation.
    """

    duration_seconds: int
    warmup_duration_seconds: int
    warmup_tps: float
    ramp_start_tps: float
    ramp_end_tps: float
    ramp_duration_seconds: int
    sustain_tps: float
    sustain_duration_seconds: int


@dataclass(frozen=True)
class DualExperimentConfig:
    """Parsed Phase 2 dual-endpoint experiment YAML.

    Distinct from ``simulate_spillover.ExperimentConfig`` because the
    Phase 2 YAML has top-level ``primary`` and ``spillover`` blocks
    (instead of a single ``model`` block) and a different policy
    structure (``reactive_params`` / ``proactive_params`` with Phase 2
    knobs).
    """

    path: pathlib.Path
    experiment_id: str
    description: str
    parent_experiment: str | None
    benchmark: str
    primary: _DeploymentBlock
    spillover: _DeploymentBlock
    call_params: dict
    effort: str
    policy_type: str
    policy_params: PhasePolicyParams
    simulation: SimulationLoadConfig
    corpus_seed: int
    target_system_prompt_tokens: int
    user_prompts_path: str
    system_prompt_corpus_path: str
    prompt_cache_key: str | None
    prompt_cache_retention: str | None
    budget_estimated_usd: float
    budget_hard_ceiling_usd: float
    budget_confirmed: bool
    pricing_snapshot_path: str
    metadata: dict
    concurrency: int


def _require(d: dict, key: str, where: str) -> Any:
    if key not in d:
        raise ValueError(f"{where}: missing required key {key!r}")
    return d[key]


def _parse_deployment_block(
    raw: dict, *, where: str, role: str
) -> _DeploymentBlock:
    """Parse one ``primary:`` / ``spillover:`` block from the YAML."""
    if not isinstance(raw, dict):
        raise ValueError(f"{where}.{role}: must be a mapping")
    family = _require(raw, "family", f"{where}.{role}")
    if family != "gpt-5.2":
        raise ValueError(
            f"{where}.{role}: dual-spillover measurement supports "
            f"family='gpt-5.2' only; got {family!r}"
        )
    auth_mode = _require(raw, "auth_mode", f"{where}.{role}")
    if auth_mode != "entra":
        raise ValueError(
            f"{where}.{role}.auth_mode must be 'entra'; got {auth_mode!r}"
        )
    tpm = raw.get("tpm")
    rpm = raw.get("rpm")
    return _DeploymentBlock(
        deployment_template=str(_require(raw, "deployment", f"{where}.{role}")),
        deployment_name=str(_require(raw, "deployment_name", f"{where}.{role}")),
        family=family,
        version=str(raw.get("version", "")),
        endpoint_env=str(
            raw.get("endpoint_env", "AZURE_OPENAI_FOUNDRY_ENDPOINT")
        ),
        auth_mode=auth_mode,
        tpm=int(tpm) if isinstance(tpm, (int, float)) else None,
        rpm=int(rpm) if isinstance(rpm, (int, float)) else None,
    )


def load_experiment(path: str | pathlib.Path) -> DualExperimentConfig:
    """Load and validate a Phase 2 dual-endpoint experiment YAML.

    Args:
        path: Filesystem path to the YAML.

    Returns:
        Parsed ``DualExperimentConfig``.

    Raises:
        FileNotFoundError: ``path`` does not exist.
        ValueError: YAML schema violation.
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
    if benchmark != "05-dual-spillover":
        raise ValueError(
            f"{where}: benchmark must be '05-dual-spillover'; got {benchmark!r}"
        )
    parent = raw.get("parent_experiment")

    primary_block = _parse_deployment_block(
        _require(raw, "primary", where), where=where, role="primary"
    )
    spillover_block = _parse_deployment_block(
        _require(raw, "spillover", where), where=where, role="spillover"
    )
    if primary_block.deployment_name == spillover_block.deployment_name:
        raise ValueError(
            f"{where}: primary.deployment_name and spillover.deployment_name "
            f"must differ (different cache pool requires different deployment "
            f"name); both are {primary_block.deployment_name!r}"
        )
    if not primary_block.version or primary_block.version != spillover_block.version:
        raise ValueError(
            f"{where}: primary.version and spillover.version must be the same "
            "non-empty immutable model version for exact pricing selection"
        )

    call_params = raw.get("call_params") or {}
    if not isinstance(call_params, dict):
        raise ValueError(f"{where}: call_params must be a mapping")
    forbidden = [k for k in ("temperature", "top_p") if k in call_params]
    if forbidden:
        raise ValueError(
            f"{where}: family=gpt-5.2 does not accept "
            f"{', '.join(forbidden)} in call_params"
        )

    effort = raw.get("effort", "minimal")
    if not isinstance(effort, str) or not effort:
        raise ValueError(f"{where}: effort must be a non-empty string")

    policy_block = _require(raw, "policy", where)
    if not isinstance(policy_block, dict):
        raise ValueError(f"{where}: policy block must be a mapping")
    policy_type = _require(policy_block, "type", f"{where}.policy")
    if policy_type not in ("reactive", "proactive"):
        raise ValueError(
            f"{where}: policy.type must be 'reactive' or 'proactive'; "
            f"got {policy_type!r}"
        )

    react_block = policy_block.get("reactive_params") or {}
    if not isinstance(react_block, dict):
        raise ValueError(f"{where}: policy.reactive_params must be a mapping")
    react_params = ReactivePolicyParams(
        first_token_timeout_ms=float(
            react_block.get("first_token_timeout_ms", 3000.0)
        ),
        stay_on_spillover_min_requests=int(
            react_block.get("stay_on_spillover_min_requests", 10)
        ),
        health_check_interval_ms=float(
            react_block.get("health_check_interval_ms", 30000.0)
        ),
    )
    treat_real_429_as_trigger = bool(
        react_block.get("treat_real_429_as_trigger", True)
    )
    if not treat_real_429_as_trigger:
        raise ValueError(
            f"{where}: policy.reactive_params.treat_real_429_as_trigger must "
            f"be true for the Phase 2 measurement contract; got false"
        )

    proact_block = policy_block.get("proactive_params") or {}
    if not isinstance(proact_block, dict):
        raise ValueError(f"{where}: policy.proactive_params must be a mapping")
    proact_params = ProactivePolicyParams(
        latency_window_size=int(proact_block.get("latency_window_size", 50)),
        p95_threshold_multiplier=float(
            proact_block.get("p95_threshold_multiplier", 1.5)
        ),
        spillover_fraction_max=float(
            proact_block.get("spillover_fraction_max", 0.8)
        ),
        measurement_window_seconds=float(
            proact_block.get("measurement_window_seconds", 10.0)
        ),
        ramp_up_step=float(proact_block.get("ramp_up_step", 0.2)),
        ramp_back_factor=float(proact_block.get("ramp_back_factor", 0.9)),
    )
    if proact_params.ramp_up_step <= 0:
        raise ValueError(
            f"{where}: policy.proactive_params.ramp_up_step must be > 0; "
            f"got {proact_params.ramp_up_step!r}"
        )
    if proact_params.ramp_up_step >= proact_params.spillover_fraction_max:
        raise ValueError(
            f"{where}: policy.proactive_params.ramp_up_step "
            f"({proact_params.ramp_up_step}) must be strictly less than "
            f"spillover_fraction_max ({proact_params.spillover_fraction_max}) "
            f"so the cap is approached progressively over multiple windows"
        )
    real_429_action = str(
        proact_block.get("real_429_observed_action", "route_to_spillover")
    )
    if real_429_action != "route_to_spillover":
        raise ValueError(
            f"{where}: policy.proactive_params.real_429_observed_action must "
            f"be 'route_to_spillover'; got {real_429_action!r}"
        )
    real_429_followup = int(proact_block.get("real_429_followup_requests", 5))
    if real_429_followup < 0:
        raise ValueError(
            f"{where}: policy.proactive_params.real_429_followup_requests "
            f"must be >= 0; got {real_429_followup}"
        )

    policy_params = PhasePolicyParams(
        reactive_params=react_params,
        proactive_params=proact_params,
        treat_real_429_as_trigger=treat_real_429_as_trigger,
        real_429_observed_action=real_429_action,
        real_429_followup_requests=real_429_followup,
    )

    sim_block = _require(raw, "simulation", where)
    if not isinstance(sim_block, dict):
        raise ValueError(f"{where}: simulation block must be a mapping")
    warmup_block = sim_block.get("warmup") or {}
    load_block = sim_block.get("load_pattern") or {}
    simulation = SimulationLoadConfig(
        duration_seconds=int(
            _require(sim_block, "duration_seconds", f"{where}.simulation")
        ),
        warmup_duration_seconds=int(warmup_block.get("duration_seconds", 120)),
        warmup_tps=float(warmup_block.get("tps", 0.3)),
        ramp_start_tps=float(load_block.get("ramp_start_tps", 0.5)),
        ramp_end_tps=float(load_block.get("ramp_end_tps", 2.5)),
        ramp_duration_seconds=int(load_block.get("ramp_duration_seconds", 600)),
        sustain_tps=float(load_block.get("sustain_tps", 2.0)),
        sustain_duration_seconds=int(
            load_block.get("sustain_duration_seconds", 600)
        ),
    )

    corpus_seed = int(_require(raw, "corpus_seed", where))
    target_tokens = int(raw.get("target_system_prompt_tokens", 30000))
    user_prompts_path = str(_require(raw, "user_prompts_path", where))
    corpus_path = str(_require(raw, "system_prompt_corpus_path", where))

    cache_key = call_params.get("prompt_cache_key")
    cache_retention = call_params.get("prompt_cache_retention")
    if cache_retention is not None and cache_retention not in (
        "in_memory",
        "24h",
    ):
        raise ValueError(
            f"{where}: call_params.prompt_cache_retention must be "
            f"'in_memory' or '24h' or null; got {cache_retention!r}"
        )

    budget = _require(raw, "budget", where)
    if not isinstance(budget, dict):
        raise ValueError(f"{where}: budget block must be a mapping")
    est = float(_require(budget, "estimated_cost_usd", f"{where}.budget"))
    hard = float(_require(budget, "hard_ceiling_usd", f"{where}.budget"))
    if hard <= 0:
        raise ValueError(f"{where}: budget.hard_ceiling_usd must be > 0")
    confirmed = bool(budget.get("confirmed", False))

    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError(f"{where}: metadata block must be a mapping")
    arch_ctx = metadata.get("architecture_context")
    if arch_ctx != "single_call_react":
        raise ValueError(
            f"{where}: metadata.architecture_context must be "
            f"'single_call_react'; got {arch_ctx!r}"
        )
    consumption_ctx = metadata.get("consumption_model_context")
    if consumption_ctx != "ptu":
        raise ValueError(
            f"{where}: metadata.consumption_model_context must be 'ptu' "
            f"(we simulate a PTU-with-spillover pattern using two PAYG "
            f"deployments); got {consumption_ctx!r}"
        )

    concurrency = int(raw.get("concurrency", 4))
    if concurrency <= 0:
        raise ValueError(f"{where}: concurrency must be > 0")

    return DualExperimentConfig(
        path=p,
        experiment_id=exp_id,
        description=description,
        parent_experiment=parent,
        benchmark=benchmark,
        primary=primary_block,
        spillover=spillover_block,
        call_params=dict(call_params),
        effort=effort,
        policy_type=policy_type,
        policy_params=policy_params,
        simulation=simulation,
        corpus_seed=corpus_seed,
        target_system_prompt_tokens=target_tokens,
        user_prompts_path=user_prompts_path,
        system_prompt_corpus_path=corpus_path,
        prompt_cache_key=(
            str(cache_key) if isinstance(cache_key, str) else None
        ),
        prompt_cache_retention=(
            str(cache_retention) if isinstance(cache_retention, str) else None
        ),
        budget_estimated_usd=est,
        budget_hard_ceiling_usd=hard,
        budget_confirmed=confirmed,
        pricing_snapshot_path=str(
            raw.get("pricing_snapshot_path", CANONICAL_PAYG_SNAPSHOT_PATH)
        ),
        metadata=dict(metadata),
        concurrency=concurrency,
    )


# ----------------------------------------------------------------------------
# Helpers — env, git, timestamps, hashing
# ----------------------------------------------------------------------------


_ENV_TEMPLATE_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _resolve_env_template(value: str, *, env: dict[str, str] | None = None) -> str:
    """Substitute ``${NAME}`` references in ``value`` with env-var values.

    Reads env-var NAMES only; the resolved VALUE is never logged.
    """
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
                "git_commit='unknown' in JSONL records."
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


def _sha256_json(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _zero_usage_dict() -> dict[str, Any]:
    """Synthetic Foundry-v1-shaped zero ``usage`` dict for dry runs."""
    return {
        "input_tokens": 0,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 0,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 0,
    }


def _usage_to_token_usage(usage_dict: dict[str, Any]) -> TokenUsage:
    """Map Foundry v1 ``usage`` dict to ``TokenUsage``."""
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


# ----------------------------------------------------------------------------
# Live client construction (one per deployment)
# ----------------------------------------------------------------------------


def _make_robust_token_provider(
    underlying: Callable[[], Awaitable[str]],
    *,
    max_retries: int = DEFAULT_TOKEN_MAX_RETRIES,
    base_backoff_seconds: float = DEFAULT_TOKEN_BASE_BACKOFF_SECONDS,
    max_backoff_seconds: float = DEFAULT_TOKEN_MAX_BACKOFF_SECONDS,
    sleeper: Callable[[float], Awaitable[None]] | None = None,
) -> Callable[[], Awaitable[str]]:
    """Wrap an async Entra ID token provider with bounded retry + stampede protection.

    Long-run hardening for the Phase 2 dual-spillover live runner
    (Task 015 blocker hotfix). The OpenAI SDK awaits the configured
    ``api_key`` callable before every Responses API request, so a
    transient ``CredentialUnavailableError`` from the underlying
    ``azure.identity.aio`` chain (typically the Azure CLI subprocess
    exceeding its 10s timeout under host load) aborts the entire
    multi-thousand-request run. This wrapper:

      * Calls ``underlying`` on every invocation. Token reuse and
        refresh-near-expiry are delegated entirely to
        ``azure.identity.aio``'s own internal credential cache,
        which knows the real Entra ID expiry. We deliberately do
        NOT add an outer fixed-window cache: that would risk
        re-caching an aged token that ``azure.identity`` happened
        to return from its internal cache while still valid, then
        extending the wrapper's perceived freshness past the real
        Azure-side expiry → 401 mid-run. Trusting only the cache
        that knows the real ``exp`` is the safe design.
      * Retries transient ``CredentialUnavailableError`` (Azure CLI
        subprocess timeout) and ``asyncio.TimeoutError`` /
        ``TimeoutError`` (asyncio-side wait timeouts) with bounded
        exponential backoff (``base_backoff_seconds``,
        ``2*base_backoff_seconds``, …, capped at
        ``max_backoff_seconds``) over up to ``max_retries`` retry
        attempts. All other exceptions propagate immediately — we
        never swallow authorisation, configuration, or programming
        errors.
      * On retry exhaustion the original transient exception is
        re-raised verbatim so callers and the operator log see the
        root cause (no silent success path).
      * Serialises concurrent calls behind an ``asyncio.Lock`` for
        stampede protection: when ``azure.identity``'s internal
        cache expires and many in-flight wrapper calls observe the
        miss simultaneously, only one drives the refresh and the
        rest pick up the freshly-issued token via the underlying's
        in-memory cache. For cache hits the lock cost is a few
        microseconds; for cache misses it prevents the host from
        spawning many concurrent ``az`` subprocesses.
      * No bearer token, no JWT, no scope value is ever logged.
        Log lines record only the attempt counter, the exception
        class name, and the backoff seconds.

    Args:
        underlying: An async callable that returns a bearer token
            string when awaited. Typically the result of
            ``azure.identity.aio.get_bearer_token_provider``.
        max_retries: Number of additional retry attempts on top of
            the first call (so total attempts = ``max_retries + 1``).
        base_backoff_seconds: Initial backoff before the first
            retry; doubles each subsequent retry.
        max_backoff_seconds: Per-attempt backoff ceiling.
        sleeper: Async sleep callable; injectable for tests.
            Defaults to ``asyncio.sleep``.

    Returns:
        An ``async def`` coroutine function (``Callable[[], Awaitable[str]]``)
        suitable for passing to ``AsyncOpenAI(api_key=...)``. The
        callable is a real coroutine function (not a callable
        instance) so ``inspect.iscoroutinefunction`` returns ``True``
        and the SDK's ``_refresh_api_key`` hook awaits it directly.

    Raises:
        azure.identity.CredentialUnavailableError: Re-raised after
            exhausting all retry attempts.
        asyncio.TimeoutError: Re-raised after exhausting all retry
            attempts.
        Exception: Any non-transient exception from ``underlying``
            propagates immediately on the first attempt.
    """
    _sleeper = sleeper or asyncio.sleep
    retries = int(max_retries)
    base_backoff = float(base_backoff_seconds)
    max_backoff = float(max_backoff_seconds)

    try:
        from azure.identity import (  # noqa: PLC0415
            CredentialUnavailableError as _CredentialUnavailableError,
        )
    except ImportError:  # pragma: no cover - exercised only when SDK absent
        class _CredentialUnavailableError(Exception):
            """Fallback sentinel when azure-identity is unavailable."""

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
                        "TOKEN_PROVIDER_TRANSIENT_FAILURE "
                        "attempt=%d/%d exc=%s backoff_seconds=%.2f",
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
                        "TOKEN_PROVIDER_RECOVERED attempts=%d",
                        attempt + 1,
                    )
                return token

    return _provider


def _build_live_client(*, endpoint_value: str) -> Any:
    """Instantiate one Foundry v1 ``AsyncOpenAI`` client (Entra ID).

    Returns one client; the caller invokes this twice (once per
    deployment). Even though both clients share the same endpoint base
    today, keeping the construction call-site distinct lets future
    variants (e.g., spillover in a different region) swap endpoint
    URLs without restructuring the call sites.

    Lazy-imports so dry-run does not require the SDK installed.

    Auth refresh contract (long-run hardening, Task 015 follow-up,
    mirroring the Task 014 fix already applied to
    ``scripts/simulate_spillover.py``). Entra ID bearer tokens have a
    ~60-minute TTL. A prior implementation called ``token_provider()``
    at construction time and embedded the resulting static JWT string
    into ``AsyncOpenAI.api_key``. The OpenAI SDK then sent that literal
    string as ``Authorization: Bearer <static_jwt>`` on every
    subsequent request with no callback hook to refresh — so any
    Phase 2 run longer than the remaining TTL (e.g. the 22-minute load
    shape per policy, repeated across reactive + proactive policies
    under SDK 429 retries) would 401 mid-stream. This was observed in
    a clean-worktree Phase 2 attempt that exited with
    ``openai.AuthenticationError: 401 - Access token ... expired`` at
    ``request_idx=202`` after ~23 minutes of wall clock.

    The fix uses the **async** token provider from
    ``azure.identity.aio`` and passes the *callable itself* (not its
    result) into ``AsyncOpenAI(api_key=...)``. The OpenAI SDK
    (``AsyncOpenAI`` ≥ 2.x) accepts ``Callable[[], Awaitable[str]]``
    on ``api_key`` and invokes it before every Responses API call via
    ``_refresh_api_key``, using the freshly returned token as the
    Bearer header. ``azure.identity.aio`` internally caches and
    refreshes the underlying access token, so we get a refreshable
    token provider end-to-end without managing TTL ourselves.

    Long-run transient-timeout hardening (Task 015 Phase 2 blocker
    hotfix). A clean-worktree Phase 2 reactive attempt exited with
    ``azure.identity.CredentialUnavailableError: Timed out waiting
    for Azure CLI`` at ``request_idx≈1006`` after ~2h of wall clock.
    Root cause: the OpenAI SDK awaits the configured ``api_key``
    callable before *every* Responses API request, so the underlying
    ``azure.identity.aio`` provider spawns ``az account
    get-access-token`` whenever its internal cache misses or nears
    expiry. Under host load that subprocess can transiently exceed
    the SDK default 10s timeout and raise, aborting the entire
    multi-thousand-request run.

    The raw provider is wrapped via ``_make_robust_token_provider``
    which (a) retries transient ``CredentialUnavailableError`` /
    ``asyncio.TimeoutError`` with bounded exponential backoff
    (1s → 2s → 4s → 8s → 16s, capped at 30s, 5 retries) before
    re-raising and (b) serialises concurrent calls behind an
    ``asyncio.Lock`` so a refresh stampede cannot spawn many
    parallel ``az`` subprocesses. The wrapper intentionally does
    NOT add an outer fixed-window cache: token reuse +
    refresh-near-expiry is delegated entirely to
    ``azure.identity.aio``'s own internal credential cache, which
    knows the real Entra ID ``exp`` and rotates within ~5 min of
    expiry. An outer fixed-window cache would risk re-caching an
    aged token that ``azure.identity`` returned from its internal
    cache while still valid, then extending the wrapper's perceived
    freshness past the real Azure-side expiry → 401 mid-run. No
    bearer string is ever logged. The wrapped callable is re-bound
    to the local ``token_provider`` symbol so the source-pinning
    test ``test_build_live_client_uses_aio_identity_not_sync``
    still matches the ``api_key=token_provider)`` form.

    Specifically NOT done here:
      * No API-key code path (would violate the auth contract for this
        repo: Entra ID only).
      * No subclassing of ``AsyncOpenAI`` or custom ``httpx`` auth
        flow (the SDK already supports the callable form natively —
        adding a custom layer would be larger surface area and not
        "minimal robust").
      * No change to ``base_url`` (Foundry v1 surface preserved) or
        to the methodology ``api_version="preview"`` recorded
        verbatim in JSONL records.
    """
    from azure.identity.aio import (  # noqa: PLC0415
        DefaultAzureCredential,
        get_bearer_token_provider,
    )
    from openai import AsyncOpenAI  # noqa: PLC0415

    raw_provider = get_bearer_token_provider(
        DefaultAzureCredential(),
        "https://ai.azure.com/.default",
    )
    # Wrap the raw provider with bounded retry + stampede protection
    # so a transient Azure CLI subprocess timeout does not kill a
    # 2000+ request long run. The wrapper does NOT cache bearer
    # strings on its own; token reuse + refresh-near-expiry is
    # delegated to azure.identity.aio's own internal credential
    # cache, which knows the real Entra ID `exp` and therefore
    # cannot extend a token's perceived freshness past Azure's
    # actual expiry.
    token_provider = _make_robust_token_provider(raw_provider)
    base_url = endpoint_value.rstrip("/") + "/openai/v1/"
    # api_key receives the async callable itself; the OpenAI SDK awaits
    # it before each request so the token is refreshed on a per-call
    # cadence. The wrapper retries transient timeouts and serialises
    # concurrent refreshes; cache-hit cost is delegated to
    # azure.identity.aio's in-memory cache (no outer caching here).
    return AsyncOpenAI(base_url=base_url, api_key=token_provider)


def _parse_response_headers(headers: Any) -> dict[str, Any]:
    """Extract retry-after + x-ms-spillover-* headers from a response.

    Returns a dict with keys:
        retry_after_ms (float | None)
        retry_after_seconds (float | None)
        x_ms_spillover_from_deployment (str | None)
        x_ms_deployment_name (str | None)
        x_ms_spillover_error (str | None)

    Any missing header maps to ``None``. ``headers`` may be a dict-like
    or an httpx-Headers-like object.
    """
    out: dict[str, Any] = {
        "retry_after_ms": None,
        "retry_after_seconds": None,
        "x_ms_spillover_from_deployment": None,
        "x_ms_deployment_name": None,
        "x_ms_spillover_error": None,
    }
    if headers is None:
        return out
    try:
        getter = headers.get
    except AttributeError:
        return out

    ms_raw = getter("retry-after-ms")
    s_raw = getter("retry-after")
    if ms_raw is not None:
        try:
            out["retry_after_ms"] = float(ms_raw)
        except (TypeError, ValueError):
            out["retry_after_ms"] = None
    if s_raw is not None:
        try:
            out["retry_after_seconds"] = float(s_raw)
        except (TypeError, ValueError):
            out["retry_after_seconds"] = None

    sp_from = getter("x-ms-spillover-from-deployment")
    dep_name = getter("x-ms-deployment-name")
    sp_err = getter("x-ms-spillover-error")
    if sp_from is not None:
        out["x_ms_spillover_from_deployment"] = str(sp_from)
    if dep_name is not None:
        out["x_ms_deployment_name"] = str(dep_name)
    if sp_err is not None:
        out["x_ms_spillover_error"] = str(sp_err)
    return out


# ----------------------------------------------------------------------------
# Per-deployment call execution
# ----------------------------------------------------------------------------


def _build_call_kwargs(
    *,
    deployment: str,
    system_prompt: str,
    user_text: str,
    effort: str,
    call_params: dict,
    prompt_cache_key: str | None,
    prompt_cache_retention: str | None,
) -> dict[str, Any]:
    """Construct ``responses.create()`` kwargs for one request."""
    kwargs: dict[str, Any] = {
        "model": deployment,
        "input": system_prompt + "\n\n" + user_text,
        "reasoning": {"effort": effort},
    }
    if "max_output_tokens" in call_params:
        kwargs["max_output_tokens"] = call_params["max_output_tokens"]
    if prompt_cache_key is not None:
        kwargs["prompt_cache_key"] = prompt_cache_key
    if prompt_cache_retention is not None:
        kwargs["prompt_cache_retention"] = prompt_cache_retention
    return kwargs


async def _create_with_raw_response(
    *, client: Any, call_kwargs: dict
) -> tuple[Any, Any]:
    """Issue one ``responses.create`` call and return ``(response, raw_headers)``.

    Uses the OpenAI Python SDK's raw-response API
    (``client.responses.with_raw_response.create(**kwargs)``) so the
    success path reliably exposes HTTP response headers. This is the
    only supported way to access the underlying ``x-ms-*`` Azure
    response headers (notably ``x-ms-deployment-name`` and the
    spillover-tracking headers ``x-ms-spillover-from-deployment`` /
    ``x-ms-spillover-error``) on a 2xx response — the default
    ``responses.create()`` discards them.

    Args:
        client: The async OpenAI client (or test fake).
        call_kwargs: The kwargs to forward to ``responses.create``.

    Returns:
        A tuple ``(response, raw_headers)`` where ``response`` is the
        parsed Pydantic ``Response`` object (same shape as
        ``responses.create()``'s return value) and ``raw_headers`` is
        an httpx-Headers-like mapping (``.get(name)`` supported).

    Raises:
        Whatever ``responses.create()`` raises. The caller handles 429
        and other status codes by inspecting the exception. Note that
        on errors, headers travel on ``exc.response.headers`` exactly
        as with the non-raw API — this wrapper only affects the
        success path.
    """
    raw_api = getattr(client.responses, "with_raw_response", None)
    if raw_api is not None and hasattr(raw_api, "create"):
        raw_resp = await raw_api.create(**call_kwargs)
        # APIResponse exposes .headers (httpx.Headers) and .parse() →
        # the typed Response object. On the async OpenAI client (SDK
        # >=2.37), the returned object is ``AsyncAPIResponse`` whose
        # ``parse()`` is itself a coroutine — calling it without
        # ``await`` silently yields a coroutine object instead of the
        # typed response and downstream ``response.usage`` access then
        # crashes. Await only when the returned value is awaitable so
        # synchronous fakes/legacy clients keep working.
        raw_headers = getattr(raw_resp, "headers", None)
        if hasattr(raw_resp, "parse"):
            parsed = raw_resp.parse()
            if inspect.isawaitable(parsed):
                parsed = await parsed
            response = parsed
        else:
            response = raw_resp
        return response, raw_headers
    # Backwards-compatible fallback: legacy client without
    # with_raw_response. Headers will not be reliably available on
    # success — best-effort scan of common attributes.
    response = await client.responses.create(**call_kwargs)
    raw_headers = getattr(response, "headers", None)
    if raw_headers is None:
        raw = getattr(response, "_raw_response", None)
        raw_headers = getattr(raw, "headers", None) if raw else None
    return response, raw_headers


async def _call_primary(
    *, client: Any, call_kwargs: dict, request_idx: int
) -> dict[str, Any]:
    """Invoke the primary deployment ONCE. Never silently retries 429.

    Returns a result dict with keys:
        usage (dict | None) — None when 429 (no usage available)
        first_token_latency_ms (float)
        total_latency_ms (float)
        real_429_observed (bool)
        headers (dict — parsed retry-after + x-ms-spillover-*)
        raised (Exception | None) — set when a non-429 exception occurred
    """
    started = time.monotonic()
    real_429 = False
    headers_parsed = _parse_response_headers(None)
    try:
        response, raw_headers = await _create_with_raw_response(
            client=client, call_kwargs=call_kwargs
        )
    except Exception as exc:
        status = getattr(exc, "status_code", None) or getattr(
            exc, "status", None
        )
        elapsed_ms = (time.monotonic() - started) * 1000.0
        resp_obj = getattr(exc, "response", None)
        raw_headers = getattr(resp_obj, "headers", None) if resp_obj else None
        headers_parsed = _parse_response_headers(raw_headers)
        if status == 429:
            real_429 = True
            logger.warning(
                "PRIMARY_429 request_idx=%d retry_after_ms=%s retry_after_s=%s",
                request_idx,
                headers_parsed["retry_after_ms"],
                headers_parsed["retry_after_seconds"],
            )
            return {
                "usage": None,
                "first_token_latency_ms": elapsed_ms,
                "total_latency_ms": elapsed_ms,
                "real_429_observed": real_429,
                "headers": headers_parsed,
                "raised": None,
            }
        return {
            "usage": None,
            "first_token_latency_ms": elapsed_ms,
            "total_latency_ms": elapsed_ms,
            "real_429_observed": False,
            "headers": headers_parsed,
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
    # Successful responses carry headers (notably x-ms-deployment-name
    # which reveals if the request was native-spilled). Headers are
    # captured via with_raw_response above; fall back to the parsed
    # response only if the raw-response API was unavailable.
    if raw_headers is None:
        raw_headers = getattr(response, "headers", None)
    if raw_headers is None:
        raw = getattr(response, "_raw_response", None)
        raw_headers = getattr(raw, "headers", None) if raw else None
    headers_parsed = _parse_response_headers(raw_headers)
    return {
        "usage": usage_dict,
        "first_token_latency_ms": elapsed_ms,
        "total_latency_ms": elapsed_ms,
        "real_429_observed": False,
        "headers": headers_parsed,
        "raised": None,
    }


async def _call_spillover_with_retry(
    *,
    client: Any,
    call_kwargs: dict,
    request_idx: int,
) -> dict[str, Any]:
    """Invoke the spillover deployment, retrying ONCE on 429.

    Returns a result dict with the same keys as ``_call_primary`` plus:
        spillover_429_count (int) — how many 429s observed on this
            request (0, 1, or higher if retries also 429)
    """
    started = time.monotonic()
    spillover_429_count = 0
    last_headers = _parse_response_headers(None)
    last_exc: Exception | None = None
    for attempt in range(SPILLOVER_429_MAX_RETRIES + 1):
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
                spillover_429_count += 1
                logger.warning(
                    "SPILLOVER_429 request_idx=%d attempt=%d "
                    "retry_after_ms=%s retry_after_s=%s",
                    request_idx,
                    attempt,
                    last_headers["retry_after_ms"],
                    last_headers["retry_after_seconds"],
                )
                if attempt < SPILLOVER_429_MAX_RETRIES:
                    ms = last_headers["retry_after_ms"]
                    s = last_headers["retry_after_seconds"]
                    if ms is not None:
                        delay = ms / 1000.0
                    elif s is not None:
                        delay = s
                    else:
                        delay = SPILLOVER_429_BASE_DELAY_S * (2**attempt)
                    await asyncio.sleep(delay)
                    last_exc = exc
                    continue
            # Either a non-429 or the final retry attempt; bubble up.
            elapsed_ms = (time.monotonic() - started) * 1000.0
            return {
                "usage": None,
                "first_token_latency_ms": elapsed_ms,
                "total_latency_ms": elapsed_ms,
                "real_429_observed": (spillover_429_count > 0),
                "headers": last_headers,
                "raised": exc if status != 429 else None,
                "spillover_429_count": spillover_429_count,
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
        if raw_headers is None:
            raw = getattr(response, "_raw_response", None)
            raw_headers = getattr(raw, "headers", None) if raw else None
        headers_parsed = _parse_response_headers(raw_headers)
        return {
            "usage": usage_dict,
            "first_token_latency_ms": elapsed_ms,
            "total_latency_ms": elapsed_ms,
            "real_429_observed": (spillover_429_count > 0),
            "headers": headers_parsed,
            "raised": None,
            "spillover_429_count": spillover_429_count,
        }
    # Unreachable: the loop returns each branch.
    assert last_exc is not None
    raise last_exc


# ----------------------------------------------------------------------------
# Pre-flight reachability
# ----------------------------------------------------------------------------


async def preflight_reachability(
    *,
    primary_client: Any,
    primary_deployment: str,
    spillover_client: Any,
    spillover_deployment: str,
) -> dict[str, Any]:
    """One short request per deployment. Aborts the entire run if either fails.

    Returns a dict with reachability + output-token-count results for
    each deployment. Raises ``PreflightReachabilityError`` on any
    failure (the caller then exits with code 2).
    """
    results: dict[str, Any] = {
        "primary_deployment": primary_deployment,
        "spillover_deployment": spillover_deployment,
        "primary_reachable": False,
        "primary_output_tokens": 0,
        "spillover_reachable": False,
        "spillover_output_tokens": 0,
    }
    for role, client, deployment in (
        ("primary", primary_client, primary_deployment),
        ("spillover", spillover_client, spillover_deployment),
    ):
        try:
            resp = await client.responses.create(
                model=deployment,
                input="ping",
                # Foundry v1 Responses API rejects max_output_tokens < 16
                # with HTTP 400 (integer_below_min_value). 16 is the
                # smallest legal value and keeps the preflight cost
                # negligible.
                max_output_tokens=16,
                # gpt-5.2-2025-12-11 rejects effort="minimal" (supported:
                # none / low / medium / high / xhigh). Use "low" — the
                # smallest non-zero reasoning effort accepted by the
                # current production model — to keep the preflight cheap
                # while exercising the same code path the workload uses.
                reasoning={"effort": "low"},
            )
        except Exception as exc:
            raise PreflightReachabilityError(
                f"pre-flight reachability failed for {role} deployment="
                f"{deployment}: {exc!r}; verify benchmarks/05-dual-spillover/"
                f"PREFLIGHT_LOG.md and the manual owner pre-flight checklist"
            ) from exc
        usage_obj = getattr(resp, "usage", None)
        out_tok = 0
        if usage_obj is not None:
            out_tok = int(getattr(usage_obj, "output_tokens", 0) or 0)
        if out_tok <= 0:
            raise PreflightReachabilityError(
                f"pre-flight reachability for {role} deployment="
                f"{deployment} returned zero output_tokens; expected > 0"
            )
        results[f"{role}_reachable"] = True
        results[f"{role}_output_tokens"] = out_tok
        logger.info(
            "PREFLIGHT_OK role=%s deployment=%s output_tokens=%d",
            role,
            deployment,
            out_tok,
        )
    return results


def _append_preflight_log_record(
    *,
    preflight_log_path: pathlib.Path,
    timestamp_iso: str,
    git_commit: str,
    experiment_id: str,
    primary_deployment_env: str,
    spillover_deployment_env: str,
    reachability_results: dict[str, Any],
) -> bool:
    """Append a timestamped reachability row to ``PREFLIGHT_LOG.md`` atomically.

    Called after ``preflight_reachability`` succeeds on a live run. The
    appended section records what the runner observed (timestamp, git
    commit, experiment id, env var names for each deployment, the
    boolean reachability flag and the ``output_tokens`` count returned by
    each ping). No secrets, no endpoint URLs, no resolved deployment
    names are written — only env var names (``${VAR}``-style templates
    from the experiment YAML). The deployment-alias names already appear
    in the owner-prefilled portion of ``PREFLIGHT_LOG.md`` and are not
    secrets, but the runtime-appended rows still avoid them to keep
    secret-leak risk minimal.

    Atomic write contract: the new content is composed in memory, written
    to a sibling tempfile in the same directory, then ``os.replace()``'d
    into place. A failed write leaves the original ``PREFLIGHT_LOG.md``
    untouched.

    Returns ``True`` if a row was appended, ``False`` if the log file
    does not exist (warns and skips — the owner-prefilled file is a
    setup precondition, not a runtime invariant; missing it should not
    halt a live run that already passed reachability).

    Args:
        preflight_log_path: Absolute or relative path to PREFLIGHT_LOG.md.
        timestamp_iso: ISO-8601 ``Z``-suffixed UTC string for the row.
        git_commit: Git commit short/long SHA captured for the run.
        experiment_id: Experiment id from the YAML.
        primary_deployment_env: ``${VAR}``-style env var template for
            the primary deployment (e.g.,
            ``${AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED}``).
        spillover_deployment_env: Same, for the spillover deployment.
        reachability_results: The dict returned by
            ``preflight_reachability``; keys read:
            ``primary_reachable``, ``primary_output_tokens``,
            ``spillover_reachable``, ``spillover_output_tokens``.

    Returns:
        True if the row was appended; False if the log was missing
        or the write failed.
    """
    if not preflight_log_path.is_file():
        logger.warning(
            "PREFLIGHT_LOG_MISSING path=%s; skipping reachability append",
            preflight_log_path,
        )
        return False
    section_lines = [
        "",
        f"### Run {timestamp_iso} — `{experiment_id}`",
        "",
        "| Field                            | Value                                                    |",
        "|----------------------------------|----------------------------------------------------------|",
        f"| `run_timestamp_utc`              | `{timestamp_iso}`                                        |",
        f"| `git_commit`                     | `{git_commit}`                                           |",
        f"| `experiment_id`                  | `{experiment_id}`                                        |",
        f"| `primary_deployment_env`         | `{primary_deployment_env}`                               |",
        f"| `primary_reachable`              | `{reachability_results.get('primary_reachable')}`        |",
        f"| `primary_output_tokens`          | `{reachability_results.get('primary_output_tokens')}`    |",
        f"| `spillover_deployment_env`       | `{spillover_deployment_env}`                             |",
        f"| `spillover_reachable`            | `{reachability_results.get('spillover_reachable')}`      |",
        f"| `spillover_output_tokens`        | `{reachability_results.get('spillover_output_tokens')}`  |",
        "",
    ]
    section = "\n".join(section_lines)
    try:
        existing = preflight_log_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "PREFLIGHT_LOG_READ_FAILED path=%s err=%r; skipping append",
            preflight_log_path,
            exc,
        )
        return False
    new_content = existing.rstrip("\n") + "\n" + section
    tmp_path = preflight_log_path.with_name(
        preflight_log_path.name + f".tmp.{os.getpid()}"
    )
    try:
        tmp_path.write_text(new_content, encoding="utf-8")
        os.replace(tmp_path, preflight_log_path)
    except OSError as exc:
        logger.warning(
            "PREFLIGHT_LOG_WRITE_FAILED path=%s err=%r; original preserved",
            preflight_log_path,
            exc,
        )
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return False
    logger.info(
        "PREFLIGHT_LOG_APPENDED path=%s timestamp=%s experiment=%s",
        preflight_log_path,
        timestamp_iso,
        experiment_id,
    )
    return True


# ----------------------------------------------------------------------------
# Charts — 4 outputs under results/dual-spillover-curves/
# ----------------------------------------------------------------------------


# Color-blind-friendly palette (Wong, 2011).
PALETTE = {
    "primary": "#0072B2",      # blue
    "spillover": "#D55E00",    # vermilion
    "cache_hit": "#009E73",    # bluish green
    "reactive": "#0072B2",     # blue
    "proactive": "#CC79A7",    # reddish purple
    "event_marker": "#E69F00",  # orange
}


def _rolling_cache_hit_ratio(
    records: list[dict[str, Any]],
    window_s: float,
    *,
    endpoint_filter: str | None = None,
) -> tuple[list[float], list[float]]:
    """Compute rolling-window cache hit ratio over the run timeline.

    Args:
        records: Per-request records in order.
        window_s: Rolling window width in seconds.
        endpoint_filter: If provided (``"primary"`` or ``"spillover"``),
            include only requests routed to that endpoint.

    Returns:
        ``(ts, ratios)`` lists.
    """
    filtered: list[tuple[float, int, int]] = []  # (t, cached, input)
    t0: float | None = None
    for rec in records:
        if endpoint_filter is not None and rec.get("endpoint_hit") != endpoint_filter:
            continue
        t = rec.get("relative_time_s")
        if t is None:
            continue
        usage = rec.get("usage") or {}
        in_t = int(usage.get("input_tokens", 0) or 0)
        in_det = usage.get("input_tokens_details") or {}
        cached = int(
            in_det.get("cached_tokens", 0) if isinstance(in_det, dict) else 0
        )
        if t0 is None:
            t0 = t
        filtered.append((t - t0, cached, in_t))
    if not filtered:
        return [], []
    ts: list[float] = []
    ratios: list[float] = []
    timestamps = [s[0] for s in filtered]
    for i, (t, _, _) in enumerate(filtered):
        lo = bisect.bisect_left(timestamps, t - window_s, hi=i + 1)
        win = filtered[lo: i + 1]
        sum_cached = sum(w[1] for w in win)
        sum_in = sum(w[2] for w in win)
        ratio = (sum_cached / sum_in) if sum_in > 0 else 0.0
        ts.append(t)
        ratios.append(ratio)
    return ts, ratios


def _real_429_per_minute(records: list[dict[str, Any]]) -> tuple[list[float], list[int]]:
    """Bucket real 429s by minute since first request (primary endpoint only).

    Returns ``(minute_centers, counts_per_minute)``.
    """
    t0: float | None = None
    by_minute: dict[int, int] = {}
    for rec in records:
        if rec.get("endpoint_hit") != "primary":
            continue
        if not rec.get("real_429_observed", False):
            continue
        t = rec.get("relative_time_s")
        if t is None:
            continue
        if t0 is None:
            t0 = t
        minute = int((t - t0) // 60)
        by_minute[minute] = by_minute.get(minute, 0) + 1
    if not by_minute:
        return [], []
    max_minute = max(by_minute.keys())
    minutes: list[float] = []
    counts: list[int] = []
    for m in range(0, max_minute + 1):
        minutes.append(m + 0.5)
        counts.append(by_minute.get(m, 0))
    return minutes, counts


def _write_line_chart(
    *,
    out_png: pathlib.Path,
    title: str,
    series: list[tuple[str, list[float], list[float], str]],
    ylabel: str = "cache hit ratio",
) -> None:
    """Emit one PNG line chart + sibling CSV."""
    import matplotlib  # noqa: PLC0415
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415

    fig, ax = plt.subplots(figsize=(9, 5))
    for label, xs, ys, color in series:
        ax.plot(xs, ys, label=label, color=color, linewidth=1.6)
    ax.set_xlabel("wallclock seconds since first request")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.set_ylim(bottom=0.0)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    plt.close(fig)

    csv_path = pathlib.Path(str(out_png) + ".csv")
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["series", "t_seconds", "value"])
        for label, xs, ys, _color in series:
            for x, y in zip(xs, ys):
                writer.writerow([label, f"{x:.3f}", f"{y:.6f}"])


def _write_bar_chart(
    *,
    out_png: pathlib.Path,
    title: str,
    series: list[tuple[str, list[float], list[int], str]],
    xlabel: str = "minute since first request",
    ylabel: str = "real 429 count",
) -> None:
    """Emit a grouped/overlaid bar chart for the 429 timeline + CSV."""
    import matplotlib  # noqa: PLC0415
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415

    fig, ax = plt.subplots(figsize=(9, 5))
    width = 0.4
    for i, (label, xs, ys, color) in enumerate(series):
        offset = (i - (len(series) - 1) / 2.0) * width
        shifted = [x + offset for x in xs]
        ax.bar(shifted, ys, width=width, label=label, color=color, alpha=0.85)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(True, alpha=0.3, axis="y")
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    plt.close(fig)

    csv_path = pathlib.Path(str(out_png) + ".csv")
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["series", "minute_center", "count"])
        for label, xs, ys, _color in series:
            for x, y in zip(xs, ys):
                writer.writerow([label, f"{x:.3f}", str(y)])


def _load_jsonl_records(path: pathlib.Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "SIBLING_JSONL_PARSE_ERROR path=%s line=%d err=%s",
                        path,
                        lineno,
                        exc,
                    )
    except OSError as exc:
        logger.warning("SIBLING_JSONL_READ_ERROR path=%s err=%s", path, exc)
        return []
    return out


def _find_sibling_policy_jsonl(
    runs_dir: pathlib.Path,
    current_jsonl: pathlib.Path,
    other_policy: str,
) -> pathlib.Path | None:
    if not runs_dir.is_dir():
        return None
    candidates = sorted(
        p
        for p in runs_dir.glob(f"*_{other_policy}.jsonl")
        if p != current_jsonl
    )
    if not candidates:
        return None
    return candidates[-1]


def _emit_per_endpoint_chart(
    *,
    chart_dir: pathlib.Path,
    policy: str,
    records: list[dict[str, Any]],
    window_s: float,
) -> pathlib.Path | None:
    """Emit ``<policy>_per_endpoint.png``: cache hit ratio per endpoint."""
    if not records:
        return None
    p_ts, p_ratios = _rolling_cache_hit_ratio(
        records, window_s, endpoint_filter="primary"
    )
    s_ts, s_ratios = _rolling_cache_hit_ratio(
        records, window_s, endpoint_filter="spillover"
    )
    if not p_ts and not s_ts:
        return None
    chart_dir.mkdir(parents=True, exist_ok=True)
    png = chart_dir / f"{policy}_per_endpoint.png"
    series: list[tuple[str, list[float], list[float], str]] = []
    if p_ts:
        series.append(("primary", p_ts, p_ratios, PALETTE["primary"]))
    if s_ts:
        series.append(("spillover", s_ts, s_ratios, PALETTE["spillover"]))
    _write_line_chart(
        out_png=png,
        title=(
            f"Phase 2 cache hit ratio ({window_s:.0f}s rolling) — "
            f"{policy} policy, per endpoint"
        ),
        series=series,
        ylabel="cache hit ratio (0-1)",
    )
    return png


def _emit_aggregate_comparison_chart(
    *,
    chart_dir: pathlib.Path,
    current_policy: str,
    current_records: list[dict[str, Any]],
    sibling_policy: str,
    sibling_records: list[dict[str, Any]],
    window_s: float,
) -> pathlib.Path | None:
    """Emit ``policy_comparison_aggregate.png`` — aggregate (both endpoints)."""
    if not current_records or not sibling_records:
        return None
    cur_ts, cur_ratios = _rolling_cache_hit_ratio(current_records, window_s)
    sib_ts, sib_ratios = _rolling_cache_hit_ratio(sibling_records, window_s)
    if not cur_ts or not sib_ts:
        return None
    chart_dir.mkdir(parents=True, exist_ok=True)
    png = chart_dir / "policy_comparison_aggregate.png"
    cur_color = PALETTE.get(current_policy, PALETTE["reactive"])
    sib_color = PALETTE.get(sibling_policy, PALETTE["proactive"])
    _write_line_chart(
        out_png=png,
        title=(
            f"Phase 2 aggregate cache hit ratio ({window_s:.0f}s rolling) — "
            f"reactive vs proactive"
        ),
        series=[
            (f"{current_policy} (this run)", cur_ts, cur_ratios, cur_color),
            (f"{sibling_policy} (sibling)", sib_ts, sib_ratios, sib_color),
        ],
        ylabel="cache hit ratio (0-1)",
    )
    return png


def _emit_real_429_timeline_chart(
    *,
    chart_dir: pathlib.Path,
    current_policy: str,
    current_records: list[dict[str, Any]],
    sibling_policy: str | None,
    sibling_records: list[dict[str, Any]] | None,
) -> pathlib.Path | None:
    """Emit ``real_429_timeline.png`` — per-minute real 429s for both policies."""
    cur_minutes, cur_counts = _real_429_per_minute(current_records)
    sib_minutes: list[float] = []
    sib_counts: list[int] = []
    if sibling_records:
        sib_minutes, sib_counts = _real_429_per_minute(sibling_records)
    if not cur_minutes and not sib_minutes:
        return None
    chart_dir.mkdir(parents=True, exist_ok=True)
    png = chart_dir / "real_429_timeline.png"
    series: list[tuple[str, list[float], list[int], str]] = []
    if cur_minutes:
        series.append(
            (
                f"{current_policy} (primary)",
                cur_minutes,
                cur_counts,
                PALETTE.get(current_policy, PALETTE["reactive"]),
            )
        )
    if sibling_policy is not None and sib_minutes:
        series.append(
            (
                f"{sibling_policy} (primary)",
                sib_minutes,
                sib_counts,
                PALETTE.get(sibling_policy, PALETTE["proactive"]),
            )
        )
    _write_bar_chart(
        out_png=png,
        title="Phase 2 real 429 timeline (primary endpoint, per minute)",
        series=series,
    )
    return png


# ----------------------------------------------------------------------------
# Summary builder
# ----------------------------------------------------------------------------


def _build_summary(
    records: list[dict[str, Any]],
    *,
    policy: str,
    total_usd: float,
    primary_429_count: int,
    spillover_429_count: int,
) -> dict[str, Any]:
    """Aggregate per-request records into the summary JSON shape."""
    n = len(records)
    by_endpoint: dict[str, dict[str, int]] = {}
    p50_p95_latencies: list[float] = []
    total_input = 0
    total_cached = 0
    total_output = 0
    total_reasoning = 0
    spillover_n = 0
    for rec in records:
        usage = rec.get("usage") or {}
        in_t = int(usage.get("input_tokens", 0) or 0)
        in_det = usage.get("input_tokens_details") or {}
        cached = int(
            in_det.get("cached_tokens", 0) if isinstance(in_det, dict) else 0
        )
        out_t = int(usage.get("output_tokens", 0) or 0)
        out_det = usage.get("output_tokens_details") or {}
        reasoning = int(
            out_det.get("reasoning_tokens", 0) if isinstance(out_det, dict) else 0
        )
        endpoint = rec.get("endpoint_hit", "primary")
        slot = by_endpoint.setdefault(endpoint, {"input": 0, "cached": 0, "n": 0})
        slot["input"] += in_t
        slot["cached"] += cached
        slot["n"] += 1
        latency = rec.get("first_token_latency_ms")
        if isinstance(latency, (int, float)):
            p50_p95_latencies.append(float(latency))
        total_input += in_t
        total_cached += cached
        total_output += out_t
        total_reasoning += reasoning
        if endpoint == "spillover":
            spillover_n += 1

    cache_hit_by_endpoint = {}
    for ep, slot in by_endpoint.items():
        cache_hit_by_endpoint[ep] = {
            "n_requests": slot["n"],
            "input_tokens": slot["input"],
            "cached_tokens": slot["cached"],
            "cache_hit_ratio": (
                slot["cached"] / slot["input"] if slot["input"] > 0 else 0.0
            ),
        }
    cache_hit_overall = (
        total_cached / total_input if total_input > 0 else 0.0
    )
    spillover_real_429_fraction = (
        spillover_429_count / spillover_n if spillover_n > 0 else 0.0
    )

    return {
        "policy": policy,
        "n_requests": n,
        "primary_real_429_count": primary_429_count,
        "spillover_real_429_count": spillover_429_count,
        "spillover_real_429_fraction": spillover_real_429_fraction,
        "first_token_latency_ms_p50": _percentile(p50_p95_latencies, 50.0),
        "first_token_latency_ms_p95": _percentile(p50_p95_latencies, 95.0),
        "spillover_request_count": spillover_n,
        "spillover_request_fraction": (spillover_n / n) if n else 0.0,
        "totals": {
            "input_tokens": total_input,
            "cached_tokens": total_cached,
            "output_tokens": total_output,
            "reasoning_tokens": total_reasoning,
            "total_tokens": total_input + total_output,
            "total_usd": round(total_usd, 6),
        },
        "cache_hit_ratio_overall": cache_hit_overall,
        "cache_hit_ratio_by_endpoint": cache_hit_by_endpoint,
    }


# ----------------------------------------------------------------------------
# Top-level measurement runner
# ----------------------------------------------------------------------------


def _apply_smoke_overrides(cfg: DualExperimentConfig) -> DualExperimentConfig:
    """Smoke override: shrink duration_seconds to 180, sustain to 60."""
    sim = cfg.simulation
    new_sim = SimulationLoadConfig(
        duration_seconds=SMOKE_DURATION_SECONDS,
        warmup_duration_seconds=min(sim.warmup_duration_seconds, 60),
        warmup_tps=sim.warmup_tps,
        ramp_start_tps=sim.ramp_start_tps,
        ramp_end_tps=sim.ramp_end_tps,
        ramp_duration_seconds=min(sim.ramp_duration_seconds, 60),
        sustain_tps=sim.sustain_tps,
        sustain_duration_seconds=SMOKE_SUSTAIN_DURATION_SECONDS,
    )
    return dataclasses.replace(cfg, simulation=new_sim)


@dataclass
class MeasurementResult:
    """Top-level summary of one Phase 2 measurement run."""

    cells_written: int
    total_usd: float
    jsonl_path: pathlib.Path
    summary_path: pathlib.Path
    per_endpoint_chart_path: pathlib.Path | None
    aggregate_comparison_chart_path: pathlib.Path | None
    real_429_timeline_chart_path: pathlib.Path | None
    primary_real_429_count: int
    spillover_real_429_count: int
    spillover_real_429_fraction: float
    halt_reason: str | None
    cache_hit_ratio_mean: float
    spillover_request_fraction: float


def _select_routing_for_request(
    *,
    cfg: DualExperimentConfig,
    request_idx: int,
    arrival_mono: float,
    react_state: ReactiveState,
    proact_state: ProactiveState,
    rng: random.Random,
    last_react_obs: ReactiveObservation,
    last_proact_obs: ProactiveObservation,
    primary_followup_remaining: int,
) -> tuple[str, ReactiveState, ProactiveState, float | None, int]:
    """Decide which endpoint this request routes to before issuing.

    Phase 2 routing always runs the underlying Phase 1 policy decision
    (so the pure policy state evolves correctly across observations,
    including past 429s) and then overrides the decision when the Phase 2
    follow-up counter is positive. The counter exists so the Phase 2
    contract — "route the failing request AND the next N to spillover" —
    holds even before the policy has accumulated enough observations to
    keep the state on spillover on its own.

    Returns ``(decision, react_state, proact_state,
    policy_fraction_at_routing, primary_followup_remaining_after)``.
    """
    if cfg.policy_type == "reactive":
        decision, react_state = reactive_decide(
            last_react_obs, react_state, cfg.policy_params.reactive_params
        )
        policy_fraction_at_routing: float | None = None
    else:
        fraction, proact_state = proactive_decide(
            last_proact_obs,
            proact_state,
            cfg.policy_params.proactive_params,
        )
        decision = "spillover" if rng.random() < fraction else "primary"
        policy_fraction_at_routing = fraction

    if primary_followup_remaining > 0:
        # Force spillover for the next N requests after a primary 429,
        # regardless of what the pure policy chose. The pure policy
        # state has already been updated above so when the counter
        # reaches zero the policy is in the right shape (e.g., reactive
        # is already on_spillover because it saw the 429 observation).
        decision = "spillover"
        primary_followup_remaining -= 1

    return (
        decision,
        react_state,
        proact_state,
        policy_fraction_at_routing,
        primary_followup_remaining,
    )


async def _run_measurement_async(
    *,
    cfg: DualExperimentConfig,
    runs_dir: pathlib.Path,
    system_prompt: str,
    user_prompts: list[str],
    git_commit: str,
    dirty: bool,
    pricing: PaygPricing,
    pricing_snapshot_path: str,
    primary_endpoint_value: str,
    spillover_endpoint_value: str,
    primary_deployment: str,
    spillover_deployment: str,
    dry_run: bool,
    smoke: bool,
    timestamp_label: str,
    pricing_policy_provenance: dict[str, Any],
) -> MeasurementResult:
    schedule = _build_arrival_schedule(cfg.simulation)
    if not schedule:
        raise ValueError(
            "arrival schedule is empty; simulation duration / TPS produce zero "
            "requests"
        )

    system_sha = sha256_text(system_prompt)
    policy_params_payload = {
        "type": cfg.policy_type,
        "reactive_params": dataclasses.asdict(cfg.policy_params.reactive_params),
        "proactive_params": dataclasses.asdict(
            cfg.policy_params.proactive_params
        ),
        "treat_real_429_as_trigger": cfg.policy_params.treat_real_429_as_trigger,
        "real_429_observed_action": cfg.policy_params.real_429_observed_action,
        "real_429_followup_requests": cfg.policy_params.real_429_followup_requests,
    }
    policy_params_sha = _sha256_json(policy_params_payload)

    logger.info(
        "MEASURE_BEGIN experiment=%s policy=%s smoke=%s dry_run=%s "
        "primary_deployment=%s spillover_deployment=%s "
        "system_prompt_sha256=%s policy_params_sha256=%s "
        "scheduled_requests=%d",
        cfg.experiment_id,
        cfg.policy_type,
        smoke,
        dry_run,
        primary_deployment,
        spillover_deployment,
        system_sha,
        policy_params_sha,
        len(schedule),
    )

    jsonl_path = (
        runs_dir / f"{timestamp_label}_{cfg.experiment_id}_{cfg.policy_type}.jsonl"
    )
    summary_path = pathlib.Path(str(jsonl_path) + ".summary.json")
    if jsonl_path.exists():
        raise FileExistsError(
            f"JSONL target already exists: {jsonl_path} (append-only)"
        )
    runs_dir.mkdir(parents=True, exist_ok=True)

    primary_client: Any = None
    spillover_client: Any = None
    if not dry_run:
        primary_client = _build_live_client(
            endpoint_value=primary_endpoint_value
        )
        spillover_client = _build_live_client(
            endpoint_value=spillover_endpoint_value
        )
        # Pre-flight reachability gate. Aborts the entire run on failure.
        preflight_results = await preflight_reachability(
            primary_client=primary_client,
            primary_deployment=primary_deployment,
            spillover_client=spillover_client,
            spillover_deployment=spillover_deployment,
        )
        # Append a timestamped row to PREFLIGHT_LOG.md so a reviewer can
        # confirm reachability evidence per run. Uses env-var names /
        # deployment aliases only — no secrets, no endpoint URLs. Safe
        # no-op (with warning) if the owner-prefilled log is absent.
        _append_preflight_log_record(
            preflight_log_path=runs_dir.parent / "PREFLIGHT_LOG.md",
            timestamp_iso=_iso8601_z(_utc_now()),
            git_commit=git_commit,
            experiment_id=cfg.experiment_id,
            primary_deployment_env=cfg.primary.deployment_template,
            spillover_deployment_env=cfg.spillover.deployment_template,
            reachability_results=preflight_results,
        )

    react_state = ReactiveState()
    proact_state = ProactiveState()
    rng = random.Random(cfg.corpus_seed + 1)
    total_usd = 0.0
    primary_429_count = 0
    spillover_429_count = 0
    spillover_request_count = 0
    halt_reason: str | None = None
    records: list[dict[str, Any]] = []
    primary_followup_remaining = 0

    sim_started_mono = time.monotonic()
    last_obs_for_proactive = ProactiveObservation(
        request_idx=-1,
        first_token_latency_ms=None,
        monotonic_time_s=sim_started_mono,
        in_warmup=True,
    )
    last_obs_for_reactive = ReactiveObservation(
        request_idx=-1,
        first_token_latency_ms=None,
        real_429_observed=False,
        monotonic_time_s=sim_started_mono,
    )

    with jsonl_path.open("w", encoding="utf-8") as out_fh:
        for idx, (target_offset, phase) in enumerate(schedule):
            scheduled_mono = sim_started_mono + target_offset
            now_mono = time.monotonic()
            sleep_s = scheduled_mono - now_mono
            if sleep_s > 0 and not dry_run:
                await asyncio.sleep(min(sleep_s, 60.0))
            arrival_mono = (
                time.monotonic() if not dry_run else scheduled_mono
            )

            (
                decision,
                react_state,
                proact_state,
                policy_fraction_at_routing,
                primary_followup_remaining,
            ) = _select_routing_for_request(
                cfg=cfg,
                request_idx=idx,
                arrival_mono=arrival_mono,
                react_state=react_state,
                proact_state=proact_state,
                rng=rng,
                last_react_obs=last_obs_for_reactive,
                last_proact_obs=last_obs_for_proactive,
                primary_followup_remaining=primary_followup_remaining,
            )

            deployment_used = (
                primary_deployment if decision == "primary"
                else spillover_deployment
            )
            user_text = user_prompts[idx % len(user_prompts)]
            call_kwargs = _build_call_kwargs(
                deployment=deployment_used,
                system_prompt=system_prompt,
                user_text=user_text,
                effort=cfg.effort,
                call_params=cfg.call_params,
                prompt_cache_key=cfg.prompt_cache_key,
                prompt_cache_retention=cfg.prompt_cache_retention,
            )

            spillover_429_this_request = 0
            if dry_run:
                usage_dict = _zero_usage_dict()
                first_token_latency_ms = 0.0
                total_latency_ms = 0.0
                real_429 = False
                headers_parsed = _parse_response_headers(None)
                cell_usd = 0.0
            else:
                if decision == "primary":
                    res = await _call_primary(
                        client=primary_client,
                        call_kwargs=call_kwargs,
                        request_idx=idx,
                    )
                else:
                    res = await _call_spillover_with_retry(
                        client=spillover_client,
                        call_kwargs=call_kwargs,
                        request_idx=idx,
                    )
                if res["raised"] is not None:
                    logger.exception(
                        "REQUEST_FAILED request_idx=%d policy=%s endpoint=%s",
                        idx,
                        cfg.policy_type,
                        decision,
                    )
                    raise res["raised"]
                usage_dict = res["usage"] or _zero_usage_dict()
                first_token_latency_ms = res["first_token_latency_ms"]
                total_latency_ms = res["total_latency_ms"]
                real_429 = res["real_429_observed"]
                headers_parsed = res["headers"]
                if decision == "spillover":
                    spillover_429_this_request = res.get(
                        "spillover_429_count", 0
                    )
                    if spillover_429_this_request > 0:
                        spillover_429_count += 1
                tu = _usage_to_token_usage(usage_dict)
                cell_usd = payg_cost_per_call(
                    tu, pricing, model=cfg.primary.family
                ).usd_per_request
                total_usd += cell_usd

                if decision == "primary" and real_429:
                    primary_429_count += 1
                    # Phase 2 contract: do NOT silently retry primary
                    # 429s. Route the failing request's payload AND the
                    # next stay_on_spillover_min_requests (reactive) /
                    # real_429_followup_requests (proactive) to
                    # spillover.
                    if cfg.policy_type == "reactive":
                        primary_followup_remaining = (
                            cfg.policy_params.reactive_params.stay_on_spillover_min_requests
                        )
                        # Eagerly feed the 429 to the underlying reactive
                        # policy so its state machine knows we diverged.
                        # Without this, the spillover re-issue's healthy
                        # observation would overwrite the 429 in the
                        # tracked last_obs and reactive_decide would
                        # never see the 429.
                        _, react_state = reactive_decide(
                            ReactiveObservation(
                                request_idx=idx,
                                first_token_latency_ms=first_token_latency_ms,
                                real_429_observed=True,
                                monotonic_time_s=arrival_mono,
                            ),
                            react_state,
                            cfg.policy_params.reactive_params,
                        )
                    else:
                        primary_followup_remaining = (
                            cfg.policy_params.real_429_followup_requests
                        )
                    # Re-issue THIS request on spillover. The original
                    # primary attempt is also recorded as its own row so
                    # the 429 is preserved in the JSONL.
                    primary_record_for_429 = _assemble_record(
                        cfg=cfg,
                        idx=idx,
                        endpoint_value=primary_endpoint_value,
                        decision="primary",
                        deployment_used=primary_deployment,
                        usage_dict=usage_dict,
                        first_token_latency_ms=first_token_latency_ms,
                        total_latency_ms=total_latency_ms,
                        retry_count=0,
                        real_429=True,
                        headers_parsed=headers_parsed,
                        primary_429_count_running_total=primary_429_count,
                        react_state=react_state,
                        policy_fraction_at_routing=policy_fraction_at_routing,
                        relative_time_s=arrival_mono - sim_started_mono,
                        phase=phase,
                        git_commit=git_commit,
                        dirty=dirty,
                        system_sha=system_sha,
                        policy_params_sha=policy_params_sha,
                        pricing_snapshot_path=pricing_snapshot_path,
                        dry_run=dry_run,
                        spillover_429_count_for_request=0,
                        sub_request_role="primary_429",
                    )
                    out_fh.write(
                        json.dumps(primary_record_for_429, sort_keys=True) + "\n"
                    )
                    out_fh.flush()
                    records.append(primary_record_for_429)
                    last_obs_for_reactive = ReactiveObservation(
                        request_idx=idx,
                        first_token_latency_ms=first_token_latency_ms,
                        real_429_observed=True,
                        monotonic_time_s=arrival_mono,
                    )
                    last_obs_for_proactive = ProactiveObservation(
                        request_idx=idx,
                        first_token_latency_ms=first_token_latency_ms,
                        monotonic_time_s=arrival_mono,
                        in_warmup=(phase == "warmup"),
                    )
                    # Re-issue on spillover.
                    decision = "spillover"
                    deployment_used = spillover_deployment
                    call_kwargs_sp = _build_call_kwargs(
                        deployment=deployment_used,
                        system_prompt=system_prompt,
                        user_text=user_text,
                        effort=cfg.effort,
                        call_params=cfg.call_params,
                        prompt_cache_key=cfg.prompt_cache_key,
                        prompt_cache_retention=cfg.prompt_cache_retention,
                    )
                    res2 = await _call_spillover_with_retry(
                        client=spillover_client,
                        call_kwargs=call_kwargs_sp,
                        request_idx=idx,
                    )
                    if res2["raised"] is not None:
                        logger.exception(
                            "SPILLOVER_REISSUE_FAILED request_idx=%d", idx
                        )
                        raise res2["raised"]
                    usage_dict = res2["usage"] or _zero_usage_dict()
                    first_token_latency_ms = res2["first_token_latency_ms"]
                    total_latency_ms = res2["total_latency_ms"]
                    real_429 = res2["real_429_observed"]
                    headers_parsed = res2["headers"]
                    spillover_429_this_request = res2.get(
                        "spillover_429_count", 0
                    )
                    if spillover_429_this_request > 0:
                        spillover_429_count += 1
                    tu2 = _usage_to_token_usage(usage_dict)
                    cell_usd2 = payg_cost_per_call(
                        tu2, pricing, model=cfg.spillover.family
                    ).usd_per_request
                    total_usd += cell_usd2

            if decision == "spillover":
                spillover_request_count += 1

            record_endpoint_value = (
                primary_endpoint_value if decision == "primary"
                else spillover_endpoint_value
            )
            record = _assemble_record(
                cfg=cfg,
                idx=idx,
                endpoint_value=record_endpoint_value,
                decision=decision,
                deployment_used=deployment_used,
                usage_dict=usage_dict,
                first_token_latency_ms=first_token_latency_ms,
                total_latency_ms=total_latency_ms,
                retry_count=0,
                real_429=real_429,
                headers_parsed=headers_parsed,
                primary_429_count_running_total=primary_429_count,
                react_state=react_state,
                policy_fraction_at_routing=policy_fraction_at_routing,
                relative_time_s=arrival_mono - sim_started_mono,
                phase=phase,
                git_commit=git_commit,
                dirty=dirty,
                system_sha=system_sha,
                policy_params_sha=policy_params_sha,
                pricing_snapshot_path=pricing_snapshot_path,
                dry_run=dry_run,
                spillover_429_count_for_request=spillover_429_this_request,
                sub_request_role="primary_request",
            )
            out_fh.write(json.dumps(record, sort_keys=True) + "\n")
            out_fh.flush()
            records.append(record)

            last_obs_for_reactive = ReactiveObservation(
                request_idx=idx,
                first_token_latency_ms=first_token_latency_ms,
                real_429_observed=False,  # the spillover re-issue handled it
                monotonic_time_s=arrival_mono,
            )
            last_obs_for_proactive = ProactiveObservation(
                request_idx=idx,
                first_token_latency_ms=first_token_latency_ms,
                monotonic_time_s=arrival_mono,
                in_warmup=(phase == "warmup"),
            )

            if total_usd >= cfg.budget_hard_ceiling_usd:
                halt_reason = "budget_hard_ceiling"
                logger.error(
                    "BUDGET_HALT experiment=%s total_usd=%.4f ceiling_usd=%.4f",
                    cfg.experiment_id,
                    total_usd,
                    cfg.budget_hard_ceiling_usd,
                )
                break

            if (
                spillover_request_count >= SPILLOVER_REAL_429_MIN_REQUESTS
                and (spillover_429_count / spillover_request_count)
                > SPILLOVER_REAL_429_HALT_FRACTION
            ):
                halt_reason = "spillover_real_429_rate_exceeded"
                logger.error(
                    "SPILLOVER_429_HALT spillover_429=%d spillover_n=%d "
                    "fraction=%.4f",
                    spillover_429_count,
                    spillover_request_count,
                    spillover_429_count / spillover_request_count,
                )
                break

    summary = _build_summary(
        records,
        policy=cfg.policy_type,
        total_usd=total_usd,
        primary_429_count=primary_429_count,
        spillover_429_count=spillover_429_count,
    )
    summary["experiment_id"] = cfg.experiment_id
    summary["git_commit"] = git_commit
    summary["dirty"] = dirty
    summary["api_version"] = FOUNDRY_API_VERSION
    summary["pricing_snapshot_path"] = pricing_snapshot_path
    summary["pricing_source_url"] = pricing.source_url
    summary["pricing_accessed_date"] = pricing.accessed_date
    summary["pricing_policy"] = pricing_policy_provenance
    summary["halt_reason"] = halt_reason
    summary["smoke"] = smoke
    summary["dry_run"] = dry_run
    summary["jsonl_path"] = str(jsonl_path)
    summary["primary_deployment"] = primary_deployment
    summary["spillover_deployment"] = spillover_deployment
    summary["system_prompt_sha256"] = system_sha
    summary["policy_params_sha256"] = policy_params_sha
    summary["scheduled_request_count"] = len(schedule)
    summary["completed_request_count"] = len(records)
    with summary_path.open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)

    chart_dir = (pathlib.Path("results") / "dual-spillover-curves").resolve()
    per_endpoint_chart = _emit_per_endpoint_chart(
        chart_dir=chart_dir,
        policy=cfg.policy_type,
        records=records,
        window_s=CACHE_ROLLING_WINDOW_S,
    )

    aggregate_chart: pathlib.Path | None = None
    timeline_chart: pathlib.Path | None = None
    if records:
        other_policy = (
            "proactive" if cfg.policy_type == "reactive" else "reactive"
        )
        sibling_jsonl = _find_sibling_policy_jsonl(
            runs_dir, jsonl_path, other_policy
        )
        sibling_records: list[dict[str, Any]] = []
        if sibling_jsonl is not None:
            sibling_records = _load_jsonl_records(sibling_jsonl)
            aggregate_chart = _emit_aggregate_comparison_chart(
                chart_dir=chart_dir,
                current_policy=cfg.policy_type,
                current_records=records,
                sibling_policy=other_policy,
                sibling_records=sibling_records,
                window_s=CACHE_ROLLING_WINDOW_S,
            )
        timeline_chart = _emit_real_429_timeline_chart(
            chart_dir=chart_dir,
            current_policy=cfg.policy_type,
            current_records=records,
            sibling_policy=(other_policy if sibling_records else None),
            sibling_records=sibling_records or None,
        )

    cache_hit_overall = summary["cache_hit_ratio_overall"]
    spillover_fraction = summary["spillover_request_fraction"]
    spillover_real_429_fraction = summary["spillover_real_429_fraction"]

    if halt_reason == "budget_hard_ceiling":
        raise BudgetHaltError(
            f"running USD {total_usd:.4f} >= ceiling "
            f"{cfg.budget_hard_ceiling_usd:.4f}"
        )
    if halt_reason == "spillover_real_429_rate_exceeded":
        raise SpilloverHalt429Error(
            f"spillover real 429 rate {spillover_429_count}/"
            f"{spillover_request_count} > "
            f"{SPILLOVER_REAL_429_HALT_FRACTION:.2%}"
        )

    # Live-smoke success criteria. Enforced ONLY when --smoke is set
    # AND we actually issued live calls (dry_run=False). Both sides of
    # the criterion must hold; a clear, typed error names the side that
    # failed. The CLI maps SmokeCriteriaError to exit code 5.
    if smoke and not dry_run:
        if primary_429_count < 1:
            raise SmokeCriteriaError(
                "SMOKE_PRIMARY_NO_429: live smoke run requires "
                "primary_real_429_count >= 1; observed "
                f"{primary_429_count}. The throttled primary deployment "
                f"({primary_deployment!r}, scheduled_requests="
                f"{len(schedule)}) did not throttle within the smoke "
                "window. Either the primary TPM is misconfigured (too "
                "high) or the workload shape is too light to exercise "
                "the rebuild-cost mechanism. Fix the deployment / "
                "workload before launching the full run."
            )
        if spillover_429_count != 0:
            raise SmokeCriteriaError(
                "SMOKE_SPILLOVER_NONZERO_429: live smoke run requires "
                "spillover_real_429_count == 0; observed "
                f"{spillover_429_count} on spillover deployment "
                f"({spillover_deployment!r}). The spillover TPM is "
                "misconfigured (too low) — Phase 2 needs a clean "
                "spillover pool with zero throttling during the smoke "
                "window. Raise spillover TPM (or lower workload) and "
                "re-run smoke before launching the full run."
            )

    return MeasurementResult(
        cells_written=len(records),
        total_usd=total_usd,
        jsonl_path=jsonl_path,
        summary_path=summary_path,
        per_endpoint_chart_path=per_endpoint_chart,
        aggregate_comparison_chart_path=aggregate_chart,
        real_429_timeline_chart_path=timeline_chart,
        primary_real_429_count=primary_429_count,
        spillover_real_429_count=spillover_429_count,
        spillover_real_429_fraction=spillover_real_429_fraction,
        halt_reason=halt_reason,
        cache_hit_ratio_mean=cache_hit_overall,
        spillover_request_fraction=spillover_fraction,
    )


def _assemble_record(
    *,
    cfg: DualExperimentConfig,
    idx: int,
    endpoint_value: str,
    decision: str,
    deployment_used: str,
    usage_dict: dict[str, Any],
    first_token_latency_ms: float,
    total_latency_ms: float,
    retry_count: int,
    real_429: bool,
    headers_parsed: dict[str, Any],
    primary_429_count_running_total: int,
    react_state: ReactiveState,
    policy_fraction_at_routing: float | None,
    relative_time_s: float,
    phase: str,
    git_commit: str,
    dirty: bool,
    system_sha: str,
    policy_params_sha: str,
    pricing_snapshot_path: str,
    dry_run: bool,
    spillover_429_count_for_request: int,
    sub_request_role: str,
) -> dict[str, Any]:
    """Build one Phase 2 per-request JSONL record.

    The Phase 1 field ``simulated_primary_throttle_state`` is INTENTIONALLY
    absent. The Phase 2-specific additions: ``deployment_used``,
    ``cache_pool``, ``real_429_observed``, ``primary_429_count_running_total``,
    ``primary_health_check_state`` (reactive only), the parsed
    ``retry_after_ms`` / ``retry_after_seconds`` and three
    ``x_ms_spillover_*`` header fields, and ``prompt_cache_key`` /
    ``prompt_cache_retention``.
    """
    in_det = usage_dict.get("input_tokens_details") or {}
    cached_tokens_canonical = (
        int(in_det.get("cached_tokens", 0) or 0)
        if isinstance(in_det, dict)
        else 0
    )
    input_tokens_canonical = int(usage_dict.get("input_tokens", 0) or 0)
    primary_health_check_state: str | None = None
    if cfg.policy_type == "reactive":
        primary_health_check_state = (
            "on_spillover" if react_state.on_spillover else "primary"
        )
    now_iso = _iso8601_z(_utc_now())
    return {
        "experiment_id": cfg.experiment_id,
        "git_commit": git_commit,
        "dirty": dirty,
        "timestamp_utc": now_iso,
        "wallclock_timestamp_iso": now_iso,
        "endpoint": endpoint_value,
        "auth_mode": "entra",
        "api_version": FOUNDRY_API_VERSION,
        "model": cfg.primary.family,
        "deployment_used": deployment_used,
        "cache_pool": deployment_used,
        "policy": cfg.policy_type,
        "endpoint_hit": decision,
        "request_idx": idx,
        "relative_time_s": relative_time_s,
        "phase": phase,
        "first_token_latency_ms": first_token_latency_ms,
        "total_latency_ms": total_latency_ms,
        "retry_count": retry_count,
        "usage": usage_dict,
        "canonical_cached_tokens": cached_tokens_canonical,
        "canonical_input_tokens": input_tokens_canonical,
        "policy_action_taken": decision,
        "simulated_proactive_fraction_at_routing": policy_fraction_at_routing,
        "primary_health_check_state": primary_health_check_state,
        "real_429_observed": real_429,
        "primary_429_count_running_total": primary_429_count_running_total,
        "spillover_429_count_for_request": spillover_429_count_for_request,
        "sub_request_role": sub_request_role,
        "retry_after_ms": headers_parsed.get("retry_after_ms"),
        "retry_after_seconds": headers_parsed.get("retry_after_seconds"),
        "x_ms_spillover_from_deployment": headers_parsed.get(
            "x_ms_spillover_from_deployment"
        ),
        "x_ms_deployment_name": headers_parsed.get("x_ms_deployment_name"),
        "x_ms_spillover_error": headers_parsed.get("x_ms_spillover_error"),
        "prompt_cache_key": cfg.prompt_cache_key,
        "prompt_cache_retention": cfg.prompt_cache_retention,
        "dry_run": dry_run,
        "cell_metadata": {
            "system_prompt_sha256": system_sha,
            "corpus_seed": cfg.corpus_seed,
            "policy_params_sha256": policy_params_sha,
        },
        "pricing_snapshot_path": pricing_snapshot_path,
    }


def run_measurement(
    *,
    cfg: DualExperimentConfig,
    benchmarks_root: pathlib.Path,
    pricing_dir: pathlib.Path,
    dry_run: bool,
    smoke: bool,
    allow_dirty: bool,
    env: dict[str, str] | None = None,
    today: datetime.date | None = None,
    pricing_policy: str = LIVE_MEASUREMENT,
) -> MeasurementResult:
    """Synchronous wrapper around the async measurement core."""
    src_env = env if env is not None else dict(os.environ)
    if smoke:
        cfg = _apply_smoke_overrides(cfg)

    verified_pricing = verify_campaign_pricing(
        snapshot_path=cfg.pricing_snapshot_path,
        model_family=cfg.primary.family,
        model_version=cfg.primary.version,
        policy_mode=pricing_policy,
        today=today,
    )
    verified_pricing.policy.require_offline_if_historical(dry_run=dry_run)
    pricing = verified_pricing.snapshot
    snapshot_path = cfg.pricing_snapshot_path

    if dry_run:
        primary_endpoint_value = src_env.get(cfg.primary.endpoint_env, "")
        spillover_endpoint_value = src_env.get(cfg.spillover.endpoint_env, "")
        primary_match = _ENV_TEMPLATE_RE.fullmatch(cfg.primary.deployment_template)
        spillover_match = _ENV_TEMPLATE_RE.fullmatch(
            cfg.spillover.deployment_template
        )
        primary_deployment = (
            src_env.get(primary_match.group(1), "")
            if primary_match
            else cfg.primary.deployment_name
        ) or cfg.primary.deployment_name
        spillover_deployment = (
            src_env.get(spillover_match.group(1), "")
            if spillover_match
            else cfg.spillover.deployment_name
        ) or cfg.spillover.deployment_name
    else:
        primary_endpoint_value = _require_env(
            cfg.primary.endpoint_env, env=src_env
        )
        spillover_endpoint_value = _require_env(
            cfg.spillover.endpoint_env, env=src_env
        )
        primary_deployment = _resolve_env_template(
            cfg.primary.deployment_template, env=src_env
        )
        spillover_deployment = _resolve_env_template(
            cfg.spillover.deployment_template, env=src_env
        )
    if primary_endpoint_value != spillover_endpoint_value:
        # Phase 2 happens to use one endpoint base; future variants may
        # diverge. Log + permit. Each client is built with its own
        # endpoint value below (no silent collapse to the primary).
        logger.info(
            "DUAL_ENDPOINT_BASES_DIFFER primary_env=%s spillover_env=%s",
            cfg.primary.endpoint_env,
            cfg.spillover.endpoint_env,
        )

    if not primary_deployment or not spillover_deployment:
        raise EndpointMisconfiguredError(
            "primary or spillover deployment resolved to empty"
        )
    if primary_deployment == spillover_deployment:
        raise EndpointMisconfiguredError(
            f"primary and spillover deployments resolved to the same name "
            f"({primary_deployment!r}); Phase 2 requires DIFFERENT deployment "
            f"names so the cache pools are physically separate"
        )

    schedule = _build_arrival_schedule(cfg.simulation)
    est_n = len(schedule)
    est_input = cfg.target_system_prompt_tokens + 50.0
    est_per_call = payg_cost_per_call(
        TokenUsage(
            input_tokens=est_input,
            cached_tokens=0.0,
            output_tokens=200.0,
            reasoning_tokens=50.0,
        ),
        pricing,
        model=cfg.primary.family,
    ).usd_per_request
    est_total = est_per_call * est_n
    logger.info(
        "PRE_RUN_ESTIMATE experiment=%s scheduled_requests=%d usd_per_call=%.6f "
        "total_usd=%.4f snapshot=%s pricing_source_url=%s accessed_date=%s",
        cfg.experiment_id,
        est_n,
        est_per_call,
        est_total,
        snapshot_path,
        pricing.source_url,
        pricing.accessed_date,
    )

    max_per_benchmark_raw = src_env.get("MAX_COST_PER_BENCHMARK_USD")
    if max_per_benchmark_raw is not None:
        try:
            max_per_benchmark = float(max_per_benchmark_raw)
        except ValueError as exc:
            raise EndpointMisconfiguredError(
                f"MAX_COST_PER_BENCHMARK_USD must parse as float; "
                f"got {max_per_benchmark_raw!r}"
            ) from exc
        if (
            cfg.budget_estimated_usd > max_per_benchmark
            and not cfg.budget_confirmed
        ):
            raise BudgetHaltError(
                f"experiment.budget.estimated_cost_usd "
                f"({cfg.budget_estimated_usd:.2f}) exceeds "
                f"MAX_COST_PER_BENCHMARK_USD ({max_per_benchmark:.2f}); "
                f"set budget.confirmed: true in the YAML to override"
            )

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

    system_prompt = build_system_prompt(
        corpus_p,
        corpus_seed=cfg.corpus_seed,
        target_tokens=cfg.target_system_prompt_tokens,
    )
    user_prompts = _load_user_prompts(user_prompts_p)

    runs_dir = benchmark_dir / "runs"
    timestamp_label = _utc_now().strftime("%Y%m%dT%H%M%SZ")

    return asyncio.run(
        _run_measurement_async(
            cfg=cfg,
            runs_dir=runs_dir,
            system_prompt=system_prompt,
            user_prompts=user_prompts,
            git_commit=git_commit,
            dirty=dirty,
            pricing=pricing,
            pricing_snapshot_path=str(snapshot_path),
            primary_endpoint_value=primary_endpoint_value,
            spillover_endpoint_value=spillover_endpoint_value,
            primary_deployment=primary_deployment,
            spillover_deployment=spillover_deployment,
            dry_run=dry_run,
            smoke=smoke,
            timestamp_label=timestamp_label,
            pricing_policy_provenance=verified_pricing.provenance(),
        )
    )


# ----------------------------------------------------------------------------
# CLI plumbing
# ----------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scripts.measure_dual_spillover",
        description=(
            "Phase 2 dual-endpoint spillover measurement (Hypothesis G "
            "weak-form, higher fidelity). Streams per-request JSONL + "
            "summary; emits four charts under "
            "results/dual-spillover-curves/."
        ),
    )
    p.add_argument("--experiment", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--smoke",
        action="store_true",
        help=(
            f"Override duration_seconds to {SMOKE_DURATION_SECONDS} and "
            f"sustain_duration_seconds to {SMOKE_SUSTAIN_DURATION_SECONDS}."
        ),
    )
    p.add_argument("--allow-dirty", action="store_true")
    p.add_argument("--benchmarks-root", default="benchmarks")
    p.add_argument("--pricing-dir", default="pricing")
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


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.log_level)

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
            pricing_dir=pathlib.Path(args.pricing_dir),
            dry_run=args.dry_run,
            smoke=args.smoke,
            allow_dirty=args.allow_dirty,
            pricing_policy=args.pricing_policy,
        )
    except PreflightReachabilityError as exc:
        logger.error("PREFLIGHT_REACHABILITY_FAILED %s", exc)
        return EXIT_PREFLIGHT
    except EndpointMisconfiguredError as exc:
        logger.error("ENDPOINT_MISCONFIGURED %s", exc)
        return EXIT_AUTH
    except CorpusMissingError as exc:
        logger.error("DATASET_MISSING %s", exc)
        return EXIT_DATASET
    except BudgetHaltError as exc:
        logger.error("BUDGET_HALT %s", exc)
        return EXIT_BUDGET
    except SpilloverHalt429Error as exc:
        logger.error("SPILLOVER_429_HALT %s", exc)
        return EXIT_BUDGET
    except SmokeCriteriaError as exc:
        logger.error("SMOKE_CRITERIA_FAILED %s", exc)
        return EXIT_SMOKE
    except PricingPolicyError as exc:
        logger.error("PRICING_POLICY_REFUSED %s", exc)
        return EXIT_PRICING

    summary_line = (
        f"\n=== measure_dual_spillover summary ===\n"
        f"experiment_id          : {cfg.experiment_id}\n"
        f"policy                 : {cfg.policy_type}\n"
        f"smoke                  : {args.smoke}\n"
        f"dry_run                : {args.dry_run}\n"
        f"completed_cells        : {result.cells_written}\n"
        f"total_usd              : ${result.total_usd:.4f}\n"
        f"primary_real_429_count : {result.primary_real_429_count}\n"
        f"spillover_real_429     : {result.spillover_real_429_count} "
        f"({result.spillover_real_429_fraction:.2%})\n"
        f"cache_hit_overall      : {result.cache_hit_ratio_mean:.4f}\n"
        f"spillover_fraction     : {result.spillover_request_fraction:.4f}\n"
        f"jsonl                  : {result.jsonl_path}\n"
        f"summary_json           : {result.summary_path}\n"
        f"per_endpoint_chart     : {result.per_endpoint_chart_path}\n"
        f"aggregate_comparison   : {result.aggregate_comparison_chart_path or '(no sibling JSONL yet)'}\n"
        f"real_429_timeline      : {result.real_429_timeline_chart_path}\n"
        f"======================================="
    )
    print(summary_line)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
