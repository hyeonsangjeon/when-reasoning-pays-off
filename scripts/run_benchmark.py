"""scripts/run_benchmark.py — deterministic Foundry v1 benchmark runner.

This module is the **only** path that writes ``benchmarks/<name>/runs/*.json``.
Every methodology invariant from ``docs/05-methodology.md`` is enforced in code:

  * Byte-identical prompts across ``(model, effort, repeat)`` cells. The system
    prompt and per-sample user input are loaded once per benchmark, SHA-256
    hashed, and re-sent without per-cell mutation.
  * The reasoning effort parameter is **conditionally** attached: present iff
    ``model.family == "gpt-5.2"``. Passing the parameter to a ``gpt-4o``
    deployment raises an API error.
  * Authentication is Entra ID via ``DefaultAzureCredential``; no plaintext
    credential code path exists in this source.
  * ``api_version="preview"`` (Foundry v1 convention). The classic legacy
    ``api_version`` value used by ``*.openai.azure.com`` endpoints is
    forbidden in this codebase and is not referenced anywhere here.
  * Append-only output. A pre-existing target file is a hard abort, never an
    overwrite or suffix rename.
  * Budget guards: pre-run estimate via ``scripts.cost_calculator``; mid-run
    halt if the running USD total crosses ``budget.hard_ceiling_usd``.
  * Cold-start tracking per deployment (``deployment_cold_start: true`` on
    first call to a deployment, or on a call separated from the prior call
    by more than 300 seconds).
  * Structured logging via the ``logging`` module; ``print()`` is used **only**
    in the CLI final summary.

CLI contract::

    python -m scripts.run_benchmark --experiment <yaml> [--dry-run]
        [--max-samples N] [--allow-dirty]
        [--benchmarks-root DIR] [--pricing-dir DIR]

Exit codes:
    0 = success
    1 = budget violation (pre-run estimate or mid-run halt)
    2 = auth/endpoint misconfiguration
    3 = dataset/prompt files missing

Reference client construction (Foundry v1 — NOT the legacy classic endpoint
format used by ``*.openai.azure.com`` URLs). The Foundry v1 surface is served
at ``<endpoint>/openai/v1/`` and requires audience
``https://ai.azure.com/.default`` (NOT ``cognitiveservices.azure.com``, which
is the classic Azure OpenAI audience and produces 401 ``audience is incorrect
(https://ai.azure.com)`` against Foundry v1)::

    from openai import AsyncOpenAI
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(),
        "https://ai.azure.com/.default",
    )
    client = AsyncOpenAI(
        base_url=os.environ["AZURE_OPENAI_FOUNDRY_ENDPOINT"].rstrip("/") + "/openai/v1/",
        api_key=token_provider(),  # bearer token; refreshed per client build
    )

This module performs **zero** outbound HTTPS calls when invoked with
``--dry-run``. The first live invocation is Task 006 (smoke).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import json
import logging
import os
import pathlib
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
    "AgentConfig",
    "BudgetExceededError",
    "BudgetTracker",
    "DatasetMissingError",
    "EndpointMisconfiguredError",
    "ExperimentConfig",
    "FilenameCollisionError",
    "Sample",
    "build_call_kwargs",
    "build_tool_list_for_request",
    "estimate_experiment_cost_usd",
    "load_experiment",
    "load_dataset",
    "main",
    "run_experiment",
    "sha256_text",
]


# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------

logger = logging.getLogger("scripts.run_benchmark")

# Model families that accept the reasoning effort parameter on the Responses
# API. Sending a reasoning effort dict to gpt-4o raises an API error.
REASONING_FAMILIES: frozenset[str] = frozenset({"gpt-5.2"})

# Pre-run estimate heuristic for reasoning-token volume by effort level. Used
# only to catch order-of-magnitude budget mistakes; not a billing prediction.
#
# Valid levels match the Foundry v1 gpt-5.2 API surface. The smoke run against
# the live gpt-5.2-2025-12-11 deployment confirmed the API rejects "minimal"
# with HTTP 400 ``unsupported_value`` and emits the canonical list
# ``none / low / medium / high / xhigh`` in the error payload — these are the
# only values the runner will accept here. The heuristic numbers are
# order-of-magnitude only (used by ``estimate_experiment_cost_usd`` for the
# pre-run budget guard; ``none`` is 0 because the model is instructed to
# emit no reasoning tokens).
PER_EFFORT_REASONING_HEURISTIC: dict[str, int] = {
    "none": 0,
    "low": 200,
    "medium": 800,
    "high": 2500,
    "xhigh": 5000,
}

# Per-cell rough estimate inputs (kept tight to the methodology §3 sample
# shape: short-factual benchmark expects ~500 in / ~200 out).
ESTIMATE_INPUT_TOKENS = 500.0
ESTIMATE_OUTPUT_TOKENS = 200.0

# Cold-start threshold: a deployment that has not been called in this many
# seconds is treated as cold for the next call.
COLD_START_GAP_SECONDS = 300.0

# Foundry v1 API version literal. The legacy ``*.openai.azure.com`` endpoint
# value is intentionally not referenced anywhere in this module.
FOUNDRY_API_VERSION = "preview"

# Env var canonical names (read-only; never assigned literal values here).
ENV_FOUNDRY_ENDPOINT_NAME = "AZURE_OPENAI_FOUNDRY_ENDPOINT"
ENV_AUTH_MODE_NAME = "AZURE_AUTH_MODE"
ENV_MAX_COST_PER_BENCHMARK_NAME = "MAX_COST_PER_BENCHMARK_USD"

# Retry policy for 429 responses (Task 006 will exercise this; the runner
# encodes the policy now so dry-run shape and live shape agree).
RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_BASE_DELAY_S = 1.0

# Default concurrency when YAML omits ``concurrency``.
DEFAULT_CONCURRENCY = 5

EXIT_OK = 0
EXIT_BUDGET = 1
EXIT_AUTH = 2
EXIT_DATASET = 3


# ----------------------------------------------------------------------------
# Typed errors
# ----------------------------------------------------------------------------


class BudgetExceededError(RuntimeError):
    """Raised when pre-run estimate or running total breaches a budget guard."""


class EndpointMisconfiguredError(RuntimeError):
    """Raised when required Azure env vars are missing, empty, or malformed."""


class DatasetMissingError(FileNotFoundError):
    """Raised when the benchmark dataset/prompt files cannot be found."""


class FilenameCollisionError(FileExistsError):
    """Raised when a target raw-response file already exists (append-only)."""


# ----------------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentConfig:
    """Additive sub-config — tool-loop mode for benchmark 03.

    The runner's default single-shot mode (benchmarks 01/02) is unaffected
    when ``tool_loop`` is False or the ``agent`` block is absent from the
    experiment YAML.

    Both the dry-run and live code paths for ``tool_loop: true`` are
    implemented. Dry runs record the byte-identical tool-config SHA on
    every cell and emit an empty ``tool_calls`` skeleton with
    ``tool_loop_terminated="dry_run_skeleton"``. Live runs execute the
    ReAct-style tool-loop body in :func:`_live_tool_loop_call`: initial
    call with ``tools=``, per-iteration dispatch through
    :data:`scripts.tools.TOOL_REGISTRY`, feeding tool results back,
    honoring ``max_tool_iterations``, summing per-iteration usage into
    the cell's top-level ``usage`` object, and setting
    ``tool_loop_terminated`` (``"ok"`` on natural termination,
    ``"iteration_cap"`` on cap-hit).

    Attributes:
        tool_loop: ``True`` enables the live (and dry-run) tool-loop branch.
        max_tool_iterations: Hard cap on iterations per cell; on cap-hit
            the live body records ``tool_loop_terminated="iteration_cap"``
            and does NOT count it as a measurement failure.
        tools: Ordered list of tool names (each must resolve to a callable
            in ``TOOL_REGISTRY``).
        tool_schema_paths: Filesystem paths of the per-tool JSON schemas,
            aligned with ``tools``.
        search_kb_path: Optional path to the canned web_search KB; required
            iff ``web_search`` is one of the registered tools.
    """

    tool_loop: bool
    max_tool_iterations: int
    tools: tuple[str, ...]
    tool_schema_paths: tuple[str, ...]
    search_kb_path: str | None


@dataclass(frozen=True)
class ExperimentConfig:
    """Parsed and validated experiment YAML.

    Attributes:
        path: Filesystem path of the source YAML.
        experiment_id: Unique experiment id (used in filenames + JSON records).
        description: Hypothesis-under-test free text.
        parent_experiment: Parent experiment id, or ``None`` for root runs.
        benchmark: Benchmark directory name under ``benchmarks_root``.
        dataset_size: ``N`` (cap on samples drawn from ``dataset.json``).
        repeats: ``R`` (repeats per ``(sample, effort)`` cell).
        model_family: ``"gpt-5.2"`` or ``"gpt-4o"``.
        model_deployment_template: Raw YAML deployment value (may contain
            ``${ENV_NAME}`` indirection).
        model_version: Free-text model-version label for the audit trail.
        model_endpoint_env: Env var name that holds the Foundry v1 URL.
        auth_mode: Auth mode literal — only ``"entra"`` is accepted.
        call_params: Constant per-call kwargs (``max_output_tokens``,
            ``temperature``, ...).
        sweep_efforts: List of effort levels to sweep. For gpt-4o this is
            normalized to ``[None]`` (single column, no reasoning param).
        capture: Per-cell capture flags (``response_text``, ...).
        budget_estimated_usd: Operator's USD estimate before the run.
        budget_hard_ceiling_usd: Mid-run halt threshold.
        budget_confirmed: Bypass for ``estimated_cost_usd`` >
            ``MAX_COST_PER_BENCHMARK_USD``.
        metadata: Recorded-but-not-controlled metadata block.
        concurrency: Async semaphore size.
    """

    path: pathlib.Path
    experiment_id: str
    description: str
    parent_experiment: str | None
    benchmark: str
    dataset_size: int
    repeats: int
    model_family: str
    model_deployment_template: str
    model_version: str
    model_endpoint_env: str
    auth_mode: str
    call_params: dict
    sweep_efforts: list[str | None]
    capture: dict
    budget_estimated_usd: float
    budget_hard_ceiling_usd: float
    budget_confirmed: bool
    metadata: dict
    concurrency: int
    agent: AgentConfig | None = None


@dataclass(frozen=True)
class Sample:
    """One benchmark sample.

    Attributes:
        sample_idx: 0-based ordinal in the dataset (also used in filenames).
        sample_id: Optional human label; defaults to the stringified idx.
        user_input: Fully-rendered user prompt (byte-identical across cells).
        sample_metadata: Free-form per-sample metadata (e.g. expected_answer).
    """

    sample_idx: int
    sample_id: str
    user_input: str
    sample_metadata: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------
# Hashing + git helpers
# ----------------------------------------------------------------------------


def sha256_text(s: str) -> str:
    """Return the lowercase hex SHA-256 of a UTF-8 encoded string.

    Args:
        s: Source string.

    Returns:
        64-character lowercase hex digest.
    """
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    """Stable SHA-256 of a JSON-serializable value (sorted keys, no spaces).

    Args:
        value: Any JSON-serializable object.

    Returns:
        Hex SHA-256 digest of ``json.dumps(value, sort_keys=True, separators=(',',':'))``.
    """
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return sha256_text(payload)


def _resolve_git_commit(allow_dirty: bool) -> tuple[str, bool]:
    """Resolve ``HEAD`` and worktree dirty status, with graceful fallback.

    Args:
        allow_dirty: If True, dirty/no-repo states do not raise.

    Returns:
        Tuple of (commit_sha_or_marker, dirty_flag). When no git repo is
        found the marker is ``"unknown"`` and dirty is ``True``.

    Raises:
        RuntimeError: Worktree is dirty (or no repo) and ``allow_dirty`` is
            False.
    """
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
                "git rev-parse HEAD failed (no repo or git not installed); "
                "pass --allow-dirty to proceed with git_commit='unknown'."
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
            "git worktree is dirty; commit changes before running, or pass "
            "--allow-dirty to embed dirty=true in every raw run JSON."
        )
    return (sha, dirty)


# ----------------------------------------------------------------------------
# Env var resolution
# ----------------------------------------------------------------------------


_ENV_TEMPLATE_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _resolve_env_template(value: str, *, env: dict[str, str] | None = None) -> str:
    """Substitute ``${ENV_NAME}`` references with values from ``os.environ``.

    Args:
        value: Source string possibly containing one or more ``${NAME}`` tokens.
        env: Optional environment mapping (defaults to ``os.environ``).

    Returns:
        Substituted string.

    Raises:
        EndpointMisconfiguredError: A referenced env var is missing or empty.
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
    """Return ``os.environ[name]`` or raise ``EndpointMisconfiguredError``."""
    src = env if env is not None else os.environ
    v = src.get(name)
    if not v:
        raise EndpointMisconfiguredError(
            f"required environment variable {name} is not set"
        )
    return v


