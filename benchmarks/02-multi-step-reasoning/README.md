# Benchmark 02 — Multi-Step Reasoning (Ceiling Case)

## Why this benchmark exists

This benchmark is the **ceiling case** for reasoning effort. The 20 samples here
are problems that require **chained inference** — every solution needs at least
two inferential steps. A non-reasoning model is expected to fail a meaningful
fraction of these on the first try; a sufficiently-equipped reasoning model is
expected to outperform it.

Paired with benchmark 01 (the floor: tasks where reasoning is unnecessary), this
benchmark establishes the **upper anchor** of the cost-vs-quality trade. The
repo can then answer the decision question:

> For multi-step reasoning tasks, at what GPT-5.2 `reasoning_effort` level (if
> any) does reasoning earn its bill, expressed both as PAYG cost-per-correct
> answer and as PTU throughput-gain at matched quality?

Without the ceiling case the floor case is unanchored; without the floor case
the ceiling case overclaims. Both are required.

## Position in the repo

- Methodology contract: [`docs/05-methodology.md`](../../docs/05-methodology.md)
  §2 (Variables) and §5 (Quality Evaluation). This benchmark inherits the full
  controlled-variables regime (byte-identical system prompt across efforts,
  R = 3 repeats, N = 20).
- Build Order: Phase 3, steps 12 and 13 — dataset + prompts (this directory)
  plus the full measurement run shipped under
  `experiments/exp002_benchmark02_*.yaml`. The cost-vs-quality verdict lands in
  this directory's `analysis.md`.
- Tooling: runner, judge, analyzer, cost calculator, and plot generator are
  all frozen (Tasks 004 / 003 / 008). Task 009 wires the existing tooling to
  the new benchmark — it changes no scripts.

## What is in this directory

```
02-multi-step-reasoning/
├── README.md            # this file
├── dataset.json         # 20 verifiable-answer samples (mr_01 .. mr_20)
├── prompts/
│   ├── system.md        # neutral system prompt, byte-identical across efforts
│   └── user_template.md # deterministic user-prompt template, two placeholders
├── runs/                # per-experiment raw response JSON (populated by runner)
├── judge_runs/          # per-cell LLM-as-judge JSON (populated by run_judge)
├── SMOKE_REPORT.md      # Phase-1 smoke gate
├── RUN_REPORT.md        # Phase-2 full-run summary (data-clean, no analysis)
├── analysis.json        # Phase-3 deterministic aggregation output
└── analysis.md          # Phase-3 narrative + cost-vs-quality verdict
```

## Dataset design

- **N = 20** samples, IDs `mr_01` through `mr_20`. The set is exact: no gaps, no
  duplicates, no out-of-range IDs.
- **Synthetic only.** No real customer information, no real email domains, no
  US phone formats, no celebrity / current-events questions whose ground truth
  shifts over time.
- Each sample carries:
  - `id` — `mr_NN`
  - `input` — the task payload, expressed in natural language
  - `verifiable_answer` — the exact correct answer (or set of acceptable
    answers) the judge will key against. Every sample has one.
  - `expected_output_shape` — a one-sentence description of the answer shape,
    used by the user-prompt template and the LLM-as-judge prompt
  - `quality_rubric_notes` — concrete acceptance criteria the judge uses to
    score the response: which values must appear, what wording flexibility is
    allowed, what disqualifies a response
  - `tags` — task-subtype labels for slicing the aggregate

## Task taxonomy

The 20 samples span the following subtypes. Each subtype meets or exceeds the
spec minimum so no single subtype dominates the aggregate.

| Subtype                  | Spec min | Samples                                                   | Count |
| ------------------------ | -------: | --------------------------------------------------------- | ----: |
| arithmetic-word          | ≥ 4      | mr_01, mr_02, mr_03, mr_04, mr_20                         | 5     |
| constraint-satisfaction  | ≥ 3      | mr_05, mr_06, mr_07                                       | 3     |
| date-time                | ≥ 2      | mr_08, mr_09                                              | 2     |
| causal-chain             | ≥ 3      | mr_10, mr_11, mr_12                                       | 3     |
| code-trace               | ≥ 3      | mr_13, mr_14, mr_15                                       | 3     |
| boolean-logic            | ≥ 2      | mr_16, mr_17                                              | 2     |
| counting                 | ≥ 2      | mr_18, mr_19                                              | 2     |

Seven distinct subtype labels appear, meeting the ≥ 6 spec requirement with
margin.

## Reasoning IS required

Every sample in this dataset requires at least two inferential steps. A
single-lookup or direct-extraction answer is not possible. If a future reader
believes a sample can be solved in one forward pass with no chain of inference,
that sample is mis-classified and belongs in benchmark 01 — file an issue.

Examples of the reasoning surface:

- `mr_01` requires geometric-series sum (12+24+48) **then** multiplication.
- `mr_03` requires computing a head-start distance **then** dividing by
  relative speed.
- `mr_05` requires elimination over a 4-position permutation under three
  joint constraints.
- `mr_12` requires chaining three modus-ponens steps to discover the
  consequent.
- `mr_13`–`mr_15` require step-by-step Python execution under aliasing,
  slicing, and conditional rules.
- `mr_19` requires inclusion-exclusion **then** subtraction from the total.

