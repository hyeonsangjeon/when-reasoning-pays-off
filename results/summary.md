# When Reasoning Pays Off — Cross-Benchmark Summary

## 1. The question

**For a new production task, which GPT model × reasoning effort is the
defensible default, expressed in both PAYG dollar terms and PTU
throughput terms?**

## 2. The three benchmarks

| Benchmark | Task shape | N | Why it exists |
| --- | --- | ---: | --- |
| [01 — short-factual](../benchmarks/01-short-factual/analysis.md) | **Null case** — reasoning unnecessary; single-shot factual / classification | 20 | Floor anchor of the cost-vs-quality trade |
| [02 — multi-step-reasoning](../benchmarks/02-multi-step-reasoning/analysis.md) | **Ceiling case** — multi-step inference; chained logic, math, code-trace | 20 | Ceiling anchor: where reasoning theoretically wins |
| [03 — tool-using-agent](../benchmarks/03-tool-using-agent/analysis.md) | **Mixed case** — 30 % no-tool / 40 % one-tool / 30 % multi-tool agent | 20 | Realistic production mix |

Every cell uses **R = 3 repeats** across **N = 20 samples**, with
byte-identical prompts and the canonical effort sweep
`{none, low, medium, high, xhigh}` for gpt-5.2 plus a gpt-4o baseline
column. Total measurement surface: **1080 cells** across the three
benchmarks. Every USD figure on this page is sourced from a per-benchmark
`analysis.md` — this document **re-cites**, never re-derives, the
underlying numbers.

## 3. Headline finding

The optimal **(model, effort)** choice depends on **task shape** and on
**consumption model** — three benchmarks, two consumption models, six
recommendations. The single biggest pattern: the right choice is **never
`effort=high` or `effort=xhigh`**. Above mid-range, every additional
effort tier adds cost / loses throughput without lifting quality on the
workloads measured here.

- **PAYG (cost per correct answer):**
  - **Null case (benchmark 01):** gpt-5.2 `none` at **$0.618 per 1,000
    correct** ($0.000618/correct) beats the gpt-4o baseline ($0.739 per
    1,000 correct) by 16 %
    ([cited from `benchmarks/01-short-factual/analysis.md` §5](../benchmarks/01-short-factual/analysis.md)).
  - **Ceiling case (benchmark 02):** gpt-5.2 `none` at **$0.000618/correct**
    beats the gpt-4o baseline ($0.001064) by 42 %
    ([cited from `benchmarks/02-multi-step-reasoning/analysis.md` §5](../benchmarks/02-multi-step-reasoning/analysis.md)).
  - **Mixed case (benchmark 03):** gpt-5.2 `low` at **$0.002773/correct**
    beats the gpt-4o baseline ($0.003651) by 24 %
    ([cited from `benchmarks/03-tool-using-agent/analysis.md` §5](../benchmarks/03-tool-using-agent/analysis.md)).

- **PTU (correct answers per minute at fixed capacity):**
  - **Null case (benchmark 01):** gpt-5.2 `none` wins on quality-adjusted
    throughput — near-parity token footprint (0.989 × throughput) times a
    higher pass-rate (95 % vs 90 %) yields **+4.4 %** correct-answers-per-
    minute vs the gpt-4o baseline; raw throughput is a wash (every gpt-5.2
    tier 0.98–0.99 ×)
    ([cited from `benchmarks/01-short-factual/analysis.md` §6](../benchmarks/01-short-factual/analysis.md)).
  - **Ceiling case (benchmark 02):** gpt-5.2 `none` wins at **0.994 ×
    throughput × 100 % pass = +32.5 %** correct-answers-per-minute
    relative to gpt-4o baseline
    ([cited from `benchmarks/02-multi-step-reasoning/analysis.md` §6](../benchmarks/02-multi-step-reasoning/analysis.md)).
  - **Mixed case (benchmark 03):** gpt-5.2 `low` wins at **0.939 ×
    throughput × 100.0 % pass = +4.3 %** correct-answers-per-minute
    relative to the gpt-4o baseline (1.0 × 90.0 %);
    [cited from `benchmarks/03-tool-using-agent/analysis.md` §6](../benchmarks/03-tool-using-agent/analysis.md).

