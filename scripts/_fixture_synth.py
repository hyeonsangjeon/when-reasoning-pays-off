"""_fixture_synth.py — deterministic synthesizer of Task 007 measurement + judge
fixtures for benchmark 01-short-factual.

This is an internal authoring helper, NOT part of the production analysis
pipeline. It is checked in so reviewers can verify how the synthetic JSONs
under ``benchmarks/01-short-factual/runs/`` and ``judge_runs/`` were generated.

Why fixtures exist
------------------

Task 008 (analysis pipeline) is built end-to-end against the deterministic
offline aggregator. The full Task 007 measurement output (360 raw JSONs) is not
yet on disk in this branch, and Task 008's spec explicitly forbids the
analysis pipeline from running any Azure call. To keep the offline pipeline
testable end-to-end we synthesize 360 measurement JSONs + 360 judge JSONs
here, each carrying the marker ``"fixture": true`` at the top level so a
human can never confuse a synthesized run with a real Task 007 cell.

Determinism
-----------

Every numeric draw uses ``random.Random(seed=...)`` with a fixed seed; the
script is idempotent. Timestamps are derived from sample/effort/repeat
indices, NOT wall-clock. Re-running this script over an empty directory
produces byte-identical files.

Counts
------

* gpt-4o baseline: 20 samples × 1 (no effort) × 3 repeats = 60
* gpt-5.2 sweep:   20 samples × 5 efforts × 3 repeats = 300
* judge runs: one per measurement cell = 360 total

Token shapes are anchored to the two real smoke runs already on disk (input
~240 tokens, output ~11-17 tokens for short-factual). Reasoning-token counts
escalate by effort tier in a monotonic-but-noisy way that exercises the
aggregator's stats + outlier code paths.
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

logger = logging.getLogger("scripts._fixture_synth")

# Canonical sweep — must match scripts.analyze_tokens.CANONICAL_EFFORT_ORDER.
EFFORT_TIERS: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh")

# Reasoning-token mean & spread per effort tier. The escalation curve is
# realistic for short-factual tasks (single-paragraph rubrics): even xhigh
# spends only ~300 reasoning tokens because the questions are trivial.
REASONING_PROFILE: dict[str, tuple[float, float]] = {
    "minimal": (4.0, 2.0),
    "low": (28.0, 12.0),
    "medium": (88.0, 26.0),
    "high": (180.0, 40.0),
    "xhigh": (305.0, 55.0),
}

# Visible-output token shape (mean, std) — anchored to the 11-17 tokens seen in
# smoke runs but allowed to drift slightly with effort (higher effort tends to
# produce marginally longer outputs).
OUTPUT_PROFILE: dict[str | None, tuple[float, float]] = {
    None: (13.0, 3.5),  # gpt-4o
    "minimal": (12.5, 3.0),
    "low": (13.5, 3.5),
    "medium": (14.5, 4.0),
    "high": (16.0, 4.5),
    "xhigh": (17.5, 5.0),
}

# Latency shape (ms). Real smoke run showed ~2300-2750 ms for gpt-5.2 high.
LATENCY_PROFILE: dict[str | None, tuple[float, float]] = {
    None: (820.0, 180.0),
    "minimal": (1100.0, 220.0),
    "low": (1450.0, 280.0),
    "medium": (1900.0, 350.0),
    "high": (2350.0, 420.0),
    "xhigh": (3050.0, 540.0),
}

# Judge-score profile per (model, effort). gpt-4o has high but not perfect
# pass rate on the null-case benchmark; gpt-5.2 effort tiers cluster around
# the same plateau — that's the point of the null benchmark.
JUDGE_PROFILE: dict[tuple[str, str | None], dict[int, float]] = {
    ("gpt-4o", None): {0: 0.03, 1: 0.10, 2: 0.87},
    ("gpt-5.2", "minimal"): {0: 0.04, 1: 0.10, 2: 0.86},
    ("gpt-5.2", "low"): {0: 0.03, 1: 0.10, 2: 0.87},
    ("gpt-5.2", "medium"): {0: 0.02, 1: 0.09, 2: 0.89},
    ("gpt-5.2", "high"): {0: 0.02, 1: 0.08, 2: 0.90},
    ("gpt-5.2", "xhigh"): {0: 0.02, 1: 0.08, 2: 0.90},
}

# Operational-event injection rates (sparse so the aggregator sees both
# clean cells and a handful of flagged ones, mirroring real Azure behavior).
COLD_START_RATE: float = 0.012   # ~4 cold-start rows over 360
RETRY_RATE: float = 0.008        # ~3 retry rows
TRUNCATED_RATE: float = 0.003    # ~1 truncated row

# Default experiment_ids — distinct from the legacy Task 007 production
# cohort ``exp001_short-factual_baseline`` so the two cohorts can coexist in
# the same runs/ directory without the analyzer cross-contaminating one with
# the other (the analyzer skips files whose experiment_id does not start
# with its --experiment-prefix before running schema validation). The fixture
# JSON also carries ``fixture: true`` as a second, content-level guard.
FIXTURE_EXPERIMENT_ID_GPT4O: str = "exp008_short-factual_fixture_gpt4o"
FIXTURE_EXPERIMENT_ID_GPT52: str = "exp008_short-factual_fixture"

FIXTURE_GIT_COMMIT: str = "FIXTURE-008-synth-1"
FIXTURE_TENANT: str = "<project>"
FIXTURE_ENDPOINT: str = (
    "https://<resource>.services.ai.azure.com/api/projects/<project>"
)
PRICING_SNAPSHOT_PATH: str = "pricing/azure-openai-payg-2026-05.yaml"


@dataclass(frozen=True)
class FixturePlan:
    """One row to be materialized."""

    sample_idx: int
    sample_id: str
    model: str
    effort: str | None
    repeat: int
    timestamp_utc: str
    target_dir_runs: pathlib.Path
    target_dir_judge: pathlib.Path
    experiment_id: str


def _per_row_seed(
    base_seed: int, sample_idx: int, model: str, effort: str | None, repeat: int
) -> int:
    """Deterministic per-row seed independent of Python's hash randomization.

    Using ``hashlib.sha256`` makes the seed identical across processes
    regardless of ``PYTHONHASHSEED``.
    """
    key = f"{base_seed}|{sample_idx:03d}|{model}|{effort or 'null'}|{repeat}"
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _normal_int(rng: random.Random, mean: float, std: float, floor: int) -> int:
    """Truncated-Gaussian integer draw with a hard floor."""
    raw = rng.gauss(mean, std)
    return max(floor, int(round(raw)))


def _normal_float(rng: random.Random, mean: float, std: float, floor: float) -> float:
    raw = rng.gauss(mean, std)
    return max(floor, raw)


def _judge_draw(rng: random.Random, model: str, effort: str | None) -> int:
    profile = JUDGE_PROFILE[(model, effort)]
    r = rng.random()
    cum = 0.0
    for score in (0, 1, 2):
        cum += profile[score]
        if r <= cum:
            return score
    return 2


def _input_tokens_for(sample_idx: int) -> int:
    """Deterministic per-sample input token count anchored to the prompt
    template + ``input`` payload size. Real smoke runs showed 240-241 tokens
    for sf_01; we vary slightly across samples to exercise the variance code
    path without losing the null-case scale (~210-280 tokens)."""
    return 220 + (sample_idx * 13) % 60


def _system_sha() -> str:
    return hashlib.sha256(b"FIXTURE_SYSTEM_PROMPT_PLACEHOLDER\n").hexdigest()


def _user_sha(sample_id: str) -> str:
    return hashlib.sha256(f"FIXTURE_USER:{sample_id}".encode("utf-8")).hexdigest()


def _judge_prompt_sha() -> str:
    """Stable SHA-256 of the judge rubric prompt template (see run_judge.py)."""
    return hashlib.sha256(JUDGE_PROMPT_TEMPLATE.encode("utf-8")).hexdigest()


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


def _build_plans(
    *,
    dataset: list[dict[str, Any]],
    runs_dir: pathlib.Path,
    judge_dir: pathlib.Path,
    experiment_id_gpt4o: str,
    experiment_id_gpt52: str,
) -> list[FixturePlan]:
    plans: list[FixturePlan] = []
    base_ts = "20260520T130000Z"  # synthetic, deterministic; no wall-clock
    for sample_idx, sample in enumerate(dataset):
        sid = sample["id"]
        # gpt-4o baseline (effort = None)
        for r in range(3):
            ts = _synthetic_ts(base_ts, sample_idx, model_idx=0, effort_idx=0, repeat=r)
            plans.append(
                FixturePlan(
                    sample_idx=sample_idx,
                    sample_id=sid,
                    model="gpt-4o",
                    effort=None,
                    repeat=r,
                    timestamp_utc=ts,
                    target_dir_runs=runs_dir,
                    target_dir_judge=judge_dir,
                    experiment_id=experiment_id_gpt4o,
                )
            )
        # gpt-5.2 sweep
        for e_idx, effort in enumerate(EFFORT_TIERS):
            for r in range(3):
                ts = _synthetic_ts(
                    base_ts, sample_idx, model_idx=1, effort_idx=e_idx + 1, repeat=r
                )
                plans.append(
                    FixturePlan(
                        sample_idx=sample_idx,
                        sample_id=sid,
                        model="gpt-5.2",
                        effort=effort,
                        repeat=r,
                        timestamp_utc=ts,
                        target_dir_runs=runs_dir,
                        target_dir_judge=judge_dir,
                        experiment_id=experiment_id_gpt52,
                    )
                )
    return plans


def _synthetic_ts(
    base_ts: str, sample_idx: int, model_idx: int, effort_idx: int, repeat: int
) -> str:
    """Produce a stable ``YYYYMMDDTHHMMSSZ``-shaped timestamp from indices.

    The timestamp is purely synthetic — it is monotonic across rows so each
    file gets a unique sortable prefix, but does NOT pretend to be wall-clock.
    Encoding: ``sample_idx * 1000 + model_idx * 500 + effort_idx * 50 + repeat``
    seconds offset from ``20260520T130000Z``. Max value with 20 samples,
    2 models, 6 effort slots, 3 repeats is ~19_000s (~5h17m) → fits one day.
    """
    offset = sample_idx * 1000 + model_idx * 500 + effort_idx * 50 + repeat
    hh = 13 + (offset // 3600)
    mm = (offset % 3600) // 60
    ss = offset % 60
    days_extra = hh // 24
    hh = hh % 24
    day = 20 + days_extra
    return f"202605{day:02d}T{hh:02d}{mm:02d}{ss:02d}Z"


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


def _render_run_record(
    plan: FixturePlan,
    sample: dict[str, Any],
    rng: random.Random,
) -> tuple[dict[str, Any], int]:
    """Synthesize one measurement JSON. Returns (payload, judge_score)."""
    input_tokens = _input_tokens_for(plan.sample_idx)
    out_mean, out_std = OUTPUT_PROFILE[plan.effort]
    output_tokens = _normal_int(rng, out_mean, out_std, floor=3)

    if plan.model == "gpt-4o":
        reasoning_tokens = 0
    else:
        r_mean, r_std = REASONING_PROFILE[plan.effort or "minimal"]
        reasoning_tokens = _normal_int(rng, r_mean, r_std, floor=0)

    lat_mean, lat_std = LATENCY_PROFILE[plan.effort]
    latency_ms = _normal_float(rng, lat_mean, lat_std, floor=80.0)

    cold = rng.random() < COLD_START_RATE
    retry_count = 1 if rng.random() < RETRY_RATE else 0
    truncated = rng.random() < TRUNCATED_RATE

    # Inject a 3-sigma outlier on flagged rows so the outlier code path is
    # exercised end-to-end (the spec requires a non-zero outlier tally only
    # when real outliers exist; fixture flags are real instrumentation events).
    # gpt-4o has no reasoning column — bump only output_tokens + latency.
    if cold or retry_count > 0 or truncated:
        if plan.model == "gpt-5.2":
            reasoning_tokens = int(reasoning_tokens * 1.8) + 250
        output_tokens = int(output_tokens * 1.6) + 12
        latency_ms = latency_ms * 1.9 + 800

    total_tokens = input_tokens + output_tokens
    payload: dict[str, Any] = {
        "fixture": True,
        "fixture_note": (
            "Synthetic Task 007 measurement cell — generated by "
            "scripts._fixture_synth for the Task 008 offline pipeline. NOT a "
            "real Azure call."
        ),
        "api_version": "preview",
        "auth_mode": "entra",
        "call_metadata": {
            "deployment_cold_start": cold,
            "system_prompt_sha256": _system_sha(),
            "time_since_last_identical_prefix_seconds": None,
            "tool_config_sha256": None,
            "user_input_sha256": _user_sha(plan.sample_id),
        },
        "cold_start": cold,
        "deployment_name": plan.model,
        "dirty": False,
        "dry_run": False,
        "effort": plan.effort,
        "endpoint": FIXTURE_ENDPOINT,
        "experiment_id": plan.experiment_id,
        "git_commit": FIXTURE_GIT_COMMIT,
        "latency_ms": round(latency_ms, 6),
        "model": plan.model,
        "pricing_snapshot_path": PRICING_SNAPSHOT_PATH,
        "repeat": plan.repeat,
        "response_text": _fake_response_text(sample),
        "retry_count": retry_count,
        "sample_id": plan.sample_id,
        "sample_idx": plan.sample_idx,
        "sample_metadata": sample,
        "timestamp_utc": _ts_iso(plan.timestamp_utc),
        "truncated_output": truncated,
        "usage": {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
            "total_tokens": total_tokens,
        },
    }

    judge_score = _judge_draw(rng, plan.model, plan.effort)
    return payload, judge_score


def _fake_response_text(sample: dict[str, Any]) -> str:
    return f"[fixture response for {sample['id']}]"


def _ts_iso(packed: str) -> str:
    # packed = "YYYYMMDDTHHMMSSZ" → "YYYY-MM-DDTHH:MM:SSZ"
    y, mo, d = packed[0:4], packed[4:6], packed[6:8]
    hh, mm, ss = packed[9:11], packed[11:13], packed[13:15]
    return f"{y}-{mo}-{d}T{hh}:{mm}:{ss}Z"


def _render_judge_record(
    plan: FixturePlan, score: int, rationale: str
) -> dict[str, Any]:
    return {
        "fixture": True,
        "fixture_note": (
            "Synthetic judge call — generated by scripts._fixture_synth for "
            "the Task 008 offline pipeline. NOT a real Azure call."
        ),
        "judge_model": "gpt-4o",
        "judge_prompt_sha256": _judge_prompt_sha(),
        "sample_id": plan.sample_id,
        "model": plan.model,
        "effort": plan.effort,
        "repeat": plan.repeat,
        "score": score,
        "rationale": rationale,
        "source_run_filename": _run_filename(plan),
    }


_RATIONALE_BY_SCORE = {
    0: "Output failed the rubric requirement.",
    1: "Output partially met the rubric requirement.",
    2: "Output met every rubric requirement.",
}


def synthesize(
    *,
    dataset_path: pathlib.Path,
    runs_dir: pathlib.Path,
    judge_dir: pathlib.Path,
    seed: int = 4242,
    experiment_id_gpt4o: str = FIXTURE_EXPERIMENT_ID_GPT4O,
    experiment_id_gpt52: str = FIXTURE_EXPERIMENT_ID_GPT52,
) -> tuple[int, int]:
    """Materialize 360 measurement JSONs + 360 judge JSONs deterministically.

    Returns:
        ``(n_runs_written, n_judge_written)``.
    """
    with dataset_path.open("r", encoding="utf-8") as fh:
        dataset = json.load(fh)
    if not isinstance(dataset, list):
        raise ValueError(f"dataset must be a list; got {type(dataset).__name__}")

    runs_dir.mkdir(parents=True, exist_ok=True)
    judge_dir.mkdir(parents=True, exist_ok=True)

    plans = _build_plans(
        dataset=dataset,
        runs_dir=runs_dir,
        judge_dir=judge_dir,
        experiment_id_gpt4o=experiment_id_gpt4o,
        experiment_id_gpt52=experiment_id_gpt52,
    )

    n_runs = 0
    n_judge = 0
    for i, plan in enumerate(plans):
        # Seed per-row to keep determinism stable even if plan ordering changes.
        rng = random.Random(
            _per_row_seed(seed, plan.sample_idx, plan.model, plan.effort, plan.repeat)
        )
        sample = dataset[plan.sample_idx]
        run_payload, judge_score = _render_run_record(plan, sample, rng)
        run_path = runs_dir / _run_filename(plan)
        run_path.write_text(
            json.dumps(run_payload, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        n_runs += 1

        judge_payload = _render_judge_record(
            plan, judge_score, _RATIONALE_BY_SCORE[judge_score]
        )
        judge_path = judge_dir / _judge_filename(plan)
        judge_path.write_text(
            json.dumps(judge_payload, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        n_judge += 1

    return n_runs, n_judge


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scripts._fixture_synth",
        description=(
            "Generate the 360 synthetic measurement + 360 synthetic judge "
            "JSONs that back the Task 008 offline aggregator. Deterministic; "
            "re-running over the same target directories produces byte-"
            "identical files. Marker `fixture: true` is written into every "
            "payload so no human can confuse a synthesized cell with a real "
            "Task 007 call."
        ),
    )
    p.add_argument(
        "--dataset",
        default="benchmarks/01-short-factual/dataset.json",
    )
    p.add_argument(
        "--runs-dir",
        default="benchmarks/01-short-factual/runs",
    )
    p.add_argument(
        "--judge-dir",
        default="benchmarks/01-short-factual/judge_runs",
    )
    p.add_argument("--seed", type=int, default=4242)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    ns = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    n_runs, n_judge = synthesize(
        dataset_path=pathlib.Path(ns.dataset),
        runs_dir=pathlib.Path(ns.runs_dir),
        judge_dir=pathlib.Path(ns.judge_dir),
        seed=ns.seed,
    )
    logger.info("fixture: wrote %d runs and %d judge records", n_runs, n_judge)
    return 0


if __name__ == "__main__":
    sys.exit(main())
