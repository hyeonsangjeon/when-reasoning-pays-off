# when-reasoning-pays-off

*Same token price, different bill — a **measured** guide to when reasoning
models earn their cost, and when they just bill for thinking nobody reads.*

[![PR fast CI](https://github.com/hyeonsangjeon/when-reasoning-pays-off/actions/workflows/ci.yml/badge.svg)](https://github.com/hyeonsangjeon/when-reasoning-pays-off/actions/workflows/ci.yml) [![Nightly offline full campaign](https://github.com/hyeonsangjeon/when-reasoning-pays-off/actions/workflows/nightly-full.yml/badge.svg)](https://github.com/hyeonsangjeon/when-reasoning-pays-off/actions/workflows/nightly-full.yml) [![Protected Azure smoke](https://github.com/hyeonsangjeon/when-reasoning-pays-off/actions/workflows/protected-azure-smoke.yml/badge.svg?branch=main)](docs/22-protected-azure-smoke.md) [![Live evidence dashboard](https://img.shields.io/badge/live-evidence%20dashboard-2563eb?logo=github&logoColor=white)](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/charts/?lang=en) [![Docs: 5 languages](https://img.shields.io/badge/docs-EN%20%C2%B7%20KO%20%C2%B7%20JA%20%C2%B7%20ZH%20%C2%B7%20HI-0ea5e9)](https://hyeonsangjeon.github.io/when-reasoning-pays-off/) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Last commit](https://img.shields.io/github/last-commit/hyeonsangjeon/when-reasoning-pays-off)](https://github.com/hyeonsangjeon/when-reasoning-pays-off/commits/main) [![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md) [![Star this repo](https://img.shields.io/github/stars/hyeonsangjeon/when-reasoning-pays-off?style=social)](https://github.com/hyeonsangjeon/when-reasoning-pays-off/stargazers)

**Badge scope:** green **PR fast CI** proves the required deterministic PR
checks, platform wheel matrix, schemas, claims, and fast test surfaces. Green
**Nightly offline full campaign** separately proves machine-verified collection
and isolated execution of all three large campaign suites plus batch and root
tests under locked/current dependency graphs, with credentials blank and
network blocked. Neither badge proves a live Azure/model call. **Protected Azure
smoke** is a third, non-PR signal: it covers one bounded call from an approved
Azure runner with managed identity. It does not publish endpoint, deployment,
tenant, subscription, or request identity. See
[`docs/21-pricing-policy-and-nightly-ci.md`](docs/21-pricing-policy-and-nightly-ci.md)
and [`docs/22-protected-azure-smoke.md`](docs/22-protected-azure-smoke.md).

![Same token price, different bill: a reasoning workload pays the same per-token price but bills extra hidden reasoning tokens, so its total bill is taller.](docs/assets/hero.svg)

> [!TIP]
> **▶ Live evidence dashboard** — interactive static charts for reasoning-effort
> cost, latency, throughput, and the PTU↔PAYG crossover, rendered from sanitized
> public data (no live service calls).
> **[Open the dashboard →](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/charts/?lang=en)**
> &nbsp;·&nbsp; [한국어](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/charts/?lang=ko) · [日本語](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/charts/?lang=ja) · [简体中文](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/charts/?lang=zh-CN) · [हिन्दी](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/charts/?lang=hi)

📖 **Project site:** https://hyeonsangjeon.github.io/when-reasoning-pays-off/ (English, Korean, Japanese, Simplified Chinese, and Hindi).

Start with the [overview essay](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/articles/when-reasoning-pays-off/), then drill into the [short factual work topic](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/articles/when-reasoning-pays-off/topics/short-factual-work/) or inspect the [evidence dashboard](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/charts/?lang=en).

This repository is **offline-first, not offline-only**. The installed CLI's
machine-readable network and cost contract is
[`batch-runner/batch_runner/data/cli_capabilities.v1.json`](batch-runner/batch_runner/data/cli_capabilities.v1.json).
The table below is checked against that file and the live argparse command tree.

<!-- CLI-CAPABILITIES:START -->
| Contract ID | CLI surface | Execution boundary | Network boundary | Cost boundary and guard |
| --- | --- | --- | --- | --- |
| `init` | `reasoning-payoff init` | Offline fixture copy | No runtime network | No provider cost |
| `analyze` | `reasoning-payoff analyze` | Offline analysis | No runtime network | No provider cost |
| `report` | `reasoning-payoff report` | Deterministic offline render | No runtime network | No provider cost |
| `experiment-list` | `reasoning-payoff experiment list` | Read-only local catalog | No runtime network | No provider cost |
| `experiment-describe` | `reasoning-payoff experiment describe` | Read-only local catalog | No runtime network | No provider cost |
| `experiment-run-dry-run` | `reasoning-payoff experiment run --stage dry-run` | Offline plan via validated adapter | No runtime network | No provider cost; immutable plan, refuses protected trees |
| `experiment-run-live` | `reasoning-payoff experiment run --stage live --confirm-cost` | Live cloud-provider call via validated runner | HTTPS to Azure OpenAI | Billed; --confirm-cost plus runner budget/CI guards |
| `sample-init` | `reasoning-payoff sample init` | Offline workspace copy | No runtime network | No provider cost |
| `sample-run-mock` | `reasoning-payoff sample run` (Mock) | Deterministic offline preview | No runtime network | No provider cost |
| `sample-run-ollama-local` | `reasoning-payoff sample run` (Ollama local) | Live local-provider call | Loopback HTTP to local Ollama; no Internet required after model pull | No cloud bill; uses local CPU/GPU |
| `sample-run-ollama-remote` | `reasoning-payoff sample run --allow-remote-ollama` | Live remote-provider call | Remote HTTP only after explicit opt-in | No Azure bill; operator infrastructure may cost money |
| `sample-run-azure` | `reasoning-payoff sample run --confirm-cost` (Azure) | Live cloud-provider call | HTTPS to Azure OpenAI | Billed; CLI + ledger confirmation, hard ceiling, and CI refusal |
| `sample-doctor` | `reasoning-payoff sample doctor` | Workspace/runtime diagnosis; guarded repair | Offline except Ollama metadata; loopback by default, remote only by opt-in | No provider prompt or cloud cost |
| `sample-retry-failed` | `reasoning-payoff sample retry-failed` | Child run for failed attempts only | Same boundary as the parent provider | Same boundary as the parent provider |
<!-- CLI-CAPABILITIES:END -->

## Run one real experiment in five minutes — DATA → IN → EXECUTE → OUT

This path **calls a real model** and shows you every stage: the exact **data**
that goes in, the **model / endpoint / provider** that were selected, the
**command** that executed, and **where the output was written**. (A *provider*
is who serves the model; an *endpoint* is the base web address the request is
sent to; a *model* — also called a *deployment* on Azure — is the specific large
language model that answers.)

The fastest no-cloud-cost path is **[Ollama](https://ollama.com)**, which runs a
small model on your own machine. The five-minute target is the **Warm Ollama**
contract: Ollama is already installed and running, and the exact model is
already present. Ollama installation, service startup, and every
`ollama pull` are outside the timer. Azure OpenAI in Microsoft Foundry is also
supported, but it is a
**billed** call and is refused unless you explicitly confirm the cost. Its
preflight verifies the ledger's snapshot ID, repository path, SHA-256, price
key, and safe model/region/deployment identity, then derives input, cached-input,
and output rates only from that immutable snapshot record.

```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install .

# 1. Copy a ready-to-run workspace (ledger + tiny dataset + .env.example):
reasoning-payoff sample init --provider ollama --out sample-workspace

# 2. (One-time) start Ollama and pull the small default model in another shell:
#    ollama serve   &&   ollama pull qwen2.5:0.5b

# 3. EXECUTE the run and read the OUT artifacts:
reasoning-payoff sample doctor --ledger sample-workspace/ledger.yaml
reasoning-payoff sample run --ledger sample-workspace/ledger.yaml
```

You get a workspace like this — `ledger.yaml` is the **IN** contract (provider,
model, endpoint env-var NAME, dataset shape, limits, cost boundary), and `out/`
holds the **OUT** artifacts of the run:

```text
sample-workspace/
├── ledger.yaml        # IN: what will run, how, and the cost boundary
├── sample.jsonl       # DATA: 3 rows of {id, input, expected?}
├── sample.json        # DATA: the same rows as one JSON array
├── .env.example       # names of the environment variables (never secrets)
└── out/
    ├── latest.json    # atomic pointer to the last completely published run
    ├── .reasoning-payoff-experiment-owned
    └── runs/
        └── <utc>_<ledger8>_<input8>_<random8>/
            ├── run.json       # provider/model, timings, usage, lineage
            ├── records.jsonl  # one row per attempted request
            ├── summary.md     # short, safe answer preview
            ├── manifest.json  # code/runtime/input/provider/pricing provenance
            └── artifacts.sha256
```

> This is an **illustrative live sample — not the published benchmark**; there
> is no quality judge and no comparable reasoning-effort sweep. It proves the
> plumbing end to end, not a scientific result. Sample output is gitignored and
> fixed to immutable directories under the workspace's owned `out/runs/`.
> A later run never rewrites an earlier run.

`sample doctor` reports package/runtime, workspace ownership, immutable output
structure, and lock health. For Ollama it also performs the same no-proxy,
no-redirect local transport checks used by a run and reports the runtime version,
installed model digest/details, and content-free hashes of template/model
metadata. Set the optional `expected_model_digest: sha256:...` ledger field to
pin that identity; a mismatch stops before any prompt call. A stale lock is
removed only with `--repair-stale-lock` after same-host liveness is proven.

Prefer no install first? `reasoning-payoff sample init --provider mock` uses a
deterministic **offline preview** provider that makes no network call — useful
to see the exact artifact shapes before you run a live model.

**Full walkthrough, data-shape tables, Azure setup, cost guard, troubleshooting,
and exit codes:** [docs/20 — Run one real experiment in five
minutes](docs/20-five-minute-experiment-run.md). To browse all 20 committed
experiments and their DATA/IN/EXECUTE/OUT view without running anything, use
`reasoning-payoff experiment list` and `reasoning-payoff experiment describe
<id>`.

> **Two different five-minute paths.** The command above *calls a model*. The
> section just below (`reasoning-payoff analyze`) instead *analyzes usage you
> already recorded* and makes **no** service call — use it when you have usage
> logs and want a provenance report rather than a fresh model answer.

## Reproducibility service objectives

These contracts do not overlap. A fast sample is functional verification, not
benchmark reproduction.

| Contract | Exact start → end | Prerequisites and exclusions | Expected artifacts | Success does **not** prove |
| --- | --- | --- | --- | --- |
| **Cold Mock** | Start with tracked files available; materialize a clean checkout-equivalent copy → create a fresh virtual environment → build and install the minimal wheel with `--no-cache-dir` → run `--help`, Mock init/run → inspect schemas, checksums, immutable run files, and `latest.json`. | CPython 3.11–3.13, Git, and package-index access. Remote clone/fetch and runner provisioning are excluded; package and virtual-environment caches are not reused. | `cold-mock-timing.json` plus the Mock run artifact set. | Model quality, provider connectivity, benchmark reproduction, or a universal install time. |
| **Warm Ollama** | Start immediately before `sample run`, after `sample doctor` reports `warm_prerequisites.ready: true` → stop when the immutable run and `latest.json` are published. | Minimal wheel installed, Ollama service running, exact tag already installed, optional digest matched. Ollama install, service startup, and model pull are excluded and never automated. | Live sample `run.json`, `records.jsonl`, `summary.md`, `manifest.json`, checksums, and pointer. | Published benchmark results, a quality score, comparable effort sweep, or performance on other hardware. |
| **Full research rerun** | Start after environment, Azure access/quota, deployment, pricing, prompts, and datasets are pinned → execute all planned benchmark/judge cells → aggregate and regenerate analyses and charts. | `.[all,dev]`, funded Azure access, quota, POSIX campaign tools, and operator review. Provisioning, quota acquisition, and private raw-archive access are external prerequisites. | Raw run/judge records, analyses, public sanitized aggregates, manifests, and regenerated charts. | Byte-identical provider responses or access to owner-only `RAW_PRIVATE` bytes. |

**Thresholds.** Cold Mock is machine-enforced at **≤300 seconds** only on the
cache-disabled `ubuntu-latest` / CPython 3.13 GitHub-hosted reference job. Warm
Ollama has a **≤300-second operator target** under the stated warm
prerequisites. Full research reruns have **no wall-clock SLO** because service
quota and live-call latency dominate; completion means every planned cell has a
terminal record and every aggregate/chart gate passes. The measured reference
SLO is evidence about that CI environment, not an expectation for every
individual machine.

Generate the same structured Cold Mock report locally:

```bash
python scripts/measure_cold_mock.py \
  --threshold-seconds 300 --output cold-mock-timing.json
```

The JSON records commit, source cleanliness, OS/Python/architecture, each
checkout/build/install/help/init/run/inspection duration, total, threshold, and
pass/fail. It contains no absolute paths, usernames, endpoints, or secrets.

## Platform support

### Minimal core and sample CLI

| Platform | CPython | Contract |
| --- | --- | --- |
| Ubuntu/Linux | 3.11–3.13 | Supported; non-editable wheels are tested on `ubuntu-latest` at 3.11 and 3.13. |
| macOS | 3.11–3.13 | Supported; non-editable wheels are tested on `macos-latest` at 3.11 and 3.13. |
| Windows | 3.11–3.13 | Supported for the minimal CLI, Mock/Ollama sample, retry/doctor, and catalog; non-editable wheels are tested on `windows-latest` at 3.11 and 3.13. |
| Other Python/platforms | — | Not in the support contract. `requires-python` is bounded to `<3.14`; future Python releases require an explicit compatibility update. |

The matrix installs no analysis/Azure extras and makes no provider call. It
checks help, Mock init/run/retry/doctor, experiment list/describe, immutable
manifests/checksums, and fail-fast optional extras. Lock creation remains
exclusive on every platform. Windows uses process handles instead of POSIX
signals for liveness and verifies file identity around no-follow-sensitive
opens. CPython cannot directory-`fsync` on Windows, so atomic same-directory
replace is supported but the stronger POSIX power-loss durability boundary is
not claimed.

### Full research campaign

| Platform | Support | Boundary |
| --- | --- | --- |
| Linux | Reference campaign platform | CPython 3.11 release lock; POSIX shell and GNU-compatible utilities expected. |
| macOS | Operator-supported, not the release-lock reference | POSIX and `fcntl` paths are available; dependency resolution is platform-specific. |
| Windows | **Not supported for the full campaign** | Use WSL/Linux. Campaign and release paths depend on Bash/POSIX commands, `fcntl` locking in `scripts/measure_max_output_tokens_sweep.py`, and SHA-256 shell utilities in validation workflows. |

This Windows boundary applies to the full campaign, not the minimal installed
CLI.

## Generate an offline report in under five minutes

CPython 3.11–3.13, no credentials, no service calls, and no telemetry:

```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install ".[analysis]"
reasoning-payoff analyze examples/five-minute/usage.jsonl \
  --workload examples/five-minute/workload.yaml \
  --out report
```

The default installation is deliberately the small core/sample surface. Choose
an extra only for the capability you need:

| Install | Capability |
| --- | --- |
| `pip install .` | Help, fixture/sample init, Mock and Ollama sample run/retry/doctor, and read-only experiment list/describe |
| `pip install ".[analysis]"` | Core plus offline `analyze`/`report` and analysis libraries |
| `pip install ".[azure]"` | Core plus OpenAI/Azure Identity for guarded Azure samples |
| `pip install ".[all]"` | Analysis and Azure capabilities together |

Missing or older optional packages stop at the command boundary with the exact
extra-install command. Azure dependency checks happen before endpoint
resolution, credential setup, token acquisition, or network access.

Open `report/report.html` directly from disk. The same run also creates
`report.json`, `report.md`, and review-only `policy.json`. See the
[numbered method guide](docs/19-five-minute-provenance-report.md) or the
[standalone Pages guide](https://hyeonsangjeon.github.io/when-reasoning-pays-off/guides/five-minute-report/)
([source](docs/guides/five-minute-report/)) for the strict JSONL/YAML
contracts, privacy boundary, interpretation rules, exit codes, and BYOW
workflow.

<!-- CLAIM-INTEGRITY:START current-headlines -->
## TL;DR — what the current measurements say

**The current evidence is workload-specific and descriptive.** In the current
GPT-5.2 short-factual cohort, `none` and `xhigh` cost
**$0.000587 → $0.000598 per request
(1.02x)**, while mean judge quality was **1.95 →
1.95**. Mean reasoning tokens were
**0 at `none`,
1.35 at `high`, and
0.62 at `xhigh`**. The measured floor is
`none`; zero-sample cells are excluded from current public claims.

On the multi-step benchmark, mean judge quality was
**1.5 for the GPT-4o baseline and
2.0 for GPT-5.2 at `none`**. Both the **model** and
**effort** dimensions changed, so this comparison does not isolate an
effort-only causal effect. Within GPT-5.2, `none` already reached the measured
quality ceiling in this cohort; higher effort increased cost without improving
that aggregate score.

| Current short-factual cost | Current short-factual quality |
| --- | --- |
| ![Benchmark 01 cost per request remains nearly flat from none to xhigh reasoning effort](docs/assets/benchmark-01-cost-per-request.png) | ![Benchmark 01 judge quality remains nearly flat across measured GPT-5.2 effort levels](docs/assets/benchmark-01-quality.png) |

- **Treat effort as a workload-specific tuning parameter, not a quality
  guarantee.** Run an evaluation before changing production policy.
- **Separate model changes from effort changes.** A cross-model comparison is
  useful evidence, but it is not an effort-only experiment.
- **Trace every headline.** The values above resolve through the versioned
  [public claim contract](batch-runner/batch_runner/data/public_claims.v1.json)
  to canonical analysis and public chart JSON.

<sub>Current headline values are generated only inside this marker block.
Historical benchmark, result, and blog inputs remain read-only.</sub>
<!-- CLAIM-INTEGRITY:END current-headlines -->

## Try it in 30 seconds

| You want to… | Do this | Azure needed? |
| --- | --- | :---: |
| **See the evidence** | [Open the live dashboard →](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/charts/?lang=en) — interactive cost / quality / latency / crossover charts | ❌ |
| **Read the story** | [Overview essay →](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/articles/when-reasoning-pays-off/) in English · 한국어 · 日本語 · 简体中文 · हिन्दी | ❌ |
| **Run one real experiment** | `reasoning-payoff sample init --provider ollama --out sample-workspace && reasoning-payoff sample run --ledger sample-workspace/ledger.yaml` — a small real local model run (a few rows), no cloud cost ([guide](docs/20-five-minute-experiment-run.md)) | ❌ (Ollama) |
| **Generate a provenance report** | `reasoning-payoff analyze examples/five-minute/usage.jsonl --workload examples/five-minute/workload.yaml --out report` | ❌ |
| **Verify it runs locally** | `bash scripts/verify_setup.sh` — no credentials, ~10 s to a green check | ❌ |
| **Run the test suite** | `pip install -r requirements-dev.txt && pytest -q -m "not adaptive_calibration" batch-runner/tests/` | ❌ |
| **Reproduce the numbers** | [Reproducing these measurements ↓](#reproducing-these-measurements) | ✅ |

> If this saved you a migration post-mortem, a ⭐ helps other teams find it.

## Contents

- [What this repo is](#what-this-repo-is) · [Terms you will see](#terms-you-will-see)
- [The question](#the-question) · [Short answer](#short-answer) · [Which customer are you?](#which-customer-are-you)
- [What's here](#whats-here) — [docs](#documentation), [code and data](#code-and-data)
- [Five-minute offline report](#generate-an-offline-report-in-under-five-minutes) · [Method guide](docs/19-five-minute-provenance-report.md)
- [Run one real experiment](#run-one-real-experiment-in-five-minutes--data--in--execute--out) · [Experiment guide](docs/20-five-minute-experiment-run.md)
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
size capacity, or debug a cost / latency / throttling change after a model
migration.

> **Research artifact with a stable offline tool boundary.** This repository
> publishes reproducible benchmarks, methodology, sanitized result slices, and
> the versioned `reasoning-payoff init / analyze / report` interface. The CLI
> is credential-free and local; there is no hosted service, live-provider
> command, uploader, telemetry server, or SLA. See
> [`SUPPORT.md`](SUPPORT.md), [`SECURITY.md`](SECURITY.md), and
> [`docs/16-release-tiers-and-redaction-policy.md`](docs/16-release-tiers-and-redaction-policy.md)
> for scope, security reporting, and how published data is sanitized.

## Terms you will see

| Term | Plain-language meaning |
| --- | --- |
| **Reasoning model** | A model that runs internal reasoning steps before producing a visible response (e.g. GPT-5.2). Those internal tokens are billed at the output rate but never returned to the caller. |
| **`reasoning_effort`** | Per-request knob whose supported values vary by model and API (`none`, `minimal`, `low`, `medium`, `high`, `xhigh`, and newer-model `max` are possible). Treat it as a measured tuning input, not a quality guarantee. |
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
- `reasoning_effort` support is model-specific. Start from the lowest supported
  level and raise it only when a task-specific quality evaluation justifies it.
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
| [`docs/18-agentic-loop-budget-governance.md`](docs/18-agentic-loop-budget-governance.md) | Operator lever L6 — governing unbounded agentic loops (thresholds, intervention, evals, traceability, governance) |
| [`docs/19-five-minute-provenance-report.md`](docs/19-five-minute-provenance-report.md) | Official offline CLI method: install, sample/BYOW contracts, artifacts, privacy, interpretation, exit codes, and timing |
| [`docs/20-five-minute-experiment-run.md`](docs/20-five-minute-experiment-run.md) | Run one real model experiment (Ollama local or Azure billed): DATA/IN/EXECUTE/OUT flow, ledger contract, data shapes, cost guard, output tree, exit codes, and mock preview |
| [Standalone five-minute Pages guide](https://hyeonsangjeon.github.io/when-reasoning-pays-off/guides/five-minute-report/) | Dependency-free web rendering of the same command and contract guide |

### Code and data

- [`batch-runner/`](batch-runner/) — Python library and official
  `reasoning-payoff` CLI: strict usage/workload contracts, deterministic local
  reports, decision calculators, observability schema, and release-tier helpers.
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
- [`schemas/`](schemas/) — JSON Schemas for the observability, sample-run, and
  release-manifest contracts. CI's **schema meta-validation** checks that every
  schema document is valid Draft 7; the separate **artifact instance
  conformance** gate validates each mapped committed provider ledger, sample
  JSON/JSONL, usage/workload sample, and public manifest. Schemas whose governed
  instances are intentionally private or not yet committed require an explicit
  exemption in [`scripts/check_schema_conformance.py`](scripts/check_schema_conformance.py).
- [`pricing/`](pricing/) — Pricing snapshots used by the cost calculator (PAYG
  and PTU density tables).
- [`release/public_sanitized_manifest.json`](release/public_sanitized_manifest.json)
  — the **release manifest**: for every published artifact, the SHA-256 of the
  sanitized file and the SHA-256 of the original raw source it derives from.

## Operator levers (L1–L5)

![A rate-limiting explainer: the same burst of requests, two outcomes. Without a limiter the burst hits the API directly and it sheds load as 503; with a token-bucket limiter, within-limit requests reach the API while over-limit requests get a 429 returned to the caller, so the API stays healthy.](docs/assets/429-rate-limit.gif)

<sub>*The same burst of requests, two outcomes: without a limiter the API sheds load as 503; with a token-bucket limiter, over-limit requests get a **429 returned to the caller** so the API stays healthy. **L2** below is where you choose that spillover behavior.*</sub>

If you operate a live deployment, [`docs/09-operator-guide-one-page.md`](docs/09-operator-guide-one-page.md)
is the one-page reference. The six levers, in plain English (L1–L5 tune a single call or deployment; **L6** governs the multi-call loop):

| Lever | What you change | Why it matters |
| --- | --- | --- |
| **L1** First-token timeout | Per-deployment `first_token_timeout_ms` (e.g. `3000`) | Decides how long a request waits while the primary is saturated before the client gives up or reroutes. Shorter timeouts shrink tail latency in the saturated window. |
| **L2** Spillover policy | Native Azure spillover vs custom proactive router | Native spillover reacts to 429s on a PTU primary; a custom router can act earlier but generates extra 429s when its heuristic mis-fires. Pick based on topology, not preference. |
| **L3** System-prompt stability | Track `sha256(system_prompt)` per request and alarm on unintended changes | A single byte change flushes the prompt cache, so the next requests bill the full input rate until the cache re-warms. Migration-era "cache hit dropped" symptoms often resolve to this. |
| **L4** Reasoning effort tuning | Per-task `reasoning_effort` default | Higher effort spends more reasoning tokens without necessarily lifting quality. Default new traffic to `minimal` (or `none` where supported); raise only where measured quality justifies it. |
| **L5** `max_output_tokens` tightening | Tighten the per-call cap to `ceil((p99_visible + p99_reasoning) × 1.15)` | On PTU, this value is an admission-time reservation. Inflated caps silently reduce concurrency and pull 429 onset earlier. |
| **L6** Loop / budget governance | Per-task step cap + cumulative cost ceiling, a fail-closed circuit-breaker, eval-gated continuation, and per-step cost traceability | L1–L5 bound a single call; **L6 bounds the loop.** Caps runaway agentic spend, makes per-loop cost traceable, and intervenes before overrun. See [`docs/18`](docs/18-agentic-loop-budget-governance.md). |

Full mechanism, evidence, and Azure docs links for each lever are in
[`docs/09`](docs/09-operator-guide-one-page.md). L6's full treatment — step
caps, cost ceilings, the circuit-breaker, eval-gated escalation, and per-step
traceability — is in
[`docs/18-agentic-loop-budget-governance.md`](docs/18-agentic-loop-budget-governance.md).

## Methodology summary

We use three benchmark task types representative of common use cases. For each,
we sweep `reasoning_effort` across its full ladder of tiers and run multiple
repeats per (sample, effort) combination. We capture the full `response.usage`
object — input, cached, reasoning, and output tokens separately — and pair
token measurements with a quality evaluation.

Detailed methodology: [`docs/05-methodology.md`](docs/05-methodology.md).

## Reproducing these measurements

These three reproducibility levels are distinct:

| Level | Availability | Meaning |
| --- | --- | --- |
| **Public evidence verification** | **Publicly verifiable** | Anyone can inspect the sanitized or aggregate evidence, verify published-byte hashes, and rerun public analysis/reporting code from public inputs. |
| **Same-method rerun on a new environment** | **Available with your own environment/provider access** | Run the committed method against a new local Ollama or Azure environment and compare the result profile within reported variance. This is not a byte-identical replay. |
| **Exact original raw reproduction** | **Not publicly available; owner-auditable only** | The original `RAW_PRIVATE` bytes and private redaction inputs stay in the owner-controlled archive. `source_raw_sha256` is a commitment to those bytes, not public access to them and not enough to reconstruct or independently verify the raw-to-public transform. |

The complete scope statement is in
[`docs/05-methodology.md` §7](docs/05-methodology.md#7-reproducibility-requirements).
“Publicly verifiable” refers only to the first level. “Owner-auditable” refers
to the private archive and transformation audit; the terms are not
interchangeable.

**No Azure account is needed except for an optional Azure live rerun.**

### Path A — verify the public evidence (nothing to install)

The [live dashboard](https://hyeonsangjeon.github.io/when-reasoning-pays-off/blog/charts/?lang=en),
[`results/summary.md`](results/summary.md), and the committed `runs/` +
`analysis.md` under each [`benchmarks/`](benchmarks/) directory already contain
every published number.

### Path B — exercise the method locally, no cloud account

```bash
git clone https://github.com/hyeonsangjeon/when-reasoning-pays-off.git
cd when-reasoning-pays-off

# CPython 3.11-3.13 required. If `python` is missing, use `python3` below.
python3 -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
python -m pip install ".[analysis]"

# Generate the four-artifact provenance bundle:
reasoning-payoff analyze examples/five-minute/usage.jsonl \
  --workload examples/five-minute/workload.yaml \
  --out report
```

Open `report/report.html`, then explore or dry-run any experiment — still no
cloud account and no tokens spent:

```bash
python experiments/examples/describe_all.py       # the full input → variable → output catalog
python experiments/examples/pure_functions.py     # the deterministic primitives, zero network
python -m scripts.run_benchmark \
  --experiment experiments/exp001_short-factual_baseline.yaml --dry-run --allow-dirty
```

Run the unit-test suite (add the dev tools first):

```bash
pip install -r requirements-dev.txt
pytest -q -m "not adaptive_calibration" batch-runner/tests/
bash scripts/run_nightly_offline_tests.sh
```

The nightly command uses separate pytest processes and blocks socket/DNS access.
For pricing-aware campaign dry-runs, select
`--pricing-policy historical-replay`; it verifies the immutable pinned snapshot
but ignores wall-clock age and refuses any non-dry execution. Commands capable
of billed work default to `live-measurement`, which requires a snapshot no more
than 90 days old and fails before endpoint or credential resolution. Full
policy and badge contracts are documented in
[`docs/21`](docs/21-pricing-policy-and-nightly-ci.md).

> A normal `pip install .` exposes the minimal official CLI and packaged sample
> resources without analysis or Azure SDKs. Install `.[analysis]`, `.[azure]`,
> or `.[all]` for those optional surfaces. Editable install remains suitable for
> contributors; measurement runners are still invoked as
> `python -m scripts.<runner>` from the repository root.

### Path C — same-method benchmark rerun against Azure

This needs Azure OpenAI access with a **GPT-5.2 deployment**. The repo uses
**Entra ID authentication** — no API keys in `.env`; run `az login` once and the
cached token is auto-refreshed by `DefaultAzureCredential`.

```bash
python -m pip install ".[azure]"
az login                       # one-time
cp .env.example .env           # then edit the endpoint + deployment names

# Drop --dry-run to spend real tokens. Every runner takes --experiment <yaml>:
python -m scripts.run_benchmark --experiment experiments/exp001_short-factual_baseline.yaml
```

See [`experiments/README.md`](experiments/README.md) for the full input →
variable → output catalog, the blog-article ↔ experiment map, and a one-call
Python interface (`experiments.run(...)`) that picks the right runner for you.

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
published result can be traced back to its untouched origin by the owner if
questioned. Public readers can verify `sanitized_sha256` against published
bytes. They cannot verify `source_raw_sha256`, recover the private source, or
independently replay the raw-to-sanitized transformation without `RAW_PRIVATE`
and the owner-only redaction inputs.

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
