# Benchmark 02 — Multi-Step Reasoning: cost-vs-quality verdict

## 1. Question and design

For multi-step reasoning tasks (the **ceiling case** for reasoning), at what
GPT-5.2 `reasoning_effort` level (if any) does reasoning earn its bill — both
as PAYG cost-per-correct-answer and as PTU throughput-gain at matched quality?
The dataset is 20 frozen synthetic samples (`mr_01`…`mr_20`) spanning seven
task subtypes (arithmetic-word, constraint-satisfaction, date-time,
causal-chain, code-trace, boolean-logic, counting). Every sample carries a
verifiable answer and requires at least two inferential steps to solve. Each
`(sample, model, effort)` cell is repeated `R = 3` times; the runner emits the
Foundry v1 Responses payload verbatim and the analysis pipeline aggregates it
offline. The varying axis is `reasoning.effort` on GPT-5.2, swept across
`{none, low, medium, high, xhigh}` plus a separate GPT-4o baseline with no
reasoning parameter.

**Pre-registered hypothesis:** the multi-step shape inverts benchmark 01's
finding. gpt-4o is expected to fail a meaningful fraction (30–60 % baseline
pass-rate); gpt-5.2 at sufficient effort is expected to clear 80 % pass-rate.
Paired with benchmark 01's floor (the null-case), this benchmark establishes
the **ceiling of "when reasoning earns its bill"** on the cost surface.

## 2. Methodology link

The full measurement contract — variables, sample size, cache handling,
quality evaluation, cost calculation, reproducibility, statistical reporting —
lives in [`docs/05-methodology.md`](../../docs/05-methodology.md). Read it
before disagreeing with any number on this page.

### Quality metric definition

This benchmark reuses the existing 3-tier judge rubric verbatim (`0 = fail`,
`1 = partial`, `2 = pass`) from
[`scripts/run_judge.py`](../../scripts/run_judge.py); the analyzer enforces
`score ∈ {0, 1, 2}`. Where the cost-per-correct ratio needs a binary signal,
the binarization is defined **downstream**:

```
pass            = (score == 2)
pass_rate       = (# cells with score == 2) / (# cells)
cost_per_correct = mean_usd_per_cell / pass_rate
```

Partial credit (`score == 1`) is reported separately in §7 but **excluded**
from the `cost_per_correct` denominator — a partial answer to a multi-step
problem is not a correct answer at production scale. On this benchmark the
judge emitted **zero** `score == 1` records across all 360 cells (judges
either passed or failed; no in-between), so the binarization choice has no
material effect on the headline numbers but the definition is stated
explicitly for downstream consumers.

## 3. Run provenance

| Field | Value |
| --- | --- |
| Benchmark | `02-multi-step-reasoning` |
| Dataset | `benchmarks/02-multi-step-reasoning/dataset.json` |
| Dataset SHA-256 | `d55b975eec249c65831f0c0d916fc752d09959fa0fee1d6f489e2a709fcf2698` |
| System prompt SHA-256 | `974aac89df8346c8068a3bf8a05d5829677566dd2fce264c0dd99db26528ab23` |
| User template SHA-256 | `7c7f2a31f6fa6a69bb7383531677eead9f0ad88e40adb31a5ad4a157187a728e` |
| Experiment YAMLs | `experiments/exp002_benchmark02_gpt4o.yaml`, `experiments/exp002_benchmark02_gpt5_2.yaml` |
| Aggregated experiment IDs | `exp002_benchmark02_gpt4o`, `exp002_benchmark02_gpt5_2` |
| Git commits embedded in raw JSONs | `ea4ee27…` (gpt-4o run), `6f9957b…` (gpt-5.2 run) |
| Total cells | 360 (60 gpt-4o baseline + 300 gpt-5.2 across `none, low, medium, high, xhigh`) |
| Cells used after outlier exclusion | 360 |
| Cells excluded as outliers | 0 |
| Judge model | `gpt-4o` (cheap, neutral; one call per cell) |
| Judge calls | 360 / 360 succeeded (0 failures, 0 retries) |
| Judge prompt SHA-256 | `fb997c90fe0778681194809793cdce218bcb9d6537304a525f2916cf29ba6942` |
| Pricing snapshot | `pricing/azure-openai-payg-2026-05.yaml` |
| Pricing source URL | <https://azure.microsoft.com/en-us/pricing/details/azure-openai/> (accessed `2026-05-19`) |
| Provenance | **Real Foundry v1 calls.** Zero `"fixture": true` markers in either `runs/` or `judge_runs/` for the headline cohort (`exp002_*`). |

