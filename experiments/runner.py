"""A tiny, uniform Python interface for every experiment in this directory.

Each experiment is a YAML file (its *input*). Each YAML is consumed by exactly
one runner under ``scripts/``. Historically you had to know which runner to
call and how to spell its flags::

    python -m scripts.run_benchmark            --experiment experiments/exp001_short-factual_baseline.yaml
    python -m scripts.simulate_spillover       --experiment experiments/exp004_spillover_baseline_reactive.yaml
    python -m scripts.measure_dual_spillover   --experiment experiments/exp005_dual_spillover_reactive.yaml
    python -m scripts.measure_cache_key_bucketing --experiment experiments/exp006_cache_key_bucketing_inmemory.yaml
    python -m scripts.measure_max_output_tokens_sweep --experiment experiments/exp007_max_output_tokens_sweep.yaml

This module collapses all of that into one call::

    import experiments
    result = experiments.run("exp001_short-factual_baseline.yaml", dry_run=True)
    print(result.summary())

and one read-only introspection call that needs no cloud credentials::

    spec = experiments.describe("exp006_cache_key_bucketing_inmemory.yaml")
    print(spec.inputs)     # what the experiment reads
    print(spec.variable)   # the one independent variable it sweeps
    print(spec.output_dir) # where its raw records land

``describe`` and ``list_experiments`` only parse YAML — they do not import the
heavy runner modules and never touch the network, so they work with just
``pyyaml`` installed. ``run`` imports the matching runner lazily and forwards
to its tested ``main([...])`` entry point; nothing here re-implements the
measurement logic.

Input  → a YAML file in ``experiments/``.
Output → an :class:`ExperimentResult` (exit code + where records were written);
         the runner writes raw JSON/JSONL under ``benchmarks/<benchmark>/runs/``.
"""

from __future__ import annotations

import dataclasses
import importlib
import pathlib
import sys
from typing import Any, Sequence

import yaml

# --------------------------------------------------------------------------
# Repo-root bootstrap so ``import scripts.<runner>`` resolves no matter what
# the caller's working directory is. The experiments package lives at
# ``<repo>/experiments/``; the runners live at ``<repo>/scripts/``.
# --------------------------------------------------------------------------
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
EXPERIMENTS_DIR = REPO_ROOT / "experiments"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

__all__ = [
    "ExperimentSpec",
    "ExperimentResult",
    "RUNNERS",
    "describe",
    "run",
    "list_experiments",
]


# --------------------------------------------------------------------------
# Static family metadata. One entry per experiment "family"; every YAML maps
# to exactly one family by its ``experiment_id`` prefix. This is the single
# source of truth that experiments/README.md documents in prose.
# --------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class _Family:
    runner: str  # module name under scripts/, e.g. "run_benchmark"
    benchmark: str  # benchmarks/<this>/ directory holding the inputs
    # Benchmark whose input corpus is actually read (differs from `benchmark`
    # only when an experiment reuses another benchmark's corpus read-only).
    input_benchmark: str | None = None
    intent: str = ""  # the question the experiment is designed to answer
    task: str = ""  # what the runner actually does, in one or two lines
    outputs: tuple[str, ...] = ()  # artifact chain a real (non-dry) run writes