# ----------------------------------------------------------------------------
# YAML loading + validation
# ----------------------------------------------------------------------------


def _require(cfg: dict, key: str, *, where: str) -> Any:
    if key not in cfg:
        raise ValueError(f"{where}: missing required key {key!r}")
    return cfg[key]


def load_experiment(path: str | pathlib.Path) -> ExperimentConfig:
    """Load and validate an experiment YAML.

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
        raise ValueError(f"experiment YAML must be a mapping at top level: {p}")

    where = str(p)
    exp_id = _require(raw, "experiment_id", where=where)
    description = _require(raw, "description", where=where)
    parent_experiment = raw.get("parent_experiment")
    benchmark = _require(raw, "benchmark", where=where)
    dataset_size = int(_require(raw, "dataset_size", where=where))
    repeats = int(_require(raw, "repeats", where=where))

    model_block = _require(raw, "model", where=where)
    if not isinstance(model_block, dict):
        raise ValueError(f"{where}: model block must be a mapping")
    family = _require(model_block, "family", where=f"{where}.model")
    if family not in ("gpt-5.2", "gpt-4o"):
        raise ValueError(
            f"{where}: model.family must be 'gpt-5.2' or 'gpt-4o'; got {family!r}"
        )
    auth_mode = _require(model_block, "auth_mode", where=f"{where}.model")
    if auth_mode != "entra":
        raise ValueError(
            f"{where}: model.auth_mode must be 'entra' (Entra ID only); got {auth_mode!r}"
        )

    sweep_block = raw.get("sweep") or {}
    if not isinstance(sweep_block, dict):
        raise ValueError(f"{where}: sweep block must be a mapping")
    sweep_effort_raw = sweep_block.get("effort") or []
    if not isinstance(sweep_effort_raw, list):
        raise ValueError(f"{where}: sweep.effort must be a list")

    if family in REASONING_FAMILIES:
        if not sweep_effort_raw:
            raise ValueError(
                f"{where}: sweep.effort must be non-empty for family={family!r}"
            )
        for level in sweep_effort_raw:
            if level not in PER_EFFORT_REASONING_HEURISTIC:
                raise ValueError(
                    f"{where}: unknown effort level {level!r}; expected one of "
                    f"{sorted(PER_EFFORT_REASONING_HEURISTIC)}"
                )
        sweep_efforts: list[str | None] = list(sweep_effort_raw)
    else:
        if sweep_effort_raw:
            raise ValueError(
                f"{where}: family={family!r} must not declare sweep.effort "
                f"(gpt-4o has no reasoning parameter)"
            )
        sweep_efforts = [None]

    capture = raw.get("capture") or {}
    if not isinstance(capture, dict):
        raise ValueError(f"{where}: capture block must be a mapping")

    budget = _require(raw, "budget", where=where)
    if not isinstance(budget, dict):
        raise ValueError(f"{where}: budget block must be a mapping")
    est = float(_require(budget, "estimated_cost_usd", where=f"{where}.budget"))
    hard = float(_require(budget, "hard_ceiling_usd", where=f"{where}.budget"))
    confirmed = bool(budget.get("confirmed", False))
    if hard <= 0:
        raise ValueError(f"{where}: budget.hard_ceiling_usd must be > 0")

    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError(f"{where}: metadata block must be a mapping")

    call_params = raw.get("call_params") or {}
    if not isinstance(call_params, dict):
        raise ValueError(f"{where}: call_params must be a mapping")

    concurrency = int(raw.get("concurrency", DEFAULT_CONCURRENCY))
    if concurrency <= 0:
        raise ValueError(f"{where}: concurrency must be > 0")

    # Task 010 additive: agent.tool_loop block. Absent or false → single-shot
    # mode, byte-identical to pre-Task-010 behavior. Required fields are
    # validated at config-load time so config errors surface before any
    # API call.
    agent_block = raw.get("agent")
    agent_cfg: AgentConfig | None = None
    if agent_block is not None:
        if not isinstance(agent_block, dict):
            raise ValueError(f"{where}: agent block must be a mapping")
        tool_loop_flag = bool(agent_block.get("tool_loop", False))
        if tool_loop_flag:
            tools_raw = agent_block.get("tools")
            if not isinstance(tools_raw, list) or not tools_raw:
                raise ValueError(
                    f"{where}: agent.tools must be a non-empty list when "
                    f"agent.tool_loop is true"
                )
            names: list[str] = []
            schema_paths: list[str] = []
            for i, t in enumerate(tools_raw):
                if not isinstance(t, dict):
                    raise ValueError(
                        f"{where}: agent.tools[{i}] must be a mapping"
                    )
                tname = t.get("name")
                tpath = t.get("schema_path")
                if not isinstance(tname, str) or not tname:
                    raise ValueError(
                        f"{where}: agent.tools[{i}].name must be a non-empty string"
                    )
                if not isinstance(tpath, str) or not tpath:
                    raise ValueError(
                        f"{where}: agent.tools[{i}].schema_path must be a non-empty string"
                    )
                names.append(tname)
                schema_paths.append(tpath)
            max_iter = int(agent_block.get("max_tool_iterations", 4))
            if max_iter < 1:
                raise ValueError(
                    f"{where}: agent.max_tool_iterations must be >= 1"
                )
            search_kb_path = agent_block.get("search_kb_path")
            if "web_search" in names and (
                not isinstance(search_kb_path, str) or not search_kb_path
            ):
                raise ValueError(
                    f"{where}: agent.search_kb_path is required when "
                    f"'web_search' is among agent.tools"
                )
            agent_cfg = AgentConfig(
                tool_loop=True,
                max_tool_iterations=max_iter,
                tools=tuple(names),
                tool_schema_paths=tuple(schema_paths),
                search_kb_path=(
                    str(search_kb_path) if isinstance(search_kb_path, str) else None
                ),
            )

    return ExperimentConfig(
        path=p,
        experiment_id=exp_id,
        description=description,
        parent_experiment=parent_experiment,
        benchmark=benchmark,
        dataset_size=dataset_size,
        repeats=repeats,
        model_family=family,
        model_deployment_template=_require(
            model_block, "deployment", where=f"{where}.model"
        ),
        model_version=str(model_block.get("version", "")),
        model_endpoint_env=str(
            model_block.get("endpoint_env", ENV_FOUNDRY_ENDPOINT_NAME)
        ),
        auth_mode=auth_mode,
        call_params=dict(call_params),
        sweep_efforts=sweep_efforts,
        capture=dict(capture),
        budget_estimated_usd=est,
        budget_hard_ceiling_usd=hard,
        budget_confirmed=confirmed,
        metadata=dict(metadata),
        concurrency=concurrency,
        agent=agent_cfg,
    )


# ----------------------------------------------------------------------------
# Dataset + prompt loading
# ----------------------------------------------------------------------------


def load_dataset(
    benchmark_dir: pathlib.Path,
    *,
    max_samples: int,
) -> tuple[str, str | None, list[Sample]]:
    """Load the benchmark's prompts and dataset.

    Layout expected (Task 005 deliverable; ``run_benchmark`` reads
    read-only)::

        <benchmark_dir>/
            dataset.json         # JSON list of sample dicts
            prompts/
                system.md
                user_template.md (optional)

    Each sample dict may carry an explicit ``user_input`` field. If absent,
    ``prompts/user_template.md`` is rendered via :func:`_render_user_template`
    — string fields pass through, non-string fields (dict, list, number,
    bool, None) are pretty-printed as JSON via ``json.dumps(...,
    ensure_ascii=False, indent=2)`` so the prompt never carries Python
    ``repr``-style output.

    Args:
        benchmark_dir: Filesystem path of ``benchmarks/<name>/``.
        max_samples: Cap on number of samples returned.

    Returns:
        Tuple ``(system_prompt, user_template_or_None, samples)``.

    Raises:
        DatasetMissingError: Any required file is missing.
    """
    if not benchmark_dir.is_dir():
        raise DatasetMissingError(
            f"benchmark directory not found: {benchmark_dir} "
            "(Task 005 produces the dataset + prompts)"
        )

    system_path = benchmark_dir / "prompts" / "system.md"
    if not system_path.is_file():
        raise DatasetMissingError(
            f"system prompt missing: {system_path} (Task 005 prerequisite)"
        )
    system_prompt = system_path.read_text(encoding="utf-8")

    template_path = benchmark_dir / "prompts" / "user_template.md"
    user_template = (
        template_path.read_text(encoding="utf-8") if template_path.is_file() else None
    )

    dataset_path = benchmark_dir / "dataset.json"
    if not dataset_path.is_file():
        raise DatasetMissingError(
            f"dataset.json missing: {dataset_path} (Task 005 prerequisite)"
        )

    with dataset_path.open("r", encoding="utf-8") as fh:
        dataset_raw = json.load(fh)
    if not isinstance(dataset_raw, list):
        raise DatasetMissingError(
            f"dataset.json must be a JSON list at top level: {dataset_path}"
        )

    samples: list[Sample] = []
    for idx, entry in enumerate(dataset_raw):
        if idx >= max_samples:
            break
        if not isinstance(entry, dict):
            raise DatasetMissingError(
                f"dataset.json entry #{idx} must be an object: {entry!r}"
            )
        explicit_user_input = entry.get("user_input")
        if explicit_user_input is not None:
            user_input = str(explicit_user_input)
        else:
            if user_template is None:
                raise DatasetMissingError(
                    f"sample #{idx} has no 'user_input' and no "
                    f"prompts/user_template.md is present at {template_path}"
                )
            try:
                user_input = _render_user_template(user_template, entry)
            except (KeyError, ValueError) as exc:
                raise DatasetMissingError(
                    f"sample #{idx} template render failed: {exc!r}"
                ) from exc
        sample_id = str(entry.get("id", idx))
        sample_meta = {k: v for k, v in entry.items() if k not in ("user_input",)}
        samples.append(
            Sample(
                sample_idx=idx,
                sample_id=sample_id,
                user_input=user_input,
                sample_metadata=sample_meta,
            )
        )
    return system_prompt, user_template, samples


class _DefaultMissing(dict):
    """Dict that returns ``"{key}"`` for missing keys during ``format_map``.

    Lets templates declare ``{question}`` placeholders without breaking when
    a sample dict omits the field. The missing field is left as a literal
    ``{question}`` in the rendered user prompt — never silently dropped.
    """

    def __missing__(self, key: str) -> str:  # type: ignore[override]
        return "{" + key + "}"


def _render_user_template(user_template: str, entry: dict) -> str:
    """Render ``user_template.md`` with ``entry`` as the placeholder source.

    Contract:
      * String values pass through ``str.format_map`` unchanged.
      * Non-string values (dict, list, int, bool, None, float, ...) are
        rendered as **pretty JSON** via
        ``json.dumps(value, ensure_ascii=False, indent=2)`` — never as
        Python's default ``str(value)``, which produces ``repr``-style
        output (single-quoted keys, ``True`` instead of ``true``) and
        materially changes the prompt the model receives.
      * Missing placeholders are preserved as the literal ``"{key}"``
        via ``_DefaultMissing``; the field is never silently dropped.

    Benchmarks 02 / 03 will route lists and nested dicts through this
    code path; a unit test pins the contract so the JSON-render path
    cannot regress to ``str()`` without a CI failure.

    Args:
        user_template: The raw ``prompts/user_template.md`` text.
        entry: One sample dict from ``dataset.json``.

    Returns:
        Rendered user-prompt string ready for the Responses API ``input``
        field.

    Raises:
        KeyError: ``str.format_map`` saw an unrecoverable format spec
            error not handled by ``_DefaultMissing``.
        ValueError: ``str.format_map`` rejected the template syntax.
    """
    rendered_fields: dict[str, str] = {}
    for key, value in entry.items():
        if isinstance(value, str):
            rendered_fields[key] = value
        else:
            rendered_fields[key] = json.dumps(value, ensure_ascii=False, indent=2)
    return user_template.format_map(_DefaultMissing(rendered_fields))


# ----------------------------------------------------------------------------
# Call kwargs builder (the conditional-reasoning invariant lives here)
# ----------------------------------------------------------------------------


def build_call_kwargs(
    *,
    family: str,
    deployment: str,
    prompt: str,
    effort: str | None,
    call_params: dict,
) -> dict:
    """Construct the ``responses.create()`` kwargs dict for one cell.

    The ``reasoning`` parameter is included **iff** ``family == "gpt-5.2"``.
    The gpt-4o branch never sends this parameter — the API rejects it with
    an error on that family. A direct unit test asserts this exact key
    layout for both branches.

    Args:
        family: Model family (``"gpt-5.2"`` or ``"gpt-4o"``).
        deployment: Resolved deployment name (sent as the ``model`` field).
        prompt: Fully-rendered prompt string.
        effort: Reasoning effort level — required for gpt-5.2, must be
            ``None`` for gpt-4o.
        call_params: Constant kwargs from the experiment YAML
            (``max_output_tokens``, ``temperature``, ``top_p``, ...).

    Returns:
        Dict suitable for ``client.responses.create(**kwargs)``.

    Raises:
        ValueError: ``family`` is unknown, or family/effort pair is invalid.
    """
    if family not in ("gpt-5.2", "gpt-4o"):
        raise ValueError(
            f"unknown family {family!r}; expected 'gpt-5.2' or 'gpt-4o'"
        )

    # Reasoning families on Foundry v1 reject ``temperature`` and ``top_p``
    # with HTTP 400 ``Unsupported parameter: 'temperature' is not supported
    # with this model.`` Surface this at config-validation time instead of
    # silently dropping the param (silent drop would hide measurement-design
    # mistakes: an operator who set temperature=0.0 expected determinism;
    # we must not pretend we honored it).
    if family in REASONING_FAMILIES:
        forbidden = [k for k in ("temperature", "top_p") if k in call_params]
        if forbidden:
            raise ValueError(
                f"family={family!r} does not accept "
                f"{', '.join(forbidden)} (Foundry v1 returns HTTP 400 "
                f"``Unsupported parameter`` for reasoning models). Remove "
                f"these keys from the experiment YAML's call_params."
            )

    kwargs: dict[str, Any] = {
        "model": deployment,
        "input": prompt,
    }
    if "max_output_tokens" in call_params:
        kwargs["max_output_tokens"] = call_params["max_output_tokens"]
    if "temperature" in call_params:
        kwargs["temperature"] = call_params["temperature"]
    if "top_p" in call_params:
        kwargs["top_p"] = call_params["top_p"]

    if family == "gpt-5.2":
        if effort is None:
            raise ValueError(
                "family=gpt-5.2 requires a non-None effort level"
            )
        # NOTE: this is the ONLY assignment to a ``reasoning`` literal dict
        # in this module; it is guarded by the gpt-5.2 family branch.
        reasoning = {"effort": effort}
        kwargs["reasoning"] = reasoning
    else:
        if effort is not None:
            raise ValueError(
                f"family={family!r} must not carry a reasoning effort "
                f"(gpt-4o has no reasoning parameter); got effort={effort!r}"
            )
    return kwargs


# ----------------------------------------------------------------------------
# Budget tracker
# ----------------------------------------------------------------------------


@dataclass
class BudgetTracker:
    """Mid-run USD ledger with a hard ceiling.

    The methodology §6 budget guard requires that the runner halt **before
    the next call** once the running total has crossed
    ``hard_ceiling_usd``. ``BudgetTracker`` encodes that rule as a tiny
    state machine so it can be unit-tested in isolation from the async
    call site.

    Attributes:
        hard_ceiling_usd: Mid-run halt threshold in USD.
        total_usd: Running total (read after each ``record``).
    """

    hard_ceiling_usd: float
    total_usd: float = 0.0

    def record(self, cost_usd: float) -> None:
        """Add ``cost_usd`` to the running total. Negative costs raise."""
        if cost_usd < 0:
            raise ValueError(
                f"BudgetTracker.record: cost_usd must be >= 0; got {cost_usd!r}"
            )
        self.total_usd += cost_usd

    @property
    def is_halted(self) -> bool:
        """True once ``total_usd >= hard_ceiling_usd`` — next call must skip."""
        return self.total_usd >= self.hard_ceiling_usd


# ----------------------------------------------------------------------------
# Pre-run cost estimate
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class EstimateResult:
    """Output of ``estimate_experiment_cost_usd``.

    Attributes:
        total_usd: Order-of-magnitude USD estimate for the experiment.
        snapshot_path: Path of the PAYG pricing snapshot used.
        source_url: ``source_url`` from the snapshot (cite in operator log).
        accessed_date: ``accessed_date`` from the snapshot.
        cells: Total cell count assumed (``N * len(efforts) * R``).
    """

    total_usd: float
    snapshot_path: str
    source_url: str
    accessed_date: str
    cells: int


def estimate_experiment_cost_usd(
    cfg: ExperimentConfig,
    *,
    pricing_dir: str | pathlib.Path = "pricing",
) -> EstimateResult:
    """Order-of-magnitude USD estimate for an experiment YAML.

    Resolves the newest PAYG snapshot deterministically (``target_date=None``
    → max ``accessed_date`` in-file, NEVER ``datetime.date.today()``), loads
    it through the schema-validating loader from
    ``scripts.cost_calculator``, and sums ``payg_cost_per_call`` across the
    ``N × len(efforts) × R`` Cartesian product.

    Args:
        cfg: Parsed experiment configuration.
        pricing_dir: Directory holding ``azure-openai-payg-*.yaml`` snapshots.

    Returns:
        ``EstimateResult`` with totals and the snapshot citation.

    Raises:
        scripts._pricing_types.SnapshotNotFoundError: No PAYG snapshot.
    """
    snapshot_path = resolve_active_snapshot(
        kind="payg", target_date=None, pricing_dir=pricing_dir
    )
    pricing = load_payg_pricing(snapshot_path)

    family = cfg.model_family
    n = cfg.dataset_size
    r = cfg.repeats
    efforts: list[str | None] = (
        cfg.sweep_efforts if family in REASONING_FAMILIES else [None]
    )

    total_usd = 0.0
    for effort in efforts:
        per_cell = TokenUsage(
            input_tokens=ESTIMATE_INPUT_TOKENS,
            cached_tokens=0.0,
            output_tokens=ESTIMATE_OUTPUT_TOKENS,
            reasoning_tokens=(
                float(PER_EFFORT_REASONING_HEURISTIC[effort])
                if (family == "gpt-5.2" and effort is not None)
                else 0.0
            ),
        )
        breakdown = payg_cost_per_call(per_cell, pricing, model=family)
        total_usd += breakdown.usd_per_request * n * r

    return EstimateResult(
        total_usd=total_usd,
        snapshot_path=str(snapshot_path),
        source_url=pricing.source_url,
        accessed_date=pricing.accessed_date,
        cells=n * len(efforts) * r,
    )


# ----------------------------------------------------------------------------
# Target path + record assembly
# ----------------------------------------------------------------------------


_FILENAME_TS_FMT = "%Y%m%dT%H%M%SZ"


def _target_path(
    *,
    runs_dir: pathlib.Path,
    timestamp_utc: datetime.datetime,
    exp_id: str,
    sample_idx: int,
    family: str,
    effort: str | None,
    repeat: int,
) -> pathlib.Path:
    """Compute the per-cell raw JSON destination path.

    Filename format::

        {YYYYMMDDTHHMMSSZ}_{exp_id}_{sample_idx:03d}_{family}_{effort}_r{repeat}.json

    ``effort`` is rendered literally as the string ``"null"`` when ``None``
    (the gpt-4o branch).
    """
    effort_token = effort if effort is not None else "null"
    ts = timestamp_utc.strftime(_FILENAME_TS_FMT)
    name = (
        f"{ts}_{exp_id}_{sample_idx:03d}_{family}_{effort_token}_r{repeat}.json"
    )
    return runs_dir / name


def build_tool_list_for_request(
    agent: AgentConfig, *, base_dir: pathlib.Path | None = None
) -> list[dict[str, Any]]:
    """Load and combine per-tool JSON schemas into a Responses-API tool list.

    Each schema file is expected to be a JSON object with ``name``,
    ``description``, and ``parameters`` keys (per the Responses API
    ``tools`` shape). The returned list is in the order declared by the
    experiment YAML, so the SHA-256 hash of the rendered list is stable
    across runs.

    Args:
        agent: Parsed AgentConfig from the experiment YAML.
        base_dir: Optional base directory for relative schema paths
            (defaults to repo root, i.e. ``pathlib.Path('.')``).

    Returns:
        List of tool-definition dicts ready for ``responses.create(tools=…)``.

    Raises:
        FileNotFoundError: A referenced schema file does not exist.
        ValueError: A schema file's content is not a JSON object or is
            missing the required keys.
    """
    base = base_dir if base_dir is not None else pathlib.Path(".")
    out: list[dict[str, Any]] = []
    for name, schema_path in zip(agent.tools, agent.tool_schema_paths):
        p = base / schema_path
        if not p.is_file():
            raise FileNotFoundError(
                f"tool schema file not found: {p} (tool={name!r})"
            )
        with p.open("r", encoding="utf-8") as fh:
            schema = json.load(fh)
        if not isinstance(schema, dict):
            raise ValueError(
                f"{p}: tool schema must be a JSON object; got {type(schema).__name__}"
            )
        for required in ("name", "parameters"):
            if required not in schema:
                raise ValueError(f"{p}: tool schema missing required key {required!r}")
        if schema["name"] != name:
            raise ValueError(
                f"{p}: tool schema name {schema['name']!r} does not match the "
                f"YAML-declared name {name!r}"
            )
        # The Responses API ``tools=`` list expects a Function-typed tool
        # definition; inject ``type: "function"`` and a default ``strict:
        # false`` (the schemas under prompts/tool_schemas/ are kept minimal
        # so they read as data documentation, not as SDK-shaped wire
        # records). The Foundry v1 ``responses.create`` endpoint requires
        # the ``type`` key on every tool item.
        wire_schema: dict[str, Any] = {
            "type": "function",
            "name": schema["name"],
            "parameters": schema["parameters"],
            "strict": False,
        }
        if "description" in schema and isinstance(schema["description"], str):
            wire_schema["description"] = schema["description"]
        out.append(wire_schema)
    return out


def tool_config_sha256(tool_list: list[dict[str, Any]]) -> str:
    """Stable SHA-256 of the tool-definition list (sorted keys, no spaces).

    Recorded as ``call_metadata.tool_config_sha256`` in every raw JSON for
    a tool-loop benchmark; the byte-identical tool-config invariant
    requires this value to be the same string across all 360 cells of a
    benchmark.
    """
    return _sha256_json(tool_list)


def _zero_usage_dict() -> dict:
    """Synthetic zero-valued ``response.usage`` dict for dry-run records.

    Mirrors the OpenAI Responses API ``usage`` shape (Foundry v1). All
    fields are explicit zeros so downstream analyze scripts can test
    aggregation logic without producing live spend.
    """
    return {
        "input_tokens": 0,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 0,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 0,
    }


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.timezone.utc)


def _iso8601_z(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------------
# Async runner core
# ----------------------------------------------------------------------------


@dataclass
class _RunState:
    """Mutable per-process state shared across cells (one runner invocation).

    Tracks last-call timestamp per deployment (cold-start detection) and per
    unique ``(system_prompt_sha, user_input_sha)`` prefix (caching audit).
    """

    last_call_per_deployment: dict[str, float] = field(default_factory=dict)
    last_time_per_prefix_sha: dict[str, float] = field(default_factory=dict)


def _usage_to_token_usage(usage_dict: dict) -> TokenUsage:
    """Map a Foundry v1 Responses API ``usage`` dict → ``TokenUsage``.

    The Responses API exposes:

      * ``input_tokens`` (total input, includes cached subset)
      * ``input_tokens_details.cached_tokens``
      * ``output_tokens`` (total output, includes reasoning subset)
      * ``output_tokens_details.reasoning_tokens``

    Missing or null nested objects are treated as zeros — those token
    categories simply did not appear for this call. The mapping is a pure
    function so it can be unit-tested without an SDK install.

    Args:
        usage_dict: ``response.usage.model_dump()`` output.

    Returns:
        ``TokenUsage`` suitable for ``payg_cost_per_call``.
    """
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


async def _execute_cell(
    *,
    cfg: ExperimentConfig,
    sample: Sample,
    effort: str | None,
    repeat: int,
    deployment: str,
    endpoint_value: str,
    system_prompt: str,
    git_commit: str,
    dirty: bool,
    runs_dir: pathlib.Path,
    budget: BudgetTracker,
    run_state: _RunState,
    client: Any,
    dry_run: bool,
    pricing_snapshot_path: str,
    pricing: PaygPricing,
    captured_call_kwargs: list[dict],
    tool_cfg_sha: str | None = None,
    tool_list_for_request: list[dict[str, Any]] | None = None,
) -> dict:
    """Execute (or simulate) one ``(sample, effort, repeat)`` cell.

    Writes a single JSON record to ``runs_dir`` and returns the parsed
    record for in-test inspection. Mutates ``run_state``. For live calls
    (``dry_run=False``) the per-cell USD cost is computed from the
    returned ``usage`` dict via ``payg_cost_per_call`` and committed to
    ``budget`` so the mid-run hard-ceiling guard fires before the next
    cell's API call.

    Tool-loop mode: when ``tool_cfg_sha`` is non-None the runner is in
    tool-loop mode (``agent.tool_loop: true``). The cell JSON gains a
    ``tool_calls`` array preserving the per-iteration trajectory and the
    ``call_metadata.tool_config_sha256`` field is set to ``tool_cfg_sha``
    (the single value shared across every cell in the benchmark). Under
    ``dry_run=True`` the trajectory is an empty list (skeleton with
    ``tool_loop_terminated="dry_run_skeleton"``); under ``dry_run=False``
    the live ReAct-style tool-loop body in :func:`_live_tool_loop_call`
    populates the trajectory and accumulates per-iteration usage into
    the cell's top-level ``usage`` dict.

    Raises:
        FilenameCollisionError: Target file already exists.
    """
    family = cfg.model_family
    prompt_for_call = system_prompt + "\n\n" + sample.user_input

    call_kwargs = build_call_kwargs(
        family=family,
        deployment=deployment,
        prompt=prompt_for_call,
        effort=effort,
        call_params=cfg.call_params,
    )
    captured_call_kwargs.append(call_kwargs)

    system_sha = sha256_text(system_prompt)
    user_sha = sha256_text(sample.user_input)
    prefix_sha = sha256_text(system_sha + ":" + user_sha)

    cell_id = (
        f"{sample.sample_idx:03d}/{family}/"
        f"{effort if effort is not None else 'null'}/r{repeat}"
    )

    timestamp_utc = _utc_now()
    target = _target_path(
        runs_dir=runs_dir,
        timestamp_utc=timestamp_utc,
        exp_id=cfg.experiment_id,
        sample_idx=sample.sample_idx,
        family=family,
        effort=effort,
        repeat=repeat,
    )
    if target.exists():
        raise FilenameCollisionError(
            f"COLLISION_ABORT target file already exists: {target} "
            "(append-only rule §7; move or delete the prior run explicitly)"
        )

    now_mono = time.monotonic()
    prior_prefix_time = run_state.last_time_per_prefix_sha.get(prefix_sha)
    time_since_prefix = (
        (now_mono - prior_prefix_time) if prior_prefix_time is not None else None
    )
    prior_deployment_time = run_state.last_call_per_deployment.get(deployment)
    cold_start = (
        prior_deployment_time is None
        or (now_mono - prior_deployment_time) > COLD_START_GAP_SECONDS
    )

    logger.info(
        "CELL_BEGIN experiment=%s cell=%s family=%s effort=%s repeat=%d dry_run=%s",
        cfg.experiment_id,
        cell_id,
        family,
        effort,
        repeat,
        dry_run,
    )

    started_at = time.monotonic()
    tool_calls_trajectory: list[dict[str, Any]] = []
    tool_loop_terminated: str | None = None
    if dry_run:
        usage_dict: dict[str, Any] = _zero_usage_dict()
        response_text = ""
        retry_count = 0
        cell_cost_usd = 0.0
        if tool_cfg_sha is not None:
            tool_loop_terminated = "dry_run_skeleton"
    else:
        if tool_cfg_sha is not None:
            # Task 010 live tool-loop branch: dispatch through
            # :mod:`scripts.tools` ``TOOL_REGISTRY`` and accumulate
            # per-iteration usage into a single summed cell ``usage`` dict.
            assert tool_list_for_request is not None, (
                "tool_cfg_sha set but tool_list_for_request is None — "
                "run_experiment must construct both together"
            )
            (
                usage_dict,
                response_text,
                retry_count,
                tool_calls_trajectory,
                tool_loop_terminated,
            ) = await _live_tool_loop_call(
                client=client,
                base_call_kwargs=call_kwargs,
                tool_list_for_request=tool_list_for_request,
                max_iterations=(
                    cfg.agent.max_tool_iterations
                    if cfg.agent is not None
                    else 4
                ),
                search_kb_path=(
                    cfg.agent.search_kb_path if cfg.agent is not None else None
                ),
                cell_id=cell_id,
                system_prompt=system_prompt,
                user_input=sample.user_input,
            )
        else:
            usage_dict, response_text, retry_count = await _live_call(
                client=client, call_kwargs=call_kwargs, cell_id=cell_id
            )
        # Methodology §6: every live call commits its USD cost to the
        # running ledger BEFORE the next cell starts, so the hard-ceiling
        # guard can fire. Skip when reasoning_tokens > 0 on gpt-4o would
        # raise Gpt4oReasoningError — that is a data-integrity failure,
        # NOT a billing question, so we let it propagate.
        tu = _usage_to_token_usage(usage_dict)
        cell_cost_usd = payg_cost_per_call(
            tu, pricing, model=family
        ).usd_per_request
        budget.record(cell_cost_usd)
        logger.info(
            "CELL_COST experiment=%s cell=%s usd=%.6f running_total_usd=%.4f",
            cfg.experiment_id,
            cell_id,
            cell_cost_usd,
            budget.total_usd,
        )
    latency_ms = (time.monotonic() - started_at) * 1000.0

    run_state.last_time_per_prefix_sha[prefix_sha] = time.monotonic()
    run_state.last_call_per_deployment[deployment] = time.monotonic()

    record: dict[str, Any] = {
        "experiment_id": cfg.experiment_id,
        "git_commit": git_commit,
        "dirty": dirty,
        "timestamp_utc": _iso8601_z(timestamp_utc),
        "endpoint": endpoint_value,
        "auth_mode": "entra",
        "api_version": FOUNDRY_API_VERSION,
        "model": family,
        "deployment_name": deployment,
        "sample_idx": sample.sample_idx,
        "sample_id": sample.sample_id,
        "effort": effort,
        "repeat": repeat,
        "dry_run": dry_run,
        "latency_ms": latency_ms,
        "retry_count": retry_count,
        "cold_start": cold_start,
        "usage": usage_dict,
        "call_metadata": {
            "system_prompt_sha256": system_sha,
            "user_input_sha256": user_sha,
            "tool_config_sha256": tool_cfg_sha,
            "time_since_last_identical_prefix_seconds": time_since_prefix,
            "deployment_cold_start": cold_start,
        },
        "pricing_snapshot_path": pricing_snapshot_path,
        "sample_metadata": sample.sample_metadata,
    }
    if tool_cfg_sha is not None:
        # Tool-loop mode: cell carries the per-iteration trajectory. Live
        # runs populate ``tool_calls`` via :func:`_live_tool_loop_call`
        # above; dry runs emit an empty skeleton with
        # ``tool_loop_terminated="dry_run_skeleton"``.
        record["tool_calls"] = tool_calls_trajectory
        record["tool_loop_terminated"] = (
            tool_loop_terminated if tool_loop_terminated is not None else "ok"
        )
    if cfg.capture.get("response_text", False):
        record["response_text"] = response_text

    target.parent.mkdir(parents=True, exist_ok=True)
    # Re-check collision under the parent mkdir; another process may have
    # created the file in between. ``x`` mode raises FileExistsError if so.
    try:
        with target.open("x", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)
    except FileExistsError as exc:
        raise FilenameCollisionError(
            f"COLLISION_ABORT target file already exists: {target}"
        ) from exc

    logger.info(
        "CELL_END experiment=%s cell=%s latency_ms=%.1f retries=%d cold=%s",
        cfg.experiment_id,
        cell_id,
        latency_ms,
        retry_count,
        cold_start,
    )
    return record


async def _live_call(
    *,
    client: Any,
    call_kwargs: dict,
    cell_id: str,
) -> tuple[dict, str, int]:
    """Execute one live Responses API call with exponential-backoff retry.

    429 responses retry with delays 1s, 2s, 4s, 8s, 16s (capped at
    ``RATE_LIMIT_MAX_ATTEMPTS`` attempts). The retry sequence is logged;
    429 attempts are **not** counted as failed measurements per the
    methodology rule.

    Args:
        client: An ``openai.AsyncAzureOpenAI`` instance.
        call_kwargs: Pre-built ``responses.create()`` kwargs.
        cell_id: Cell identifier for log correlation.

    Returns:
        Tuple of (usage_dict, response_text, retry_count).
    """
    last_exc: Exception | None = None
    for attempt in range(RATE_LIMIT_MAX_ATTEMPTS):
        try:
            response = await client.responses.create(**call_kwargs)
        except Exception as exc:
            status = getattr(exc, "status_code", None) or getattr(
                exc, "status", None
            )
            if status == 429 and attempt < RATE_LIMIT_MAX_ATTEMPTS - 1:
                delay = RATE_LIMIT_BASE_DELAY_S * (2**attempt)
                logger.warning(
                    "RATE_LIMIT cell=%s attempt=%d delay_s=%.1f", cell_id, attempt, delay
                )
                await asyncio.sleep(delay)
                last_exc = exc
                continue
            raise
        usage_obj = getattr(response, "usage", None)
        if usage_obj is None:
            usage_dict: dict = {}
        elif hasattr(usage_obj, "model_dump"):
            usage_dict = usage_obj.model_dump()
        else:
            usage_dict = dict(usage_obj)  # best-effort fallback
        response_text = getattr(response, "output_text", "") or ""
        return usage_dict, response_text, attempt
    # Exhausted retries
    assert last_exc is not None
    raise last_exc


def _sum_usage(into: dict, add: dict) -> dict:
    """Add ``add`` token totals into ``into`` (Foundry v1 Responses shape).

    Sums ``input_tokens``, ``output_tokens``,
    ``input_tokens_details.cached_tokens``, and
    ``output_tokens_details.reasoning_tokens``. ``total_tokens`` is
    recomputed as ``input + output``. Missing nested objects are treated
    as zero. Returns the mutated ``into`` for chaining.
    """
    into["input_tokens"] = int(into.get("input_tokens", 0)) + int(
        add.get("input_tokens", 0) or 0
    )
    into["output_tokens"] = int(into.get("output_tokens", 0)) + int(
        add.get("output_tokens", 0) or 0
    )
    in_det = into.setdefault("input_tokens_details", {"cached_tokens": 0})
    add_in_det = add.get("input_tokens_details") or {}
    in_det["cached_tokens"] = int(in_det.get("cached_tokens", 0)) + int(
        add_in_det.get("cached_tokens", 0) or 0
    )
    add_out_det = add.get("output_tokens_details") or {}
    add_reasoning = int(add_out_det.get("reasoning_tokens", 0) or 0)
    if add_reasoning > 0 or "output_tokens_details" in into:
        out_det = into.setdefault("output_tokens_details", {"reasoning_tokens": 0})
        out_det["reasoning_tokens"] = int(
            out_det.get("reasoning_tokens", 0)
        ) + add_reasoning
    into["total_tokens"] = into["input_tokens"] + into["output_tokens"]
    return into


def _extract_output_items(response: Any) -> list[Any]:
    """Return ``response.output`` as a Python list, robust to SDK shape.

    The OpenAI SDK exposes ``Response.output`` as a typed list of items
    that may include ``ResponseFunctionToolCall``, message items, and
    reasoning items. We iterate and normalize each entry to a plain
    Python dict via ``model_dump`` (or ``__dict__`` fallback) so the
    runner can introspect ``type``, ``name``, ``arguments``, etc. without
    binding to specific SDK class names.
    """
    items = getattr(response, "output", None)
    if items is None:
        return []
    out: list[Any] = []
    for it in items:
        if hasattr(it, "model_dump"):
            out.append(it.model_dump())
        elif isinstance(it, dict):
            out.append(it)
        else:
            out.append(dict(getattr(it, "__dict__", {})))
    return out


def _sanitize_assistant_item_for_input(item: dict[str, Any]) -> dict[str, Any] | None:
    """Project a model-emitted output item to the strict ``input`` schema.

    The Foundry v1 Responses API rejects model-emitted items fed back
    verbatim — fields like ``id``, ``status``, ``namespace``,
    ``summary`` on reasoning items, and even ``content`` shape on
    message items can violate the input schema with HTTP 400
    ``invalid_payload``. This helper keeps only the fields the input
    schema explicitly allows, returning ``None`` for items whose type
    is not a valid input-side item (e.g. raw reasoning items that the
    server does not want to see again).
    """
    t = item.get("type")
    if t == "function_call":
        call_id = item.get("call_id")
        if not call_id:
            return None
        out = {
            "type": "function_call",
            "name": item.get("name", ""),
            "call_id": call_id,
            "arguments": item.get("arguments", "") or "",
        }
        return out
    if t == "message":
        # Assistant message: keep role + a textual content payload. The
        # SDK emits ``content`` as a list of output_text parts; collapse
        # to plain strings on the input side.
        role = item.get("role", "assistant")
        content_raw = item.get("content")
        parts: list[dict[str, Any]] = []
        if isinstance(content_raw, list):
            for c in content_raw:
                if not isinstance(c, dict):
                    continue
                text = c.get("text")
                if isinstance(text, str):
                    parts.append({"type": "output_text", "text": text})
        if not parts:
            return None
        return {"type": "message", "role": role, "content": parts}
    # Reasoning items and any other unknown types are NOT echoed back —
    # the server keeps reasoning state implicitly when previous items
    # are present, and echoing them back trips invalid_payload.
    return None


def _tool_call_summary(result: str, *, max_len: int = 400) -> str:
    """Truncate a tool's string output for the audit trail."""
    if not isinstance(result, str):
        result = str(result)
    if len(result) > max_len:
        return result[:max_len] + "...[truncated]"
    return result


