# Experiments — how each measurement was loaded

This directory holds the **frozen experiment definitions** (`exp*.yaml`) that
drove every number in this repo. Each YAML is a complete, re-runnable
description of one measurement, so a reader can see at a glance *how the load
was applied*: what **input** was sent, which single **variable** was swept, and
where the **output** landed.

Nothing here calls a live service on its own — a YAML is a declarative recipe.
The runner named for each family (below) reads the YAML, replays the pinned
input, sweeps the one declared axis, and writes append-only JSON under
`benchmarks/<name>/runs/`.

## How to read any experiment

Every experiment is three things, and only three things change meaning between
experiments:

| Part | What it is | Where it comes from |
| --- | --- | --- |
| **Input** | The exact bytes sent to the model or simulator. Pinned and SHA-256-fingerprinted so it cannot drift silently. | `benchmarks/<name>/dataset.json` + `prompts/` (Family A), or `benchmarks/<name>/system_prompt_corpus.json` + `user_prompts.json` (Family B) |
| **Variable** | The **one** axis that changes across cells. Everything else is held constant. | `sweep:` in the YAML (or a sibling YAML that flips exactly one field) |
| **Output** | Raw per-request JSON, then offline aggregates, then the promoted chart data behind the dashboard. | `benchmarks/<name>/runs/` → `analysis.md` → `results/public/chart-data/` |

Sibling experiments share a `parent_experiment` and change exactly one variable
from that parent — that lineage is how a comparison stays honest.

---

## The easy way — one Python call

Prefer not to memorize five runner names and their flags? The
[`experiments`](__init__.py) package wraps every runner behind a single, uniform
interface, and [`experiments/examples/`](examples/) has runnable examples for
each family. Run from the repository root:

```python
import experiments

# What does an experiment read, sweep, and write?  (YAML only — no network.)
spec = experiments.describe("exp001_short-factual_baseline.yaml")
print(spec.summary())

# Run it. dry_run=True spends nothing and makes no HTTPS call.
result = experiments.run("exp001_short-factual_baseline.yaml", dry_run=True)
print(result.ok, "->", result.spec.output_dir)

# Browse the whole catalog.
for s in experiments.list_experiments():
    print(s.experiment_id, "|", s.variable)
```

- **Input is the YAML; output is an `ExperimentResult`.** `run()` picks the right
  runner from the YAML and forwards `--dry-run` / `--allow-dirty` for you, then
  returns the exit code and where records landed.
- `describe()` and `list_experiments()` need only `pyyaml` — no credentials, no
  runner imports — so you can inspect the catalog for free.
- Zero-setup, network-free demos of the deterministic primitives the runners are
  built on (`select_bucket`, `reactive_decide`, the TPM/cost estimators) live in
  [`examples/pure_functions.py`](examples/pure_functions.py).

See [`examples/README.md`](examples/README.md) for the full interface reference.

---

## Family A — reasoning-effort sweeps (does thinking pay off?)

**The question:** on the *same* prompt and the *same* dataset, does turning the
GPT-5.2 `reasoning_effort` dial up buy enough quality to justify its extra
tokens? Each GPT-5.2 sweep is paired with a **GPT-4o baseline column** (a
non-reasoning model, no effort parameter) so the effort axis has a floor to
beat.

**Runner:** `scripts/run_benchmark.py`
**Held constant across every cell:** system prompt, user template, dataset,
`max_output_tokens = 4096`. GPT-5.2 rejects `temperature`/`top_p` (HTTP 400), so
those keys are absent; the GPT-4o baseline pins `temperature = 0.0`.

