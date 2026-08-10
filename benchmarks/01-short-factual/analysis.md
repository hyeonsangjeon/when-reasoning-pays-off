# Benchmark 01 — Short-Factual: cost-vs-quality verdict

## 1. Question and design

For short-factual tasks (the null case for reasoning), does any GPT-5.2
`reasoning_effort` level justify its additional token spend versus a non-
reasoning GPT-4o baseline? The dataset is 20 frozen authored samples
(`sf_01`…`sf_20`) drawn from the structured-input-to-natural-language family
(extraction, formatting, classification, transliteration, trivial arithmetic).
Each `(sample, model, effort)` cell is repeated `R = 3` times; the runner
emits the Foundry v1 Responses payload verbatim and the analysis pipeline
aggregates it offline. The varying axis is `reasoning.effort` on GPT-5.2,
swept across `{none, low, medium, high, xhigh}` plus a separate GPT-4o
baseline with no reasoning parameter.

> **Provenance.** The 360 measurement JSONs aggregated into `analysis.json`
> and the 360 judge JSONs aggregated alongside them are **real Azure AI
> Foundry v1 measurements** — the Task 007 production cohort with experiment
> IDs `exp001_short-factual_baseline` (gpt-5.2 cells) and
> `exp001_short-factual_baseline_gpt4o` (gpt-4o cells). No file carries the
> `"fixture": true` marker, and every judge JSON is a real gpt-4o judge call
> (360/360 successful). `analysis.json` was regenerated offline with
> `--experiment-prefix exp001_short-factual_baseline --judge-dir
> benchmarks/01-short-factual/judge_runs_real` and is byte-stable across
> repeated runs. The synthetic Task 008 fixtures
> (`exp008_short-factual_fixture*`, produced by `scripts/_fixture_synth.py`)
> remain on disk under `runs/` and `judge_runs/` as a credential-free
> offline scaffold; the experiment prefix filters them out of this
> aggregate. See `benchmarks/01-short-factual/runs/FIXTURE_NOTE.md`. Note
> that the live gpt-5.2 deployment rejects the fixtures' fictional
> `minimal` tier with HTTP 400, so the **measured effort floor is `none`,
> not `minimal`.**

## 2. Methodology link

The full measurement contract — variables, sample size, cache handling,
quality evaluation, cost calculation, reproducibility, statistical
reporting — lives in [`docs/05-methodology.md`](../../docs/05-methodology.md).
Read it before disagreeing with any number on this page.

## 3. Run provenance

| Field | Value |
| --- | --- |
| Benchmark | `01-short-factual` |
| Dataset | `benchmarks/01-short-factual/dataset.json` |
| Dataset SHA-256 | `09caa4fe525c5cfd4e94696f70ff61bf30d7615bd659d3c80892b2e1b4f2974f` |
| Experiment YAMLs (executed) | `experiments/exp001_short-factual_baseline.yaml`, `experiments/exp001_short-factual_baseline_gpt4o.yaml` |
| Aggregated experiment IDs (this run) | `exp001_short-factual_baseline`, `exp001_short-factual_baseline_gpt4o` (real Task 007 cohort; judges under `judge_runs_real/`) |
| Git commits (recorded in raw JSON) | `a9c9cce`, `b0ee8ff` |
| Total cells | 360 (60 gpt-4o baseline + 300 gpt-5.2 across 5 efforts) |
| Cells used after outlier exclusion | 360 |
| Cells excluded as outliers | 0 |
| Judge model | `gpt-4o` (cheap, neutral; one call per cell) |
| Judge prompt SHA-256 | `fb997c90fe0778681194809793cdce218bcb9d6537304a525f2916cf29ba6942` |
| Pricing snapshot | `pricing/azure-openai-payg-2026-05.yaml` |
| Pricing source URL | <https://azure.microsoft.com/en-us/pricing/details/azure-openai/> (accessed `2026-05-19`) |

## 4. Token composition

Mean ± std per `(model, effort)` cell, computed over the rows kept after
outlier exclusion. All counts are per request.

