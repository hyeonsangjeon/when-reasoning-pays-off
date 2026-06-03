"""run_judge.py — LLM-as-judge invocation that produces append-only
``benchmarks/<name>/judge_runs/*.json`` for the Task 008 analysis pipeline.

This is the ONLY component in the Task 008 implementation allowed to make
network calls. The analyzer (``scripts.analyze_tokens``) and the plot
generator (``scripts.plot_results``) are pure offline consumers — they read
the JSON this script writes.

**The script is NOT executed in the Task 008 implementation pass that lands
this code.** The judge_runs/ tree is populated by a deterministic offline
fixture (``scripts._fixture_synth``) so the analysis pipeline is end-to-end
testable without Azure spend. When real judge runs are needed, a future
phase will run::

    python -m scripts.run_judge --benchmark 01-short-factual --confirm

…against the same Foundry v1 endpoint the benchmark runner uses
(``AZURE_OPENAI_FOUNDRY_ENDPOINT``), with Entra ID authentication via
``DefaultAzureCredential``. The judge deployment is held in
``AZURE_OPENAI_DEPLOYMENT_GPT_4O`` — gpt-4o is the cheap, neutral judge per
Task 008 spec.

Contract
========

* One judge call per ``(sample_id, model, effort, repeat)`` measurement cell.
  If a judge JSON for that cell already exists on disk, the call is skipped
  (append-only invariant — never overwrite an existing record).
* Judge prompt is a single fixed template referencing the per-sample
  ``quality_rubric_notes`` and the model's actual ``response_text``. The
  template's SHA-256 is recorded in every judge JSON so a future analyst
  can detect rubric drift.
* Output JSON must be a strict object ``{"score": 0|1|2, "rationale": "..."}``
  with no surrounding prose. Any other shape → retry once with a stricter
  reminder; if it still fails, write a ``parse_error`` field and skip the
  cell (the analyzer ignores cells without a numeric score).
* Cost guard: budget.estimated_cost_usd <= $3, hard_ceiling_usd = $10. The
  CLI prints the estimate and refuses to send without ``--confirm`` when
  the estimate exceeds the per-benchmark MAX.

Foundry v1 specifics
====================

* Endpoint: ``https://<resource>.services.ai.azure.com/api/projects/<project>``
* SDK: ``openai.AsyncAzureOpenAI(api_version="preview", ...)``
* API call: ``client.responses.create(model=deployment, input=...,
  max_output_tokens=320)``
* Authentication: ``DefaultAzureCredential`` (Entra ID; no API keys).

Pure tier
=========

Functions below the ``# === Pure helpers ===`` marker are pure and unit-
testable without an SDK install. The Azure-touching wrapper lives in
``_call_judge`` and is the only function gated behind the ``--confirm`` flag.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import pathlib
import sys
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("scripts.run_judge")

JUDGE_PROMPT_TEMPLATE = """You are an impartial evaluator for a short-factual LLM task.

Apply the rubric verbatim. Score on this 0|1|2 scale:
- 0 = fail (the rubric requirement is not met)
- 1 = partial (some rubric requirements met, some not)
- 2 = pass (every rubric requirement met)

Rubric: {rubric}

Sample input (verbatim): {input}
Model response (verbatim): {response}

Reply with a strict JSON object: {{"score": <0|1|2>, "rationale": "<one sentence>"}}.
No prose around the JSON. No code fences.
"""

# Additive Task 010 extension: when the measurement JSON being judged carries
# a non-empty ``tool_calls`` trajectory (benchmark 03), the judge prompt below
# is used INSTEAD of JUDGE_PROMPT_TEMPLATE. It preserves the correctness
# rubric block byte-identically (same 0|1|2 scale, same wording) and
# *additively* emits a second field, ``tool_efficiency_score`` in [0.0, 1.0]
# (two decimal places), graded against the dataset sample's
# ``expected_tool_calls`` and the actual trajectory. The correctness rubric
# (`score ∈ {0,1,2}`) is the SAME contract benchmarks 01/02 use; the analyzer
# at scripts/analyze_tokens.py:497-505 is unchanged.
JUDGE_PROMPT_TEMPLATE_WITH_TOOLS = """You are an impartial evaluator for a tool-using LLM task.

Apply the rubric verbatim. Score on this 0|1|2 scale:
- 0 = fail (the rubric requirement is not met)
- 1 = partial (some rubric requirements met, some not)
- 2 = pass (every rubric requirement met)

