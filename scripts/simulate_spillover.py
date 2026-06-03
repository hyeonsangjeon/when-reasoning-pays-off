"""scripts/simulate_spillover.py — Phase 1 spillover policy simulator.

This module implements the Phase 1 (single-endpoint, simulated-throttle)
spillover-policy simulator described in the doc-07 cache-hit-degradation
sub-spec and called for by Tasks 012 / 014. It is a sibling to
``scripts/run_benchmark.py`` (which it never imports or modifies) and reuses
the same Foundry v1 client construction discipline — Entra ID via
``DefaultAzureCredential``, ``AsyncOpenAI`` against
``<endpoint>/openai/v1/``, ``api_version="preview"`` recorded verbatim in
every JSONL record, no API-key code path.

Scope reminder. This file produces evidence for **weak-form Hypothesis G**
only (the saturation-sensitive cache dip and the shape of the
reactive-vs-proactive recovery curves), not the strong form (cross-pool
reactive thrash as root cause), which customer field measurement
disproved. The simulator's PTU throttle state is bookkeeping for the
policy router — the canonical ``cached_tokens`` always comes from
``usage.input_tokens_details.cached_tokens`` returned by the API, never
from the simulator's bookkeeping.

CLI contract::

    python -m scripts.simulate_spillover \
        --experiment experiments/exp004_spillover_baseline_reactive.yaml \
        [--dry-run] [--smoke]

Exit codes:
    0 = success
    1 = budget violation OR real_429 rate > 5% halt
    2 = auth/endpoint misconfiguration
    3 = dataset/corpus/prompt files missing
    4 = experiment YAML invalid

Key design properties (verbatim contract from Task 012):
  * Two pure-function policy logic blocks (``reactive_decide`` and
    ``proactive_decide``) that take ``(observation, state, params)`` and
    return ``(routing_decision, new_state)``. They are unit-tested
    independently of network I/O.
  * Deterministic system prompt construction from
    ``benchmarks/04-spillover-simulation/system_prompt_corpus.json`` plus
    a ``corpus_seed`` integer. Same seed → byte-identical 30K-token
    system prompt across runs. SHA-256 logged at run start.
  * Per-request JSONL streaming output. Each record carries the full
    ``response.usage`` object plus simulator bookkeeping fields:
    ``endpoint_hit``, ``first_token_latency_ms``, ``total_latency_ms``,
    ``simulated_primary_throttle_state``, ``policy_action_taken``,
    ``prompt_cache_key`` (passed value), ``retry_after_ms`` and
    ``retry_after_seconds`` (parsed from 429 response headers), and a
    ``cell_metadata`` block with ``system_prompt_sha256``, ``corpus_seed``,
    and ``policy_params_sha256``.
  * Budget enforcement: pre-run estimate via ``scripts.cost_calculator``;
    mid-run running USD check halts the simulation at
    ``budget.hard_ceiling_usd``.
  * ``--dry-run`` produces zero outbound HTTPS calls. Synthetic zero-usage
    records still stream to JSONL so downstream tooling can be exercised
    end-to-end without spend.
  * Real 429 responses count toward an anomaly tally; if the running
    fraction exceeds ``REAL_429_HALT_FRACTION`` (5%), the simulator halts
    and returns exit code 1 (signal of misconfigured TPM threshold).
  * Two charts per run plus a comparison chart, each with a sibling CSV.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import csv
import dataclasses
import datetime
import hashlib
import json
import logging
import math
import os
import pathlib
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import yaml

from scripts._pricing_types import PaygPricing, TokenUsage
from scripts.cost_calculator import (
    load_payg_pricing,
    payg_cost_per_call,
    resolve_active_snapshot,
)

__all__ = [
    "BudgetHaltError",
    "ExperimentConfig",
    "ProactivePolicyParams",
    "ProactiveState",
    "ReactivePolicyParams",
    "ReactiveState",
    "Real429HaltError",
    "SimulationResult",
    "build_system_prompt",
    "load_experiment",
    "main",
    "proactive_decide",
    "reactive_decide",
    "run_simulation",
]


logger = logging.getLogger("scripts.simulate_spillover")

# Foundry v1 API version literal (recorded verbatim in JSONL).
FOUNDRY_API_VERSION = "preview"

# Token-count heuristic divisor (chars / 4 ≈ tokens).
DEFAULT_TOKEN_ESTIMATE_DIVISOR = 4

# Halt threshold for real (Azure-emitted) 429 responses as a fraction of
# completed requests. Exceeding this is a signal of a misconfigured
# simulated TPM threshold (the simulator is supposed to model throttle
# internally, NOT to push the real deployment into throttling).
REAL_429_HALT_FRACTION = 0.05

# Smoke-mode overrides for total + sustain duration.
SMOKE_DURATION_SECONDS = 180
SMOKE_SUSTAIN_DURATION_SECONDS = 60

# Rolling-window size (seconds) for the cache-hit-ratio chart.
CACHE_ROLLING_WINDOW_S = 60.0

# Retry/backoff for live 429s. Treated separately from the anomaly tally:
# we retry per the standard backoff but the FIRST 429 observation per
# attempt counts in real_429_count regardless of retry outcome.
RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_BASE_DELAY_S = 1.0

EXIT_OK = 0
EXIT_BUDGET = 1
EXIT_AUTH = 2
EXIT_DATASET = 3
EXIT_CONFIG = 4


# ----------------------------------------------------------------------------
# Typed errors
# ----------------------------------------------------------------------------


class BudgetHaltError(RuntimeError):
    """Running USD total crossed ``budget.hard_ceiling_usd``."""


class Real429HaltError(RuntimeError):
    """Real 429 rate exceeded ``REAL_429_HALT_FRACTION``."""


class EndpointMisconfiguredError(RuntimeError):
    """Required Azure env vars are missing or empty."""


class CorpusMissingError(FileNotFoundError):
    """Corpus or user-prompts file cannot be found / parsed."""


# ----------------------------------------------------------------------------
# Pure policy data + logic
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ReactivePolicyParams:
    """Reactive spillover policy parameters (verbatim Task 012 contract)."""

    first_token_timeout_ms: float = 3000.0
    stay_on_spillover_min_requests: int = 10
    health_check_interval_ms: float = 30000.0


@dataclass(frozen=True)
class ProactivePolicyParams:
    """Proactive spillover policy parameters (verbatim Task 012 contract).

    Attributes:
        latency_window_size: Number of recent first-token latencies kept
            in the sliding window for the p95 calculation.
        p95_threshold_multiplier: Multiplier applied to the warm-up
            baseline p95 to derive the breach threshold.
        spillover_fraction_max: Upper bound (cap) on the spillover
            fraction; a fully-saturated router still routes at most this
            fraction of traffic to spillover.
        measurement_window_seconds: Minimum monotonic-clock spacing
            between successive fraction re-evaluations. Implements the
            "ramp up ... over measurement_window_seconds" cadence as one
            discrete update per elapsed window.
        ramp_up_step: Per-window additive increment applied to the
            spillover fraction while p95 is over threshold. Default
            ``0.2`` means the router needs at least
            ``ceil(spillover_fraction_max / ramp_up_step)`` consecutive
            breached windows to reach the cap (4 windows at the
            defaults). Set lower for a slower ramp; raising toward
            ``spillover_fraction_max`` collapses the ramp into a single
            step, which the policy contract explicitly forbids.
        ramp_back_factor: Per-window multiplicative decay applied while
            p95 is at or below threshold. A value of 0.9 means the
            fraction halves over roughly 7 healthy windows.
    """

    latency_window_size: int = 50
    p95_threshold_multiplier: float = 1.5
    spillover_fraction_max: float = 0.8
    measurement_window_seconds: float = 10.0
    ramp_up_step: float = 0.2
    ramp_back_factor: float = 0.9


@dataclass
class ReactiveState:
    """Mutable state for the reactive policy router.

    Attributes:
        on_spillover: ``True`` while traffic is being routed to spillover.
        spillover_started_idx: ``request_idx`` at which the most recent
            spillover divergence began. ``None`` when not on spillover.
        requests_on_spillover: Count of completed requests since the most
            recent divergence began. Used to enforce
            ``stay_on_spillover_min_requests``.
        last_health_check_time_s: Monotonic-clock seconds of the most
            recent attempted return-to-primary health check.
    """

    on_spillover: bool = False
    spillover_started_idx: int | None = None
    requests_on_spillover: int = 0
    last_health_check_time_s: float = -math.inf


@dataclass
class ProactiveState:
    """Mutable state for the proactive policy router.

    Attributes:
        latency_window: Most recent ``latency_window_size`` first-token
            latencies (milliseconds). FIFO; oldest popped when full.
        baseline_p95_ms: Warm-up baseline p95 first-token latency. Set
            once at end of warm-up; never re-set.
        current_spillover_fraction: Current fraction of requests routed
            to spillover ([0.0, ``spillover_fraction_max``]).
        last_window_eval_time_s: Monotonic-clock seconds of the most
            recent fraction-update evaluation.
    """

    latency_window: list[float] = field(default_factory=list)
    baseline_p95_ms: float | None = None
    current_spillover_fraction: float = 0.0
    last_window_eval_time_s: float = -math.inf


@dataclass(frozen=True)
class ReactiveObservation:
    """Observation passed to ``reactive_decide``."""

    request_idx: int
    first_token_latency_ms: float | None
    real_429_observed: bool
    monotonic_time_s: float


@dataclass(frozen=True)
class ProactiveObservation:
    """Observation passed to ``proactive_decide``."""

    request_idx: int
    first_token_latency_ms: float | None
    monotonic_time_s: float
    in_warmup: bool


def _percentile(values: list[float], pct: float) -> float:
    """Return the ``pct``-percentile of ``values`` (linear interpolation).

    Pure helper; no NumPy dependency so this module stays light to import
    in tests. ``pct`` is in [0, 100]. Empty input returns ``0.0``.
    """
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


def reactive_decide(
    obs: ReactiveObservation,
    state: ReactiveState,
    params: ReactivePolicyParams,
) -> tuple[str, ReactiveState]:
    """Pure reactive-policy decision function.

    Contract (verbatim from Task 012):
      * Send each request to "primary" by default.
      * On a first-token latency > ``first_token_timeout_ms`` OR a real
        429, route subsequent requests to "spillover".
      * Health-check "primary" every ``health_check_interval_ms`` (a
        single trial request — the simulator implements this as the next
        request after the interval elapses while ``requests_on_spillover``
        has met the minimum-stay floor).
      * Stay on spillover for at least ``stay_on_spillover_min_requests``
        after first divergence.

    Args:
        obs: New observation (latency or 429 result of the most recent
            request).
        state: Current router state.
        params: Policy parameters.

    Returns:
        Tuple ``(decision, new_state)`` where ``decision`` is
        ``"primary"`` or ``"spillover"`` and ``new_state`` is the
        post-observation state.
    """
    # Work on a copy so this remains a pure function (no in-place mutation
    # of the caller's state object).
    new_state = ReactiveState(
        on_spillover=state.on_spillover,
        spillover_started_idx=state.spillover_started_idx,
        requests_on_spillover=state.requests_on_spillover,
        last_health_check_time_s=state.last_health_check_time_s,
    )

    triggered = False
    if obs.real_429_observed:
        triggered = True
    elif (
        obs.first_token_latency_ms is not None
        and obs.first_token_latency_ms > params.first_token_timeout_ms
    ):
        triggered = True

    if not new_state.on_spillover:
        if triggered:
            new_state.on_spillover = True
            new_state.spillover_started_idx = obs.request_idx
            new_state.requests_on_spillover = 1
            new_state.last_health_check_time_s = obs.monotonic_time_s
            return "spillover", new_state
        return "primary", new_state

    new_state.requests_on_spillover += 1

    elapsed_since_health_ms = (
        (obs.monotonic_time_s - new_state.last_health_check_time_s) * 1000.0
    )
    can_attempt_return = (
        new_state.requests_on_spillover >= params.stay_on_spillover_min_requests
        and elapsed_since_health_ms >= params.health_check_interval_ms
    )

    if can_attempt_return and not triggered:
        new_state.on_spillover = False
        new_state.spillover_started_idx = None
        new_state.requests_on_spillover = 0
        new_state.last_health_check_time_s = obs.monotonic_time_s
        return "primary", new_state

    if can_attempt_return and triggered:
        new_state.requests_on_spillover = 1
        new_state.spillover_started_idx = obs.request_idx
        new_state.last_health_check_time_s = obs.monotonic_time_s

    return "spillover", new_state


def proactive_decide(
    obs: ProactiveObservation,
    state: ProactiveState,
    params: ProactivePolicyParams,
) -> tuple[float, ProactiveState]:
    """Pure proactive-policy decision function.

    Contract (verbatim from Task 012):
      * Maintain a sliding window of the last ``latency_window_size``
        first-token latencies. Compute p95.
      * If p95 > ``p95_threshold_multiplier`` × the warm-up baseline p95,
        ramp UP the spillover fraction toward ``spillover_fraction_max``
        by ``ramp_up_step`` once per ``measurement_window_seconds``
        (progressive multi-window ramp; the cap is reached only after
        ``ceil(spillover_fraction_max / ramp_up_step)`` consecutive
        breached windows).
      * When p95 returns to or below the threshold, ramp BACK by
        ``ramp_back_factor`` per window.

    Args:
        obs: New observation (latency of the most recent request and
            warm-up flag).
        state: Current router state.
        params: Policy parameters.

    Returns:
        Tuple ``(spillover_fraction, new_state)``. The caller routes
        each subsequent request to "spillover" with the returned
        probability (deterministic given a seeded RNG at the call site).
    """
    new_window = list(state.latency_window)
    if obs.first_token_latency_ms is not None:
        new_window.append(obs.first_token_latency_ms)
        if len(new_window) > params.latency_window_size:
            new_window = new_window[-params.latency_window_size:]

    new_state = ProactiveState(
        latency_window=new_window,
        baseline_p95_ms=state.baseline_p95_ms,
        current_spillover_fraction=state.current_spillover_fraction,
        last_window_eval_time_s=state.last_window_eval_time_s,
    )

    # Baseline is locked at end of warm-up (first non-warmup observation
    # after a warm-up period). Before baseline is locked, fraction stays
    # at zero.
    if obs.in_warmup:
        return 0.0, new_state

    if new_state.baseline_p95_ms is None:
        new_state.baseline_p95_ms = max(_percentile(new_window, 95.0), 1.0)
        return new_state.current_spillover_fraction, new_state

    # Only re-evaluate the fraction once per measurement window. This
    # matches the verbatim contract: "ramp up ... over
    # measurement_window_seconds" implies a single discrete step per
    # window. The ramp_up_step (default 0.2) controls per-window
    # increment so the cap is approached progressively rather than in
    # one jump.
    elapsed_s = obs.monotonic_time_s - new_state.last_window_eval_time_s
    if elapsed_s < params.measurement_window_seconds:
        return new_state.current_spillover_fraction, new_state

    new_state.last_window_eval_time_s = obs.monotonic_time_s
    current_p95 = _percentile(new_window, 95.0)
    threshold_ms = new_state.baseline_p95_ms * params.p95_threshold_multiplier

    if current_p95 > threshold_ms:
        # Progressive ramp toward the cap. Per-window additive increment
        # ``ramp_up_step`` is clamped at ``spillover_fraction_max`` so
        # the router needs multiple consecutive breached windows to
        # reach the cap (verbatim "ramp up over measurement windows").
        step = max(0.0, params.ramp_up_step)
        new_state.current_spillover_fraction = min(
            params.spillover_fraction_max,
            new_state.current_spillover_fraction + step,
        )
    else:
        new_state.current_spillover_fraction *= params.ramp_back_factor
        if new_state.current_spillover_fraction < 1e-6:
            new_state.current_spillover_fraction = 0.0

    return new_state.current_spillover_fraction, new_state


# ----------------------------------------------------------------------------
# Deterministic system prompt construction
# ----------------------------------------------------------------------------


def build_system_prompt(
    corpus_path: pathlib.Path,
    corpus_seed: int,
    target_tokens: int,
) -> str:
    """Build a deterministic system prompt of ~``target_tokens`` tokens.

    Construction procedure (verbatim from Task 012, lightly extended to
    handle the case where the corpus is smaller in total token count
    than ``target_tokens``):

      1. Load ``corpus_path`` as a JSON list of strings.
      2. Seed a ``random.Random`` with ``corpus_seed`` and deterministically
         shuffle the list.
      3. Concatenate snippets in the shuffled order with ``"\\n\\n"`` as
         the separator until the estimated token count (``len(text) //
         DEFAULT_TOKEN_ESTIMATE_DIVISOR``) reaches ``target_tokens``.
      4. If the shuffled corpus is exhausted before ``target_tokens`` is
         reached, the loop wraps to the beginning of the shuffled list
         (same shuffle order) and continues. This preserves determinism
         under any combination of corpus size and target.

    The result's SHA-256 is logged at the call site so reproducers can
    verify byte-identity.

    Args:
        corpus_path: Filesystem path to the JSON corpus.
        corpus_seed: Integer seed for the deterministic shuffle.
        target_tokens: Approximate target token count
            (``len(text) // 4``).

    Returns:
        The assembled system prompt string.

    Raises:
        CorpusMissingError: ``corpus_path`` is missing, unreadable, or
            does not contain a non-empty JSON list of strings.
    """
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
        total_chars += len(snip) + 2  # "\n\n" separator
        idx += 1

    return "\n\n".join(parts)


# ----------------------------------------------------------------------------
# Experiment YAML loader (independent of run_benchmark.load_experiment)
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class SimulationLoadConfig:
    """Parsed simulation load-shape block (warmup + ramp + sustain)."""

    duration_seconds: int
    warmup_duration_seconds: int
    warmup_tps: float
    ramp_start_tps: float
    ramp_end_tps: float
    ramp_duration_seconds: int
    sustain_tps: float
    sustain_duration_seconds: int


@dataclass(frozen=True)
class ExperimentConfig:
    """Parsed spillover-experiment YAML.

    Independent from ``run_benchmark.ExperimentConfig`` — the simulator
    needs a different schema (no sweep, no dataset.json layout, custom
    ``simulation`` and ``policy`` blocks).
    """

    path: pathlib.Path
    experiment_id: str
    description: str
    parent_experiment: str | None
    benchmark: str
    model_family: str
    model_deployment_template: str
    model_version: str
    model_endpoint_env: str
    auth_mode: str
    call_params: dict
    effort: str
    policy_type: str
    reactive_params: ReactivePolicyParams
    proactive_params: ProactivePolicyParams
    simulation: SimulationLoadConfig
    primary_simulated_throttle_threshold_tpm: int
    corpus_seed: int
    target_system_prompt_tokens: int
    user_prompts_path: str
    system_prompt_corpus_path: str
    prompt_cache_key: str | None
    prompt_cache_retention: str | None
    budget_estimated_usd: float
    budget_hard_ceiling_usd: float
    budget_confirmed: bool
    metadata: dict
    concurrency: int


def _require(d: dict, key: str, where: str) -> Any:
    if key not in d:
        raise ValueError(f"{where}: missing required key {key!r}")
    return d[key]


def load_experiment(path: str | pathlib.Path) -> ExperimentConfig:
    """Load and validate a spillover-experiment YAML.

    Args:
        path: Filesystem path to the YAML.

    Returns:
        Parsed ``ExperimentConfig``.

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
    parent = raw.get("parent_experiment")

    model_block = _require(raw, "model", where)
    if not isinstance(model_block, dict):
        raise ValueError(f"{where}: model block must be a mapping")
    family = _require(model_block, "family", f"{where}.model")
    if family != "gpt-5.2":
        raise ValueError(
            f"{where}: simulator supports family='gpt-5.2' only; got {family!r}"
        )
    auth_mode = _require(model_block, "auth_mode", f"{where}.model")
    if auth_mode != "entra":
        raise ValueError(
            f"{where}: model.auth_mode must be 'entra'; got {auth_mode!r}"
        )

    call_params = raw.get("call_params") or {}
    if not isinstance(call_params, dict):
        raise ValueError(f"{where}: call_params must be a mapping")
    # gpt-5.2 rejects temperature/top_p on Foundry v1 — mirror runner.
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

    react_block = policy_block.get("reactive") or {}
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

    proact_block = policy_block.get("proactive") or {}
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
            f"{where}: policy.proactive.ramp_up_step must be > 0; "
            f"got {proact_params.ramp_up_step!r}"
        )
    if proact_params.ramp_up_step >= proact_params.spillover_fraction_max:
        raise ValueError(
            f"{where}: policy.proactive.ramp_up_step "
            f"({proact_params.ramp_up_step}) must be strictly less than "
            f"spillover_fraction_max ({proact_params.spillover_fraction_max}) "
            f"so the cap is approached progressively over multiple windows"
        )

    sim_block = _require(raw, "simulation", where)
    if not isinstance(sim_block, dict):
        raise ValueError(f"{where}: simulation block must be a mapping")
    warmup_block = sim_block.get("warmup") or {}
    load_block = sim_block.get("load_pattern") or {}
    simulation = SimulationLoadConfig(
        duration_seconds=int(_require(sim_block, "duration_seconds", f"{where}.simulation")),
        warmup_duration_seconds=int(warmup_block.get("duration_seconds", 120)),
        warmup_tps=float(warmup_block.get("tps", 0.3)),
        ramp_start_tps=float(load_block.get("ramp_start_tps", 0.5)),
        ramp_end_tps=float(load_block.get("ramp_end_tps", 2.5)),
        ramp_duration_seconds=int(load_block.get("ramp_duration_seconds", 600)),
        sustain_tps=float(load_block.get("sustain_tps", 2.0)),
        sustain_duration_seconds=int(load_block.get("sustain_duration_seconds", 600)),
    )

    primary_block = sim_block.get("primary") or {}
    primary_throttle_tpm = int(
        primary_block.get("simulated_throttle_threshold_tpm", 30000)
    )

    corpus_seed = int(_require(raw, "corpus_seed", where))
    target_tokens = int(raw.get("target_system_prompt_tokens", 30000))
    user_prompts_path = str(
        _require(raw, "user_prompts_path", where)
    )
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
            f"for benchmark 04 spillover (the simulator models PTU "
            f"saturation); got {consumption_ctx!r}"
        )

    concurrency = int(raw.get("concurrency", 4))
    if concurrency <= 0:
        raise ValueError(f"{where}: concurrency must be > 0")

    return ExperimentConfig(
        path=p,
        experiment_id=exp_id,
        description=description,
        parent_experiment=parent,
        benchmark=benchmark,
        model_family=family,
        model_deployment_template=_require(
            model_block, "deployment", f"{where}.model"
        ),
        model_version=str(model_block.get("version", "")),
        model_endpoint_env=str(
            model_block.get("endpoint_env", "AZURE_OPENAI_FOUNDRY_ENDPOINT")
        ),
        auth_mode=auth_mode,
        call_params=dict(call_params),
        effort=effort,
        policy_type=policy_type,
        reactive_params=react_params,
        proactive_params=proact_params,
        simulation=simulation,
        primary_simulated_throttle_threshold_tpm=primary_throttle_tpm,
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
        metadata=dict(metadata),
        concurrency=concurrency,
    )