## Prompts

- `prompts/system.md` — a single neutral system prompt under 2000 characters.
  It is byte-identical for every `(model, effort, repeat)` cell. It **does not**
  contain reasoning-triggering language ("think step by step", "reason
  carefully", "show your work", etc.) — the forbidden-phrase audit is enforced
  by grep against this file. This is the fairness invariant with benchmark 01:
  the model decides on its own how much reasoning to spend; the prompt does not
  tilt the scale.
- `prompts/user_template.md` — a two-line template with exactly two
  placeholders, `{input}` and `{expected_output_shape}`. The runner
  (`scripts/run_benchmark.py`) renders these per sample via `str.format_map`,
  producing a byte-identical rendered prompt across efforts and repeats.

A single-character edit to either file forces a new experiment ID and a
re-baseline; the cache-invalidation cost of an unintentional edit would
silently contaminate every cross-effort comparison.

## Expected baseline behavior (pre-registered)

- **gpt-4o pass-rate: 30–60 %.** Multi-step inference without a reasoning
  surface is genuinely hard for gpt-4o on tasks like aliasing and inclusion-
  exclusion. The pre-registered floor is 30 %; values below 30 % would
  indicate the dataset is too punishing and would require recalibration.
- **gpt-5.2 high pass-rate: ≥ 80 %.** With a high reasoning budget the model
  should handle every subtype in the taxonomy. Values below 80 % would
  indicate either dataset ambiguity or a quality regression in the reasoning
  surface.
- **Quality monotonicity:** gpt-5.2 pass-rate should be **non-decreasing**
  across `[none, low, medium, high, xhigh]` in expectation. If the curve
  inverts, the benchmark did not measure what it claimed and the result is a
  BLOCK on review.

The quality metric used in `analysis.md` is the **binarized 3-tier judge
rubric**: `pass = (score == 2)`. The 3-tier rubric (`0 = fail`, `1 = partial`,
`2 = pass`) is reused verbatim from
[`scripts/run_judge.py`](../../scripts/run_judge.py); the analyzer enforces
`score ∈ {0, 1, 2}` and the binarization choice lives downstream in
`analysis.md` and `analysis.json`. Partial credit (`score == 1`) is reported
separately and **excluded** from the `cost_per_correct` denominator.

## Pre-registered cost surface

- **PAYG lens:** `cost_per_correct = mean_usd_per_cell / pass_rate`. The
  gpt-4o baseline cost per call is small, but a low pass-rate inflates the
  cost-per-correct dramatically; the gpt-5.2 high-effort cell is more
  expensive per call but, if it converts to a high pass-rate, the
  cost-per-correct can fall below the baseline. The crossover point (if any)
  is the headline finding of this benchmark.
- **PTU lens:** `throughput_gain = baseline_tokens_per_cell / cell_tokens` at
  matched pass-rate. A PTU customer pays a fixed PTU price; their lever is
  throughput. The same chart pair tells both stories.

## Reasoning IS required — but the prompt does not say so

The fairness invariant is critical: we measure what the model decides to spend
on reasoning, not what the prompt nudged it to spend. Adding "think step by
step" to the system prompt would conflate the effort-level variable with the
prompt-driven reasoning trigger. The system prompt therefore says nothing
about reasoning depth.

## Verification (deterministic)

The following checks are enforced; each is a single shell or python one-liner
and must pass for the dataset to be considered correct.

```bash
# Sample count and ID uniqueness
python -c "import json; d = json.load(open('benchmarks/02-multi-step-reasoning/dataset.json')); assert len(d) == 20; assert len({s['id'] for s in d}) == 20"

# IDs are exactly the set mr_01 .. mr_20 (strict set equality, not a regex)
python -c "import json; d = json.load(open('benchmarks/02-multi-step-reasoning/dataset.json')); expected = {f'mr_{i:02d}' for i in range(1, 21)}; actual = {s['id'] for s in d}; assert actual == expected"

# Required fields present on every sample
python -c "import json; d = json.load(open('benchmarks/02-multi-step-reasoning/dataset.json')); req = {'id','input','verifiable_answer','expected_output_shape','quality_rubric_notes','tags'}; assert all(req.issubset(s.keys()) for s in d)"

# Tag diversity >= 6 distinct tags
test "$(jq -r '.[].tags[]' benchmarks/02-multi-step-reasoning/dataset.json | sort -u | wc -l | tr -d ' ')" -ge 6

# Forbidden phrases absent from prompts and dataset (exit code 1 = pass)
grep -iE "step by step|reason carefully|reason through|let's think|show your work|carefully consider" \
    benchmarks/02-multi-step-reasoning/prompts/system.md \
    benchmarks/02-multi-step-reasoning/prompts/user_template.md \
    benchmarks/02-multi-step-reasoning/dataset.json
```

## Out of scope for this directory

- Running the benchmark — see `experiments/exp002_benchmark02_*.yaml` and the
  smoke YAMLs under `experiments/exp_smoke_02*.yaml`.
- Cross-benchmark synthesis — Task 010 (`results/summary.md`).
- Benchmark 03 (tool-using agent) — Task 010.
- Re-engineering scripts — Task 009 changes no scripts; every tool is a frozen
  contract.