| Experiment | Benchmark | Model | Input (pinned) | Variable swept | Cells | Output |
| --- | --- | --- | --- | --- | --- | --- |
| `exp001_short-factual_baseline` | `01-short-factual` | gpt-5.2 | `benchmarks/01-short-factual/dataset.json` (N=20) + `prompts/` | `reasoning.effort` = none · low · medium · high · xhigh | 20×3×5 | `benchmarks/01-short-factual/runs/` → `analysis.md` → chart `benchmark-01` |
| `exp001_short-factual_baseline_gpt4o` | `01-short-factual` | gpt-4o | same input | — (baseline column, no reasoning param) | 20×3 | same tree |
| `exp002_benchmark02_gpt5_2` | `02-multi-step-reasoning` | gpt-5.2 | `benchmarks/02-multi-step-reasoning/dataset.json` (N=20) + `prompts/` | `reasoning.effort` = none · low · medium · high · xhigh | 20×3×5 | `benchmarks/02-multi-step-reasoning/runs/` → `analysis.md` → chart `benchmark-02` |
| `exp002_benchmark02_gpt4o` | `02-multi-step-reasoning` | gpt-4o | same input | — (baseline column) | 20×3 | same tree |
| `exp003_benchmark03_gpt5_2` | `03-tool-using-agent` | gpt-5.2 | `benchmarks/03-tool-using-agent/dataset.json` (N=20) + `prompts/` + `search_kb.json` (tool corpus) | `reasoning.effort` = none · low · medium · high · xhigh | 20×3×5 | `benchmarks/03-tool-using-agent/runs/` → `analysis.md` → chart `benchmark-03` |
| `exp003_benchmark03_gpt4o` | `03-tool-using-agent` | gpt-4o | same input | — (baseline column) | 20×3 | same tree |

*Cells* = `N samples × R=3 repeats × effort levels`. The GPT-4o baseline sends
no reasoning parameter, so it is a single column.

**What the output records.** Each `runs/*.json` is one `(sample, model, effort,
repeat)` cell and captures the full `response.usage` object — input, cached,
**reasoning**, and output tokens separately — plus latency and the response
text. Quality is scored offline into `judge_runs/`, and both are aggregated
into `analysis.json` / `analysis.md`. The dashboard-facing slices are promoted
to `results/public/chart-data/` (see the map below).

---

## Family B — operations simulations (cost / throughput / reliability levers)

**The question:** these do not measure model *quality*. They measure **operator
levers** — how spillover routing, prompt-cache-key bucketing, and admission-time
token reservation behave under load. Each experiment changes exactly one
operational axis, expressed either as a **sibling YAML pair** or as a `sweep:`.

| Experiment(s) | Benchmark | Runner | Input (pinned) | Variable | Output |
| --- | --- | --- | --- | --- | --- |
| `exp004_spillover_baseline_reactive` → `exp004_spillover_proactive` | `04-spillover-simulation` | `scripts/simulate_spillover.py` | `benchmarks/04-spillover-simulation/system_prompt_corpus.json` (`corpus_seed=4242`, ~30K-token assembled prompt) + `user_prompts.json` | `policy.type` = **reactive** vs **proactive** | `benchmarks/04-spillover-simulation/runs/` → `analysis.md` |
| `exp005_dual_spillover_reactive` → `exp005_dual_spillover_proactive` | `05-dual-spillover` | `scripts/measure_dual_spillover.py` | `benchmarks/05-dual-spillover/system_prompt_corpus.json` + `user_prompts.json` | `policy.type` = **reactive** vs **proactive** (two-endpoint / dual-pool) | `benchmarks/05-dual-spillover/runs/` |
| `exp006_cache_key_bucketing_inmemory` → `exp006_cache_key_bucketing_24h` | `06-cache-key-bucketing` | `scripts/measure_cache_key_bucketing.py` | `benchmarks/06-cache-key-bucketing/system_prompt_corpus.json` (~9.5K-token prompt) + `user_prompts.json` | `sweep.bucket_cardinality` = **1, 8** × `prompt_cache_retention` = **in_memory / 24h** | `benchmarks/06-cache-key-bucketing/runs/` → `analysis.md` |
| `exp007_max_output_tokens_sweep` | `07-max-output-tokens-reservation` | `scripts/measure_max_output_tokens_sweep.py` | reuses `benchmarks/04-spillover-simulation/` corpus + prompts, read-only (~2K-token assembled prompt) | `sweep.max_output_tokens` = **256 · 512 · 1024 · 2048 · 4096 · 8192 · 16384** | `benchmarks/07-max-output-tokens-reservation/runs/` → `analysis.md` |

**Reading the sibling pairs.** `exp004`/`exp005` express their variable as two
YAML files that differ only in `policy.type`; the `reactive` file is the parent
(baseline) and the `proactive` file is its child. `exp006` combines a `sweep`
(`bucket_cardinality`) with a two-file retention split. `exp007` is a single
YAML whose `sweep.max_output_tokens` is the only moving part; all prompt bytes
are pinned to benchmark 04 by SHA-256 so the admission-reservation effect is not
confounded by prompt drift.