_FAMILIES: dict[str, _Family] = {
    "A1": _Family(
        "run_benchmark",
        "01-short-factual",
        intent=(
            "On short factual Q&A, does higher reasoning effort buy enough "
            "quality to justify its extra (mostly hidden) reasoning tokens? "
            "Expectation: quality stays roughly flat while cost climbs, so "
            "effort should not pay here."
        ),
        task=(
            "Ask the same 20 questions 3x each at five effort levels on "
            "gpt-5.2, with a gpt-4o column as the no-reasoning floor."
        ),
        outputs=(
            "benchmarks/01-short-factual/runs/*.json  (raw per-cell usage + latency)",
            "benchmarks/01-short-factual/judge_runs/  (LLM-judge quality grades)",
            "benchmarks/01-short-factual/analysis.{json,md}  (aggregated verdict)",
            "results/public/chart-data/{cost-curves-effort,token-composition,"
            "ptu-payg-crossover}/benchmark-01/",
        ),
    ),
    "A2": _Family(
        "run_benchmark",
        "02-multi-step-reasoning",
        intent=(
            "On genuinely multi-step problems, does effort start earning its "
            "cost -- does quality climb enough at some tier to beat the flat "
            "short-factual case?"
        ),
        task=(
            "Same 20x3 design across five effort levels on gpt-5.2 (plus a "
            "gpt-4o baseline), over the multi-step-reasoning dataset."
        ),
        outputs=(
            "benchmarks/02-multi-step-reasoning/runs/*.json  (raw per-cell usage + latency)",
            "benchmarks/02-multi-step-reasoning/judge_runs/  (LLM-judge quality grades)",
            "benchmarks/02-multi-step-reasoning/analysis.{json,md}  (aggregated verdict)",
            "results/public/chart-data/{cost-curves-effort,token-composition,"
            "ptu-payg-crossover}/benchmark-02/",
        ),
    ),
    "A3": _Family(
        "run_benchmark",
        "03-tool-using-agent",
        intent=(
            "For tool-using agent tasks, where is the quality ceiling, and "
            "what latency does each effort tier cost? Agent work must be read "
            "on quality AND latency together."
        ),
        task=(
            "Same 20x3 five-effort design (plus a gpt-4o baseline) over the "
            "agent dataset, which adds a tool knowledge base and tool schemas."
        ),
        outputs=(
            "benchmarks/03-tool-using-agent/runs/*.json  (raw per-cell usage + latency)",
            "benchmarks/03-tool-using-agent/judge_runs/  (LLM-judge quality grades)",
            "benchmarks/03-tool-using-agent/analysis.{json,md}  (aggregated verdict)",
            "results/public/chart-data/{cost-curves-effort,token-composition,"
            "ptu-payg-crossover}/benchmark-03/",
        ),
    ),
    "B4": _Family(
        "simulate_spillover",
        "04-spillover-simulation",
        intent=(
            "Which spillover routing policy recovers cache-hit ratio faster "
            "after throttling -- a reactive (timeout/429-triggered) router or "
            "a proactive (p95-latency ramp) one? Phase 1 isolates the policy "
            "layer, not the network."
        ),
        task=(
            "Replay a ~30K-token ReAct workload (corpus seed 4242) through the "
            "policy router over a 22-minute ramp-then-sustain load; the "
            "reactive and proactive policies are two sibling YAMLs."
        ),
        outputs=(
            "benchmarks/04-spillover-simulation/runs/*.jsonl  (+ *.summary.json)",
            "benchmarks/04-spillover-simulation/analysis.{json,md}",
            "results/spillover-recovery-curves/*.png  (cache-hit ratio over time)",
        ),
    ),
    "B5": _Family(
        "measure_dual_spillover",
        "05-dual-spillover",
        intent=(
            "Does the reactive-vs-proactive comparison hold under REAL "
            "throttling across two live deployments -- a low-TPM primary that "
            "emits real 429s and a high-TPM spillover pool?"
        ),
        task=(
            "Phase-2 dual-endpoint measurement of the same policy pair against "
            "two real gpt-5.2 deployments under the same load profile."
        ),
        outputs=(
            "benchmarks/05-dual-spillover/runs/*.jsonl  (+ *.summary.json)",
            "results/dual-spillover-curves/*.png  (per-endpoint, aggregate, "
            "real-429 timeline)",
        ),
    ),
    "B6": _Family(
        "measure_cache_key_bucketing",
        "06-cache-key-bucketing",
        intent=(
            "Is prompt_cache_key bucket cardinality a real capacity lever? "
            "Splitting one hot key into 8 buckets should change routing "
            "affinity (and cache-hit ratio), and 24h retention should reshape "
            "the overflow threshold vs the in_memory default."
        ),
        task=(
            "Async-scheduled sweep on one unthrottled gpt-5.2 pool over "
            "bucket_cardinality {1, 8}, run once under in_memory retention and "
            "once under 24h (~9.5K-token corpus, seed 4242)."
        ),
        outputs=(
            "benchmarks/06-cache-key-bucketing/runs/*.jsonl  (+ *.summary.json)",
            "benchmarks/06-cache-key-bucketing/analysis.md",
        ),
    ),
    "B7": _Family(
        "measure_max_output_tokens_sweep",
        "07-max-output-tokens-reservation",
        input_benchmark="04-spillover-simulation",
        intent=(
            "Does admission-time token reservation scale with the requested "
            "max_output_tokens even when the actual output is tiny? A PAYG "
            "proxy for how PTU reservation consumes throughput budget."
        ),
        task=(
            "Log2 sweep of max_output_tokens {256..16384} on a deliberately "
            "throttled low-TPM deployment, reusing benchmark 04's corpus "
            "read-only (~2K-token prompt, SHA-pinned)."
        ),
        outputs=(
            "benchmarks/07-max-output-tokens-reservation/runs/*.jsonl  (+ *.summary.json)",
            "benchmarks/07-max-output-tokens-reservation/analysis.md",
        ),
    ),
}