async def _live_tool_loop_call(
    *,
    client: Any,
    base_call_kwargs: dict,
    tool_list_for_request: list[dict[str, Any]],
    max_iterations: int,
    search_kb_path: str | None,
    cell_id: str,
    system_prompt: str | None = None,
    user_input: str | None = None,
) -> tuple[dict, str, int, list[dict[str, Any]], str]:
    """Execute one ReAct-style tool-loop cell against the live Foundry v1 API.

    Loop semantics (per Task 010 spec):

    1. Initial ``responses.create`` with ``tools=tool_list_for_request``,
       ``instructions=<system_prompt>`` (when supplied — avoids tripping
       Azure's jailbreak content-filter on tool-rich system text), and
       ``input=<user_input>`` as a single user message.
    2. If the response emits one or more ``function_call`` items, dispatch
       each through :data:`scripts.tools.TOOL_REGISTRY` (the ``web_search``
       callable is rebuilt against ``search_kb_path`` so canned KB hits are
       honored). Append a ``function_call_output`` item per call.
    3. Feed the assembled list (original input + every assistant output so
       far + every function_call_output) back as the next ``input=``.
    4. Repeat until the response emits no function_call items, or
       ``iterations >= max_iterations``. On the iteration cap, fire one
       final call WITHOUT ``tools=`` so the model is forced to emit a
       final answer from what it already has;
       ``tool_loop_terminated="iteration_cap"`` is recorded and the
       cap-recovery call is appended as a trajectory row with
       ``tool_name=None`` / ``tool_args=None`` so the audit trail
       captures that the recovery leg actually happened.
    5. Per-iteration usage objects are summed into a single cell-level
       ``usage`` dict (Foundry v1 shape) so the downstream analyzer sees
       the same schema as single-shot benchmarks.

    A tool callable that raises an exception is captured: the exception
    message becomes the tool's ``output`` string fed back to the model.
    This matches the spec's "tool-call failure is captured as the tool's
    tool_result_summary and fed back to the model — the model is
    responsible for recovery". The cell is still a completed measurement.

    Args:
        client: An ``openai.AsyncOpenAI`` instance.
        base_call_kwargs: ``responses.create`` kwargs (model, input,
            reasoning, max_output_tokens, …). ``tools`` is added by this
            function; the caller MUST NOT include it. ``input`` here is
            only used when ``user_input`` is None (legacy fallback path
            used by the test fixtures).
        tool_list_for_request: Function-typed tools list (Responses API
            wire shape — see :func:`build_tool_list_for_request`).
        max_iterations: Hard cap on tool-loop iterations
            (``agent.max_tool_iterations``). Required; must be >= 1.
        search_kb_path: Filesystem path to the canned ``web_search`` KB
            (or ``None`` if ``web_search`` is not registered).
        cell_id: Diagnostic label for log correlation.
        system_prompt: System text sent via ``instructions=`` (separate
            channel from user content; this is what prevents Azure's
            jailbreak filter from treating tool-rich system instructions
            as user-supplied prompt injection). When ``None``, the legacy
            "combined-string in ``input=``" path is used (kept for the
            test fixtures, which exercise the loop end-to-end without
            real Azure).
        user_input: The byte-identical user-input string. When provided
            it replaces ``base_call_kwargs['input']`` as the seed of the
            assistant conversation. The ``sha256(user_input)`` is also
            what backs the byte-identical-prompt invariant on the cell
            record.

    Returns:
        Tuple ``(summed_usage, final_response_text, retry_count_initial,
        tool_calls_trajectory, tool_loop_terminated)``.
    """
    from scripts.tools import TOOL_REGISTRY, load_search_kb, make_web_search  # noqa: PLC0415

    # Build a per-call dispatch map. The default ``web_search`` callable
    # in TOOL_REGISTRY always returns ``"no results"``; rebind to the
    # KB-aware variant when a KB path is provided.
    dispatch: dict[str, Any] = dict(TOOL_REGISTRY)
    if search_kb_path is not None and "web_search" in dispatch:
        try:
            kb = load_search_kb(search_kb_path)
            dispatch["web_search"] = make_web_search(kb)
        except (FileNotFoundError, ValueError) as exc:
            logger.warning(
                "TOOL_LOOP_KB_LOAD_FAILED cell=%s path=%s err=%s",
                cell_id,
                search_kb_path,
                exc,
            )
            # Continue with the no-results stub; the model will see a
            # cache miss and the tool-efficiency score will reflect it.

    # Decide on the conversation seed. Live runs pass system_prompt +
    # user_input separately so Azure's content-filter does not parse
    # tool-rich system text as user content (which triggers a jailbreak
    # false positive). Tests still drive the loop with a combined string
    # in base_call_kwargs['input'] when no separated text is passed in.
    seed_user_text: str
    use_instructions = False
    if user_input is not None:
        seed_user_text = user_input
        use_instructions = system_prompt is not None
    else:
        legacy_input = base_call_kwargs.get("input")
        if not isinstance(legacy_input, str):
            raise ValueError(
                f"_live_tool_loop_call expects base_call_kwargs['input'] to "
                f"be a string prompt when user_input is None; got "
                f"{type(legacy_input).__name__}"
            )
        seed_user_text = legacy_input

    # ``input_chain`` is the accumulating list of items for the NEXT request.
    input_chain: list[Any] = [
        {"role": "user", "content": seed_user_text},
    ]

    summed_usage: dict[str, Any] = {
        "input_tokens": 0,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 0,
        "total_tokens": 0,
    }
    saw_reasoning = False
    trajectory: list[dict[str, Any]] = []
    initial_retry_count = 0
    tool_loop_terminated: str = "ok"
    final_response_text: str = ""

    for iteration in range(1, max_iterations + 1):
        iter_kwargs = dict(base_call_kwargs)
        iter_kwargs["input"] = input_chain
        iter_kwargs["tools"] = tool_list_for_request
        if use_instructions and system_prompt is not None:
            iter_kwargs["instructions"] = system_prompt

        iter_started = time.monotonic()
        iter_usage_dict, iter_text, iter_retries, iter_output = (
            await _one_responses_call(client, iter_kwargs, cell_id, iteration)
        )
        iter_latency_ms = (time.monotonic() - iter_started) * 1000.0

        if iteration == 1:
            initial_retry_count = iter_retries

        _sum_usage(summed_usage, iter_usage_dict)
        out_det = iter_usage_dict.get("output_tokens_details") or {}
        if int(out_det.get("reasoning_tokens", 0) or 0) > 0:
            saw_reasoning = True

        function_calls = [
            it for it in iter_output if it.get("type") == "function_call"
        ]

        if not function_calls:
            final_response_text = iter_text or ""
            break

        for it in iter_output:
            sanitized = _sanitize_assistant_item_for_input(it)
            if sanitized is not None:
                input_chain.append(sanitized)

        for fc in function_calls:
            tname = fc.get("name", "")
            args_str = fc.get("arguments", "") or ""
            call_id = fc.get("call_id", "")
            tool_args: dict[str, Any] = {}
            tool_result: str = ""
            try:
                if args_str:
                    parsed_args = json.loads(args_str)
                    if isinstance(parsed_args, dict):
                        tool_args = parsed_args
                if tname not in dispatch:
                    tool_result = (
                        f"error: unknown tool {tname!r}; allowed tools are "
                        f"{sorted(dispatch)}"
                    )
                else:
                    callable_obj = dispatch[tname]
                    try:
                        tool_result = str(callable_obj(**tool_args))
                    except TypeError as exc:
                        tool_result = f"error: bad arguments for {tname}: {exc}"
                    except Exception as exc:  # noqa: BLE001
                        tool_result = f"error: {type(exc).__name__}: {exc}"
            except json.JSONDecodeError as exc:
                tool_result = f"error: malformed JSON arguments: {exc}"

            trajectory.append(
                {
                    "iteration": iteration,
                    "tool_name": tname,
                    "tool_args": tool_args,
                    "tool_result_summary": _tool_call_summary(tool_result),
                    "latency_ms": iter_latency_ms,
                    "usage": iter_usage_dict,
                }
            )
            input_chain.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": tool_result,
                }
            )
    else:
        tool_loop_terminated = "iteration_cap"
        final_kwargs = dict(base_call_kwargs)
        final_kwargs["input"] = input_chain
        # Force a final answer: per Task 017 spec the cap-recovery call
        # MUST omit ``tools=`` so the model can no longer emit another
        # function_call and is required to answer from what it already
        # has. ``base_call_kwargs`` never contains ``tools=`` (the loop
        # only adds it onto ``iter_kwargs``), but defend explicitly so a
        # future refactor of the call-kwargs builder cannot regress this.
        final_kwargs.pop("tools", None)
        if use_instructions and system_prompt is not None:
            final_kwargs["instructions"] = system_prompt
        final_started = time.monotonic()
        final_usage, final_text, _retries, _items = await _one_responses_call(
            client, final_kwargs, cell_id, max_iterations + 1
        )
        final_latency_ms = (time.monotonic() - final_started) * 1000.0
        _sum_usage(summed_usage, final_usage)
        out_det = final_usage.get("output_tokens_details") or {}
        if int(out_det.get("reasoning_tokens", 0) or 0) > 0:
            saw_reasoning = True
        final_response_text = final_text or ""
        # Per Task 017 Implementation Notes: the forced final-answer call
        # counts as an iteration on the trajectory so the audit trail
        # records that the cap-recovery leg actually happened. ``tool_name``
        # and ``tool_args`` are ``None`` (no tool was dispatched);
        # ``tool_result_summary`` carries the final-answer text. Its usage
        # is the per-iteration value already summed into ``summed_usage``
        # above — capture the per-iteration usage on the row so the
        # downstream usage-summation invariant holds bit-for-bit. The row
        # carries ONLY the Task 010 key set
        # ``{iteration, tool_name, tool_args, tool_result_summary,
        # latency_ms, usage}`` — no audit-only fields are appended, so the
        # row passes the explicit jsonschema ``additionalProperties: false``
        # check the unit tests enforce.
        trajectory.append(
            {
                "iteration": max_iterations + 1,
                "tool_name": None,
                "tool_args": None,
                "tool_result_summary": _tool_call_summary(final_response_text),
                "latency_ms": final_latency_ms,
                "usage": final_usage,
            }
        )

    if not saw_reasoning:
        # Even when no iteration emitted reasoning tokens, keep the
        # ``output_tokens_details.reasoning_tokens: 0`` block so the
        # analyzer's schema audit (which requires the key on every
        # Foundry v1 cell) is satisfied — matching the per-iteration
        # API shape benchmarks 01/02 use.
        summed_usage["output_tokens_details"] = {"reasoning_tokens": 0}
    summed_usage["total_tokens"] = (
        summed_usage["input_tokens"] + summed_usage["output_tokens"]
    )

    logger.info(
        "TOOL_LOOP_END cell=%s iterations=%d terminated=%s trajectory_len=%d",
        cell_id,
        min(iteration, max_iterations),
        tool_loop_terminated,
        len(trajectory),
    )
    return (
        summed_usage,
        final_response_text,
        initial_retry_count,
        trajectory,
        tool_loop_terminated,
    )


