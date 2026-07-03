# when-reasoning-pays-off

*Same token price, different bill — a **measured** guide to when reasoning
models earn their cost, and when they just bill for thinking nobody reads.*

[![CI](https://github.com/hyeonsangjeon/when-reasoning-pays-off/actions/workflows/ci.yml/badge.svg)](https://github.com/hyeonsangjeon/when-reasoning-pays-off/actions/workflows/ci.yml) [![Live evidence dashboard](https://img.shields.io/badge/live-evidence%20dashboard-2563eb?logo=github&logoColor=white)](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/charts/?lang=en) [![Docs: 5 languages](https://img.shields.io/badge/docs-EN%20%C2%B7%20KO%20%C2%B7%20JA%20%C2%B7%20ZH%20%C2%B7%20HI-0ea5e9)](https://hyeonsangjeon.github.io/when-reasoning-pays-off/) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Last commit](https://img.shields.io/github/last-commit/hyeonsangjeon/when-reasoning-pays-off)](https://github.com/hyeonsangjeon/when-reasoning-pays-off/commits/main) [![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md) [![Star this repo](https://img.shields.io/github/stars/hyeonsangjeon/when-reasoning-pays-off?style=social)](https://github.com/hyeonsangjeon/when-reasoning-pays-off/stargazers)

![Same token price, different bill: a reasoning workload pays the same per-token price but bills extra hidden reasoning tokens, so its total bill is taller.](docs/assets/hero.svg)

> [!TIP]
> **▶ Live evidence dashboard** — interactive static charts for reasoning-effort
> cost, latency, throughput, and the PTU↔PAYG crossover, rendered from sanitized
> public data (no live service calls).
> **[Open the dashboard →](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/charts/?lang=en)**
> &nbsp;·&nbsp; [한국어](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/charts/?lang=ko) · [日本語](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/charts/?lang=ja) · [简体中文](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/charts/?lang=zh-CN) · [हिन्दी](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/charts/?lang=hi)

📖 **Project site:** https://hyeonsangjeon.github.io/when-reasoning-pays-off/ (English, Korean, Japanese, and Simplified Chinese; Hindi fallback in progress).

Start with the [overview essay](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/articles/when-reasoning-pays-off/), then drill into the [short factual work topic](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/articles/when-reasoning-pays-off/topics/short-factual-work/) or inspect the [evidence dashboard](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/charts/?lang=en).

## TL;DR — what the measurements say

**Reasoning models genuinely earn their price on hard work.** On a multi-step
task, turning the effort dial up measurably lifted answer quality (**1.5 →
2.0**) — exactly what you hope for when you reach for a reasoning model. The
catch is matching the dial to the job. On an everyday, non-reasoning task, that
same extra effort bought **7.6× the cost for essentially no quality gain**,
because the money flowed into hidden "thinking" tokens the task never needed.
So the honest answer is *it depends on the task* — and this repo measures
exactly where that line falls, so you spend effort only where it pays back.

*The two charts below are the everyday-task case (benchmark 01) — the one where
extra effort does **not** pay. On harder tasks the quality line climbs instead.*

| 💸 Cost climbs steeply with effort… | 🎯 …but on easy tasks, quality stays flat |
| --- | --- |
| ![Benchmark 01 cost per request rises about 7.6x from minimal to extra-high reasoning effort](docs/assets/benchmark-01-cost-per-request.png) | ![Benchmark 01 judge quality score stays roughly flat across every reasoning effort level](docs/assets/benchmark-01-quality.png) |

- **On genuinely hard work, effort pays.** On a multi-step reasoning task, the
  same effort ladder lifted quality from **1.5 → 2.0** — the upside that makes
  reasoning models worth reaching for.
- **On easy work, more effort mostly buys hidden tokens, not better answers.**
  On short factual work, cost climbed **7.6×** (minimal → extra-high) while the
  mean judge score stayed flat (**1.88 → 1.78**) and billed reasoning tokens
  exploded from **~4 to ~311** per request.
- **The sweet spot is rarely `high` / `xhigh`.** Across all three benchmarks,
  pushing past the mid-range added cost and latency without lifting measured
  quality.
- **Switching model can beat switching effort.** A low gpt-5.2 tier beat the
  gpt-4o baseline by **11–42 %** on cost-per-correct-answer, depending on the
  task shape.

<sub>Measured over **3 benchmarks × N=20 samples × R=3 repeats = 1080 cells**,
byte-identical prompts, single Azure OpenAI tenant. Every number is re-cited
from a per-benchmark `analysis.md`, never re-derived — see the
[evidence dashboard](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/charts/?lang=en)
and [`results/summary.md`](results/summary.md).</sub>

## Try it in 30 seconds

| You want to… | Do this | Azure needed? |
| --- | --- | :---: |
| **See the evidence** | [Open the live dashboard →](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/charts/?lang=en) — interactive cost / quality / latency / crossover charts | ❌ |
| **Read the story** | [Overview essay →](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/articles/when-reasoning-pays-off/) in English · 한국어 · 日本語 · 简体中文 · हिन्दी | ❌ |
| **Run the test suite** | `pip install -e ".[dev]" && pytest -q -m "not adaptive_calibration" batch-runner/tests/` | ❌ |
| **Reproduce the numbers** | [Reproducing these measurements ↓](#reproducing-these-measurements) | ✅ |

> If this saved you a migration post-mortem, a ⭐ helps other teams find it.

## Contents

- [What this repo is](#what-this-repo-is) · [Terms you will see](#terms-you-will-see)
- [The question](#the-question) · [Short answer](#short-answer) · [Which customer are you?](#which-customer-are-you)
- [What's here](#whats-here) — [docs](#documentation), [code and data](#code-and-data)
- [Operator levers (L1–L5)](#operator-levers-l1l5) · [Methodology](#methodology-summary) · [Reproducing](#reproducing-these-measurements)
- [Data publication policy](#data-publication-policy) · [Contributing, governance, security](#contributing-governance-security)

## What this repo is

When teams move a workload from a non-reasoning model (e.g. GPT-4o) to a
**reasoning model** like GPT-5.2, the bill often goes up even though the
per-token prices look similar. The reason: reasoning models charge for
"thinking" — internal reasoning tokens that are billed but never appear in the
response. This repo measures, on a small set of representative tasks, when
those extra reasoning tokens are worth the cost and when they are not, and
publishes the raw measurement scripts so you can rerun them on your own
deployment.

**Who this is for.** Engineers and architects running an Azure OpenAI
deployment who are trying to decide whether (and how much) reasoning to enable,
sizing capacity, or debugging a cost / latency / throttling change after a
model migration.

> **Research artifact, not a product.** This repository publishes reproducible
> benchmarks, methodology, decision tools, and sanitized result slices. There
> is no hosted service, no SLA, no managed library. See
> [`SUPPORT.md`](SUPPORT.md), [`SECURITY.md`](SECURITY.md), and
> [`docs/16-release-tiers-and-redaction-policy.md`](docs/16-release-tiers-and-redaction-policy.md)
> for scope, security reporting, and how published data is sanitized.

## Terms you will see

| Term | Plain-language meaning |
| --- | --- |
| **Reasoning model** | A model that runs internal reasoning steps before producing a visible response (e.g. GPT-5.2). Those internal tokens are billed at the output rate but never returned to the caller. |
| **`reasoning_effort`** | Per-request knob (`minimal` / `low` / `medium` / `high`) that controls how many reasoning tokens the model is allowed to spend. The primary cost lever on reasoning models. |
| **PAYG** (Pay-As-You-Go) | Consumption-based pricing on Azure OpenAI: billed per uncached input token, cached input token (at a lower rate), and output token actually used. Reasoning tokens are charged at the output rate. |
| **PTU** (Provisioned Throughput Units) | Pre-paid, reserved capacity on Azure OpenAI: billed by the hour for a fixed throughput budget, independent of token volume. Once you have bought PTU, reducing tokens per request does not lower the bill — it lets the same capacity serve more traffic. |
| **HTTP 429** | The "too many requests" / capacity-rejection response the service returns when a deployment is over its rate or PTU budget. We refer to "429 onset" as the request rate at which 429s start appearing. |
| **Prompt cache / cached input** | Azure OpenAI bills the cached portion of an input prompt at a lower rate. A single byte change to the **cacheable prefix** — typically the system prompt or tool-definition block — invalidates that prefix until it re-warms. |
| **Single-call ReAct** | A workflow in which one large model call plans, reasons over tool definitions and retrieval context, and synthesizes the answer, instead of splitting the task across several smaller orchestration calls. The shape of the prompt (tools, schemas, retrieval) becomes part of the cacheable prefix. |
| **Operator lever (L1–L5)** | Five operational knobs an engineer can change on a live deployment without re-architecting it. Defined in [`docs/09-operator-guide-one-page.md`](docs/09-operator-guide-one-page.md) and summarized [below](#operator-levers-l1l5). |
| **Release tier** (`SANITIZED_PUBLIC`, `RAW_PRIVATE`, `AGGREGATE_AZURE_SAMPLE`) | Labels on every published artifact saying how it was processed before publication. See [Data publication policy](#data-publication-policy). |

## The question

GPT-4o → GPT-5.2 migrations often show rising token costs because reasoning
models charge for invisible internal reasoning tokens. The question is not
whether reasoning models are useful (they are), but **when their cost is
justified**. This repo measures that question with reproducible benchmarks.

## Short answer

- Reasoning tokens are billed but do not appear in the response. **Always
  measure them.** Every benchmark in this repo captures the full
  `response.usage` object — input, cached, reasoning, and output tokens
  separately.
- Not every task benefits from reasoning. Short factual answers,
  structured-input-to-natural-language synthesis, and simple classification
  often do not need it.
- `reasoning_effort` (`minimal` / `low` / `medium` / `high`) is the primary
  cost lever. Default to the lowest level and raise it only when the task's
  quality evaluation actually justifies it.
- Prompt caching behaves differently on reasoning models. Why the hit ratio
  changes depends on the **architecture** (multi-node orchestration vs
  single-call ReAct) and on the **billing model** (PAYG vs PTU). The repo
  enumerates testable hypotheses in
  [`docs/07-cache-hit-degradation.md`](docs/07-cache-hit-degradation.md).
- On **PAYG**, cutting reasoning tokens directly reduces the bill. On **PTU**,
  the same cut is a throughput gain at a fixed bill. The repo measures both.
- At scale, the architectural answer is to route different task types to
  different effort levels — regardless of billing model.

## Which customer are you?

**PAYG users.** You are billed per token. Reducing tokens reduces your bill.
The cost curves under [`results/cost-curves/`](results/cost-curves/) show the
dollar delta per request at each effort level.

**PTU users.** You are billed for a fixed capacity budget. Reducing tokens does
not change your bill — it lets the same PTU capacity serve more requests, and
it cushions latency spikes during peak load. PTU users investigating cache hit
drops or earlier 429 onset after migrating to a reasoning model should read
[`docs/07-cache-hit-degradation.md`](docs/07-cache-hit-degradation.md).
`docs/07` ranks nine candidate causes from most to least useful for diagnosis.
Three to look at first for a PTU + reasoning migration are:

- **Capacity reservation from `max_output_tokens`** — `max_output_tokens` acts
  as an admission-time PTU reservation, not a soft cap. Inflating it to give
  reasoning headroom silently lowers how many concurrent requests the
  deployment can admit, so 429s appear at a lower request rate while the bill
  per completed request is unchanged.
- **Load-correlated prompt-cache dip** — a transient dip in cache hit ratio
  while a PTU deployment is running near saturation, which recovers as load
  drops. It is a load-correlated effect, not a permanent change.
- **Cacheable-prefix shape change during single-call ReAct migration** — the
  input side of the prompt changes shape during a single-call ReAct migration
  (tool definitions, structured-output schemas, retrieval context placement)
  and that changes the cacheable prefix even if the user-visible system prompt
  did not.

Of these, only the **load-correlated prompt-cache dip** has direct in-repo
measurement so far; the **capacity reservation from `max_output_tokens`** and
the **cacheable-prefix shape change during single-call ReAct migration** are
mechanism-backed diagnostic hypotheses with recipes in `docs/07` rather than
measured magnitudes.

## What's here

### Documentation

| Doc | Topic |
| --- | --- |
| [`docs/04-decision-framework.md`](docs/04-decision-framework.md) | Task → effort decision framework |
| [`docs/05-methodology.md`](docs/05-methodology.md) | How we measured (the reproducibility contract — frozen) |
| [`docs/07-cache-hit-degradation.md`](docs/07-cache-hit-degradation.md) | Hypotheses for cache hit ratio drop on reasoning models |
| [`docs/08-customer-simulation-findings.md`](docs/08-customer-simulation-findings.md) | PTU + single-call ReAct: pattern, mechanisms, leverages |
| [`docs/09-operator-guide-one-page.md`](docs/09-operator-guide-one-page.md) | Operational quick-reference (operator levers L1–L5 for PTU + reasoning) |
| [`docs/10-ptu-admission-controller.md`](docs/10-ptu-admission-controller.md) | Header-driven admission controller design |
| [`docs/11-multi-worker-cooldown.md`](docs/11-multi-worker-cooldown.md) | Multi-worker cooldown coordination |
| [`docs/12-prompt-cache-key-policy.md`](docs/12-prompt-cache-key-policy.md) | `prompt_cache_key` policy library + sizing runbook |
| [`docs/13-ptu-vs-payg-decision-runbook.md`](docs/13-ptu-vs-payg-decision-runbook.md) | PTU vs PAYG decision calculator + runbook |
| [`docs/14-observability-schema.md`](docs/14-observability-schema.md) | Canonical per-request / per-cell record contract |
| [`docs/15-spec-vs-inference-taxonomy.md`](docs/15-spec-vs-inference-taxonomy.md) | Two-tier citation taxonomy (Tier 1 official spec vs Tier 2 operational inference) |
| [`docs/15-spec-vs-inference-taxonomy.examples.md`](docs/15-spec-vs-inference-taxonomy.examples.md) | Worked examples of the citation taxonomy |
| [`docs/16-release-tiers-and-redaction-policy.md`](docs/16-release-tiers-and-redaction-policy.md) | Three-tier release classification and redaction rules |
| [`docs/17-foundry-packaging-relationship.md`](docs/17-foundry-packaging-relationship.md) | Publication boundaries and Azure AI Foundry packaging contract |

### Code and data

- [`batch-runner/`](batch-runner/) — Python library: decision calculators,
  `prompt_cache_key` policy, observability schema, release-tier helpers.
- [`scripts/`](scripts/) — Measurement, public-data generation, and article
  publication pipeline. Start with [`scripts/README.md`](scripts/README.md).
- [`benchmarks/`](benchmarks/) — Per-task measurement targets with sanitized
  run captures and per-target analysis. Tier classification per `docs/16`.
- [`results/`](results/) — Cross-benchmark synthesis and charts. See
  [`results/summary.md`](results/summary.md),
  [`results/public/chart-data/README.md`](results/public/chart-data/README.md),
  and [`results/supplementary/README.md`](results/supplementary/README.md).
- [`scripts/article_topics/`](scripts/article_topics/) — Article-topic registry
  that maps public articles to generators, input evidence, sample units, and
  output contracts.
- [`schemas/`](schemas/) — JSON Schemas for the observability and
  release-manifest record contracts.
- [`pricing/`](pricing/) — Pricing snapshots used by the cost calculator (PAYG
  and PTU density tables).
- [`release/public_sanitized_manifest.json`](release/public_sanitized_manifest.json)
  — the **release manifest**: for every published artifact, the SHA-256 of the
  sanitized file and the SHA-256 of the original raw source it derives from.

## Operator levers (L1–L5)

If you operate a live deployment, [`docs/09-operator-guide-one-page.md`](docs/09-operator-guide-one-page.md)
is the one-page reference. The five levers, in plain English:

| Lever | What you change | Why it matters |
| --- | --- | --- |
| **L1** First-token timeout | Per-deployment `first_token_timeout_ms` (e.g. `3000`) | Decides how long a request waits while the primary is saturated before the client gives up or reroutes. Shorter timeouts shrink tail latency in the saturated window. |
| **L2** Spillover policy | Native Azure spillover vs custom proactive router | Native spillover reacts to 429s on a PTU primary; a custom router can act earlier but generates extra 429s when its heuristic mis-fires. Pick based on topology, not preference. |
| **L3** System-prompt stability | Track `sha256(system_prompt)` per request and alarm on unintended changes | A single byte change flushes the prompt cache, so the next requests bill the full input rate until the cache re-warms. Migration-era "cache hit dropped" symptoms often resolve to this. |
| **L4** Reasoning effort tuning | Per-task `reasoning_effort` default | Higher effort spends more reasoning tokens without necessarily lifting quality. Default new traffic to `minimal` (or `none` where supported); raise only where measured quality justifies it. |
| **L5** `max_output_tokens` tightening | Tighten the per-call cap to `ceil((p99_visible + p99_reasoning) × 1.15)` | On PTU, this value is an admission-time reservation. Inflated caps silently reduce concurrency and pull 429 onset earlier. |

Full mechanism, evidence, and Azure docs links for each lever are in
[`docs/09`](docs/09-operator-guide-one-page.md).

## Methodology summary

Three benchmark task types representative of common use cases. For each, we
sweep `reasoning_effort` across its full ladder of tiers and run multiple
repeats per (sample, effort) combination. We capture the full `response.usage`
object — input, cached, reasoning, and output tokens separately — and pair
token measurements with a quality evaluation.

Detailed methodology: [`docs/05-methodology.md`](docs/05-methodology.md).

## Reproducing these measurements

**No Azure account needed to explore the results.** Open the
[live dashboard](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/charts/?lang=en),
read [`results/summary.md`](results/summary.md), or run the unit-test suite
locally:

```bash
git clone https://github.com/hyeonsangjeon/when-reasoning-pays-off.git
cd when-reasoning-pays-off

python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# No credentials required for the test suite:
pytest -q -m "not adaptive_calibration" batch-runner/tests/
```

**To re-run the benchmarks themselves** you need Azure OpenAI access with a
GPT-5.2 deployment. This repo uses **Entra ID authentication** — no API keys in
`.env`; run `az login` once and the cached token is auto-refreshed by
`DefaultAzureCredential`.

```bash
az login                       # one-time
cp .env.example .env           # then edit the endpoint + deployment names
python scripts/run_benchmark.py experiments/exp001_short-factual_baseline.yaml
```

## Status

Per-benchmark status, run reports, and per-target analyses live under
[`benchmarks/`](benchmarks/) — each directory carries its own `RUN_REPORT.md`
and `analysis.md`. The cross-benchmark synthesis is at
[`results/summary.md`](results/summary.md).

## Data publication policy

Every artifact published from this repository carries one of three **release
tiers**, defined fully in
[`docs/16-release-tiers-and-redaction-policy.md`](docs/16-release-tiers-and-redaction-policy.md):

- **`RAW_PRIVATE`** — the original, unmodified run output. Kept in a private
  owner-controlled archive and **never** published from this tree.
- **`SANITIZED_PUBLIC`** — a sanitized derivative of a raw run. The scientific
  content is preserved; sensitive fields (e.g. deployment names, internal
  hostnames) are redacted or pseudonymized.
- **`AGGREGATE_AZURE_SAMPLE`** — aggregate-only exports. No per-request rows.

For every sanitized file we publish, the **release manifest**
[`release/public_sanitized_manifest.json`](release/public_sanitized_manifest.json)
records the SHA-256 of the published bytes and the SHA-256 of the original raw
source it derives from. The raw source is preserved, not deleted, so any
published result can be traced back to its untouched origin if questioned.

## Contributing, governance, security

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to file issues and PRs.
- [`GOVERNANCE.md`](GOVERNANCE.md) — single-owner model with documented
  escalation for frozen artifacts.
- [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) — Contributor Covenant 2.1.
- [`SECURITY.md`](SECURITY.md) — how to report a security or data-leakage issue
  (please do **not** open a public issue for these).
- [`SUPPORT.md`](SUPPORT.md) — scope and response expectations.

## License

[MIT](LICENSE)