# Public, human-readable map of every runner module to its one-line role.
RUNNERS: dict[str, str] = {
    "run_benchmark": "Family A — reasoning-effort sweep against a live model",
    "simulate_spillover": "Family B — single-endpoint spillover policy simulation",
    "measure_dual_spillover": "Family B — dual-endpoint spillover measurement",
    "measure_cache_key_bucketing": "Family B — prompt_cache_key bucketing sweep",
    "measure_max_output_tokens_sweep": "Family B — max_output_tokens reservation sweep",
}

_PRICING_POLICY_RUNNERS = {
    "measure_dual_spillover",
    "measure_cache_key_bucketing",
    "measure_max_output_tokens_sweep",
}

# Candidate input files, in read-order, probed against a benchmark directory.
_INPUT_CANDIDATES = (
    "dataset.json",
    "prompts",
    "system_prompt_corpus.json",
    "user_prompts.json",
    "search_kb.json",
    "tool_schemas.json",
)


def _family_of(experiment_id: str) -> str:
    """Map an ``experiment_id`` to its family key (see ``_FAMILIES``)."""
    eid = experiment_id
    if eid.startswith("exp001") or eid.startswith("exp_smoke_01"):
        return "A1"
    if eid.startswith("exp002") or eid.startswith("exp_smoke_02"):
        return "A2"
    if eid.startswith("exp003") or eid.startswith("exp_smoke_03"):
        return "A3"
    for prefix, key in (("exp004", "B4"), ("exp005", "B5"), ("exp006", "B6"), ("exp007", "B7")):
        if eid.startswith(prefix):
            return key
    raise KeyError(
        f"cannot classify experiment_id {experiment_id!r}: no known runner. "
        f"Known prefixes: exp001-exp007, exp_smoke_01-03."
    )


# --------------------------------------------------------------------------
# Public dataclasses — the "clear out" half of the interface.
# --------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class ExperimentSpec:
    """A read-only description of one experiment: input → variable → output.

    Produced by :func:`describe`. Purely derived from the YAML plus the
    on-disk benchmark layout; constructing it makes no network call and does
    not import the runner module.
    """

    experiment_id: str
    config_path: str  # repo-relative path to the YAML input
    runner_module: str  # scripts/<this>.py consumes the YAML
    command: str  # the exact CLI equivalent of experiments.run(...)
    benchmark: str  # benchmarks/<this>/ the runner operates on
    inputs: list[str]  # files the experiment reads (repo-relative)
    variable: str  # the single independent variable being swept
    output_dir: str  # where raw records are written
    description: str  # first sentence of the YAML's own description
    intent: str  # the question this experiment is designed to answer
    task: str  # what the runner actually does, in one or two lines
    outputs: list[str]  # the artifact chain a real (non-dry) run produces

    def summary(self) -> str:
        lines = [
            f"experiment_id : {self.experiment_id}",
            f"intent        : {self.intent}",
            f"task          : {self.task}",
            f"config (input): {self.config_path}",
            f"runner        : scripts/{self.runner_module}.py",
            f"variable swept: {self.variable}",
            "inputs        : " + (self.inputs[0] if self.inputs else "(none)"),
        ]
        for extra in self.inputs[1:]:
            lines.append(f"                {extra}")
        primary_output = self.outputs[0] if self.outputs else self.output_dir
        lines.append(f"outputs       : {primary_output}")
        for extra in self.outputs[1:]:
            lines.append(f"                {extra}")
        lines.append(f"run command   : {self.command}")
        return "\n".join(lines)