async def _one_responses_call(
    client: Any,
    call_kwargs: dict,
    cell_id: str,
    iteration: int,
) -> tuple[dict, str, int, list[dict[str, Any]]]:
    """One ``responses.create`` invocation with 429 backoff.

    Returns ``(usage_dict, output_text, retry_count, output_items)``.
    """
    last_exc: Exception | None = None
    for attempt in range(RATE_LIMIT_MAX_ATTEMPTS):
        try:
            response = await client.responses.create(**call_kwargs)
        except Exception as exc:
            status = getattr(exc, "status_code", None) or getattr(
                exc, "status", None
            )
            if status == 429 and attempt < RATE_LIMIT_MAX_ATTEMPTS - 1:
                delay = RATE_LIMIT_BASE_DELAY_S * (2**attempt)
                logger.warning(
                    "RATE_LIMIT cell=%s iter=%d attempt=%d delay_s=%.1f",
                    cell_id,
                    iteration,
                    attempt,
                    delay,
                )
                await asyncio.sleep(delay)
                last_exc = exc
                continue
            raise
        usage_obj = getattr(response, "usage", None)
        if usage_obj is None:
            usage_dict: dict = {}
        elif hasattr(usage_obj, "model_dump"):
            usage_dict = usage_obj.model_dump()
        else:
            usage_dict = dict(usage_obj)
        output_text = getattr(response, "output_text", "") or ""
        output_items = _extract_output_items(response)
        return usage_dict, output_text, attempt, output_items
    assert last_exc is not None
    raise last_exc