- **Quality saturation point:**
  - Benchmark 01 saturates at `none` (judge score ~1.95 on a 0–2 scale,
    flat across every effort tier within one SD;
    [`benchmarks/01-short-factual/analysis.md` §7](../benchmarks/01-short-factual/analysis.md)).
  - Benchmark 02 saturates at `none` for gpt-5.2 (100 % pass-rate already;
    [`benchmarks/02-multi-step-reasoning/analysis.md` §7](../benchmarks/02-multi-step-reasoning/analysis.md)).
  - Benchmark 03 saturates at `low` (100 % pass-rate; flat through
    `xhigh`;
    [`benchmarks/03-tool-using-agent/analysis.md` §7](../benchmarks/03-tool-using-agent/analysis.md)).

## 4. Per-benchmark summary cards

### Benchmark 01 — short-factual (null case)

Reasoning is unnecessary; the question is whether GPT-5.2 at any effort
saves PAYG dollars or PTU throughput over gpt-4o.

- **PAYG winner:** gpt-5.2 `none` ($0.587 per 1,000 req vs gpt-4o $0.665;
  $0.618 vs $0.739 per 1,000 correct;
  [01 §5](../benchmarks/01-short-factual/analysis.md)).
- **PTU winner:** gpt-5.2 `none` — near-parity throughput (0.989 ×) times a
  higher pass-rate (95 % vs 90 %) nets +4.4 % correct-answers-per-minute
  ([01 §6](../benchmarks/01-short-factual/analysis.md)).
- **Mechanism:** gpt-5.2's input rate ($1.75/M tokens) undercuts gpt-4o's
  ($2.50/M), and at `none` the reasoning surface is ≈ 0, so the saving
  flows straight through with no token penalty
  ([pricing/azure-openai-payg-2026-05.yaml](../pricing/azure-openai-payg-2026-05.yaml)).
- **Charts:**
  [cost-per-request](../results/cost-curves/benchmark-01-cost-per-request.png),
  [throughput-gain](../results/cost-curves/benchmark-01-throughput-gain.png),
  [quality](../results/cost-curves/benchmark-01-quality.png).

(Full numbers + provenance:
[`benchmarks/01-short-factual/analysis.md`](../benchmarks/01-short-factual/analysis.md))

### Benchmark 02 — multi-step reasoning (ceiling case)

Every sample requires at least two inferential steps. The pre-registered
prediction was that high reasoning effort pays off; the measurement
**falsifies** that expectation.

- **PAYG winner:** gpt-5.2 `none` ($0.000618/correct vs gpt-4o $0.001064
  — a 42 % saving;
  [02 §5](../benchmarks/02-multi-step-reasoning/analysis.md)).
- **PTU winner:** gpt-5.2 `none` (+32.5 % correct-answers-per-minute at
  fixed PTU vs gpt-4o baseline).
- **Mechanism:** gpt-5.2 *baseline* (effort=none) already gets 100 % pass
  on this dataset; the effort knob spends reasoning tokens without
  lifting quality. The headline win is the **model upgrade**, not the
  effort dial.
- **Caveat:** the observed gpt-4o pass-rate (75 %) was above the
  pre-registered 30–60 % range — the dataset turned out easier for
  gpt-4o than expected. The headline direction (gpt-5.2 `none` Pareto-
  dominates) is unchanged; the dollar-saving magnitude is attenuated.
  Documented in
  [`benchmarks/02-multi-step-reasoning/analysis.md` §10](../benchmarks/02-multi-step-reasoning/analysis.md).
- **Charts:**
  [cost-per-request](../results/cost-curves/benchmark-02-cost-per-request.png),
  [throughput-gain](../results/cost-curves/benchmark-02-throughput-gain.png),
  [quality](../results/cost-curves/benchmark-02-quality.png).

### Benchmark 03 — tool-using agent (mixed case)

20 samples split 6 / 8 / 6 across no-tool / one-tool / multi-tool. Per-
subtype shape is the headline.