@dataclasses.dataclass(frozen=True)
class ExperimentResult:
    """The outcome of :func:`run`.

    ``exit_code == 0`` means the runner completed. Raw records are written by
    the runner under :attr:`ExperimentSpec.output_dir`; the runner also prints
    its own ``=== <runner> summary ===`` block to the console.
    """

    spec: ExperimentSpec
    exit_code: int
    dry_run: bool
    argv: list[str]

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def summary(self) -> str:
        status = "OK" if self.ok else f"FAILED (exit {self.exit_code})"
        mode = "dry-run" if self.dry_run else "evidence"
        return (
            f"{self.spec.experiment_id}  [{mode}]  {status}\n"
            f"  ran     : python -m scripts.{self.spec.runner_module} "
            + " ".join(self.argv)
            + f"\n  outputs : {self.spec.output_dir}"
        )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _resolve_config_path(config: str | pathlib.Path) -> pathlib.Path:
    """Accept a bare filename, a repo-relative path, or an absolute path."""
    p = pathlib.Path(config)
    candidates = [p]
    if not p.is_absolute():
        candidates += [REPO_ROOT / p, EXPERIMENTS_DIR / p.name]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    raise FileNotFoundError(
        f"experiment YAML not found: {config!r} "
        f"(looked in {EXPERIMENTS_DIR} and repo root)"
    )