async def _run_async(
    *,
    cfg: ExperimentConfig,
    runs_dir: pathlib.Path,
    samples: list[Sample],
    system_prompt: str,
    deployment: str,
    endpoint_value: str,
    git_commit: str,
    dirty: bool,
    dry_run: bool,
    pricing_snapshot_path: str,
    pricing: PaygPricing,
    captured_call_kwargs: list[dict],
    tool_cfg_sha: str | None = None,
    tool_list_for_request: list[dict[str, Any]] | None = None,
    skip_existing: bool = False,
) -> tuple[int, BudgetTracker]:
    """Async core. Returns (cells_written, budget_tracker).

    Concurrency is real: every ``(sample, effort, repeat)`` cell is wrapped
    in a coroutine and scheduled via ``asyncio.gather``. An
    ``asyncio.Semaphore`` of size ``cfg.concurrency`` actually limits the
    number of in-flight ``client.responses.create`` invocations. Each cell
    re-checks ``budget.is_halted`` *inside* its semaphore slot so the
    hard-ceiling guard skips not-yet-started cells once the running USD
    total crosses ``budget.hard_ceiling_usd`` (per methodology §6 — the
    "halt before the next call" rule).

    Per-cell raw-response JSON paths embed ``(sample_idx, family, effort,
    repeat)``; the set of records written is therefore invariant to the
    scheduling order. The determinism unit test exercises this by running
    the same synthetic experiment at concurrency=1 and concurrency=5 and
    asserting identical ``call_metadata`` digests across the two runs.
    """
    client: Any = None
    if not dry_run:
        client = _build_live_client(endpoint_value=endpoint_value)

    budget = BudgetTracker(hard_ceiling_usd=cfg.budget_hard_ceiling_usd)
    run_state = _RunState()
    semaphore = asyncio.Semaphore(cfg.concurrency)

    _SKIPPED = object()  # sentinel for budget-halted cells

    # Build an "already-on-disk" index of (sample_idx, family, effort,
    # repeat) tuples so a previously-killed run can resume without
    # double-firing cells that already wrote a JSON. Each filename
    # carries (sample_idx, family, effort, repeat) deterministically
    # (see :func:`_target_path`) so we can rebuild this set with a
    # cheap directory scan.
    existing_keys: set[tuple[int, str, str | None, int]] = set()
    if skip_existing and runs_dir.is_dir():
        for path in runs_dir.glob("*.json"):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    d = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(d, dict):
                continue
            try:
                key = (
                    int(d["sample_idx"]),
                    str(d["model"]),
                    d.get("effort"),
                    int(d["repeat"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            existing_keys.add(key)

    async def _bound_cell(
        sample: Sample, effort: str | None, repeat: int
    ) -> dict | object:
        async with semaphore:
            if budget.is_halted:
                # Halt rule §6: stop scheduling new API calls once the
                # running total has crossed the hard ceiling. The
                # one-shot BUDGET_HALT log line is emitted after gather()
                # so the operator sees a single citation per run.
                return _SKIPPED
            if skip_existing and (
                sample.sample_idx,
                cfg.model_family,
                effort,
                repeat,
            ) in existing_keys:
                logger.info(
                    "CELL_SKIP_EXISTING experiment=%s sample=%d effort=%s repeat=%d",
                    cfg.experiment_id,
                    sample.sample_idx,
                    effort,
                    repeat,
                )
                return _SKIPPED
            return await _execute_cell(
                cfg=cfg,
                sample=sample,
                effort=effort,
                repeat=repeat,
                deployment=deployment,
                endpoint_value=endpoint_value,
                system_prompt=system_prompt,
                git_commit=git_commit,
                dirty=dirty,
                runs_dir=runs_dir,
                budget=budget,
                run_state=run_state,
                client=client,
                dry_run=dry_run,
                pricing_snapshot_path=pricing_snapshot_path,
                pricing=pricing,
                captured_call_kwargs=captured_call_kwargs,
                tool_cfg_sha=tool_cfg_sha,
                tool_list_for_request=tool_list_for_request,
            )

    coroutines = [
        _bound_cell(sample, effort, repeat)
        for sample in samples
        for effort in cfg.sweep_efforts
        for repeat in range(cfg.repeats)
    ]

    results = await asyncio.gather(*coroutines, return_exceptions=True)

    # Surface the first non-budget exception (filename collision, schema
    # error, gpt-4o reasoning-token integrity failure, …). These are hard
    # aborts that cannot be reconciled by skipping cells.
    for r in results:
        if isinstance(r, BaseException) and not isinstance(
            r, BudgetExceededError
        ):
            raise r

    # If the budget halted at any point — either via in-flight
    # ``budget.record`` crossing the ceiling, or via skipped cells — emit
    # one BUDGET_HALT log line and raise so the CLI maps to exit code 1.
    if budget.is_halted:
        logger.error(
            "BUDGET_HALT experiment=%s total_usd=%.4f ceiling_usd=%.4f",
            cfg.experiment_id,
            budget.total_usd,
            budget.hard_ceiling_usd,
        )
        raise BudgetExceededError(
            f"running total {budget.total_usd:.4f} >= "
            f"hard ceiling {budget.hard_ceiling_usd:.4f}"
        )

    cells_written = sum(1 for r in results if isinstance(r, dict))
    return cells_written, budget


def _build_live_client(*, endpoint_value: str) -> Any:
    """Instantiate the Foundry v1 ``AsyncOpenAI`` client (Entra ID auth).

    Foundry v1 is served at ``<endpoint>/openai/v1/`` and accepts a standard
    OpenAI client construction — NOT the classic ``AsyncAzureOpenAI`` client,
    which targets ``*.openai.azure.com/openai/responses`` and 404s / returns
    "API version not supported" against the Foundry v1 surface. The Entra ID
    audience for Foundry v1 is ``https://ai.azure.com/.default``; the classic
    audience ``https://cognitiveservices.azure.com/.default`` produces a 401
    ``audience is incorrect (https://ai.azure.com)`` against this endpoint.

    The bearer token is acquired at client construction time and embedded as
    ``api_key``. Tokens have a ~60-minute TTL; runs longer than the TTL must
    rebuild the client (acceptable for the 6-call smoke and the 300-call
    Task 007 full run; revisit for any multi-hour run).

    Lazy-imports the SDK so dry-run execution does not require the
    ``openai`` / ``azure.identity`` packages to be initialized.
    """
    from azure.identity import (  # noqa: PLC0415 (lazy import: live path only)
        DefaultAzureCredential,
        get_bearer_token_provider,
    )
    from openai import AsyncOpenAI  # noqa: PLC0415

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(),
        "https://ai.azure.com/.default",
    )
    # FOUNDRY_API_VERSION remains the "preview" methodology label, recorded
    # verbatim in every raw JSON's ``api_version`` field for the audit trail.
    # The actual URL path "/openai/v1/" is the wire-level surface that serves
    # the preview channel of Foundry v1 — no api-version query parameter is
    # appended; the path encodes the version.
    assert FOUNDRY_API_VERSION == "preview", (
        "FOUNDRY_API_VERSION drift: must remain the literal 'preview'"
    )
    base_url = endpoint_value.rstrip("/") + "/openai/v1/"
    # api_version="preview" — methodology label recorded verbatim in every
    # raw JSON's api_version field; the wire-level "/openai/v1/" path is the
    # Foundry v1 preview surface, so no api-version query parameter is sent.
    return AsyncOpenAI(
        base_url=base_url,
        api_key=token_provider(),
    )


# ----------------------------------------------------------------------------
# High-level entry point (sync wrapper around async)
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    """Return value of ``run_experiment`` for tests + CLI summary."""

    cells_written: int
    runs_dir: pathlib.Path
    estimate: EstimateResult
    captured_call_kwargs: list[dict]


def run_experiment(
    *,
    cfg: ExperimentConfig,
    benchmarks_root: pathlib.Path,
    pricing_dir: pathlib.Path,
    dry_run: bool,
    max_samples: int | None,
    allow_dirty: bool,
    env: dict[str, str] | None = None,
    skip_existing: bool = False,
) -> RunResult:
    """Top-level entry point — synchronous wrapper around the async core.

    Args:
        cfg: Parsed experiment YAML.
        benchmarks_root: Root directory holding ``<benchmark>/dataset.json``.
        pricing_dir: Directory holding PAYG pricing snapshots.
        dry_run: If True, no HTTPS call is made; synthetic zero-usage
            records are still written to ``runs/``.
        max_samples: Optional cap on samples drawn from the dataset (also
            capped by ``cfg.dataset_size``).
        allow_dirty: If True, dirty/no-repo states are tolerated.
        env: Optional env-var mapping (defaults to ``os.environ``).

    Returns:
        ``RunResult`` with cells_written, runs_dir, pre-run estimate, and
        captured ``call_kwargs`` (for in-test introspection).

    Raises:
        EndpointMisconfiguredError: Missing/empty Azure env vars (exit 2).
        DatasetMissingError: Missing dataset/prompt files (exit 3).
        BudgetExceededError: Pre-run estimate over MAX or mid-run halt (exit 1).
    """
    src_env = env if env is not None else dict(os.environ)

    # Resolve endpoint + deployment env vars. The endpoint VALUE is read,
    # embedded in every JSON record, and NEVER logged (the env var NAME
    # may be logged).
    endpoint_value = _require_env(cfg.model_endpoint_env, env=src_env)
    deployment = _resolve_env_template(cfg.model_deployment_template, env=src_env)
    if not deployment:
        raise EndpointMisconfiguredError(
            f"deployment resolved to empty string from "
            f"{cfg.model_deployment_template!r}"
        )

    # Pre-run estimate (always — even in dry-run, so the operator sees the
    # budget shape before any live invocation). The loaded ``PaygPricing``
    # snapshot is also reused below for mid-run per-cell cost recording, so
    # the running USD ledger is computed against the SAME snapshot the
    # estimator cited (no drift between pre-run and during-run pricing).
    estimate = estimate_experiment_cost_usd(cfg, pricing_dir=pricing_dir)
    pricing = load_payg_pricing(estimate.snapshot_path)
    logger.info(
        "PRE_RUN_ESTIMATE experiment=%s cells=%d total_usd=%.4f "
        "snapshot_path=%s pricing_source_url=%s pricing_accessed_date=%s",
        cfg.experiment_id,
        estimate.cells,
        estimate.total_usd,
        estimate.snapshot_path,
        estimate.source_url,
        estimate.accessed_date,
    )

    # MAX_COST_PER_BENCHMARK_USD enforcement (pre-run). Override available
    # via ``budget.confirmed: true`` in the YAML.
    max_per_benchmark_raw = src_env.get(ENV_MAX_COST_PER_BENCHMARK_NAME)
    if max_per_benchmark_raw is not None:
        try:
            max_per_benchmark = float(max_per_benchmark_raw)
        except ValueError as exc:
            raise EndpointMisconfiguredError(
                f"{ENV_MAX_COST_PER_BENCHMARK_NAME} must parse as float; "
                f"got {max_per_benchmark_raw!r}"
            ) from exc
        if cfg.budget_estimated_usd > max_per_benchmark and not cfg.budget_confirmed:
            raise BudgetExceededError(
                f"experiment.budget.estimated_cost_usd "
                f"({cfg.budget_estimated_usd:.2f}) exceeds "
                f"{ENV_MAX_COST_PER_BENCHMARK_NAME} ({max_per_benchmark:.2f}); "
                f"set budget.confirmed: true in the YAML to override"
            )

    git_commit, dirty = _resolve_git_commit(allow_dirty=allow_dirty)

    benchmark_dir = benchmarks_root / cfg.benchmark
    effective_n = cfg.dataset_size
    if max_samples is not None:
        effective_n = min(effective_n, max_samples)

    system_prompt, _user_template, samples = load_dataset(
        benchmark_dir, max_samples=effective_n
    )

    runs_dir = benchmark_dir / "runs"
    captured_call_kwargs: list[dict] = []

    # Task 010 additive: build the tool list + SHA once per run, when the
    # experiment YAML declares ``agent.tool_loop: true``. The hash is the
    # tool-loop analogue of the byte-identical prompt invariant: it must
    # be a single value across all cells of the benchmark.
    tool_cfg_sha: str | None = None
    tool_list_for_request: list[dict[str, Any]] | None = None
    if cfg.agent is not None and cfg.agent.tool_loop:
        tool_list_for_request = build_tool_list_for_request(cfg.agent)
        tool_cfg_sha = tool_config_sha256(tool_list_for_request)
        logger.info(
            "TOOL_LOOP_ENABLED experiment=%s tools=%s tool_config_sha256=%s max_iter=%d",
            cfg.experiment_id,
            ",".join(cfg.agent.tools),
            tool_cfg_sha,
            cfg.agent.max_tool_iterations,
        )

    cells_written, _budget = asyncio.run(
        _run_async(
            cfg=cfg,
            runs_dir=runs_dir,
            samples=samples,
            system_prompt=system_prompt,
            deployment=deployment,
            endpoint_value=endpoint_value,
            git_commit=git_commit,
            dirty=dirty,
            dry_run=dry_run,
            pricing_snapshot_path=estimate.snapshot_path,
            pricing=pricing,
            captured_call_kwargs=captured_call_kwargs,
            tool_cfg_sha=tool_cfg_sha,
            tool_list_for_request=tool_list_for_request,
            skip_existing=skip_existing,
        )
    )

    return RunResult(
        cells_written=cells_written,
        runs_dir=runs_dir,
        estimate=estimate,
        captured_call_kwargs=captured_call_kwargs,
    )


# ----------------------------------------------------------------------------
# CLI plumbing
# ----------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scripts.run_benchmark",
        description=(
            "Foundry v1 reasoning-effort benchmark runner. Append-only "
            "raw-response JSON output per cell."
        ),
    )
    p.add_argument(
        "--experiment",
        required=True,
        help="Path to an experiment YAML (see experiments/_template.yaml).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Make zero outbound HTTPS calls. Still writes per-cell JSON "
            "records with dry_run=true and a synthetic zero-valued usage "
            "object so downstream analysis can be exercised end-to-end."
        ),
    )
    p.add_argument(
        "--max-samples",
        type=int,
        default=None,
        help="Override dataset_size with a smaller cap (smoke tests).",
    )
    p.add_argument(
        "--allow-dirty",
        action="store_true",
        help=(
            "Tolerate a dirty (or absent) git worktree. The resulting JSON "
            "records carry dirty=true."
        ),
    )
    p.add_argument(
        "--benchmarks-root",
        default="benchmarks",
        help="Root directory holding <benchmark>/dataset.json (default: benchmarks).",
    )
    p.add_argument(
        "--pricing-dir",
        default="pricing",
        help="Directory holding azure-openai-payg-*.yaml snapshots.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Logger level (default: INFO).",
    )
    p.add_argument(
        "--skip-existing",
        action="store_true",
        help=(
            "Resume mode. Before scheduling each cell, scan runs/ for an "
            "existing JSON whose (sample_idx, model, effort, repeat) "
            "matches; if found, skip that cell. Lets a previously-killed "
            "batch resume without re-firing existing cells."
        ),
    )
    return p