ALSO grade tool-efficiency on a 0.0 – 1.0 continuous scale:
- 1.00 = optimal tool use: every expected tool invoked, no superfluous calls, correct argument shape
- ~0.50 = adequate but inefficient: required tools invoked but with extra exploratory calls, or correct count but with one malformed argument that the model recovered from
- 0.00 = inadequate tool use: required tool(s) skipped, excessive redundant calls (> 2x expected count), or the model produced a final answer without invoking a tool the dataset marks as required

Rubric: {rubric}
Expected tool calls (dataset ground truth; null means no tool was required): {expected_tool_calls}

Sample input (verbatim): {input}
Model response (verbatim): {response}
Tool-call trajectory (verbatim list of {{tool_name, tool_args, tool_result_summary}} entries): {tool_calls}

Reply with a strict JSON object: {{"score": <0|1|2>, "tool_efficiency_score": <float in [0.0, 1.0], two decimals>, "rationale": "<one sentence>"}}.
No prose around the JSON. No code fences.
"""

DEFAULT_JUDGE_MAX_OUTPUT_TOKENS: int = 320
DEFAULT_BUDGET_HARD_CEILING_USD: float = 10.0
DEFAULT_BUDGET_ESTIMATE_USD: float = 3.0


# === Pure helpers ==========================================================


@dataclass(frozen=True)
class JudgeTask:
    """One pending judge call.

    Attributes:
        source_run_path: Source measurement JSON.
        sample_id: Dataset ID.
        model: Measurement model under test.
        effort: Effort label or None.
        repeat: Repeat index.
        rubric: ``quality_rubric_notes`` from dataset.
        sample_input: Sample's ``input`` field, JSON-rendered.
        response_text: The model's response.
        target_path: Where the judge JSON will be written.
        tool_calls: Per-iteration tool-call trajectory from the measurement
            JSON (Task 010 extension). ``None`` for non-tool-using benchmarks
            (01/02) — the judge falls back to the original
            ``JUDGE_PROMPT_TEMPLATE`` and the output schema does **not**
            include a ``tool_efficiency_score`` field. When this is a list
            (including the empty list ``[]`` for benchmark-03 no-tool cells
            where the model correctly declined to invoke any tool), the
            extended template ``JUDGE_PROMPT_TEMPLATE_WITH_TOOLS`` is used
            and the output JSON additionally carries
            ``tool_efficiency_score``. The gating signal is **presence vs
            absence of the trajectory** (key present on the source
            measurement JSON), not the list's length.
        expected_tool_calls: Dataset's ground-truth list of expected tool
            names (or ``None`` for "no tool needed" rows). Only consulted
            when ``tool_calls`` is non-None.
    """

    source_run_path: str
    sample_id: str
    model: str
    effort: str | None
    repeat: int
    rubric: str
    sample_input: str
    response_text: str
    target_path: pathlib.Path
    tool_calls: list[dict[str, Any]] | None = None
    expected_tool_calls: list[str] | None = None


def judge_prompt_sha256() -> str:
    """SHA-256 of the prompt template. Recorded in every judge JSON for
    rubric-drift detection across re-runs."""
    return hashlib.sha256(JUDGE_PROMPT_TEMPLATE.encode("utf-8")).hexdigest()


def judge_prompt_sha256_with_tools() -> str:
    """SHA-256 of the tool-aware prompt template (Task 010 additive
    extension). Recorded in benchmark-03 judge JSONs only — benchmarks 01/02
    continue to emit the original ``judge_prompt_sha256`` value byte-
    identically."""
    return hashlib.sha256(JUDGE_PROMPT_TEMPLATE_WITH_TOOLS.encode("utf-8")).hexdigest()


def render_judge_prompt(rubric: str, sample_input: str, response_text: str) -> str:
    """Apply the fixed template to one cell's data. Pure / unit-testable."""
    return JUDGE_PROMPT_TEMPLATE.format(
        rubric=rubric,
        input=sample_input,
        response=response_text,
    )


def render_judge_prompt_with_tools(
    rubric: str,
    sample_input: str,
    response_text: str,
    tool_calls: list[dict[str, Any]],
    expected_tool_calls: list[str] | None,
) -> str:
    """Apply the tool-aware template (Task 010). The tool_calls trajectory
    and the dataset's expected_tool_calls list are JSON-rendered inline; the
    correctness rubric block is preserved verbatim from
    ``JUDGE_PROMPT_TEMPLATE`` (same 0|1|2 scale)."""
    return JUDGE_PROMPT_TEMPLATE_WITH_TOOLS.format(
        rubric=rubric,
        input=sample_input,
        response=response_text,
        tool_calls=json.dumps(tool_calls, ensure_ascii=False),
        expected_tool_calls=json.dumps(expected_tool_calls, ensure_ascii=False),
    )