# ----------------------------------------------------------------------------
# Helpers — hashing, env, git, timestamps
# ----------------------------------------------------------------------------


def sha256_text(s: str) -> str:
    """Lowercase hex SHA-256 of a UTF-8 encoded string."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return sha256_text(payload)


_ENV_TEMPLATE_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _resolve_env_template(value: str, *, env: dict[str, str] | None = None) -> str:
    """Substitute ``${NAME}`` references with values from ``env``.

    Mirrors ``run_benchmark._resolve_env_template`` so the same YAML
    indirection works for both runners. Reads env-var NAMES only; the
    resolved VALUE is never logged.
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
            )
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
        raise RuntimeError(
            "git worktree is dirty; commit or pass --allow-dirty."
        )
    return (sha, dirty)


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.timezone.utc)


def _iso8601_z(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


# ----------------------------------------------------------------------------
# Load shape — arrival-time schedule
# ----------------------------------------------------------------------------


def _build_arrival_schedule(sim: SimulationLoadConfig) -> list[tuple[float, str]]:
    """Build the deterministic arrival schedule.

    Returns a list of ``(monotonic_offset_seconds, phase_label)`` tuples
    where ``phase_label`` is ``"warmup"``, ``"ramp"``, or ``"sustain"``.
    Arrival times are computed by integrating the (piecewise) TPS curve
    so the inter-arrival gap shrinks linearly across the ramp phase.

    For a phase of duration ``D`` seconds with constant TPS ``r``, we
    emit ``int(D * r)`` arrivals at equal spacing. For the ramp phase
    (TPS linear from ``a`` to ``b``), the total expected arrivals is
    ``D * (a + b) / 2``; arrival times are placed at the inverse-CDF of
    the linear schedule so the rate matches at every point.
    """
    schedule: list[tuple[float, str]] = []
    t0 = 0.0

    # Warmup: constant rate.
    n_warm = max(int(sim.warmup_duration_seconds * sim.warmup_tps), 0)
    if n_warm > 0:
        step = sim.warmup_duration_seconds / n_warm
        for i in range(n_warm):
            schedule.append((t0 + (i + 0.5) * step, "warmup"))
    t0 += sim.warmup_duration_seconds

    # Ramp: linear from ramp_start_tps to ramp_end_tps over
    # ramp_duration_seconds. Number of arrivals = D * (a+b)/2.
    a = sim.ramp_start_tps
    b = sim.ramp_end_tps
    D = sim.ramp_duration_seconds
    n_ramp = max(int(D * (a + b) / 2.0), 0)
    if n_ramp > 0:
        # Cumulative arrivals at time t (offset from start of ramp):
        #   N(t) = a*t + (b-a)/(2D) * t^2
        # Solve N(t) = k for k = 1..n_ramp:
        #   t = (-a + sqrt(a^2 + 2*(b-a)/D * k)) / ((b-a)/D)   (b>a)
        if abs(b - a) < 1e-9:
            step = D / n_ramp
            for i in range(n_ramp):
                schedule.append((t0 + (i + 0.5) * step, "ramp"))
        else:
            slope = (b - a) / D
            for k in range(1, n_ramp + 1):
                disc = a * a + 2.0 * slope * (k - 0.5)
                if disc < 0:
                    disc = 0.0
                t = (-a + math.sqrt(disc)) / slope
                t = max(0.0, min(D, t))
                schedule.append((t0 + t, "ramp"))
    t0 += D

    # Sustain: constant rate.
    n_sus = max(int(sim.sustain_duration_seconds * sim.sustain_tps), 0)
    if n_sus > 0:
        step = sim.sustain_duration_seconds / n_sus
        for i in range(n_sus):
            schedule.append((t0 + (i + 0.5) * step, "sustain"))

    schedule.sort()
    return schedule


# ----------------------------------------------------------------------------
# Throttle bookkeeping (simulator-internal, never overrides API truth)
# ----------------------------------------------------------------------------


@dataclass
class _ThrottleModel:
    """Simulator-internal PTU throttle state. Bookkeeping only.

    Tracks a 60-second rolling input-token rate; reports
    ``headroom`` / ``near_threshold`` / ``throttled`` based on the
    configured TPM threshold. This is the simulator's view of "would a
    real PTU here be throttled?"; it never overrides the canonical
    ``cached_tokens`` from the API response.
    """

    threshold_tpm: int
    samples: list[tuple[float, int]] = field(default_factory=list)

    def add(self, monotonic_s: float, tokens: int) -> None:
        self.samples.append((monotonic_s, tokens))
        cutoff = monotonic_s - 60.0
        # Trim left.
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.pop(0)

    def current_state(self, monotonic_s: float) -> str:
        cutoff = monotonic_s - 60.0
        total = sum(tok for ts, tok in self.samples if ts >= cutoff)
        if total >= self.threshold_tpm:
            return "throttled"
        if total >= self.threshold_tpm * 0.8:
            return "near_threshold"
        return "headroom"


# ----------------------------------------------------------------------------
# Live client + 429 handling
# ----------------------------------------------------------------------------


def _build_live_client(*, endpoint_value: str) -> Any:
    """Instantiate the Foundry v1 ``AsyncOpenAI`` client (Entra ID).

    Mirrors ``run_benchmark._build_live_client``: Foundry v1 surface at
    ``<endpoint>/openai/v1/``, audience ``https://ai.azure.com/.default``.
    Lazy-imports so dry-run does not require the SDK installed.

    Auth refresh contract (long-run hardening, Task 014 follow-up).
    Entra ID bearer tokens have a ~60-minute TTL. The previous
    implementation called ``token_provider()`` at construction time and
    embedded the resulting static token string in ``api_key``; runs
    longer than the TTL (e.g. the 22-minute load shape repeated across
    reactive + proactive policies under a 60 min wall clock plus retry
    backoffs) would 401 silently mid-stream.

    The fix uses the **async** token provider from ``azure.identity.aio``
    and passes the *callable itself* (not its result) into the OpenAI
    SDK. ``AsyncOpenAI`` ≥ 2.x's ``api_key`` parameter accepts
    ``Callable[[], Awaitable[str]]``; the SDK invokes the callable
    before every request with ``bearer_auth`` (i.e. every Responses API
    call) via ``_refresh_api_key`` and uses the freshly returned token
    as the Bearer header. ``azure.identity.aio`` internally caches and
    refreshes the underlying access token, so we get a refreshable
    token provider end-to-end without managing TTL ourselves.

    Specifically NOT done here:
      * No API-key code path (would violate the auth contract for this
        repo: Entra ID only).
      * No subclassing of ``AsyncOpenAI`` or custom ``httpx`` auth flow
        (the SDK already supports the callable form natively — adding a
        custom layer would be larger surface and not "minimal robust").
      * No change to ``base_url`` or methodology ``api_version="preview"``
        recorded verbatim in JSONL records (Foundry v1 surface preserved).
    """
    from azure.identity.aio import (  # noqa: PLC0415
        DefaultAzureCredential,
        get_bearer_token_provider,
    )
    from openai import AsyncOpenAI  # noqa: PLC0415

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(),
        "https://ai.azure.com/.default",
    )
    base_url = endpoint_value.rstrip("/") + "/openai/v1/"
    # api_key receives the async callable itself; the OpenAI SDK awaits
    # it before each request so the token is refreshed on a per-call
    # cadence (azure.identity.aio caches the underlying access token
    # and renews it automatically when it nears expiry).
    return AsyncOpenAI(base_url=base_url, api_key=token_provider)