| model | effort | n_used | input | cached | output (visible) | reasoning | total |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| gpt-4o | — (baseline) | 60 | 224.5 ± 12.6 | 0.0 ± 0.0 | 10.4 ± 7.1 | 0.00 ± 0.00 | 234.9 ± 17.0 |
| gpt-5.2 | none | 60 | 223.5 ± 12.6 | 0.0 ± 0.0 | 14.0 ± 7.9 | 0.00 ± 0.00 | 237.5 ± 18.1 |
| gpt-5.2 | low | 60 | 223.5 ± 12.6 | 0.0 ± 0.0 | 14.1 ± 8.1 | 0.00 ± 0.00 | 237.6 ± 18.2 |
| gpt-5.2 | medium | 60 | 223.5 ± 12.6 | 0.0 ± 0.0 | 14.2 ± 8.0 | 0.00 ± 0.00 | 237.7 ± 18.2 |
| gpt-5.2 | high | 60 | 223.5 ± 12.6 | 0.0 ± 0.0 | 15.5 ± 10.0 | 1.35 ± 5.94 | 239.0 ± 18.0 |
| gpt-5.2 | xhigh | 60 | 223.5 ± 12.6 | 0.0 ± 0.0 | 14.8 ± 8.8 | 0.62 ± 4.78 | 238.3 ± 19.2 |

The reasoning column is ≈ 0 at every tier. Even at `high` the mean is
1.35 tokens (SD 5.94 — a few requests
emit a couple dozen, most emit none) and `xhigh` is lower still at 0.62. The
input shape is fixed across effort levels (the prompt is byte-identical) and
visible output moves only marginally (10–16 tokens), so total tokens per
request stay inside a ~2 % band (234.9 → 239.0) across the whole ladder. On
short-factual work the effort dial buys almost no reasoning tokens because
there is nothing for the model to deliberate about.

## 5. Cost by Effort