> **Scope note.** `exp007` is a PAYG throttled-quota **proxy** for the
> admission-time reservation mechanism, not direct PTU evidence — see the
> benchmark's `analysis.md` for the exact caveat.

---

## Per-experiment reference (one card each)

Every card answers the same six questions: **what is it asking, what does the
runner do, what goes in, what is the one variable, what comes out, and how do
you read the result.** The `experiments.describe(...)` call prints the machine
version of the first five for any YAML; the *How to read it* line is the human
takeaway. Every card ends with a **zero-spend dry-run** you can paste now.

### `exp001` — short factual · `benchmarks/01-short-factual`

- **Intent (hypothesis).** On easy factual questions, quality is already near
  the ceiling, so turning `reasoning_effort` up should **not** pay — it should
  add reasoning tokens (and cost) without moving accuracy.
- **Task.** `run_benchmark` asks the same 20 questions, 3 repeats each, across
  effort `{none, low, medium, high, xhigh}` on gpt-5.2 (300 cells); the
  `_gpt4o` sibling adds a non-reasoning baseline column at `temperature=0.0`
  (60 cells).
- **Input.** `dataset.json` (N=20) + `prompts/{system,user_template}.md`. Held
  constant: `max_output_tokens=4096`; `temperature`/`top_p` omitted (gpt-5.2
  returns HTTP 400 for them).
- **Variable.** `reasoning.effort` — five levels.
- **Output.** `runs/*.json` (per-cell `usage` incl. **reasoning** tokens +
  latency) → `judge_runs/` (quality) → `analysis.{json,md}` → chart-data
  `{cost-curves-effort,token-composition,ptu-payg-crossover}/benchmark-01/`.
- **How to read it.** In `analysis.md` / the cost-curve chart, compare
  **cost-per-correct-answer** across effort. Flat quality with rising cost
  confirms the hypothesis: effort does not pay on short factual work.
- **Dry-run.** `experiments.run("exp001_short-factual_baseline.yaml", dry_run=True, extra_args=["--max-samples", "1"])`

### `exp002` — multi-step reasoning · `benchmarks/02-multi-step-reasoning`

- **Intent (hypothesis).** On genuinely multi-step problems, effort should
  **start earning its cost** — quality should climb with effort enough to beat
  the flat short-factual case at some tier.
- **Task.** Identical 20×3×5 design on gpt-5.2 (+ `_gpt4o` baseline) over the
  multi-step dataset.
- **Input.** `dataset.json` (N=20) + `prompts/`. `max_output_tokens=4096`.
- **Variable.** `reasoning.effort` — five levels.
- **Output.** Same chain as exp001 → chart-data `.../benchmark-02/`.
- **How to read it.** Look for a **rising** quality curve in `analysis.md` and
  the effort tier where cost-per-correct is minimized — that tier is where
  effort begins to pay.
- **Dry-run.** `experiments.run("exp002_benchmark02_gpt5_2.yaml", dry_run=True, extra_args=["--max-samples", "1"])`

### `exp003` — tool-using agent · `benchmarks/03-tool-using-agent`

- **Intent (hypothesis).** Agent tasks are latency-bound, so effort must be
  read on **two** axes: find the quality **ceiling** and the **latency** each
  effort tier costs to reach it.
- **Task.** Same 20×3×5 (+ `_gpt4o`) design, plus a tool knowledge base
  (`search_kb.json`) and tool schemas; the YAML carries an extra `agent` block.
- **Input.** `dataset.json` (N=20) + `prompts/` + `search_kb.json` (tool
  corpus). `max_output_tokens=4096`.
- **Variable.** `reasoning.effort` — five levels.
- **Output.** Same chain as exp001 → chart-data `.../benchmark-03/`.
- **How to read it.** Read quality **and** latency together in `analysis.md`;
  the ceiling is where quality plateaus while latency keeps climbing — past it,
  effort only buys delay.
- **Dry-run.** `experiments.run("exp003_benchmark03_gpt5_2.yaml", dry_run=True, extra_args=["--max-samples", "1"])`

### `exp004` — spillover simulation · `benchmarks/04-spillover-simulation`

- **Intent (hypothesis).** Which spillover-routing policy restores cache-hit
  ratio faster after throttling — **reactive** (act on a timeout/429) or
  **proactive** (ramp on rising p95 latency)? Phase 1 isolates the policy logic
  with a simulated router (no real network 429s).