def _parse_retry_after_headers(headers: Any) -> tuple[float | None, float | None]:
    """Extract ``retry-after-ms`` and ``retry-after`` from response headers.

    Returns ``(retry_after_ms, retry_after_seconds)``. Either may be
    ``None`` if the corresponding header was absent.
    """
    if headers is None:
        return (None, None)
    # ``headers`` may be a dict-like or an httpx Headers object.
    try:
        getter = headers.get
    except AttributeError:
        return (None, None)
    ms_raw = getter("retry-after-ms")
    s_raw = getter("retry-after")
    ms_val: float | None = None
    s_val: float | None = None
    if ms_raw is not None:
        try:
            ms_val = float(ms_raw)
        except (TypeError, ValueError):
            ms_val = None
    if s_raw is not None:
        try:
            s_val = float(s_raw)
        except (TypeError, ValueError):
            s_val = None
    return (ms_val, s_val)


async def _live_call(
    *,
    client: Any,
    call_kwargs: dict,
    request_idx: int,
) -> tuple[dict[str, Any], float, float, int, bool, float | None, float | None]:
    """Execute one live Responses-API call with backoff on 429.

    Returns:
        Tuple
        ``(usage_dict, first_token_latency_ms, total_latency_ms,
        retry_count, real_429_observed, retry_after_ms,
        retry_after_seconds)``.

    The Responses API used here is non-streaming, so we cannot observe
    a true "first token" event from the wire. We record the total
    response latency as ``first_token_latency_ms`` as well — this is
    documented in benchmark 04's README as a Phase-1 limitation;
    Phase 2 (Task 013) is expected to add streaming and a true TTFT.
    """
    last_exc: Exception | None = None
    real_429 = False
    retry_after_ms: float | None = None
    retry_after_s: float | None = None
    started = time.monotonic()
    for attempt in range(RATE_LIMIT_MAX_ATTEMPTS):
        try:
            response = await client.responses.create(**call_kwargs)
        except Exception as exc:
            status = getattr(exc, "status_code", None) or getattr(
                exc, "status", None
            )
            if status == 429:
                real_429 = True
                # Try to parse retry-after headers from the embedded
                # response, if the SDK exposed it.
                resp_obj = getattr(exc, "response", None)
                headers = getattr(resp_obj, "headers", None) if resp_obj else None
                ms, s = _parse_retry_after_headers(headers)
                if ms is not None:
                    retry_after_ms = ms
                if s is not None:
                    retry_after_s = s
                if attempt < RATE_LIMIT_MAX_ATTEMPTS - 1:
                    # Respect retry-after-ms if provided; otherwise fall
                    # back to exponential backoff.
                    if ms is not None:
                        delay = ms / 1000.0
                    elif s is not None:
                        delay = s
                    else:
                        delay = RATE_LIMIT_BASE_DELAY_S * (2**attempt)
                    logger.warning(
                        "RATE_LIMIT request_idx=%d attempt=%d delay_s=%.2f",
                        request_idx,
                        attempt,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    last_exc = exc
                    continue
            raise
        total_latency_ms = (time.monotonic() - started) * 1000.0
        usage_obj = getattr(response, "usage", None)
        if usage_obj is None:
            usage_dict: dict[str, Any] = {}
        elif hasattr(usage_obj, "model_dump"):
            usage_dict = usage_obj.model_dump()
        else:
            usage_dict = dict(usage_obj)
        return (
            usage_dict,
            total_latency_ms,
            total_latency_ms,
            attempt,
            real_429,
            retry_after_ms,
            retry_after_s,
        )
    assert last_exc is not None
    raise last_exc


# ----------------------------------------------------------------------------
# Per-request record assembly
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
    """Construct ``responses.create()`` kwargs for one simulator request.

    Mirrors ``run_benchmark.build_call_kwargs`` for gpt-5.2 but adds the
    additive ``prompt_cache_key`` and ``prompt_cache_retention`` fields
    introduced by the Task 011 v2 appendix (forwarded only when set).
    """
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


# ----------------------------------------------------------------------------
# Charts + CSV emission
# ----------------------------------------------------------------------------


# Color-blind-friendly palette (Wong, 2011). Used by the chart helpers.
PALETTE = {
    "primary": "#0072B2",      # blue
    "spillover": "#D55E00",    # vermilion
    "cache_hit": "#009E73",    # bluish green
    "comparison_a": "#0072B2",
    "comparison_b": "#CC79A7",  # reddish purple
    "event_marker": "#E69F00",  # orange
}


def _rolling_cache_hit_ratio(
    records: list[dict[str, Any]], window_s: float
) -> tuple[list[float], list[float]]:
    """Compute rolling-window cache hit ratio over the run timeline.

    For each completed request, returns ``(t_seconds, ratio)`` where
    ``ratio = sum(cached_tokens in window) / sum(input_tokens in window)``.
    """
    series: list[tuple[float, int, int]] = []  # (t, cached, input)
    t0: float | None = None
    for rec in records:
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
        series.append((t - t0, cached, in_t))
    if not series:
        return [], []
    ts: list[float] = []
    ratios: list[float] = []
    timestamps = [s[0] for s in series]
    for i, (t, _, _) in enumerate(series):
        lo = bisect.bisect_left(timestamps, t - window_s, hi=i + 1)
        win = series[lo: i + 1]
        sum_cached = sum(w[1] for w in win)
        sum_in = sum(w[2] for w in win)
        ratio = (sum_cached / sum_in) if sum_in > 0 else 0.0
        ts.append(t)
        ratios.append(ratio)
    return ts, ratios


def _spillover_event_times(records: list[dict[str, Any]]) -> list[float]:
    """Return the relative times at which the router first diverged.

    For reactive policy: the first request after primary→spillover flip.
    For proactive policy: each crossing into a positive fraction from 0.
    """
    out: list[float] = []
    prev_action: str = "primary"
    prev_fraction: float = 0.0
    t0: float | None = None
    for rec in records:
        t = rec.get("relative_time_s")
        if t is None:
            continue
        if t0 is None:
            t0 = t
        action = rec.get("policy_action_taken")
        fraction = rec.get("simulated_proactive_fraction_at_routing")
        if action == "spillover" and prev_action == "primary":
            out.append(t - t0)
        if isinstance(fraction, (int, float)) and fraction > 0 and prev_fraction == 0:
            out.append(t - t0)
        prev_action = action if isinstance(action, str) else prev_action
        prev_fraction = (
            fraction if isinstance(fraction, (int, float)) else prev_fraction
        )
    return out


def _write_chart_and_csv(
    *,
    out_png: pathlib.Path,
    title: str,
    series: list[tuple[str, list[float], list[float], str]],
    events: list[tuple[float, str]] | None = None,
    ylabel: str = "cache hit ratio",
) -> None:
    """Emit one PNG chart and a sibling CSV.

    Args:
        out_png: Target PNG path; sibling CSV is ``<out_png>.csv``.
        title: Chart title.
        series: List of ``(label, xs, ys, color)`` tuples to plot.
        events: Optional ``[(t, label)]`` vertical event markers.
        ylabel: y-axis label.
    """
    import matplotlib  # noqa: PLC0415
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415

    fig, ax = plt.subplots(figsize=(9, 5))
    for label, xs, ys, color in series:
        ax.plot(xs, ys, label=label, color=color, linewidth=1.6)
    if events:
        seen_label = False
        for t, _lbl in events:
            ax.axvline(
                x=t,
                color=PALETTE["event_marker"],
                linestyle="--",
                linewidth=0.8,
                label=("spillover events" if not seen_label else None),
            )
            seen_label = True
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


def _load_jsonl_records(path: pathlib.Path) -> list[dict[str, Any]]:
    """Load all newline-delimited JSON records from ``path``.

    Lines that fail to parse are skipped with a warning so a corrupted
    sibling JSONL never blocks the current run's primary outputs.
    """
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
        logger.warning(
            "SIBLING_JSONL_READ_ERROR path=%s err=%s", path, exc
        )
        return []
    return out


def _find_sibling_policy_jsonl(
    runs_dir: pathlib.Path,
    current_jsonl: pathlib.Path,
    other_policy: str,
) -> pathlib.Path | None:
    """Locate the most recent sibling-policy JSONL in ``runs_dir``.

    Matches files whose name ends with ``_<other_policy>.jsonl`` and that
    are not the current run's own JSONL. Returns ``None`` when no such
    file exists.
    """
    if not runs_dir.is_dir():
        return None
    candidates = sorted(
        p
        for p in runs_dir.glob(f"*_{other_policy}.jsonl")
        if p != current_jsonl
    )
    if not candidates:
        return None
    # Most-recent by lexicographic order works because the timestamp
    # prefix is fixed-width ISO-like (``YYYYmmddTHHMMSSZ``).
    return candidates[-1]


def _emit_policy_comparison_chart(
    *,
    chart_dir: pathlib.Path,
    current_policy: str,
    current_records: list[dict[str, Any]],
    sibling_policy: str,
    sibling_records: list[dict[str, Any]],
    window_s: float,
) -> tuple[pathlib.Path, pathlib.Path] | None:
    """Write ``policy_comparison.png`` + sibling CSV overlaying both policies.

    Args:
        chart_dir: Output directory (``results/spillover-recovery-curves``).
        current_policy: This run's policy label.
        current_records: This run's per-request records (in order).
        sibling_policy: Other policy label (``reactive`` if current is
            ``proactive`` and vice versa).
        sibling_records: Records loaded from the sibling-policy JSONL.
        window_s: Rolling-window width for the cache-hit ratio.

    Returns:
        ``(png_path, csv_path)`` on success, or ``None`` if either side
        has no usable records.
    """
    if not current_records or not sibling_records:
        return None
    cur_ts, cur_ratios = _rolling_cache_hit_ratio(current_records, window_s)
    sib_ts, sib_ratios = _rolling_cache_hit_ratio(sibling_records, window_s)
    if not cur_ts or not sib_ts:
        return None

    chart_dir.mkdir(parents=True, exist_ok=True)
    png = chart_dir / "policy_comparison.png"
    cur_color = (
        PALETTE["comparison_a"]
        if current_policy == "reactive"
        else PALETTE["comparison_b"]
    )
    sib_color = (
        PALETTE["comparison_a"]
        if sibling_policy == "reactive"
        else PALETTE["comparison_b"]
    )
    _write_chart_and_csv(
        out_png=png,
        title=(
            f"Cache hit ratio comparison ({window_s:.0f}s rolling) — "
            f"reactive vs proactive"
        ),
        series=[
            (f"{current_policy} (this run)", cur_ts, cur_ratios, cur_color),
            (f"{sibling_policy} (sibling)", sib_ts, sib_ratios, sib_color),
        ],
        events=None,
        ylabel="cache hit ratio (0-1)",
    )
    return (png, pathlib.Path(str(png) + ".csv"))


# ----------------------------------------------------------------------------
# Simulation runner
# ----------------------------------------------------------------------------


@dataclass
class SimulationResult:
    """Top-level summary of one simulator run."""

    cells_written: int
    total_usd: float
    jsonl_path: pathlib.Path
    summary_path: pathlib.Path
    chart_path: pathlib.Path | None
    chart_csv_path: pathlib.Path | None
    comparison_chart_path: pathlib.Path | None
    comparison_chart_csv_path: pathlib.Path | None
    real_429_count: int
    real_429_fraction: float
    halt_reason: str | None
    cache_hit_ratio_mean: float
    spillover_request_fraction: float


def _build_summary(
    records: list[dict[str, Any]],
    policy: str,
    total_usd: float,
    real_429_count: int,
) -> dict[str, Any]:
    """Aggregate per-request records into the summary JSON shape.

    Reports:
      * time-weighted mean cache hit ratio per endpoint
      * p50 / p95 first-token latency
      * fraction of requests served by spillover
      * total tokens
      * total USD
    """
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

    return {
        "policy": policy,
        "n_requests": n,
        "real_429_count": real_429_count,
        "real_429_fraction": (real_429_count / n) if n else 0.0,
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


def _apply_smoke_overrides(cfg: ExperimentConfig) -> ExperimentConfig:
    """Return a copy of ``cfg`` with smoke-mode load durations applied.

    Smoke mode shrinks ``duration_seconds`` to 180 and
    ``sustain_duration_seconds`` to 60. Other knobs (warmup, ramp,
    policy params) unchanged so the policy contract still exercises.
    """
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


async def _run_simulation_async(
    *,
    cfg: ExperimentConfig,
    benchmarks_root: pathlib.Path,
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
    smoke: bool,
    timestamp_label: str,
) -> SimulationResult:
    schedule = _build_arrival_schedule(cfg.simulation)
    if not schedule:
        raise ValueError(
            "arrival schedule is empty; simulation duration / TPS produce zero "
            "requests"
        )

    system_sha = sha256_text(system_prompt)
    policy_params_payload = (
        dataclasses.asdict(cfg.reactive_params)
        if cfg.policy_type == "reactive"
        else dataclasses.asdict(cfg.proactive_params)
    )
    policy_params_sha = _sha256_json(
        {"type": cfg.policy_type, "params": policy_params_payload}
    )

    logger.info(
        "SIM_BEGIN experiment=%s policy=%s smoke=%s dry_run=%s "
        "system_prompt_sha256=%s policy_params_sha256=%s scheduled_requests=%d",
        cfg.experiment_id,
        cfg.policy_type,
        smoke,
        dry_run,
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

    react_state = ReactiveState()
    proact_state = ProactiveState()
    throttle = _ThrottleModel(
        threshold_tpm=cfg.primary_simulated_throttle_threshold_tpm
    )
    rng = random.Random(cfg.corpus_seed + 1)  # routing RNG (proactive only)
    total_usd = 0.0
    real_429_count = 0
    halt_reason: str | None = None
    records: list[dict[str, Any]] = []

    client: Any = None
    if not dry_run:
        client = _build_live_client(endpoint_value=endpoint_value)

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
            if sleep_s > 0:
                if dry_run:
                    # Dry-run: do not actually sleep through 22 minutes.
                    # Advance the simulator's reported time without
                    # consuming real wall-clock.
                    pass
                else:
                    await asyncio.sleep(min(sleep_s, 60.0))

            arrival_mono = time.monotonic() if not dry_run else scheduled_mono

            # Policy routing decision based on PRIOR observation. (The
            # current request's latency is not yet known.)
            if cfg.policy_type == "reactive":
                decision, react_state = reactive_decide(
                    last_obs_for_reactive, react_state, cfg.reactive_params
                )
                policy_fraction_at_routing: float | None = None
            else:
                fraction, proact_state = proactive_decide(
                    last_obs_for_proactive,
                    proact_state,
                    cfg.proactive_params,
                )
                policy_fraction_at_routing = fraction
                decision = (
                    "spillover" if rng.random() < fraction else "primary"
                )

            throttle_state = throttle.current_state(arrival_mono)
            user_text = user_prompts[idx % len(user_prompts)]

            call_kwargs = _build_call_kwargs(
                deployment=deployment,
                system_prompt=system_prompt,
                user_text=user_text,
                effort=cfg.effort,
                call_params=cfg.call_params,
                prompt_cache_key=cfg.prompt_cache_key,
                prompt_cache_retention=cfg.prompt_cache_retention,
            )

            real_429 = False
            retry_after_ms: float | None = None
            retry_after_s: float | None = None
            if dry_run:
                usage_dict = _zero_usage_dict()
                first_token_latency_ms = 0.0
                total_latency_ms = 0.0
                retry_count = 0
                cell_usd = 0.0
            else:
                try:
                    (
                        usage_dict,
                        first_token_latency_ms,
                        total_latency_ms,
                        retry_count,
                        real_429,
                        retry_after_ms,
                        retry_after_s,
                    ) = await _live_call(
                        client=client,
                        call_kwargs=call_kwargs,
                        request_idx=idx,
                    )
                except Exception:
                    logger.exception(
                        "REQUEST_FAILED request_idx=%d policy=%s",
                        idx,
                        cfg.policy_type,
                    )
                    raise
                tu = _usage_to_token_usage(usage_dict)
                cell_usd = payg_cost_per_call(
                    tu, pricing, model=cfg.model_family
                ).usd_per_request
                total_usd += cell_usd
                if real_429:
                    real_429_count += 1

            in_det = usage_dict.get("input_tokens_details") or {}
            cached_tokens_canonical = (
                int(in_det.get("cached_tokens", 0) or 0)
                if isinstance(in_det, dict)
                else 0
            )
            input_tokens_canonical = int(usage_dict.get("input_tokens", 0) or 0)
            throttle.add(arrival_mono, input_tokens_canonical)

            record = {
                "experiment_id": cfg.experiment_id,
                "git_commit": git_commit,
                "dirty": dirty,
                "timestamp_utc": _iso8601_z(_utc_now()),
                "wallclock_timestamp_iso": _iso8601_z(_utc_now()),
                "endpoint": endpoint_value,
                "auth_mode": "entra",
                "api_version": FOUNDRY_API_VERSION,
                "model": cfg.model_family,
                "deployment_name": deployment,
                "policy": cfg.policy_type,
                "endpoint_hit": decision,
                "request_idx": idx,
                "relative_time_s": arrival_mono - sim_started_mono,
                "phase": phase,
                "first_token_latency_ms": first_token_latency_ms,
                "total_latency_ms": total_latency_ms,
                "retry_count": retry_count,
                "usage": usage_dict,
                "canonical_cached_tokens": cached_tokens_canonical,
                "canonical_input_tokens": input_tokens_canonical,
                "simulated_primary_throttle_state": throttle_state,
                "policy_action_taken": decision,
                "simulated_proactive_fraction_at_routing": (
                    policy_fraction_at_routing
                ),
                "simulated_primary_recovery_state": (
                    ("on_spillover" if react_state.on_spillover else "primary")
                    if cfg.policy_type == "reactive"
                    else None
                ),
                "real_429_observed": real_429,
                "retry_after_ms": retry_after_ms,
                "retry_after_seconds": retry_after_s,
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
            out_fh.write(json.dumps(record, sort_keys=True) + "\n")
            out_fh.flush()
            records.append(record)

            # Update observations for next routing decision.
            last_obs_for_reactive = ReactiveObservation(
                request_idx=idx,
                first_token_latency_ms=first_token_latency_ms,
                real_429_observed=real_429,
                monotonic_time_s=arrival_mono,
            )
            last_obs_for_proactive = ProactiveObservation(
                request_idx=idx,
                first_token_latency_ms=first_token_latency_ms,
                monotonic_time_s=arrival_mono,
                in_warmup=(phase == "warmup"),
            )

            # Budget halt.
            if total_usd >= cfg.budget_hard_ceiling_usd:
                halt_reason = "budget_hard_ceiling"
                logger.error(
                    "BUDGET_HALT experiment=%s total_usd=%.4f ceiling_usd=%.4f",
                    cfg.experiment_id,
                    total_usd,
                    cfg.budget_hard_ceiling_usd,
                )
                break

            # Real-429 halt.
            if (
                idx >= 19
                and real_429_count / (idx + 1) > REAL_429_HALT_FRACTION
            ):
                halt_reason = "real_429_rate_exceeded"
                logger.error(
                    "REAL_429_HALT experiment=%s real_429_count=%d total=%d fraction=%.3f",
                    cfg.experiment_id,
                    real_429_count,
                    idx + 1,
                    real_429_count / (idx + 1),
                )
                break

    summary = _build_summary(
        records,
        policy=cfg.policy_type,
        total_usd=total_usd,
        real_429_count=real_429_count,
    )
    summary["experiment_id"] = cfg.experiment_id
    summary["git_commit"] = git_commit
    summary["dirty"] = dirty
    summary["api_version"] = FOUNDRY_API_VERSION
    summary["pricing_snapshot_path"] = pricing_snapshot_path
    summary["pricing_source_url"] = pricing.source_url
    summary["pricing_accessed_date"] = pricing.accessed_date
    summary["halt_reason"] = halt_reason
    summary["smoke"] = smoke
    summary["dry_run"] = dry_run
    summary["jsonl_path"] = str(jsonl_path)
    summary["system_prompt_sha256"] = system_sha
    summary["policy_params_sha256"] = policy_params_sha
    summary["scheduled_request_count"] = len(schedule)
    summary["completed_request_count"] = len(records)
    with summary_path.open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)

    # Chart.
    chart_dir = (pathlib.Path("results") / "spillover-recovery-curves").resolve()
    chart_dir.mkdir(parents=True, exist_ok=True)
    chart_path: pathlib.Path | None = None
    chart_csv_path: pathlib.Path | None = None
    if records:
        ts, ratios = _rolling_cache_hit_ratio(records, CACHE_ROLLING_WINDOW_S)
        events = [
            (t, "spillover") for t in _spillover_event_times(records)
        ]
        chart_path = chart_dir / f"{cfg.policy_type}_recovery.png"
        chart_csv_path = pathlib.Path(str(chart_path) + ".csv")
        _write_chart_and_csv(
            out_png=chart_path,
            title=(
                f"Cache hit ratio ({CACHE_ROLLING_WINDOW_S:.0f}s rolling) — "
                f"{cfg.policy_type} policy"
            ),
            series=[
                (
                    "cache hit ratio",
                    ts,
                    ratios,
                    PALETTE["cache_hit"],
                ),
            ],
            events=events,
            ylabel="cache hit ratio (0-1)",
        )

    # Comparison chart — emitted iff a sibling-policy JSONL is present
    # in the same runs_dir (works for both live pairs and dry-run pairs).
    comparison_chart_path: pathlib.Path | None = None
    comparison_chart_csv_path: pathlib.Path | None = None
    if records:
        other_policy = (
            "proactive" if cfg.policy_type == "reactive" else "reactive"
        )
        sibling_jsonl = _find_sibling_policy_jsonl(
            runs_dir, jsonl_path, other_policy
        )
        if sibling_jsonl is not None:
            sibling_records = _load_jsonl_records(sibling_jsonl)
            emitted = _emit_policy_comparison_chart(
                chart_dir=chart_dir,
                current_policy=cfg.policy_type,
                current_records=records,
                sibling_policy=other_policy,
                sibling_records=sibling_records,
                window_s=CACHE_ROLLING_WINDOW_S,
            )
            if emitted is not None:
                comparison_chart_path, comparison_chart_csv_path = emitted
                logger.info(
                    "POLICY_COMPARISON_CHART_EMITTED current_policy=%s "
                    "sibling_policy=%s sibling_jsonl=%s png=%s",
                    cfg.policy_type,
                    other_policy,
                    sibling_jsonl.name,
                    comparison_chart_path,
                )
        else:
            logger.info(
                "POLICY_COMPARISON_SKIPPED reason=no_sibling_jsonl "
                "current_policy=%s expected_suffix=_%s.jsonl",
                cfg.policy_type,
                other_policy,
            )

    cache_hit_overall = summary["cache_hit_ratio_overall"]
    spillover_fraction = summary["spillover_request_fraction"]

    if halt_reason == "budget_hard_ceiling":
        raise BudgetHaltError(
            f"running USD {total_usd:.4f} >= ceiling "
            f"{cfg.budget_hard_ceiling_usd:.4f}"
        )
    if halt_reason == "real_429_rate_exceeded":
        raise Real429HaltError(
            f"real 429 rate {real_429_count}/{len(records)} > "
            f"{REAL_429_HALT_FRACTION:.2%}"
        )

    return SimulationResult(
        cells_written=len(records),
        total_usd=total_usd,
        jsonl_path=jsonl_path,
        summary_path=summary_path,
        chart_path=chart_path,
        chart_csv_path=chart_csv_path,
        comparison_chart_path=comparison_chart_path,
        comparison_chart_csv_path=comparison_chart_csv_path,
        real_429_count=real_429_count,
        real_429_fraction=summary["real_429_fraction"],
        halt_reason=halt_reason,
        cache_hit_ratio_mean=cache_hit_overall,
        spillover_request_fraction=spillover_fraction,
    )


def run_simulation(
    *,
    cfg: ExperimentConfig,
    benchmarks_root: pathlib.Path,
    pricing_dir: pathlib.Path,
    dry_run: bool,
    smoke: bool,
    allow_dirty: bool,
    env: dict[str, str] | None = None,
) -> SimulationResult:
    """Synchronous wrapper around the async simulator core."""
    src_env = env if env is not None else dict(os.environ)
    if smoke:
        cfg = _apply_smoke_overrides(cfg)

    endpoint_value = _require_env(cfg.model_endpoint_env, env=src_env)
    deployment = _resolve_env_template(cfg.model_deployment_template, env=src_env)
    if not deployment:
        raise EndpointMisconfiguredError(
            f"deployment resolved to empty from "
            f"{cfg.model_deployment_template!r}"
        )

    # Pre-run estimate against pricing snapshot. Tokens are estimated as
    # (system_prompt_tokens + ~50 user tokens) per request, output
    # ~200 tokens, reasoning ~50 at effort='minimal' (order-of-magnitude
    # only; the running ledger uses canonical usage values).
    snapshot_path = resolve_active_snapshot(
        kind="payg", target_date=None, pricing_dir=pricing_dir
    )
    pricing = load_payg_pricing(snapshot_path)

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
        model=cfg.model_family,
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
        if cfg.budget_estimated_usd > max_per_benchmark and not cfg.budget_confirmed:
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
        _run_simulation_async(
            cfg=cfg,
            benchmarks_root=benchmarks_root,
            runs_dir=runs_dir,
            system_prompt=system_prompt,
            user_prompts=user_prompts,
            git_commit=git_commit,
            dirty=dirty,
            pricing=pricing,
            pricing_snapshot_path=str(snapshot_path),
            endpoint_value=endpoint_value,
            deployment=deployment,
            dry_run=dry_run,
            smoke=smoke,
            timestamp_label=timestamp_label,
        )
    )


# ----------------------------------------------------------------------------
# CLI plumbing
# ----------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scripts.simulate_spillover",
        description=(
            "Phase 1 single-endpoint spillover-policy simulator (Hypothesis "
            "G weak-form). Streams per-request JSONL + summary; emits "
            "rolling cache-hit-ratio chart per policy."
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
        result = run_simulation(
            cfg=cfg,
            benchmarks_root=pathlib.Path(args.benchmarks_root),
            pricing_dir=pathlib.Path(args.pricing_dir),
            dry_run=args.dry_run,
            smoke=args.smoke,
            allow_dirty=args.allow_dirty,
        )
    except EndpointMisconfiguredError as exc:
        logger.error("ENDPOINT_MISCONFIGURED %s", exc)
        return EXIT_AUTH
    except CorpusMissingError as exc:
        logger.error("DATASET_MISSING %s", exc)
        return EXIT_DATASET
    except BudgetHaltError as exc:
        logger.error("BUDGET_HALT %s", exc)
        return EXIT_BUDGET
    except Real429HaltError as exc:
        logger.error("REAL_429_HALT %s", exc)
        return EXIT_BUDGET

    summary_line = (
        f"\n=== simulate_spillover summary ===\n"
        f"experiment_id     : {cfg.experiment_id}\n"
        f"policy            : {cfg.policy_type}\n"
        f"smoke             : {args.smoke}\n"
        f"dry_run           : {args.dry_run}\n"
        f"completed_cells   : {result.cells_written}\n"
        f"total_usd         : ${result.total_usd:.4f}\n"
        f"real_429_count    : {result.real_429_count} "
        f"({result.real_429_fraction:.2%})\n"
        f"cache_hit_overall : {result.cache_hit_ratio_mean:.4f}\n"
        f"spillover_fraction: {result.spillover_request_fraction:.4f}\n"
        f"jsonl             : {result.jsonl_path}\n"
        f"summary_json      : {result.summary_path}\n"
        f"chart_png         : {result.chart_path}\n"
        f"comparison_png    : {result.comparison_chart_path or '(sibling-policy JSONL not present yet)'}\n"
        f"=================================="
    )
    print(summary_line)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