def _configure_logging(level: str) -> None:
    """Idempotent logger setup. Adds a single stderr handler if none exists."""
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
        return EXIT_AUTH

    try:
        result = run_experiment(
            cfg=cfg,
            benchmarks_root=pathlib.Path(args.benchmarks_root),
            pricing_dir=pathlib.Path(args.pricing_dir),
            dry_run=args.dry_run,
            max_samples=args.max_samples,
            allow_dirty=args.allow_dirty,
            skip_existing=args.skip_existing,
        )
    except EndpointMisconfiguredError as exc:
        logger.error("ENDPOINT_MISCONFIGURED %s", exc)
        return EXIT_AUTH
    except DatasetMissingError as exc:
        logger.error("DATASET_MISSING %s", exc)
        return EXIT_DATASET
    except BudgetExceededError as exc:
        logger.error("BUDGET_EXCEEDED %s", exc)
        return EXIT_BUDGET
    except FilenameCollisionError as exc:
        logger.error("FILENAME_COLLISION %s", exc)
        return EXIT_DATASET

    # Single CLI summary block — the only ``print()`` in this module.
    summary = (
        f"\n=== run_benchmark summary ===\n"
        f"experiment_id   : {cfg.experiment_id}\n"
        f"benchmark       : {cfg.benchmark}\n"
        f"model.family    : {cfg.model_family}\n"
        f"dry_run         : {args.dry_run}\n"
        f"cells_written   : {result.cells_written}\n"
        f"runs_dir        : {result.runs_dir}\n"
        f"pre_run_estimate: ${result.estimate.total_usd:.4f} "
        f"(cells={result.estimate.cells})\n"
        f"pricing_snapshot: {result.estimate.snapshot_path}\n"
        f"=============================="
    )
    print(summary)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