- **Task.** `simulate_spillover` replays a ~30K-token ReAct workload
  (`corpus_seed=4242`) over a **22-minute** profile: 120 s warmup @0.3 tps →
  ramp 0.5→2.5 tps over 600 s → sustain. `max_output_tokens=1024`. The reactive
  and proactive YAMLs differ **only** in `policy.type` (reactive is the parent).
- **Input.** `system_prompt_corpus.json` + `user_prompts.json`.
- **Variable.** `policy.type` = reactive vs proactive.
- **Output.** `runs/*.jsonl` (+ `*.summary.json`) → `analysis.{json,md}`;
  recovery-curve PNGs in `results/spillover-recovery-curves/`.
- **How to read it.** Overlay the two recovery curves — the policy that returns
  cache-hit ratio to baseline in **fewer requests** after a spillover event
  wins.
- **Dry-run.** `experiments.run("exp004_spillover_baseline_reactive.yaml", dry_run=True)`

### `exp005` — dual-endpoint spillover · `benchmarks/05-dual-spillover`

- **Intent (hypothesis).** Does the reactive-vs-proactive result survive
  **real** throttling across two live pools — a low-TPM primary that emits
  genuine 429s and a high-TPM spillover pool?
- **Task.** `measure_dual_spillover` drives two real gpt-5.2 deployments
  (`primary`/`spillover` blocks) with the same policy pair and load profile;
  `corpus_seed=4242`, `max_output_tokens=1024`.
- **Input.** `system_prompt_corpus.json` + `user_prompts.json`.
- **Variable.** `policy.type` = reactive vs proactive (two-pool).
- **Output.** `runs/*.jsonl` (+ `*.summary.json`); per-endpoint, aggregate, and
  **real-429-timeline** PNGs in `results/dual-spillover-curves/`.
- **How to read it.** Confirm the Phase-1 curve shapes hold once real 429s
  appear; the real-429 timeline shows exactly when the primary throttled and
  how each policy responded.
- **Dry-run.** `experiments.run("exp005_dual_spillover_reactive.yaml", dry_run=True)`

### `exp006` — cache-key bucketing · `benchmarks/06-cache-key-bucketing`

- **Intent (hypothesis).** Is `prompt_cache_key` bucket **cardinality** a real
  capacity lever? Splitting one hot key into 8 buckets trades single-key
  affinity for spread, and **24h** retention should push the cache-overflow
  threshold out versus the `in_memory` default.
- **Task.** `measure_cache_key_bucketing` runs an async-scheduled sweep over
  `bucket_cardinality {1, 8}` on one unthrottled gpt-5.2 pool (500K TPM), once
  under `in_memory` retention and once under `24h`. ~9.5K-token corpus
  (seed 4242); per-request `max_output_tokens=512`, `effort=low`; sustain
  0.5 tps, 960 s cells.
- **Input.** `system_prompt_corpus.json` + `user_prompts.json`.
- **Variable.** `sweep.bucket_cardinality {1, 8}` × `prompt_cache_retention
  {in_memory, 24h}` — four cells.
- **Output.** `runs/*.jsonl` (+ `*.summary.json`) → `analysis.md`.
- **How to read it.** Compare steady-state **cache-hit ratio** and **TTFT p95**
  across the four cells; a higher-cardinality / longer-retention cell that holds
  its hit ratio is the lever paying off.
- **Dry-run.** `experiments.run("exp006_cache_key_bucketing_inmemory.yaml", dry_run=True)`

### `exp007` — max_output_tokens reservation · `benchmarks/07-max-output-tokens-reservation`

- **Intent (hypothesis).** Does admission-time reservation scale with the
  **requested** `max_output_tokens` even when the actual output is tiny? If so,
  an oversized cap silently eats throughput budget — a PAYG **proxy** for PTU
  admission reservation.
- **Task.** `measure_max_output_tokens_sweep` runs a log2 sweep of
  `max_output_tokens {256 … 16384}` on a deliberately throttled low-TPM
  deployment (60K TPM / 600 RPM), reusing benchmark 04's corpus **read-only**
  (SHA-256 pinned, ~2K-token prompt), `effort=low`.
- **Input.** `benchmarks/04-spillover-simulation/system_prompt_corpus.json` +
  `user_prompts.json` (reused read-only, hash-pinned).