- **Per-subtype pass-rate** (from
  [`benchmarks/03-tool-using-agent/analysis.md` §7](../benchmarks/03-tool-using-agent/analysis.md)):

  | model         | effort   | no-tool | one-tool | multi-tool |
  | ------------- | -------- | ------: | -------: | ---------: |
  | gpt-4o        | —        |  83.3 % |  100.0 % |     83.3 % |
  | gpt-5.2       | none     | 100.0 % |  100.0 % |     94.4 % |
  | gpt-5.2       | low      | 100.0 % |  100.0 % |    100.0 % |
  | gpt-5.2       | medium   | 100.0 % |  100.0 % |     94.4 % |
  | gpt-5.2       | high     | 100.0 % |  100.0 % |     94.4 % |
  | gpt-5.2       | xhigh    | 100.0 % |  100.0 % |    100.0 % |

- **PAYG winner:** **gpt-5.2 `low`** at **$0.002773 / correct**
  ([03 §5](../benchmarks/03-tool-using-agent/analysis.md)) — 24 %
  cheaper than gpt-4o ($0.003651). The mechanism: gpt-5.2's input rate
  ($1.75/M) undercuts gpt-4o's ($2.50/M), and **input tokens dominate
  the tool-loop cell cost** (the system prompt + tool defs get re-sent
  on every iteration). `low` adds ~4 reasoning tokens — enough to plan
  multi-tool chains, not enough to inflate the bill.
- **PTU winner:** **gpt-5.2 `low`** at **+4.3 %
  correct-answers-per-minute** relative to the gpt-4o baseline (0.939 ×
  tokens × 100.0 % pass);
  [cited from `benchmarks/03-tool-using-agent/analysis.md` §6](../benchmarks/03-tool-using-agent/analysis.md).
  Above `low` the throughput penalty (-8.5 % at `medium`, -10.8 % at
  `xhigh`) reverses the gain.
- **Surprise:** the per-subtype gpt-4o no-tool pass-rate is only **83.3
  %** — the model over-uses the calculator on prompts that explicitly
  say "answer from general knowledge." Every gpt-5.2 cell holds 100 %
  on no-tool. **gpt-4o's saturated single-shot benchmark behavior does
  not transfer to a tool-using context.**
- **Charts:**
  [cost-per-request](../results/cost-curves/benchmark-03-cost-per-request.png),
  [throughput-gain](../results/cost-curves/benchmark-03-throughput-gain.png),
  [quality](../results/cost-curves/benchmark-03-quality.png).

## 5. Decision boundary

A reader who knows their workload's shape and consumption model can read
the recommendation off this table directly. Every cell cites a
per-benchmark analysis.

| Task shape | PAYG default | PTU default |
| --- | --- | --- |
| **Short-factual / null** (benchmark 01) | **gpt-5.2 `none`** — saves 12 % USD/req (16 % per correct) vs gpt-4o ([01 §5](../benchmarks/01-short-factual/analysis.md)) | **gpt-5.2 `none`** — +4.4 % correct-answers-per-minute (near-parity throughput × higher pass-rate) ([01 §6](../benchmarks/01-short-factual/analysis.md)) |
| **Multi-step reasoning** (benchmark 02) | **gpt-5.2 `none`** — 42 % cost-per-correct saving ($0.000618 vs $0.001064; [02 §5](../benchmarks/02-multi-step-reasoning/analysis.md)) | **gpt-5.2 `none`** — +32.5 % correct-answers-per-minute relative to gpt-4o baseline ([02 §6](../benchmarks/02-multi-step-reasoning/analysis.md)) |
| **Tool-using, no/one tool** (benchmark 03 subset) | **gpt-5.2 `low`** or **`none`** — both at 100.0 % no-tool / one-tool pass and ~$0.00277 / correct, beating gpt-4o ($0.00365 / correct, 83 % no-tool) ([03 §5, §7](../benchmarks/03-tool-using-agent/analysis.md)) | **gpt-5.2 `low`** — +4.3 % correct-answers-per-minute vs gpt-4o ([03 §6](../benchmarks/03-tool-using-agent/analysis.md)) |
| **Tool-using, multi-tool** (benchmark 03 subset) | **gpt-5.2 `low`** — 100.0 % multi-tool pass at $0.002773 / correct; gpt-4o is 83.3 % at $0.003651 / correct ([03 §5, §7](../benchmarks/03-tool-using-agent/analysis.md)) | **gpt-5.2 `low`** — same 0.939 × throughput × 100.0 % pass beats gpt-4o (1.0 × 83.3 %) ([03 §6](../benchmarks/03-tool-using-agent/analysis.md)) |

