"""_fixture_synth_03.py — deterministic synthesizer of Task 010 measurement +
judge fixtures for benchmark 03-tool-using-agent.

⚠️⚠️⚠️ WARNING — HYPOTHESIS-ENCODING, NOT MEASUREMENT ⚠️⚠️⚠️
============================================================

The :data:`PASS_RATE` and :data:`TEF_PROFILE` tables below **ENCODE THE
PRE-REGISTERED HYPOTHESIS** for benchmark 03; they are **NOT measured
values**. Every numeric draw in the synthesized cohort is conditioned
on these tables, so every downstream chart, cost-per-correct figure,
PTU throughput-gain ratio, and tool-efficiency breakdown bullet in
`benchmarks/03-tool-using-agent/analysis.md`, `results/summary.md`, and
`docs/04-decision-framework.md` is **a re-statement of the hypothesis,
not an independent confirmation of it**.

The fixture cohort exists so the offline analysis pipeline
(analyze_tokens, plot_results, run_judge) can be exercised end-to-end
without live Azure spend — it is the analogue of the Task 008
`scripts/_fixture_synth.py` for benchmark 01. The live ReAct-style
tool-loop runner body is now shipped in
`scripts.run_benchmark._live_tool_loop_call`, so a live (real) Foundry
v1 cohort can be produced by running the `exp003_*` experiment YAMLs
against Azure; that live cohort is what confirms or refutes the
hypothesis encoded below.

If you change a single number in :data:`PASS_RATE` or
:data:`TEF_PROFILE` you change the headline finding of benchmark 03
across three documents. Treat them as committed hypothesis, not as
tunable knobs.

============================================================

This is an internal authoring helper, NOT part of the production analysis
pipeline. It is checked in so reviewers can verify how the synthetic JSONs
under ``benchmarks/03-tool-using-agent/runs/`` and ``judge_runs/`` were
generated.

Why fixtures exist
------------------

Task 010 specifies a 360-cell Foundry v1 measurement run (60 gpt-4o + 300
gpt-5.2 across [none, low, medium, high, xhigh]). When live Azure
infrastructure is unavailable, the task spec's explicit escape hatch (under
"Provenance discipline") allows generating a fixture cohort under a distinct
``experiment_id`` (e.g. ``exp010_benchmark-03_fixture``) with
``"fixture": true`` on every JSON. This file implements that escape hatch,
following the Task 008 ``scripts/_fixture_synth.py`` pattern.

Determinism
-----------

Every numeric draw uses ``random.Random(seed=...)`` with a fixed seed
derived from ``(sample_idx, model, effort, repeat)`` via SHA-256. Re-running
the script over empty directories produces byte-identical files.

What is modeled
---------------

The benchmark 03 dataset has three sub-types:

* **no-tool** (6 samples, tu_01..tu_06): the correct trajectory has **zero**
  tool calls; invoking a tool is excessive and penalises tool_efficiency.
* **one-tool** (8 samples, tu_07..tu_14): exactly one tool call is expected.
* **multi-tool** (6 samples, tu_15..tu_20): 2-3 tool calls expected.

The synthesizer's quality model captures the headline finding the task
spec pre-registers:

* gpt-4o handles no-tool and trivial one-tool well, but struggles on
  multi-tool sequencing (~50% pass on multi-tool).
* gpt-5.2 at ``none`` matches gpt-4o on no-tool but is **worse** on
  multi-tool because the model has no reasoning surface to plan the tool
  sequence.
* gpt-5.2 at ``low`` is the Pareto knee: lifts multi-tool pass-rate near
  saturation while keeping reasoning tokens modest.
* gpt-5.2 at ``medium`` saturates multi-tool quality but spends more.
* gpt-5.2 at ``high`` / ``xhigh`` saturated as well; the cost-per-correct
  worsens because per-call USD grows.

Token shapes are anchored to ~300 in / ~50 out + reasoning escalating by
effort. The ``tool_calls`` trajectory length matches the subtype (with a
small noise probability for over-calling or under-calling).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import pathlib
import random
import sys
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("scripts._fixture_synth_03")

EFFORT_TIERS: tuple[str, ...] = ("none", "low", "medium", "high", "xhigh")

# Reasoning-token mean / spread per effort tier. Tool-loop multi-step tasks
# spend more reasoning tokens than benchmark 01 (single-shot trivial) per
# tier because the model is planning across iterations.
REASONING_PROFILE: dict[str, tuple[float, float]] = {
    "none": (0.0, 0.0),
    "low": (180.0, 60.0),
    "medium": (520.0, 130.0),
    "high": (1280.0, 280.0),
    "xhigh": (2640.0, 480.0),
}

# Visible-output token shape (final answer text). Short integers / words
# per the dataset's expected_output_shape.
OUTPUT_PROFILE: dict[str | None, tuple[float, float]] = {
    None: (28.0, 9.0),  # gpt-4o
    "none": (32.0, 9.0),
    "low": (38.0, 11.0),
    "medium": (45.0, 13.0),
    "high": (54.0, 16.0),
    "xhigh": (62.0, 20.0),
}

# Latency (ms). Tool-loop is more expensive than single-shot: each
# iteration is a separate API roundtrip. Anchored to realistic Foundry v1
# behavior (per-call ~1.5-2.5s + per-tool dispatch overhead).
LATENCY_PROFILE: dict[str | None, tuple[float, float]] = {
    None: (1800.0, 420.0),
    "none": (2050.0, 480.0),
    "low": (2840.0, 620.0),
    "medium": (3950.0, 840.0),
    "high": (5120.0, 1050.0),
    "xhigh": (7340.0, 1430.0),
}

# Per-sample subtype tagging — drives the quality and tool-call-length
# profiles below.
SUBTYPE_BY_SAMPLE_ID: dict[str, str] = {
    "tu_01": "no-tool", "tu_02": "no-tool", "tu_03": "no-tool",
    "tu_04": "no-tool", "tu_05": "no-tool", "tu_06": "no-tool",
    "tu_07": "one-tool", "tu_08": "one-tool", "tu_09": "one-tool",
    "tu_10": "one-tool", "tu_11": "one-tool", "tu_12": "one-tool",
    "tu_13": "one-tool", "tu_14": "one-tool",
    "tu_15": "multi-tool", "tu_16": "multi-tool", "tu_17": "multi-tool",
    "tu_18": "multi-tool", "tu_19": "multi-tool", "tu_20": "multi-tool",
}

EXPECTED_TOOL_COUNT: dict[str, int] = {
    "no-tool": 0,
    "one-tool": 1,
    "multi-tool": 2,  # avg; tu_17, tu_19, tu_20 have 3
}

# ---------------------------------------------------------------------------
# ⚠️ HYPOTHESIS-ENCODING TABLES — read the module docstring before touching ⚠️
# ---------------------------------------------------------------------------
# PASS_RATE and TEF_PROFILE below encode the pre-registered hypothesis for
# benchmark 03. They are NOT measurements. Every benchmark 03 chart,
# cost-per-correct figure, and decision-framework recommendation is a
# downstream consequence of these tables. Treat as committed hypothesis,
# not as tunable knobs. The live (real) Foundry v1 cohort required to
# confirm/refute the hypothesis is blocked on
# .internal/tasks/017-tool-loop-runner-body.md.
# ---------------------------------------------------------------------------

# Pass-rate profile per (model, effort, subtype) — produces the headline
# finding for benchmark 03.
PASS_RATE: dict[tuple[str, str | None, str], float] = {
    ("gpt-4o", None, "no-tool"): 0.92,
    ("gpt-4o", None, "one-tool"): 0.85,
    ("gpt-4o", None, "multi-tool"): 0.50,
    ("gpt-5.2", "none", "no-tool"): 0.95,
    ("gpt-5.2", "none", "one-tool"): 0.84,
    ("gpt-5.2", "none", "multi-tool"): 0.42,
    ("gpt-5.2", "low", "no-tool"): 0.95,
    ("gpt-5.2", "low", "one-tool"): 0.92,
    ("gpt-5.2", "low", "multi-tool"): 0.85,
    ("gpt-5.2", "medium", "no-tool"): 0.95,
    ("gpt-5.2", "medium", "one-tool"): 0.95,
    ("gpt-5.2", "medium", "multi-tool"): 0.95,
    ("gpt-5.2", "high", "no-tool"): 0.95,
    ("gpt-5.2", "high", "one-tool"): 0.95,
    ("gpt-5.2", "high", "multi-tool"): 0.97,
    ("gpt-5.2", "xhigh", "no-tool"): 0.95,
    ("gpt-5.2", "xhigh", "one-tool"): 0.95,
    ("gpt-5.2", "xhigh", "multi-tool"): 0.95,
}

# Tool-efficiency score profile per (model, effort, subtype). Captures the
# task spec's pre-registered hypothesis: medium effort dominates because
# high effort over-calls on trivial samples and minimal effort under-calls
# on multi-tool samples.
TEF_PROFILE: dict[tuple[str, str | None, str], tuple[float, float]] = {
    # (mean, std) of tool_efficiency_score
    ("gpt-4o", None, "no-tool"): (0.95, 0.08),
    ("gpt-4o", None, "one-tool"): (0.86, 0.12),
    ("gpt-4o", None, "multi-tool"): (0.55, 0.20),
    ("gpt-5.2", "none", "no-tool"): (0.95, 0.08),
    ("gpt-5.2", "none", "one-tool"): (0.85, 0.13),
    ("gpt-5.2", "none", "multi-tool"): (0.50, 0.22),
    ("gpt-5.2", "low", "no-tool"): (0.94, 0.09),
    ("gpt-5.2", "low", "one-tool"): (0.93, 0.10),
    ("gpt-5.2", "low", "multi-tool"): (0.87, 0.12),
    ("gpt-5.2", "medium", "no-tool"): (0.92, 0.11),
    ("gpt-5.2", "medium", "one-tool"): (0.95, 0.08),
    ("gpt-5.2", "medium", "multi-tool"): (0.94, 0.09),
    ("gpt-5.2", "high", "no-tool"): (0.78, 0.18),
    ("gpt-5.2", "high", "one-tool"): (0.88, 0.11),
    ("gpt-5.2", "high", "multi-tool"): (0.95, 0.07),
    ("gpt-5.2", "xhigh", "no-tool"): (0.62, 0.22),
    ("gpt-5.2", "xhigh", "one-tool"): (0.78, 0.16),
    ("gpt-5.2", "xhigh", "multi-tool"): (0.93, 0.09),
}

# Probability of over-calling (extra tool invocation) per (model, effort).
# High-effort models over-explore on trivial tasks.
OVERCALL_PROB: dict[tuple[str, str | None], float] = {
    ("gpt-4o", None): 0.02,
    ("gpt-5.2", "none"): 0.02,
    ("gpt-5.2", "low"): 0.04,
    ("gpt-5.2", "medium"): 0.06,
    ("gpt-5.2", "high"): 0.18,
    ("gpt-5.2", "xhigh"): 0.30,
}

COLD_START_RATE: float = 0.014
RETRY_RATE: float = 0.006
TRUNCATED_RATE: float = 0.002

FIXTURE_EXPERIMENT_ID_GPT4O: str = "exp010_benchmark-03_fixture_gpt4o"
FIXTURE_EXPERIMENT_ID_GPT52: str = "exp010_benchmark-03_fixture_gpt5_2"

FIXTURE_GIT_COMMIT: str = "FIXTURE-010-synth-1"
FIXTURE_TENANT: str = "<project>"
FIXTURE_ENDPOINT: str = (
    "https://<resource>.services.ai.azure.com/api/projects/<project>"
)
PRICING_SNAPSHOT_PATH: str = "pricing/azure-openai-payg-2026-05.yaml"

JUDGE_PROMPT_TEMPLATE_WITH_TOOLS_REF = "scripts/run_judge.py::JUDGE_PROMPT_TEMPLATE_WITH_TOOLS"


@dataclass(frozen=True)
class FixturePlan:
    sample_idx: int
    sample_id: str
    model: str
    effort: str | None
    repeat: int
    timestamp_utc: str
    experiment_id: str


def _per_row_seed(
    base_seed: int, sample_idx: int, model: str, effort: str | None, repeat: int
) -> int:
    key = f"{base_seed}|{sample_idx:03d}|{model}|{effort or 'null'}|{repeat}"
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _normal_int(rng: random.Random, mean: float, std: float, floor: int) -> int:
    raw = rng.gauss(mean, std)
    return max(floor, int(round(raw)))


def _normal_float(rng: random.Random, mean: float, std: float, floor: float) -> float:
    raw = rng.gauss(mean, std)
    return max(floor, raw)


def _system_sha(system_prompt: str) -> str:
    return hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()


def _user_sha(user_input: str) -> str:
    return hashlib.sha256(user_input.encode("utf-8")).hexdigest()


def _build_plans(
    dataset: list[dict[str, Any]],
    base_ts: str,
) -> list[FixturePlan]:
    plans: list[FixturePlan] = []
    for sample_idx, sample in enumerate(dataset):
        sid = sample["id"]
        # gpt-4o baseline (effort = None)
        for r in range(3):
            ts = _synthetic_ts(base_ts, sample_idx, 0, 0, r)
            plans.append(
                FixturePlan(
                    sample_idx=sample_idx,
                    sample_id=sid,
                    model="gpt-4o",
                    effort=None,
                    repeat=r,
                    timestamp_utc=ts,
                    experiment_id=FIXTURE_EXPERIMENT_ID_GPT4O,
                )
            )
        for e_idx, effort in enumerate(EFFORT_TIERS):
            for r in range(3):
                ts = _synthetic_ts(base_ts, sample_idx, 1, e_idx + 1, r)
                plans.append(
                    FixturePlan(
                        sample_idx=sample_idx,
                        sample_id=sid,
                        model="gpt-5.2",
                        effort=effort,
                        repeat=r,
                        timestamp_utc=ts,
                        experiment_id=FIXTURE_EXPERIMENT_ID_GPT52,
                    )
                )
    return plans


def _synthetic_ts(
    base_ts: str, sample_idx: int, model_idx: int, effort_idx: int, repeat: int
) -> str:
    """Encode the row indices into a sortable, deterministic timestamp."""
    offset = sample_idx * 1000 + model_idx * 500 + effort_idx * 60 + repeat
    hh = 13 + (offset // 3600)
    mm = (offset % 3600) // 60
    ss = offset % 60
    days_extra = hh // 24
    hh = hh % 24
    day = 24 + days_extra
    return f"202605{day:02d}T{hh:02d}{mm:02d}{ss:02d}Z"


def _ts_iso(packed: str) -> str:
    y, mo, d = packed[0:4], packed[4:6], packed[6:8]
    hh, mm, ss = packed[9:11], packed[11:13], packed[13:15]
    return f"{y}-{mo}-{d}T{hh}:{mm}:{ss}Z"


def _run_filename(plan: FixturePlan) -> str:
    effort_token = plan.effort if plan.effort is not None else "null"
    return (
        f"{plan.timestamp_utc}_{plan.experiment_id}_"
        f"{plan.sample_idx:03d}_{plan.model}_{effort_token}_r{plan.repeat}.json"
    )


def _judge_filename(plan: FixturePlan) -> str:
    effort_token = plan.effort if plan.effort is not None else "null"
    return (
        f"{plan.timestamp_utc}_judge_{plan.sample_idx:03d}_"
        f"{plan.model}_{effort_token}_r{plan.repeat}.json"
    )


@dataclass
class CallContext:
    sample: dict[str, Any]
    subtype: str
    rng: random.Random
    overcall_prob: float


def _decide_trajectory(ctx: CallContext) -> list[dict[str, Any]]:
    """Synthesize a plausible tool_calls trajectory for one cell.

    The trajectory matches the dataset's ``expected_tool_calls`` with
    occasional over- or under-calls depending on the (model, effort) profile.
    """
    expected: list[str] | None = ctx.sample.get("expected_tool_calls")
    trajectory: list[dict[str, Any]] = []

    if expected is None:
        # No-tool task. A small probability of an over-call.
        if ctx.rng.random() < ctx.overcall_prob:
            trajectory.append(
                _synth_tool_call(
                    "calculator", {"expr": "1+1"}, "2", ctx.rng
                )
            )
        return trajectory

    # Build the canonical sequence from expected, then optionally add
    # over-calls or replace one with a wrong-arg attempt that recovers.
    for tool_name in expected:
        if tool_name == "calculator":
            args = {"expr": "<expr>"}
            result = ctx.sample.get("verifiable_answer", "0")
        elif tool_name == "web_search":
            args = {"query": "<query>"}
            result = "<kb-value>"
        else:
            args = {}
            result = ""
        trajectory.append(_synth_tool_call(tool_name, args, result, ctx.rng))

    # Add a redundant over-call with low probability.
    if ctx.rng.random() < ctx.overcall_prob and trajectory:
        # Append a redundant calculator call.
        trajectory.append(
            _synth_tool_call("calculator", {"expr": "1*1"}, "1", ctx.rng)
        )
    return trajectory


def _synth_tool_call(
    tool_name: str, args: dict[str, Any], result: str, rng: random.Random
) -> dict[str, Any]:
    return {
        "iteration": 0,  # will be backfilled
        "tool_name": tool_name,
        "tool_args": args,
        "tool_result_summary": result,
        "latency_ms": round(rng.uniform(40.0, 320.0), 2),
        "usage": {
            "input_tokens": rng.randint(20, 60),
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": rng.randint(8, 40),
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 0,  # filled after
        },
    }


def _judge_score_from_pass(rng: random.Random, p_pass: float) -> int:
    """0/1/2 draw mostly polarized (very few partials) — matches benchmark
    02 finding that authored datasets produce clean pass/fail."""
    r = rng.random()
    if r < p_pass * 0.97:
        return 2
    if r < p_pass * 0.97 + (1.0 - p_pass) * 0.95:
        return 0
    return 1


def _tef_draw(rng: random.Random, mean: float, std: float) -> float:
    raw = rng.gauss(mean, std)
    raw = max(0.0, min(1.0, raw))
    return round(raw, 2)


def _render_run_record(
    plan: FixturePlan,
    sample: dict[str, Any],
    system_sha: str,
    user_sha: str,
    tool_config_sha: str,
    rng: random.Random,
) -> tuple[dict[str, Any], int, float]:
    """Synthesize one measurement JSON. Returns (payload, judge_score, tef)."""
    subtype = SUBTYPE_BY_SAMPLE_ID.get(plan.sample_id, "one-tool")
    overcall_p = OVERCALL_PROB.get((plan.model, plan.effort), 0.05)

    ctx = CallContext(sample=sample, subtype=subtype, rng=rng, overcall_prob=overcall_p)
    trajectory = _decide_trajectory(ctx)
    # Backfill iteration index and total_tokens per iteration.
    for i, tc in enumerate(trajectory, start=1):
        tc["iteration"] = i
        u = tc["usage"]
        u["total_tokens"] = (
            u["input_tokens"] + u["output_tokens"]
        )

    # Aggregate per-iteration usage into the cell-level usage object.
    # Plus a final-answer usage component (the wrap-up call).
    base_input = 285 + (plan.sample_idx * 7) % 30  # rough per-sample variance
    final_out_mean, final_out_std = OUTPUT_PROFILE[plan.effort]
    final_output_tokens = _normal_int(rng, final_out_mean, final_out_std, floor=4)

    if plan.model == "gpt-4o":
        reasoning_tokens = 0
    else:
        r_mean, r_std = REASONING_PROFILE[plan.effort or "none"]
        reasoning_tokens = _normal_int(rng, r_mean, r_std, floor=0)

    iter_input = sum(tc["usage"]["input_tokens"] for tc in trajectory)
    iter_output = sum(tc["usage"]["output_tokens"] for tc in trajectory)
    input_tokens = base_input + iter_input
    output_tokens = final_output_tokens + iter_output + reasoning_tokens
    # NOTE: per Foundry v1 contract, output_tokens already includes the
    # reasoning subset. So we add reasoning to the visible output here so
    # that output_tokens_details.reasoning_tokens stays a strict subset.

    lat_mean, lat_std = LATENCY_PROFILE[plan.effort]
    base_latency = _normal_float(rng, lat_mean, lat_std, floor=300.0)
    # Add the per-iteration latencies on top
    total_latency = base_latency + sum(tc["latency_ms"] for tc in trajectory)

    cold = rng.random() < COLD_START_RATE
    retry_count = 1 if rng.random() < RETRY_RATE else 0
    truncated = rng.random() < TRUNCATED_RATE

    if cold or retry_count > 0 or truncated:
        # Mimic real Azure flakiness: inflate the cell's usage tail.
        if plan.model == "gpt-5.2":
            reasoning_tokens = int(reasoning_tokens * 1.6) + 200
            output_tokens = int(output_tokens * 1.5) + 200
        else:
            output_tokens = int(output_tokens * 1.5) + 10
        total_latency = total_latency * 1.6 + 600

    total_tokens = input_tokens + output_tokens
    payload: dict[str, Any] = {
        "fixture": True,
        "fixture_note": (
            "Synthetic Task 010 measurement cell — generated by "
            "scripts._fixture_synth_03 for the offline analysis pipeline. "
            "NOT a real Azure call. The exp003_benchmark03_* YAMLs target "
            "live Foundry v1 calls; this fixture cohort lives under a "
            "distinct experiment_id (exp010_benchmark-03_fixture_*) per the "
            "Task 010 provenance discipline."
        ),
        "api_version": "preview",
        "auth_mode": "entra",
        "call_metadata": {
            "deployment_cold_start": cold,
            "system_prompt_sha256": system_sha,
            "time_since_last_identical_prefix_seconds": None,
            "tool_config_sha256": tool_config_sha,
            "user_input_sha256": user_sha,
        },
        "cold_start": cold,
        "deployment_name": plan.model,
        "dirty": False,
        "dry_run": False,
        "effort": plan.effort,
        "endpoint": FIXTURE_ENDPOINT,
        "experiment_id": plan.experiment_id,
        "git_commit": FIXTURE_GIT_COMMIT,
        "latency_ms": round(total_latency, 6),
        "model": plan.model,
        "pricing_snapshot_path": PRICING_SNAPSHOT_PATH,
        "repeat": plan.repeat,
        "response_text": str(sample.get("verifiable_answer", "")),
        "retry_count": retry_count,
        "sample_id": plan.sample_id,
        "sample_idx": plan.sample_idx,
        "sample_metadata": sample,
        "timestamp_utc": _ts_iso(plan.timestamp_utc),
        "tool_calls": trajectory,
        "tool_loop_terminated": "ok",
        "truncated_output": truncated,
        "usage": {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
            "total_tokens": total_tokens,
        },
    }

    pass_rate = PASS_RATE.get((plan.model, plan.effort, subtype), 0.5)
    score = _judge_score_from_pass(rng, pass_rate)
    tef_mean, tef_std = TEF_PROFILE.get(
        (plan.model, plan.effort, subtype), (0.5, 0.2)
    )
    tef = _tef_draw(rng, tef_mean, tef_std)
    return payload, score, tef


def _render_judge_record(
    plan: FixturePlan,
    score: int,
    tef: float,
    rationale: str,
    judge_prompt_sha: str,
    source_run_filename: str,
) -> dict[str, Any]:
    return {
        "fixture": True,
        "fixture_note": (
            "Synthetic judge call — generated by scripts._fixture_synth_03 "
            "for the Task 010 offline pipeline. NOT a real Azure call."
        ),
        "judge_model": "gpt-4o",
        "judge_prompt_sha256": judge_prompt_sha,
        "sample_id": plan.sample_id,
        "model": plan.model,
        "effort": plan.effort,
        "repeat": plan.repeat,
        "score": score,
        "tool_efficiency_score": tef,
        "rationale": rationale,
        "source_run_filename": source_run_filename,
        "timestamp_utc": _ts_iso(plan.timestamp_utc),
        "git_commit": FIXTURE_GIT_COMMIT,
    }


_RATIONALE_BY_SCORE = {
    0: "Trajectory or final answer did not satisfy the rubric.",
    1: "Partial credit: some rubric requirements met but final answer incomplete.",
    2: "Every rubric requirement met; trajectory matched the dataset's expected tool calls.",
}


def synthesize(
    *,
    dataset_path: pathlib.Path,
    system_prompt_path: pathlib.Path,
    user_template_path: pathlib.Path,
    tool_schema_paths: list[pathlib.Path],
    runs_dir: pathlib.Path,
    judge_dir: pathlib.Path,
    seed: int = 4242,
    base_ts: str = "20260524T130000Z",
) -> tuple[int, int, str]:
    """Materialize 360 measurement JSONs + 360 judge JSONs deterministically.

    Returns ``(n_runs_written, n_judge_written, tool_config_sha)``.
    """
    with dataset_path.open("r", encoding="utf-8") as fh:
        dataset = json.load(fh)
    if not isinstance(dataset, list):
        raise ValueError("dataset must be a list")

    system_prompt = system_prompt_path.read_text(encoding="utf-8")
    user_template = user_template_path.read_text(encoding="utf-8")
    system_sha = _system_sha(system_prompt)

    tool_list: list[dict[str, Any]] = []
    for p in tool_schema_paths:
        with p.open("r", encoding="utf-8") as fh:
            tool_list.append(json.load(fh))
    tool_config_sha = hashlib.sha256(
        json.dumps(tool_list, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    # The tool-aware judge prompt SHA — must match scripts.run_judge's
    # ``judge_prompt_sha256_with_tools()`` output. We re-import to avoid
    # hard-coding the digest.
    from scripts.run_judge import judge_prompt_sha256_with_tools

    judge_prompt_sha = judge_prompt_sha256_with_tools()

    runs_dir.mkdir(parents=True, exist_ok=True)
    judge_dir.mkdir(parents=True, exist_ok=True)
    plans = _build_plans(dataset, base_ts)

    n_runs = 0
    n_judge = 0
    for plan in plans:
        rng = random.Random(
            _per_row_seed(seed, plan.sample_idx, plan.model, plan.effort, plan.repeat)
        )
        sample = dataset[plan.sample_idx]
        # Pre-render user input via the template (string-only fields here,
        # so no JSON pretty-printing is needed).
        rendered = user_template.format(
            input=sample.get("input", ""),
            expected_output_shape=sample.get("expected_output_shape", ""),
        )
        user_sha = _user_sha(rendered)

        run_payload, judge_score, tef = _render_run_record(
            plan, sample, system_sha, user_sha, tool_config_sha, rng
        )
        run_filename = _run_filename(plan)
        run_path = runs_dir / run_filename
        run_path.write_text(
            json.dumps(run_payload, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        n_runs += 1

        judge_payload = _render_judge_record(
            plan,
            judge_score,
            tef,
            _RATIONALE_BY_SCORE[judge_score],
            judge_prompt_sha,
            run_filename,
        )
        judge_path = judge_dir / _judge_filename(plan)
        judge_path.write_text(
            json.dumps(judge_payload, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        n_judge += 1

    return n_runs, n_judge, tool_config_sha


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scripts._fixture_synth_03",
        description=(
            "Generate 360 synthetic measurement + 360 synthetic judge JSONs "
            "for benchmark 03-tool-using-agent (Task 010 fixture cohort)."
        ),
    )
    p.add_argument("--dataset", default="benchmarks/03-tool-using-agent/dataset.json")
    p.add_argument("--system", default="benchmarks/03-tool-using-agent/prompts/system.md")
    p.add_argument(
        "--user-template", default="benchmarks/03-tool-using-agent/prompts/user_template.md"
    )
    p.add_argument(
        "--calculator-schema",
        default="benchmarks/03-tool-using-agent/prompts/tool_schemas/calculator.json",
    )
    p.add_argument(
        "--web-search-schema",
        default="benchmarks/03-tool-using-agent/prompts/tool_schemas/web_search.json",
    )
    p.add_argument("--runs-dir", default="benchmarks/03-tool-using-agent/runs")
    p.add_argument("--judge-dir", default="benchmarks/03-tool-using-agent/judge_runs")
    p.add_argument("--seed", type=int, default=4242)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    ns = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    n_runs, n_judge, sha = synthesize(
        dataset_path=pathlib.Path(ns.dataset),
        system_prompt_path=pathlib.Path(ns.system),
        user_template_path=pathlib.Path(ns.user_template),
        tool_schema_paths=[
            pathlib.Path(ns.calculator_schema),
            pathlib.Path(ns.web_search_schema),
        ],
        runs_dir=pathlib.Path(ns.runs_dir),
        judge_dir=pathlib.Path(ns.judge_dir),
        seed=ns.seed,
    )
    logger.info(
        "fixture03: wrote %d runs and %d judge records; tool_config_sha=%s",
        n_runs,
        n_judge,
        sha,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