def parse_judge_response(text: str) -> tuple[int, str] | None:
    """Parse a judge response JSON. Returns ``(score, rationale)`` or
    ``None`` if the response is malformed.

    Strict policy:
        * Must be a JSON object.
        * ``score`` must be int in ``{0, 1, 2}``.
        * ``rationale`` must be a string (empty allowed).
    """
    s = text.strip()
    # Tolerate accidental code-fence wrapping.
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    score_raw = obj.get("score")
    if not isinstance(score_raw, int) or score_raw not in (0, 1, 2):
        return None
    rationale = obj.get("rationale", "")
    if not isinstance(rationale, str):
        return None
    return (score_raw, rationale.strip())


def parse_judge_response_with_tools(text: str) -> tuple[int, float, str] | None:
    """Parse a tool-aware judge response (Task 010). Returns
    ``(score, tool_efficiency_score, rationale)`` or ``None`` on malformed.

    Strict policy (additive on top of :func:`parse_judge_response`):
        * Must be a JSON object.
        * ``score`` must be int in ``{0, 1, 2}``.
        * ``tool_efficiency_score`` must be a number (int or float) in
          ``[0.0, 1.0]``; it is rounded to two decimal places at write time.
        * ``rationale`` must be a string (empty allowed).
    """
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    score_raw = obj.get("score")
    if not isinstance(score_raw, int) or score_raw not in (0, 1, 2):
        return None
    tef_raw = obj.get("tool_efficiency_score")
    if not isinstance(tef_raw, (int, float)) or isinstance(tef_raw, bool):
        return None
    tef = float(tef_raw)
    if tef < 0.0 or tef > 1.0:
        return None
    rationale = obj.get("rationale", "")
    if not isinstance(rationale, str):
        return None
    return (score_raw, round(tef, 2), rationale.strip())


def _judge_filename(sample_idx: int, model: str, effort: str | None, repeat: int) -> str:
    """Filename convention mirrors the measurement-run filename — readers
    can grep by sample_id + effort to find the paired judge record.
    """
    effort_token = effort if effort is not None else "null"
    return f"judge_{sample_idx:03d}_{model}_{effort_token}_r{repeat}.json"