- **Variable.** `sweep.max_output_tokens` — seven values.
- **Output.** `runs/*.jsonl` (+ `*.summary.json`) → `analysis.md`.
- **How to read it.** With actual output held roughly constant, watch when the
  **first 429** appears / effective TPM headroom shrinks as the cap rises.
  Earlier throttling at higher caps means reservation scales with the requested
  max, not the produced output.
- **Dry-run.** `experiments.run("exp007_max_output_tokens_sweep.yaml", dry_run=True)`

> Prefer to browse this programmatically? `python experiments/examples/describe_all.py`
> prints the intent · task · input · variable · output block for **all 20**
> experiment files (including the `_gpt4o` and smoke variants) with no
> credentials and no network.

---

## Smoke experiments (pipeline pre-flight, not evidence)

`exp_smoke_01`, `exp_smoke_02`, `exp_smoke_03` (each with a `_gpt4o` sibling,
plus `exp_smoke_01_warmprobe`) are **tiny** runs — `N=2`, `R=1`, effort
`{low, high}` — used to confirm that authentication, the runner, budget guards,
and the aggregation pipeline all work end-to-end before spending on a full
sweep. They are deliberately too small for any scientific claim and are excluded
from the published analyses.

---

## From a blog article to its experiment

Came from the [evidence blog](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/articles/)?
Each article opens with a question, names the charts it used, and ends with an
operator takeaway. This table points from every article to the **exact evidence**
behind it in this repo. Links are English; each article has an in-page language
switcher (한국어 · 日本語 · 简体中文 · हिन्दी).

| Blog article | Evidence in this repo | Experiment(s) here |
| --- | --- | --- |
| [Overview — same token price, different bill](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/articles/when-reasoning-pays-off/) | `results/summary.md` (cross-benchmark synthesis) | all of Family A |
| **Evidence topics** | | |
| [Short factual work](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/articles/when-reasoning-pays-off/topics/short-factual-work/) | `benchmarks/01-short-factual/` | `exp001_short-factual_baseline` (+ `_gpt4o`) |
| [Invisible reasoning tokens](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/articles/when-reasoning-pays-off/topics/invisible-reasoning-tokens/) | `results/public/chart-data/token-composition/` | `exp001`–`exp003` (the reasoning-token split each run captures) |
| [Multi-step work](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/articles/when-reasoning-pays-off/topics/multi-step-work/) | `benchmarks/02-multi-step-reasoning/` | `exp002_benchmark02_gpt5_2` (+ `_gpt4o`) |
| [Tool-agent ceiling checks](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/articles/when-reasoning-pays-off/topics/tool-agent-ceiling-checks/) | `benchmarks/03-tool-using-agent/` | `exp003_benchmark03_gpt5_2` (+ `_gpt4o`) |
| [Agentic-loop & budget governance](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/articles/when-reasoning-pays-off/topics/agentic-loop-budget-governance/) | `docs/18-agentic-loop-budget-governance.md` (operations pattern L6) | — no new measurement |
| [PTU / PAYG planning](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/articles/when-reasoning-pays-off/topics/ptu-payg-planning/) | `results/public/chart-data/ptu-payg-crossover/` + `docs/13-ptu-vs-payg-decision-runbook.md` | modeled from `exp001`–`exp003` (planning guidance, not direct capacity evidence) |
| **Bridge** | | |
| [From measurement to production](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/articles/when-reasoning-pays-off/topics/bridge-from-measurement-to-production/) | narrative only | — no new measurement |
| **Operations** | | |
| [429 recovery via `retry-after-ms`](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/articles/ptu-retry-after-recovery/) | `benchmarks/08-retry-after-characterization/` | — characterization-based (no sweep YAML) |
| [`prompt_cache_key` bucketing](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/articles/prompt-cache-key-bucketing/) | `benchmarks/06-cache-key-bucketing/` | `exp006_cache_key_bucketing_inmemory` → `_24h` |
| [Explicit cache retention](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/articles/prompt-cache-retention/) | `benchmarks/06-cache-key-bucketing/` (the `retention` axis) | `exp006_cache_key_bucketing_24h` |
| [Reasoning-migration sizing](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/articles/reasoning-migration-sizing/) | `scripts/ptu_sizing.py` + `docs/13-ptu-vs-payg-decision-runbook.md` | — modeled (price-only, no sweep) |