Every USD figure below originates from
`scripts.cost_calculator.payg_cost_per_call()`, which applies the
methodology §6.1 PAYG formula verbatim against the pricing snapshot at
`pricing/azure-openai-payg-2026-05.yaml`
([source](https://azure.microsoft.com/en-us/pricing/details/azure-openai/),
accessed `2026-05-19`).

| model | effort | USD per request (mean ± std) |
| --- | --- | ---: |
| gpt-4o | — (baseline) | **$0.000665 ± $0.000089** ($0.665 per 1,000 req; `pricing/azure-openai-payg-2026-05.yaml`, accessed `2026-05-19`) |
| gpt-5.2 | none | $0.000587 ± $0.000124 ($0.587 per 1,000 req; `pricing/azure-openai-payg-2026-05.yaml`, accessed `2026-05-19`) |
| gpt-5.2 | low | $0.000589 ± $0.000126 ($0.589 per 1,000 req; `pricing/azure-openai-payg-2026-05.yaml`, accessed `2026-05-19`) |
| gpt-5.2 | medium | $0.000589 ± $0.000126 ($0.589 per 1,000 req; `pricing/azure-openai-payg-2026-05.yaml`, accessed `2026-05-19`) |
| gpt-5.2 | high | $0.000608 ± $0.000147 ($0.608 per 1,000 req; `pricing/azure-openai-payg-2026-05.yaml`, accessed `2026-05-19`) |
| gpt-5.2 | xhigh | $0.000598 ± $0.000138 ($0.598 per 1,000 req; `pricing/azure-openai-payg-2026-05.yaml`, accessed `2026-05-19`) |

GPT-5.2 at `none` undercuts the GPT-4o baseline by **$0.000078 per request**
(≈12 %, i.e. $0.587 vs $0.665 per 1,000 requests) because of a lower input rate
(`$1.75/$0.175` vs `$2.50/$1.25` per 1 M tokens) and zero reasoning spend.
Raising the effort dial barely moves the bill: `low` and `medium` are
statistically identical to `none` ($0.589), and even `xhigh` ($0.598) and
`high` ($0.608) stay **below** the gpt-4o baseline. The entire gpt-5.2 column
sits in a $0.587–$0.608 band — a ~4 % spread dwarfed by the per-cell SD.
Unlike the reasoning-heavy benchmarks there is no cost cliff to climb here,
because there are no reasoning tokens to pay for (§4).

Sibling chart pair (PAYG lens):
- [`results/cost-curves/benchmark-01-cost-per-request.png`](../../results/cost-curves/benchmark-01-cost-per-request.png)
- [`results/cost-curves/benchmark-01-cost-per-request.csv`](../../results/cost-curves/benchmark-01-cost-per-request.csv)

## 6. Consumption Model Translation

**For PAYG:** at `gpt-5.2 none`, USD per call drops from $0.000665 (gpt-4o
baseline) to $0.000587 — a saving of **$0.000078 per request** (≈12 %, or
$0.587 vs $0.665 per 1,000 requests) at list price
([source](https://azure.microsoft.com/en-us/pricing/details/azure-openai/),
accessed `2026-05-19`, snapshot `pricing/azure-openai-payg-2026-05.yaml`).
Turning the effort dial up does not undo that saving: `low`/`medium` match
`none` at $0.000589, and `high` ($0.000608) and `xhigh` ($0.000598) still
land below the gpt-4o baseline. For short-factual workloads **every gpt-5.2
tier is cheaper than gpt-4o**, and none of the paid-up tiers buys a
proportionate quality lift (see §7) — so the cost-minimizing choice is simply
`none`.

**For PTU:** the throughput-gain factor relative to **gpt-4o baseline**
(mean tokens-per-request = 234.9) hovers just below parity across the whole
gpt-5.2 ladder:
`none = 0.989 ×`, `low = 0.989 ×`, `medium = 0.988 ×`,
`high = 0.983 ×`, `xhigh = 0.986 ×`.
Worked example: if a customer runs **1,000 req/min on 500 PTU** for
gpt-4o today, the same 500 PTU running gpt-5.2 at `none` would serve
**~989 req/min**, and even at `high` **~983 req/min** — a ~1–2 % throughput
give-back, essentially a wash. Because gpt-5.2's token footprint on this
benchmark stays within ~2 % of gpt-4o's (§4), **the dramatic PTU collapse
seen on reasoning-heavy workloads does not appear here.** For PTU
short-factual traffic gpt-4o and gpt-5.2 `none` are effectively
interchangeable on throughput; the decision then rests on the −12 % PAYG
cost and the quality edge (§7), both of which favor gpt-5.2 `none`.

## 7. Quality by Effort

Judge score (0 = fail, 1 = partial, 2 = pass) — one judge call per cell;
mean ± std per `(model, effort)`. Standard deviation only: the
methodology §8 caveat (N = 20, R = 3, authored samples) does **not**
support confidence-interval or significance claims.

| model | effort | judge score (mean ± std) | judge_n |
| --- | --- | ---: | ---: |
| gpt-4o | — (baseline) | 1.90 ± 0.30 | 60 |
| gpt-5.2 | none | 1.95 ± 0.22 | 60 |
| gpt-5.2 | low | 1.97 ± 0.18 | 60 |
| gpt-5.2 | medium | 1.97 ± 0.18 | 60 |
| gpt-5.2 | high | 1.93 ± 0.31 | 60 |
| gpt-5.2 | xhigh | 1.95 ± 0.22 | 60 |

**gpt-5.2 matches or beats gpt-4o at every tier, starting from `none`**
(1.95 vs 1.90). Effort does not help:
the gpt-5.2 column spans just 1.93–1.97, a **0.04-point** spread, and the
widest gap anywhere on the ladder is **0.07 points** (low/medium vs gpt-4o)
— comfortably *within one SD* in every cell. Per the methodology §8
"Comparisons across efforts" rule we report the direction but make no
significance claim: on this null-case benchmark, **quality is already
saturated at `none`, and judge score does not increase with effort.**

Per-tag breakdown (selected; full table in `analysis.json` under
`judge_breakdown_by_tag`):

| tag | gpt-4o | gpt-5.2 none | gpt-5.2 high |
| --- | ---: | ---: | ---: |
| formatting (n=18) | 2.00 ± 0.00 | 2.00 ± 0.00 | 2.00 ± 0.00 |
| extraction (n=12) | 1.75 ± 0.45 | 2.00 ± 0.00 | 2.00 ± 0.00 |
| summarization (n=9) | 1.33 ± 0.50 | 1.78 ± 0.44 | 1.78 ± 0.44 |
| arithmetic-trivial (n=6) | 2.00 ± 0.00 | 1.83 ± 0.41 | 1.67 ± 0.82 |
| classification (n=6) | 2.00 ± 0.00 | 2.00 ± 0.00 | 2.00 ± 0.00 |
| transliteration (n=6) | 2.00 ± 0.00 | 2.00 ± 0.00 | 2.00 ± 0.00 |

The aggregate parity hides a tag-level trade. gpt-5.2 `none` *earns*
its edge on the two tags gpt-4o fumbles — **extraction** (1.75 → 2.00) and
**summarization** (1.33 → 1.78) — and does so with zero reasoning tokens.
The one tag where gpt-4o wins is **arithmetic-trivial** (2.00 vs 1.83), and
turning the effort dial up makes gpt-5.2 *worse* there, not better (1.83 at
`none` → 1.67 at `high`): the model over-thinks "1 + 1". Everything else is
a 2.00 wash on both sides. The quality difference comes from the model swap,
not the effort dial.

Sibling chart pair (quality):
- [`results/cost-curves/benchmark-01-quality.png`](../../results/cost-curves/benchmark-01-quality.png)
- [`results/cost-curves/benchmark-01-quality.csv`](../../results/cost-curves/benchmark-01-quality.csv)

## 8. Latency

End-to-end wall time per call, ms, after outlier exclusion:

| model | effort | mean latency (ms) ± std |
| --- | --- | ---: |
| gpt-4o | — (baseline) | 1494 ± 753 |
| gpt-5.2 | none | 1657 ± 700 |
| gpt-5.2 | low | 1713 ± 677 |
| gpt-5.2 | medium | 1410 ± 293 |
| gpt-5.2 | high | 1580 ± 416 |
| gpt-5.2 | xhigh | 1441 ± 305 |

Latency shows **no monotonic effort trend** — the values bounce between
1410 ms (`medium`) and 1713 ms (`low`) with no ordering by effort, and the
spread is swamped by large per-cell SDs (± 300–750 ms). This is what you
expect once reasoning tokens are ≈ 0 (§4): with almost nothing to generate,
end-to-end time is dominated by network and queueing noise, not by the
effort dial. The practical read is that on short-factual traffic the effort
setting carries **no latency penalty** — but also no benefit — so latency
does not tip the decision either way.

Sibling chart pair (latency box plot):
- [`results/cost-curves/benchmark-01-latency.png`](../../results/cost-curves/benchmark-01-latency.png)
- [`results/cost-curves/benchmark-01-latency.csv`](../../results/cost-curves/benchmark-01-latency.csv)

## 9. Outliers

The outlier policy (`scripts.analyze_tokens.flag_outliers`) is the
methodology §8.2 rule verbatim: a row is excluded **only when** it is
> 3 SDs from the cell mean *and* it carries an operational instrumentation
flag (`cold_start`, `retry_count > 0`, or `truncated_output`). Quality
outcomes are not outlier criteria.

Excluded cells (0 of 360 = 0.0 %):

No row met the exclusion criterion — every one of the 360 measured cells is
within 3 SDs of its cell mean, or carries no operational instrumentation
flag. The full cohort is aggregated as-is; there is no outlier mask on this
run. (The policy still runs; it simply found nothing to exclude.)

## 10. Conclusion

**For short-factual tasks, no gpt-5.2 effort tier above `none` earns its
keep — but `none` itself is a clean win over the gpt-4o baseline.** At
`none`, gpt-5.2 costs **−12 % per request** ($0.587 vs $0.665 per 1,000
requests) *and* scores marginally higher on quality (1.95 vs 1.90 on a 0–2
judge scale), because it quietly fixes the extraction and summarization
misses that cost gpt-4o points (§7) — all with **zero reasoning tokens**
(§4). Turning the effort dial up buys nothing measurable: cost stays flat
inside a $0.587–$0.608 band, judge score moves ≤ 0.07 points (within one
SD), latency shows no trend, and on trivial arithmetic effort actually
*hurts*. For PTU the token footprint is within ~2 % of gpt-4o, so
throughput is a wash. The Pareto-optimal choice on this benchmark is
**gpt-5.2 at effort = `none`** for both PAYG and PTU; any reasoning effort
beyond `none` is empirically wasted on workloads of this shape.

## 11. Reproducibility footer

Re-derive every number on this page from the raw JSONs. The real cohort is
selected by an **explicit** experiment prefix and judge directory — the bare
default flags would instead aggregate the offline `exp008_*` fixtures:

```bash
# Run the offline aggregator over the REAL Task 007 cohort (no network):
python -m scripts.analyze_tokens \
    --benchmark 01-short-factual \
    --experiment-prefix exp001_short-factual_baseline \
    --judge-dir benchmarks/01-short-factual/judge_runs_real \
    --out benchmarks/01-short-factual/analysis.json

# Regenerate every PNG + paired CSV under results/ (reads analysis.json):
python -m scripts.plot_results \
    --benchmark 01-short-factual \
    --out results/

# Verify byte-stability — two consecutive analyze runs MUST produce identical bytes:
python -m scripts.analyze_tokens --benchmark 01-short-factual \
    --experiment-prefix exp001_short-factual_baseline \
    --judge-dir benchmarks/01-short-factual/judge_runs_real --out /tmp/a.json
python -m scripts.analyze_tokens --benchmark 01-short-factual \
    --experiment-prefix exp001_short-factual_baseline \
    --judge-dir benchmarks/01-short-factual/judge_runs_real --out /tmp/b.json
diff /tmp/a.json /tmp/b.json   # empty diff = byte-stable
```

The synthetic `exp008_*` fixtures remain under `runs/` and `judge_runs/` as
a credential-free offline scaffold; running `analyze_tokens` with its default
flags reproduces the *fixture* artifact, not this one. This page's structure
is stable regardless of which cohort is aggregated — only the numbers move.
See `benchmarks/01-short-factual/runs/FIXTURE_NOTE.md` for the full cohort
map and the fixture/real swap procedure.