def _load_existing_judge_keys(judge_dir: pathlib.Path) -> set[tuple[str, str, str | None, int]]:
    """Return the set of ``(sample_id, model, effort, repeat)`` tuples already
    judged on disk. Robust against the historical filename convention drift —
    the index is built from the JSON payload, not the filename."""
    out: set[tuple[str, str, str | None, int]] = set()
    if not judge_dir.is_dir():
        return out
    for p in sorted(judge_dir.glob("*.json")):
        try:
            with p.open("r", encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(d, dict):
            continue
        try:
            key = (
                str(d["sample_id"]),
                str(d["model"]),
                d.get("effort"),
                int(d["repeat"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        out.add(key)
    return out


def build_judge_tasks(
    *,
    runs_dir: pathlib.Path,
    judge_dir: pathlib.Path,
    dataset: list[dict[str, Any]],
    experiment_prefix: str = "exp001_short-factual_baseline",
) -> list[JudgeTask]:
    """Walk runs_dir, build a JudgeTask per cell that does NOT yet have a
    judge JSON on disk. Pure / no network.
    """
    rubric_by_sample: dict[str, str] = {}
    input_by_sample: dict[str, str] = {}
    idx_by_sample: dict[str, int] = {}
    expected_tools_by_sample: dict[str, list[str] | None] = {}
    for i, s in enumerate(dataset):
        if not isinstance(s, dict) or "id" not in s:
            continue
        rubric_by_sample[str(s["id"])] = str(s.get("quality_rubric_notes", ""))
        input_by_sample[str(s["id"])] = json.dumps(s.get("input"), sort_keys=True)
        idx_by_sample[str(s["id"])] = i
        etc = s.get("expected_tool_calls")
        if etc is None:
            expected_tools_by_sample[str(s["id"])] = None
        elif isinstance(etc, list):
            expected_tools_by_sample[str(s["id"])] = [str(x) for x in etc]
        else:
            expected_tools_by_sample[str(s["id"])] = None

    tasks: list[JudgeTask] = []
    already_judged = _load_existing_judge_keys(judge_dir)
    for path in sorted(runs_dir.glob("*.json")):
        with path.open("r", encoding="utf-8") as fh:
            try:
                data = json.load(fh)
            except json.JSONDecodeError:
                continue
        if not isinstance(data, dict):
            continue
        eid = data.get("experiment_id", "")
        if not eid.startswith(experiment_prefix):
            continue
        sid = str(data.get("sample_id", ""))
        if sid not in rubric_by_sample:
            continue
        sample_idx = idx_by_sample.get(sid, -1)
        effort = data.get("effort")
        repeat = int(data.get("repeat", 0))
        model = str(data.get("model"))
        if (sid, model, effort, repeat) in already_judged:
            continue
        target = judge_dir / _judge_filename(sample_idx, model, effort, repeat)
        if target.exists():
            continue
        # Task 010 extension: when the measurement JSON CARRIES a
        # ``tool_calls`` key (i.e., the cell came from a tool-loop runner;
        # benchmark 03), propagate that list (and the dataset's
        # ``expected_tool_calls``) to the JudgeTask so the tool-aware
        # prompt template fires. The gate is on KEY PRESENCE, not on list
        # length: a no-tool benchmark-03 cell where the runner observed
        # zero tool invocations writes ``tool_calls: []`` and MUST still
        # be judged via the tool-aware template (so its judge JSON carries
        # ``tool_efficiency_score``). For benchmarks 01/02 the key is
        # absent entirely and ``tool_calls`` stays None — the judge falls
        # back to the original template, byte-identical to pre-Task-010
        # behavior.
        if "tool_calls" in data:
            raw_tool_calls = data.get("tool_calls")
            if not isinstance(raw_tool_calls, list):
                raise ValueError(
                    f"{path}: 'tool_calls' must be a list when present; "
                    f"got {type(raw_tool_calls).__name__}"
                )
            tool_calls_for_task = [
                tc for tc in raw_tool_calls if isinstance(tc, dict)
            ]
        else:
            tool_calls_for_task = None
        tasks.append(
            JudgeTask(
                source_run_path=str(path),
                sample_id=sid,
                model=model,
                effort=effort,
                repeat=repeat,
                rubric=rubric_by_sample[sid],
                sample_input=input_by_sample[sid],
                response_text=str(data.get("response_text", "")),
                target_path=target,
                tool_calls=tool_calls_for_task,
                expected_tool_calls=expected_tools_by_sample.get(sid),
            )
        )
    return tasks


def write_judge_record(
    task: JudgeTask,
    score: int,
    rationale: str,
    *,
    judge_model: str,
    raw_response: dict[str, Any] | None,
    timestamp_utc: str,
    git_commit: str,
    tool_efficiency_score: float | None = None,
) -> None:
    """Persist one judge JSON to ``task.target_path`` (append-only).

    The optional ``tool_efficiency_score`` field (Task 010) is included in
    the output JSON **only** when not None — the gate is the same as the
    judge-prompt selection (tool_calls present on the source measurement
    JSON). For benchmarks 01/02 the field is omitted and the output schema
    is byte-identical to the pre-Task-010 contract.

    Raises:
        FileExistsError: Target file already exists (the runner pre-checks
            but this is a belt-and-braces guard).
    """
    if task.target_path.exists():
        raise FileExistsError(f"refuse to overwrite existing judge file: {task.target_path}")
    # Gate the judge_prompt_sha256 value on whether the tool-aware template
    # was used — otherwise downstream readers cannot distinguish which
    # template produced this record.
    prompt_sha = (
        judge_prompt_sha256_with_tools()
        if tool_efficiency_score is not None
        else judge_prompt_sha256()
    )
    payload: dict[str, Any] = {
        "judge_model": judge_model,
        "judge_prompt_sha256": prompt_sha,
        "sample_id": task.sample_id,
        "model": task.model,
        "effort": task.effort,
        "repeat": task.repeat,
        "score": score,
        "rationale": rationale,
        "source_run_filename": pathlib.Path(task.source_run_path).name,
        "timestamp_utc": timestamp_utc,
        "git_commit": git_commit,
    }
    if tool_efficiency_score is not None:
        payload["tool_efficiency_score"] = round(float(tool_efficiency_score), 2)
    if raw_response is not None:
        payload["raw_response"] = raw_response
    task.target_path.parent.mkdir(parents=True, exist_ok=True)
    task.target_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


# === Azure path ============================================================


async def _call_judge(
    client: Any,
    deployment: str,
    prompt: str,
    *,
    max_output_tokens: int,
    retries: int = 2,
) -> tuple[str, dict[str, Any]]:
    """Make one Responses-API judge call. Returns ``(response_text, usage)``.

    Exponential backoff on 429s; raises after ``retries`` failures. No
    reasoning parameter — the judge is gpt-4o, which does not accept it (Foundry
    v1 returns 400 if it is sent).
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.responses.create(
                model=deployment,
                input=prompt,
                max_output_tokens=max_output_tokens,
            )
            text = getattr(resp, "output_text", None) or ""
            usage = resp.usage.model_dump() if hasattr(resp, "usage") else {}
            return text, usage
        except Exception as exc:  # noqa: BLE001 — surfaced to caller after retries
            last_exc = exc
            sleep_for = (2 ** attempt) * 1.5
            logger.warning(
                "judge call failed (attempt %d/%d): %s; sleeping %.1fs",
                attempt + 1,
                retries + 1,
                exc,
                sleep_for,
            )
            await asyncio.sleep(sleep_for)
    assert last_exc is not None
    raise last_exc


async def _run_async(
    *,
    runs_dir: pathlib.Path,
    judge_dir: pathlib.Path,
    dataset_path: pathlib.Path,
    deployment: str,
    endpoint: str,
    api_version: str,
    concurrency: int,
    max_output_tokens: int,
    experiment_prefix: str,
    confirm: bool,
) -> int:
    """Top-level async runner — the only Azure-touching path in this module."""
    if not confirm:
        logger.error("refusing to call Azure without --confirm")
        return 2

    # Foundry v1 client construction — mirrors the run_benchmark.py fix landed
    # by Task 006 (SMOKE_REPORT surprise #1). The classic AsyncAzureOpenAI
    # path with audience ``cognitiveservices.azure.com`` 401s against the
    # Foundry v1 endpoint with ``Unauthorized ... audience is incorrect
    # (https://ai.azure.com)``. The Foundry v1 surface is served at
    # ``<endpoint>/openai/v1/`` and accepts a plain AsyncOpenAI client; the
    # Entra audience is ``https://ai.azure.com/.default``. The api_version
    # parameter is preserved as the methodology label ("preview") but is no
    # longer a query parameter — the wire-level ``/openai/v1/`` path encodes it.
    try:
        from openai import AsyncOpenAI  # imported lazily
        from azure.identity.aio import DefaultAzureCredential
        from azure.identity.aio import get_bearer_token_provider
    except ImportError as exc:
        logger.error(
            "Azure SDKs not installed (%s). Install openai + azure-identity to run judge calls.",
            exc,
        )
        return 3

    with dataset_path.open("r", encoding="utf-8") as fh:
        dataset = json.load(fh)
    tasks = build_judge_tasks(
        runs_dir=runs_dir,
        judge_dir=judge_dir,
        dataset=dataset,
        experiment_prefix=experiment_prefix,
    )
    if not tasks:
        logger.info("no judge tasks remaining; judge_runs/ is fully populated.")
        return 0

    cred = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(
        cred, "https://ai.azure.com/.default"
    )
    assert api_version == "preview", (
        "api_version drift: Foundry v1 only accepts 'preview' (path-encoded)"
    )
    base_url = endpoint.rstrip("/") + "/openai/v1/"
    bearer = await token_provider()
    client = AsyncOpenAI(
        base_url=base_url,
        api_key=bearer,
    )

    sem = asyncio.Semaphore(concurrency)
    n_done = 0
    n_fail = 0
    git_commit = os.environ.get("GIT_COMMIT", "")
    started = time.time()

    async def _one(task: JudgeTask) -> None:
        nonlocal n_done, n_fail
        async with sem:
            # Task 010 gating: if the source measurement JSON carried a
            # ``tool_calls`` trajectory, fire the tool-aware prompt and parse
            # for the extra ``tool_efficiency_score`` field. Otherwise the
            # behavior is byte-identical to pre-Task-010 (benchmarks 01/02
            # unchanged).
            if task.tool_calls is not None:
                prompt = render_judge_prompt_with_tools(
                    task.rubric,
                    task.sample_input,
                    task.response_text,
                    task.tool_calls,
                    task.expected_tool_calls,
                )
            else:
                prompt = render_judge_prompt(
                    task.rubric, task.sample_input, task.response_text
                )
            try:
                text, usage = await _call_judge(
                    client, deployment, prompt, max_output_tokens=max_output_tokens
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("judge cell failed permanently: %s", exc)
                n_fail += 1
                return
            score: int
            tool_efficiency: float | None
            rationale: str
            if task.tool_calls is not None:
                parsed_with_tools = parse_judge_response_with_tools(text)
                if parsed_with_tools is None:
                    logger.warning(
                        "judge response unparseable for %s/%s/%s r=%d; skipping cell",
                        task.sample_id,
                        task.model,
                        task.effort,
                        task.repeat,
                    )
                    n_fail += 1
                    return
                score, tool_efficiency, rationale = parsed_with_tools
            else:
                parsed = parse_judge_response(text)
                if parsed is None:
                    logger.warning(
                        "judge response unparseable for %s/%s/%s r=%d; skipping cell",
                        task.sample_id,
                        task.model,
                        task.effort,
                        task.repeat,
                    )
                    n_fail += 1
                    return
                score, rationale = parsed
                tool_efficiency = None
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            write_judge_record(
                task,
                score,
                rationale,
                judge_model=deployment,
                raw_response={"output_text": text, "usage": usage},
                timestamp_utc=ts,
                git_commit=git_commit,
                tool_efficiency_score=tool_efficiency,
            )
            n_done += 1

    await asyncio.gather(*[_one(t) for t in tasks])
    await cred.close()
    elapsed = time.time() - started
    logger.info(
        "judge: %d ok, %d failed, elapsed=%.1fs", n_done, n_fail, elapsed
    )
    return 0 if n_fail == 0 else 1


# === CLI ===================================================================


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scripts.run_judge",
        description=(
            "Append-only LLM-as-judge invocation. One Azure Responses call per "
            "measurement cell that does not yet have a judge JSON on disk. "
            "Must be run with --confirm to actually call Azure."
        ),
    )
    p.add_argument("--benchmark", required=True)
    p.add_argument("--runs-dir", default=None)
    p.add_argument("--judge-dir", default=None)
    p.add_argument("--dataset", default=None)
    p.add_argument(
        "--experiment-prefix",
        default="exp001_short-factual_baseline",
    )
    p.add_argument(
        "--deployment",
        default=os.environ.get("AZURE_OPENAI_DEPLOYMENT_GPT_4O", "gpt-4o"),
        help="Judge deployment (default: $AZURE_OPENAI_DEPLOYMENT_GPT_4O).",
    )
    p.add_argument(
        "--endpoint",
        default=os.environ.get(
            "AZURE_OPENAI_FOUNDRY_ENDPOINT",
            "https://<resource>.services.ai.azure.com/api/projects/<project>",
        ),
    )
    p.add_argument("--api-version", default="preview")
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument(
        "--max-output-tokens", type=int, default=DEFAULT_JUDGE_MAX_OUTPUT_TOKENS
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help="Required to actually call Azure. Without this flag the script "
        "exits without making any network call.",
    )
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: list[str] | None = None) -> int:
    ns = _build_argparser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, ns.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    bench_root = pathlib.Path("benchmarks") / ns.benchmark
    runs_dir = pathlib.Path(ns.runs_dir) if ns.runs_dir else bench_root / "runs"
    judge_dir = (
        pathlib.Path(ns.judge_dir) if ns.judge_dir else bench_root / "judge_runs"
    )
    dataset_path = (
        pathlib.Path(ns.dataset) if ns.dataset else bench_root / "dataset.json"
    )

    if not ns.confirm:
        with dataset_path.open("r", encoding="utf-8") as fh:
            dataset = json.load(fh)
        tasks = build_judge_tasks(
            runs_dir=runs_dir,
            judge_dir=judge_dir,
            dataset=dataset,
            experiment_prefix=ns.experiment_prefix,
        )
        logger.info(
            "dry-run: %d cells would be judged (estimate ~$%.2f at gpt-4o rates). "
            "Re-run with --confirm to make the calls.",
            len(tasks),
            len(tasks) * 0.01,
        )
        return 0

    return asyncio.run(
        _run_async(
            runs_dir=runs_dir,
            judge_dir=judge_dir,
            dataset_path=dataset_path,
            deployment=ns.deployment,
            endpoint=ns.endpoint,
            api_version=ns.api_version,
            concurrency=ns.concurrency,
            max_output_tokens=ns.max_output_tokens,
            experiment_prefix=ns.experiment_prefix,
            confirm=ns.confirm,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