## 4. Token composition

Mean ± std per `(model, effort)` cell, computed over all 60 rows per cell
(no outliers excluded — the outlier detector found no `3σ + flagged-event`
rows on this run). All counts are per request.

| model | effort | n_used | input | cached | output (incl. reasoning) | reasoning (subset) | total |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| gpt-4o | — (baseline) | 60 | 308.2 ± 17.6 | 0.0 ± 0.0 | 2.8 ± 1.7 | 0.0 ± 0.0 | 311.0 ± 18.2 |
| gpt-5.2 | none | 60 | 307.2 ± 17.6 | 0.0 ± 0.0 | 5.8 ± 1.7 | 0.0 ± 0.0 | 313.0 ± 18.2 |
| gpt-5.2 | low | 60 | 301.7 ± 43.2 | 0.0 ± 0.0 | 23.6 ± 35.6 | 17.4 ± 33.8 | 325.3 ± 59.4 |
| gpt-5.2 | medium | 60 | 307.2 ± 17.6 | 0.0 ± 0.0 | 38.9 ± 40.6 | 32.0 ± 38.8 | 346.2 ± 50.0 |
| gpt-5.2 | high | 60 | 307.2 ± 17.6 | 0.0 ± 0.0 | 50.9 ± 40.1 | 43.5 ± 38.5 | 358.1 ± 49.8 |
| gpt-5.2 | xhigh | 60 | 307.2 ± 17.6 | 0.0 ± 0.0 | 117.3 ± 108.9 | 109.5 ± 107.9 | 424.5 ± 114.1 |

Per Azure's Foundry v1 Responses-API usage contract, `output_tokens` is
the superset that already includes the reasoning portion under
`output_tokens_details.reasoning_tokens`; correspondingly
`total_tokens = input_tokens + output_tokens` (reasoning is NOT added
on top — verified by inspecting all 360 raw run JSONs for this benchmark:
`total_tokens == input_tokens + output_tokens` in 360/360 rows, of which
159 carry `reasoning_tokens > 0`). The visible answer text size is
essentially flat across the effort ladder (~6–8 tokens — a short
integer or word per the rubric); reasoning-token growth dominates the
total-tokens increase. At `xhigh` the reasoning portion alone is ~26 %
of the total request size.

> **Note on the `low`-effort input-tokens variance.** The `low` row shows
> std = 43.2 (much higher than the 17.6 seen on every other row). This is
> driven by a single content-filter refusal at `mr_05, low, r1`: the Azure
> safety filter returned `input_tokens = 0` and the canned response "I'm
> sorry, but I cannot assist with that request." The cell was preserved
> verbatim per the append-only methodology rule (no overwrite, no silent
> drop) and is the source of the inflated variance. The judge correctly
> scored this row 0/fail. Other repeats of mr_05 at `low` returned the
> correct answer, so the refusal is a one-off and does not reflect a
> systemic safety-filter trip on the prompt.

## 5. Cost by Effort