The six **evidence topics** and the **token-composition** / **crossover**
charts are the parts backed by the sweeps in *this* directory (`exp001`–`exp003`,
`exp006`). The **operations** articles that have no `exp*.yaml` here
(`retry-after`, `migration-sizing`) are backed by their named benchmark or
sizing module instead — the row's "Evidence in this repo" column is the
authoritative source in every case.

---

## Anatomy of one experiment YAML

`_template.yaml` is the annotated canonical form. The fields that tell you *how
the load was applied*:

| Field | Meaning |
| --- | --- |
| `experiment_id` / `parent_experiment` | Identity and lineage. A child differs from its parent by **exactly one** variable. |
| `benchmark` | Which `benchmarks/<name>/` supplies the pinned input. |
| `model.family` | `gpt-5.2` (the effort axis applies) or `gpt-4o` (baseline, no reasoning param). |
| `dataset_size` (N) / `repeats` (R) | Sample count and repeats per cell. |
| `sweep` | The independent variable — the one axis that moves. |
| `call_params` / `request_template` | The pinned call shape (`max_output_tokens`, cache keys, retention). |
| `capture` | What each raw JSON records: token categories, latency, response text. |
| `budget` | Pre-run cost estimate plus a mid-run `hard_ceiling_usd` that halts the batch on overrun. |
| `dataset_sha256` / `system_prompt_sha256` | Fingerprints of the frozen input. A mismatch aborts the run rather than measure a drifted prompt. |

---

## Input → output map (full traceability)

```
benchmarks/<name>/
├── dataset.json            # Family A input: the N task samples
├── prompts/                # Family A input: system.md + user_template.md (byte-identical across cells)
├── system_prompt_corpus.json + user_prompts.json   # Family B input: assembled-prompt corpus + user turns
├── runs/                   # OUTPUT: one append-only JSON per (sample, model, effort, repeat) cell
├── judge_runs/             # OUTPUT: per-response quality judgments (Family A)
├── analysis.json / .md     # OUTPUT: offline aggregate + written verdict
results/public/chart-data/  # OUTPUT: sanitized slices behind the live dashboard
├── cost-curves-effort/benchmark-0{1,2,3}/{cost-per-request,latency,quality,throughput-gain}.json
├── token-composition/benchmark-0{1,2,3}/tokens.json
└── ptu-payg-crossover/benchmark-0{1,2,3}/crossover.json
```

`benchmark-01` ← `exp001`, `benchmark-02` ← `exp002`, `benchmark-03` ← `exp003`.

---

## Reproducing a run

```bash
# One-time setup (see the top-level README for the full contract)
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
az login                       # Entra ID auth; no API keys
cp .env.example .env           # set your Azure OpenAI endpoint + deployment names

# Family A — reasoning-effort sweep
python -m scripts.run_benchmark --experiment experiments/exp001_short-factual_baseline.yaml
#   add --dry-run for a zero-network dress rehearsal, --max-samples N to cap N

# Family B — operations simulations
python -m scripts.simulate_spillover --experiment experiments/exp004_spillover_baseline_reactive.yaml
python -m scripts.measure_dual_spillover --experiment experiments/exp005_dual_spillover_reactive.yaml
python -m scripts.measure_cache_key_bucketing --experiment experiments/exp006_cache_key_bucketing_inmemory.yaml
python -m scripts.measure_max_output_tokens_sweep --experiment experiments/exp007_max_output_tokens_sweep.yaml
```

Or skip the runner names entirely and let the Python interface pick the right
one (see **The easy way** above):

```bash
python experiments/examples/run_any_experiment.py exp004_spillover_baseline_reactive.yaml   # dry-run
python experiments/examples/describe_all.py                                                 # catalog, no setup
```

Running a live sweep requires Azure OpenAI access with a GPT-5.2 deployment.
You do **not** need Azure credentials to inspect the frozen inputs, read the
committed `runs/` and `analysis.md`, or run the unit-test suite
(`pytest -q -m "not adaptive_calibration" batch-runner/tests/`).

> **A note on effort labels.** Each YAML's `sweep.effort` lists the exact levels
> sent to the deployment. GPT-5.2 rejects `minimal` with HTTP 400, so `none` is
> the lowest tier these runs actually request; the published `analysis.md` and
> chart data label the lowest tier accordingly. Always treat each experiment's
> `sweep.effort` as the source of truth for what was sent.