The "effort > low" column is **empty on every benchmark**: `high` and
`xhigh` consistently add cost without lifting quality on the workloads
measured here. On benchmark 01 the effort dial does nothing at all —
quality, cost, and reasoning tokens are all flat from `none` upward — so
`none` wins both the PAYG and PTU lenses.

## 6. Caveats

- **Single-tenant, single-snapshot pricing.** Every USD figure cites
  `pricing/azure-openai-payg-2026-05.yaml`
  ([Azure source](https://azure.microsoft.com/en-us/pricing/details/azure-openai/),
  accessed 2026-05-19). When Azure publishes a rate change, the snapshot
  must be refreshed and the analyses re-run. The repo's append-only
  pricing-snapshot pattern preserves the historical record.
- **N = 20 per benchmark; no confidence intervals.** Methodology §8
  forbids CI / SEM reporting on this sample size. Effect sizes are
  reported as mean ± sample std (`statistics.stdev`, ddof=1). Direction-
  of-effect findings are robust; absolute magnitudes carry the obvious
  small-N caveat.
- **Judge subjectivity.** The LLM-as-judge rubric (`scripts/run_judge.py`)
  uses gpt-4o as the neutral judge. Judge drift across pricing snapshots
  is a known risk; the judge prompt's SHA-256 is recorded on every judge
  JSON for rubric-drift detection.
- **No live-load PTU measurement.** The PTU throughput-gain figures are
  derived from token counts only (no live PTU load test). A real
  production PTU deployment may see additional throughput effects from
  rate-limit shaping, queueing, and parallel batch composition not
  captured here.
- **Tool-using agent uses mocked search.** Benchmark 03's `web_search`
  tool is a canned KB lookup, not live web search. Live web search would
  introduce uncontrolled variance and is out of scope. The headline
  finding (multi-tool reliability improves with a small reasoning
  surface; effort > low wastes tokens) is robust to this simplification.
- **All three benchmarks are real Foundry v1 cohorts.** Every raw JSON
  under `benchmarks/0[123]-*/runs/` and `judge_runs/` came from a live
  `responses.create()` call against
  `https://<resource>.services.ai.azure.com/api/projects/<project>/openai/v1/responses`
  with Entra ID auth. No `"fixture": true` sentinel exists in any
  headline cohort.

## 7. How to reproduce

```bash
# Aggregate every benchmark into a deterministic analysis.json:
python -m scripts.analyze_tokens --benchmark 01-short-factual --experiment-prefix exp001_short-factual_baseline --judge-dir benchmarks/01-short-factual/judge_runs_real
python -m scripts.analyze_tokens --benchmark 02-multi-step-reasoning --experiment-prefix exp002
python -m scripts.analyze_tokens --benchmark 03-tool-using-agent --experiment-prefix exp003

# Regenerate every chart pair:
python -m scripts.plot_results --benchmark 01-short-factual
python -m scripts.plot_results --benchmark 02-multi-step-reasoning
python -m scripts.plot_results --benchmark 03-tool-using-agent
```

Outputs land under `results/cost-curves/` (PAYG cost-per-request, PTU
throughput-gain, latency, quality charts) and `results/token-composition/`
(stacked token-composition charts). Every CSV next to a PNG is the source
of truth; the PNGs are derived.

For details on the decision-rule translation (the "given my task, which
model × effort should I pick?" question), see
[`docs/04-decision-framework.md`](../docs/04-decision-framework.md).