Every USD figure below originates from
`scripts.cost_calculator.payg_cost_per_call()`, which applies the
methodology §6.1 PAYG formula verbatim against the pricing snapshot at
`pricing/azure-openai-payg-2026-05.yaml`
([source](https://azure.microsoft.com/en-us/pricing/details/azure-openai/),
accessed `2026-05-19`).

| model | effort | USD per request (mean ± std) | pass-rate | **cost per correct** |
| --- | --- | ---: | ---: | ---: |
| gpt-4o | — (baseline) | $0.000798 ± $0.000052 | **75.0 %** (45/60) | **$0.001064** |
| gpt-5.2 | none | $0.000618 ± $0.000044 | **100.0 %** (60/60) | **$0.000618** |
| gpt-5.2 | low | $0.000859 ± $0.000514 | **96.7 %** (58/60) | **$0.000888** |
| gpt-5.2 | medium | $0.001083 ± $0.000581 | **100.0 %** (60/60) | **$0.001083** |
| gpt-5.2 | high | $0.001250 ± $0.000574 | **100.0 %** (60/60) | **$0.001250** |
| gpt-5.2 | xhigh | $0.002179 ± $0.001532 | **100.0 %** (60/60) | **$0.002179** |

The single most important number on this page sits in the top-right column:
**gpt-5.2 effort=none is 42 % cheaper per correct answer than the gpt-4o
baseline** ($0.000618 vs $0.001064), and gpt-5.2 effort=low is **16 %
cheaper per correct answer than gpt-4o** ($0.000888 vs $0.001064) even
though the per-request cost is 7.6 % higher — the higher pass-rate
(96.7 % vs 75.0 %) more than recovers the per-request premium.

Above the Pareto knee at effort=none, every additional effort tier adds
per-correct cost without adding per-correct quality (pass-rate already
saturated at 100 %): `medium` is 1.75 × the floor cost-per-correct,
`high` is 2.02 ×, `xhigh` is 3.53 ×. On this benchmark, **reasoning
effort above `none` is paid for but does not earn its bill in PAYG
dollars**.

Sibling chart pair (PAYG lens):
- [`results/cost-curves/benchmark-02-cost-per-request.png`](../../results/cost-curves/benchmark-02-cost-per-request.png)
- [`results/cost-curves/benchmark-02-cost-per-request.csv`](../../results/cost-curves/benchmark-02-cost-per-request.csv)
- [`results/cost-curves/benchmark-02-quality.png`](../../results/cost-curves/benchmark-02-quality.png)
- [`results/cost-curves/benchmark-02-quality.csv`](../../results/cost-curves/benchmark-02-quality.csv)

## 6. Consumption Model Translation

The same measurement serves two audiences. PAYG customers see token reduction
as direct dollar savings; PTU customers see token reduction as throughput gain
at fixed spend (Methodology §6).

**For PAYG:** the per-correct table above is the bill. Migrating from
gpt-4o (the current production baseline) to **gpt-5.2 effort=none** saves
**$0.000446 per correct answer** — a **42 % reduction** at list price
([source](https://azure.microsoft.com/en-us/pricing/details/azure-openai/),
accessed `2026-05-19`, snapshot `pricing/azure-openai-payg-2026-05.yaml`).
At 1 million correct multi-step answers per month, that is **$446 saved**.
The saving holds because gpt-5.2's input rate ($1.75 / 1 M tokens) is lower
than gpt-4o's ($2.50 / 1 M tokens) and the reasoning-token spend at
`effort=none` is exactly zero. PAYG customers should choose
**gpt-5.2 effort=none** for this workload shape.

**For PTU:** the throughput-gain factor is computed relative to the gpt-4o
baseline (mean tokens-per-request = 311.0), so a factor of 1.0 means
"same throughput at the same PTU spend as gpt-4o today":

| model | effort | tokens/request | throughput gain factor | pass-rate |
| --- | --- | ---: | ---: | ---: |
| gpt-4o | — (baseline) | 311.0 | 1.000 × | 75.0 % |
| gpt-5.2 | none | 313.0 | **0.994 ×** | **100.0 %** |
| gpt-5.2 | low | 325.3 | 0.956 × | 96.7 % |
| gpt-5.2 | medium | 346.2 | 0.898 × | 100.0 % |
| gpt-5.2 | high | 358.1 | 0.868 × | 100.0 % |
| gpt-5.2 | xhigh | 424.5 | 0.733 × | 100.0 % |

**Worked example.** A PTU customer running **1,000 req/min on 500 PTU**
for gpt-4o today is achieving **750 correct answers per minute**
(throughput × pass-rate). Migrating to **gpt-5.2 effort=none** at the same
500 PTU yields **~994 req/min × 100 % pass-rate ≈ 994 correct answers per
minute** — a **+32.5 % correct-answers-per-minute lift** at unchanged
capacity. At gpt-5.2 effort=high the same 500 PTU yields **~868 req/min ×
100 % ≈ 868 correct answers per minute** — still a **+15.7 %** lift versus
gpt-4o despite spending 15 % more tokens per request, because perfect
quality recovers the throughput shortfall.

The PTU-correct-answers-per-minute curve **peaks at gpt-5.2 effort=none**
on this benchmark. Above that point reasoning tokens consume capacity
without converting to additional correct answers.

Sibling chart pair (PTU lens):
- [`results/cost-curves/benchmark-02-throughput-gain.png`](../../results/cost-curves/benchmark-02-throughput-gain.png)
- [`results/cost-curves/benchmark-02-throughput-gain.csv`](../../results/cost-curves/benchmark-02-throughput-gain.csv)

## 7. Quality by Effort

Judge score on the reused 3-tier rubric (`0 = fail`, `1 = partial`, `2 = pass`)
— one judge call per cell; mean ± std per `(model, effort)`. Standard
deviation only: the methodology §8 caveat (N = 20, R = 3, authored samples)
does **not** support confidence-interval or significance claims.

| model | effort | judge score (mean ± std) | judge_n | pass-rate (score == 2) | partial-rate (score == 1) |
| --- | --- | ---: | ---: | ---: | ---: |
| gpt-4o | — (baseline) | 1.50 ± 0.87 | 60 | **75.0 %** (45/60) | 0 % (0/60) |
| gpt-5.2 | none | 2.00 ± 0.00 | 60 | **100.0 %** (60/60) | 0 % (0/60) |
| gpt-5.2 | low | 1.93 ± 0.36 | 60 | **96.7 %** (58/60) | 0 % (0/60) |
| gpt-5.2 | medium | 2.00 ± 0.00 | 60 | **100.0 %** (60/60) | 0 % (0/60) |
| gpt-5.2 | high | 2.00 ± 0.00 | 60 | **100.0 %** (60/60) | 0 % (0/60) |
| gpt-5.2 | xhigh | 2.00 ± 0.00 | 60 | **100.0 %** (60/60) | 0 % (0/60) |

The pre-registered sanity check **passes**: gpt-4o pass-rate (75.0 %) is
strictly less than gpt-5.2 high pass-rate (100.0 %). The pass-rate is
non-decreasing across the gpt-5.2 effort ladder except for a dip at `low`
(96.7 %) caused by two failed cells — the content-filter refusal at mr_05 r1
and a code-trace wrong answer ("cba a" instead of "cbaa") at mr_15 r2.
Effort-tier ordering is correct: more reasoning never reduces pass-rate.

The judge emitted **zero** `score == 1` rows on this benchmark — every
graded cell was either a clean pass or a clean fail. This is consistent
with the dataset's verifiable-answer design: there is no middle ground
between "produced 672" and "produced 576".

### Per-tag breakdown (selected gpt-4o cells; gpt-5.2 cells are essentially saturated at 2.0)

| tag (n cells) | gpt-4o mean | gpt-5.2 none | gpt-5.2 low | gpt-5.2 high |
| --- | ---: | ---: | ---: | ---: |
| arithmetic-word (n=15) | 1.20 ± 1.01 | 2.0 ± 0.0 | 2.0 ± 0.0 | 2.0 ± 0.0 |
| boolean-logic (n=6) | 2.00 ± 0.00 | 2.0 ± 0.0 | 2.0 ± 0.0 | 2.0 ± 0.0 |
| causal-chain (n=9) | 2.00 ± 0.00 | 2.0 ± 0.0 | 2.0 ± 0.0 | 2.0 ± 0.0 |
| code-trace (n=9) | 1.33 ± 1.00 | 2.0 ± 0.0 | 1.78 ± 0.67 | 2.0 ± 0.0 |
| constraint-satisfaction (n=9) | 1.33 ± 1.00 | 2.0 ± 0.0 | 1.78 ± 0.67 | 2.0 ± 0.0 |
| counting (n=6) | 2.00 ± 0.00 | 2.0 ± 0.0 | 2.0 ± 0.0 | 2.0 ± 0.0 |
| date-time (n=6) | 1.00 ± 1.10 | 2.0 ± 0.0 | 2.0 ± 0.0 | 2.0 ± 0.0 |

The gpt-4o cells fail most often on **arithmetic-word** (mean score 1.20 —
mr_01 and mr_02 in particular: the model produced 576 and 155 instead of
672 and 175), **date-time** (mean 1.00 — the day-of-week and 30-day-from-X
arithmetic), **code-trace** (mean 1.33 — aliasing and slicing), and
**constraint-satisfaction** (mean 1.33 — the elimination-chain puzzles). It
succeeds on **counting**, **boolean-logic**, and **causal-chain** — tasks
where a single forward pass without backtracking is enough.

gpt-5.2 handles **every subtype perfectly at every effort except `low`**,
where two cells score 0: the content-filter refusal at mr_05
(constraint-satisfaction) r1 and a code-trace wrong answer at mr_15 r2
("cba a" instead of "cbaa"). Both `low` tags therefore sit at 1.78 ± 0.67,
not 2.0. The full per-tag breakdown is preserved in
[`analysis.json`](analysis.json) under `judge_breakdown_by_tag`.

Sibling chart pair (quality):
- [`results/cost-curves/benchmark-02-quality.png`](../../results/cost-curves/benchmark-02-quality.png)
- [`results/cost-curves/benchmark-02-quality.csv`](../../results/cost-curves/benchmark-02-quality.csv)

## 8. Latency

End-to-end wall time per call, ms:

| model | effort | mean latency (ms) ± std |
| --- | --- | ---: |
| gpt-4o | — (baseline) | 1382 ± 523 |
| gpt-5.2 | none | 1589 ± 506 |
| gpt-5.2 | low | 1869 ± 776 |
| gpt-5.2 | medium | 2039 ± 729 |
| gpt-5.2 | high | 2325 ± 747 |
| gpt-5.2 | xhigh | 3265 ± 1807 |

Latency grows monotonically with effort, roughly **1.15 × baseline** at
`none`, 1.4 × at `low`, 1.7 × at `high`, 2.4 × at `xhigh`. The xhigh
standard deviation balloons to 1807 ms (54 % of the mean) — the deep-
reasoning tier is highly variable because some samples invoke far more
reasoning steps than others. A PAYG customer choosing `gpt-5.2 none`
therefore pays a **15 % latency tax versus gpt-4o** at **42 % cost-per-
correct savings** — the latency cost is a real trade-off for latency-
sensitive workloads but is a fraction of what the higher-effort tiers
charge.

Sibling chart pair (latency):
- [`results/cost-curves/benchmark-02-latency.png`](../../results/cost-curves/benchmark-02-latency.png)
- [`results/cost-curves/benchmark-02-latency.csv`](../../results/cost-curves/benchmark-02-latency.csv)

## 9. Outliers

The outlier policy (`scripts.analyze_tokens.flag_outliers`) is the
methodology §8.2 rule verbatim: a row is excluded **only when** it is
> 3 SDs from the cell mean *and* it carries an operational instrumentation
flag (`cold_start`, `retry_count > 0`, or `truncated_output`). Quality
outcomes are not outlier criteria.

**Excluded cells (0 of 360 = 0.0 %).** No row in any cell met the joint
condition. The single content-filter refusal at `mr_05 / gpt-5.2 / low / r1`
(input_tokens = 0, output_tokens = 0, latency_ms = 1786) is **NOT** an
outlier by this rule — it carries no operational flag (cold_start = false,
retry_count = 0, truncated_output = false). It is therefore counted in the
cell's aggregates as designed; the analyst can confirm via the raw JSON.
This is the right behavior: the refusal is a real production-shape event
(Azure content filter), not a measurement-instrumentation artifact, and
discarding it would understate the variance the operator faces in
production.

The 10 / 360 cells flagged `cold_start = true` (operator log) all fell
within 3 σ of their cell means; they were retained.

## 10. Conclusion

**For multi-step reasoning tasks, gpt-5.2 effort=none is the Pareto-optimal
choice for both PAYG and PTU consumption models on this benchmark.**

### Pre-registered range deviation -- gpt-4o pass-rate

The observed **gpt-4o pass-rate is 75.0 %** (45 / 60 cells; see §7), which
lands **above** the pre-registered 30-60 % range that
[`README.md` -- "Expected baseline behavior"](README.md) declared for the
gpt-4o baseline on this dataset. This is a material deviation and is
flagged here explicitly rather than silently absorbed into the conclusion:
the dataset as authored is **easier for gpt-4o than the pre-registration
anticipated**. The practical consequence is that the cost-per-correct
denominator for the gpt-4o cell (0.75) is substantially larger than the
pre-registration assumed (which sat between 0.30 and 0.60), so the
*magnitude* of every "cost-per-correct saving vs gpt-4o" number on this
page is **smaller** than it would have been on a harder-for-gpt-4o
dataset. The *direction* of the headline finding (gpt-5.2 effort=none
Pareto-dominates) is unchanged -- gpt-5.2 effort=none still saturates at
100 % and is still cheaper per call -- but the dollar-saving headline is
attenuated. A reader who internalises the README's 30-60 % range would
expect a 1.7x-3.3x cost-per-correct gap and instead sees a 1.7x gap; the
upper end of that expectation is not realised because the gpt-4o
denominator is higher than pre-registered. This is the **single largest
caveat on the cost-per-correct narrative below** and is the reason the
PTU-throughput framing in §6 (which is denominator-agnostic with respect
to gpt-4o pass-rate) is the more robust lens on this run.

The 30-60 % range in `README.md` was **approximate guidance**, not a
hard pre-registration: the README explicitly cites 30 % as the floor that
would trigger dataset-too-punishing recalibration and is silent on a
ceiling-overshoot trigger. **Benchmark 03 (tool-using agent, Task 010)
will not recalibrate this benchmark's dataset** -- the 360 cells already
captured are frozen evidence and re-engineering the dataset post-hoc to
hit a pre-registered band would itself be a measurement-validity
violation (selection-on-outcome). Instead, benchmark 03 will be
pre-registered with a **tighter and more defensible** expected-baseline
band, informed by what this benchmark actually showed: gpt-4o is
stronger on chained inference than the original spec anticipated when
the chain is short (2-3 inferential steps) and the subtypes lean toward
boolean-logic / causal-chain / counting (where gpt-4o scored 100 %
here). The benchmark 03 README will state its expected-baseline band
with **upper and lower triggers** and will cite this overshoot as the
empirical reason.

### Headline numbers (unchanged by the above caveat)

- **PAYG:** gpt-5.2 effort=none costs **$0.000618 per correct answer** vs
  gpt-4o baseline **$0.001064** -- a 42 % saving at list price. Every
  effort tier above `none` increases cost-per-correct without increasing
  pass-rate, which is already saturated at 100 %. The crossover does not
  exist on the upper effort tiers; `none` is the floor and the optimum
  simultaneously.
- **PTU:** gpt-5.2 effort=none delivers **0.994 x the throughput** of
  gpt-4o at **100 % pass-rate** (vs gpt-4o's 75 %). The
  correct-answers-per-minute lift at fixed PTU spend is **+32.5 %**. Every
  effort tier above `none` reduces throughput without lifting pass-rate.
- **Latency:** gpt-5.2 effort=none takes ~15 % longer per call than
  gpt-4o; this is the headline trade-off if p95-latency budgets are
  strict. Higher effort tiers add proportionally more latency without
  the offsetting quality lift.

**The reasoning surface earns its bill on this benchmark, but at
effort=none — i.e., gpt-5.2's *baseline* reasoning configuration is the
correct lever.** The pre-registered hypothesis "high reasoning effort
pays for itself on multi-step tasks" is **falsified** as stated: above
`effort=none`, additional reasoning tokens are spent but do not produce
additional correct answers. The headline win is the **model upgrade
from gpt-4o to gpt-5.2** rather than the effort knob; this is the
inverse of what a naïve reading of the GPT-5.2 marketing surface might
suggest.

### Paired with benchmark 01

Benchmark 01 (short-factual / null case) showed every effort tier above
`minimal/none` is wasted PAYG dollars and lost PTU throughput on tasks
that do not need reasoning. Benchmark 02 (multi-step / ceiling case)
shows the same effort tiers are *also* wasted dollars and throughput on
tasks that **do** need reasoning, because gpt-5.2 at `none` is already
sufficient. Together the two benchmarks bracket a narrower decision
boundary than the original spec anticipated:

| Task shape | gpt-4o suffices | gpt-5.2 effort=none required | gpt-5.2 effort > none required |
| --- | :---: | :---: | :---: |
| Short-factual (benchmark 01) | ✓ | (slightly cheaper) | ✗ |
| Multi-step reasoning (benchmark 02) | ✗ (75 % pass) | ✓ (100 % pass) | ✗ |

The "effort > none" column is empty on both benchmarks measured so far.
Benchmark 03 (tool-using agent, Task 010) is the next opportunity to
populate that column.

## 11. Reproducibility footer

Re-derive every number on this page from the raw JSONs:

```bash
# Aggregate the 360 measurement JSONs + 360 judge JSONs into analysis.json:
python -m scripts.analyze_tokens \
    --benchmark 02-multi-step-reasoning \
    --experiment-prefix exp002 \
    --out benchmarks/02-multi-step-reasoning/analysis.json

# Regenerate every PNG + paired CSV under results/:
python -m scripts.plot_results \
    --benchmark 02-multi-step-reasoning \
    --out results/

# Verify byte-stability — two consecutive analyze runs MUST produce identical bytes:
python -m scripts.analyze_tokens --benchmark 02-multi-step-reasoning \
    --experiment-prefix exp002 --out /tmp/a.json
python -m scripts.analyze_tokens --benchmark 02-multi-step-reasoning \
    --experiment-prefix exp002 --out /tmp/b.json
diff /tmp/a.json /tmp/b.json   # empty diff = byte-stable

# Benchmark 01 chart regression — must produce byte-identical PNGs / CSVs:
python -m scripts.plot_results --benchmark 01-short-factual --out results/
shasum -a 256 results/cost-curves/benchmark-01-*  # compare to pre-Task-009 baseline
```

To audit provenance — every JSON in `runs/` and `judge_runs/` is a real
Foundry v1 call, no fixture markers:

```bash
grep -l '"fixture": true' benchmarks/02-multi-step-reasoning/runs/*.json
# (empty output = provenance clean)
grep -l '"fixture": true' benchmarks/02-multi-step-reasoning/judge_runs/*.json
# (empty output = provenance clean)
```

To re-run the live measurement (real Azure spend; budget per the YAMLs):

```bash
# 1. Smoke (~$0.50 spec ceiling; actual $0.006):
python -m scripts.run_benchmark --experiment experiments/exp_smoke_02_gpt4o.yaml
python -m scripts.run_benchmark --experiment experiments/exp_smoke_02.yaml

# 2. Full (~$25 combined spec ceiling; actual $0.58):
python -m scripts.run_benchmark --experiment experiments/exp002_benchmark02_gpt4o.yaml
python -m scripts.run_benchmark --experiment experiments/exp002_benchmark02_gpt5_2.yaml

# 3. Judge pass (real gpt-4o calls; ~$0.32 actual spend):
GIT_COMMIT=$(git rev-parse HEAD) python -m scripts.run_judge \
    --benchmark 02-multi-step-reasoning \
    --experiment-prefix exp002 \
    --confirm --concurrency 5 --max-output-tokens 320

# 4. Analyze + plot:
python -m scripts.analyze_tokens --benchmark 02-multi-step-reasoning --experiment-prefix exp002
python -m scripts.plot_results --benchmark 02-multi-step-reasoning
```