def _repo_rel(path: pathlib.Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _read_yaml(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def _first_sentence(text: str) -> str:
    if not text:
        return ""
    flat = " ".join(text.split())
    dot = flat.find(". ")
    return (flat[: dot + 1] if dot != -1 else flat).strip()


def _inputs_for(family_key: str, fam: _Family) -> list[str]:
    """List the input files an experiment reads, verified to exist on disk."""
    read_bench = fam.input_benchmark or fam.benchmark
    base = REPO_ROOT / "benchmarks" / read_bench
    found: list[str] = []
    for name in _INPUT_CANDIDATES:
        if (base / name).exists():
            rel = f"benchmarks/{read_bench}/{name}"
            if fam.input_benchmark is not None:
                rel += "  (reused read-only)"
            found.append(rel)
    return found


def _variable_for(family_key: str, cfg: dict[str, Any]) -> str:
    """Describe the single independent variable this YAML sweeps."""
    if family_key.startswith("A"):
        model = cfg.get("model") or {}
        if model.get("family") == "gpt-4o":
            return "baseline column — gpt-4o sends no reasoning parameter (temperature=0.0)"
        effort = (cfg.get("sweep") or {}).get("effort") or []
        levels = ", ".join(str(e) for e in effort) if effort else "(see sweep.effort)"
        return f"reasoning.effort \u2208 {{{levels}}}"
    if family_key in ("B4", "B5"):
        return "policy.type = reactive vs proactive (two sibling YAMLs)"
    if family_key == "B6":
        return "sweep.bucket_cardinality {1, 8} \u00d7 prompt_cache_retention {in_memory, 24h}"
    if family_key == "B7":
        return "max_output_tokens \u2208 {256, 512, 1024, 2048, 4096, 8192, 16384}"
    return "(see YAML)"


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def describe(config: str | pathlib.Path) -> ExperimentSpec:
    """Introspect an experiment YAML without running anything.

    Args:
        config: A bare filename (``"exp001_short-factual_baseline.yaml"``), a
            repo-relative path, or an absolute path to an ``experiments/*.yaml``.

    Returns:
        An :class:`ExperimentSpec` stating the runner, the inputs it reads, the
        single variable it sweeps, and where its output lands.

    Raises:
        FileNotFoundError: The YAML cannot be located.
        KeyError: The ``experiment_id`` matches no known runner.
    """
    path = _resolve_config_path(config)
    cfg = _read_yaml(path)
    experiment_id = str(cfg.get("experiment_id") or path.stem)
    family_key = _family_of(experiment_id)
    fam = _FAMILIES[family_key]
    rel = _repo_rel(path)
    if experiment_id.startswith("exp_smoke"):
        intent = (
            "Pipeline pre-flight -- exercises auth, the runner, budget guards, "
            "and aggregation end-to-end. Deliberately too small to be evidence."
        )
        task = (
            "Tiny smoke run (N=2, R=1) at effort {low, high}; excluded from "
            "every published analysis and chart."
        )
        outputs = [f"benchmarks/{fam.benchmark}/runs/  (smoke records only; not analyzed)"]
    else:
        intent = fam.intent
        task = fam.task
        outputs = list(fam.outputs)
    return ExperimentSpec(
        experiment_id=experiment_id,
        config_path=rel,
        runner_module=fam.runner,
        command=f"python -m scripts.{fam.runner} --experiment {rel}",
        benchmark=fam.benchmark,
        inputs=_inputs_for(family_key, fam),
        variable=_variable_for(family_key, cfg),
        output_dir=f"benchmarks/{fam.benchmark}/runs/",
        description=_first_sentence(str(cfg.get("description") or "")),
        intent=intent,
        task=task,
        outputs=outputs,
    )


def run(
    config: str | pathlib.Path,
    *,
    dry_run: bool = False,
    allow_dirty: bool | None = None,
    extra_args: Sequence[str] | None = None,
) -> ExperimentResult:
    """Run one experiment by dispatching to its runner's ``main([...])``.

    Args:
        config: The experiment YAML (see :func:`describe` for accepted forms).
        dry_run: If True, forward ``--dry-run``. Pricing-aware campaign runners
            also receive ``--pricing-policy historical-replay`` so committed
            evidence remains deterministic without authorizing a live call.
        allow_dirty: Forward ``--allow-dirty`` (tolerate an uncommitted git
            tree). Defaults to the value of ``dry_run`` — evidence runs
            (``dry_run=False``) require a clean tree so the ``git_commit``
            embedded in every raw record is meaningful; dry-runs do not.
        extra_args: Extra CLI flags passed verbatim to the runner (e.g.
            ``["--max-samples", "2"]`` for run_benchmark, or ``["--smoke"]``).

    Returns:
        An :class:`ExperimentResult` carrying the exit code and the resolved
        argv. The runner writes its raw records under ``spec.output_dir`` and
        prints its own summary block.

    Note:
        Runners read Azure endpoint/deployment configuration from environment
        variables even in ``dry_run`` mode (they are embedded in the audit
        trail, not called). See ``.env.example`` for the required names.
    """
    spec = describe(config)
    if allow_dirty is None:
        allow_dirty = dry_run

    path = _resolve_config_path(config)
    argv: list[str] = ["--experiment", _repo_rel(path)]
    if dry_run:
        argv.append("--dry-run")
        if spec.runner_module in _PRICING_POLICY_RUNNERS:
            argv.extend(["--pricing-policy", "historical-replay"])
    if allow_dirty:
        argv.append("--allow-dirty")
    if extra_args:
        argv.extend(extra_args)

    module = importlib.import_module(f"scripts.{spec.runner_module}")
    exit_code = int(module.main(argv))
    return ExperimentResult(spec=spec, exit_code=exit_code, dry_run=dry_run, argv=argv)


def list_experiments(experiments_dir: str | pathlib.Path | None = None) -> list[ExperimentSpec]:
    """Describe every ``exp*.yaml`` in the experiments directory.

    The annotated ``_template.yaml`` is intentionally excluded.
    """
    directory = pathlib.Path(experiments_dir) if experiments_dir else EXPERIMENTS_DIR
    specs: list[ExperimentSpec] = []
    for yaml_path in sorted(directory.glob("exp*.yaml")):
        specs.append(describe(yaml_path))
    return specs
