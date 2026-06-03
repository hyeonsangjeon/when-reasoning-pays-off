# when-reasoning-pays-off

*Same token price, different bill. A practical guide to deciding when
reasoning models earn their cost.*

> **Research artifact, not a production service.** This repository
> publishes reproducible benchmarks, methodology, decision tools, and
> sanitized result slices. It is not a hosted product, has no SLAs, and
> does not ship a managed library. See
> [`SUPPORT.md`](SUPPORT.md), [`SECURITY.md`](SECURITY.md), and
> [`docs/16-release-tiers-and-redaction-policy.md`](docs/16-release-tiers-and-redaction-policy.md)
> for scope, security reporting, and the data-publication policy.

---

## The Question

GPT-4o → GPT-5.2 migrations often show rising token costs. Reasoning
models charge for thinking — tokens consumed during the model's
internal reasoning steps, billed but invisible in the final output.
The question isn't whether reasoning models are useful (they are), but
**when their cost is justified**.

This repo measures that question with reproducible benchmarks.

## Short Answer

- Reasoning tokens are billed but don't appear in the response. Always
  measure them.
- Not every task benefits from reasoning. Short factual answers,
  structured-input-to-natural-language synthesis, and simple
  classification often don't need it.
- `reasoning_effort` (minimal / low / medium / high) is the primary
  cost lever.
- Prompt caching behaves differently on reasoning models. Why hit
  ratios change depends on **architecture** (multi-node orchestration
  vs single-call ReAct) and **consumption model** (PAYG vs PTU). The
  repo enumerates testable hypotheses in
  [`docs/07-cache-hit-degradation.md`](docs/07-cache-hit-degradation.md).
- For PAYG users: cutting reasoning tokens is direct cost reduction.
  For PTU users: the same cut is throughput gain at fixed cost. The
  repo measures both.
- Routing different task types to different effort levels is the
  architectural answer at scale, regardless of consumption model.

## Which Customer Are You?

**PAYG users.** You pay per token. Reducing tokens reduces your bill.
The benchmark cost curves under [`results/cost-curves/`](results/cost-curves/)
show the dollar delta per request at each effort level.

**PTU users.** You pay for fixed capacity. Reducing tokens doesn't
change your bill — it lets the same PTU capacity serve more requests,
or it cushions latency spikes during peak load. PTU users investigating
cache hit ratio drops or earlier 429 onset after migrating to a
reasoning model should see
[`docs/07-cache-hit-degradation.md`](docs/07-cache-hit-degradation.md),
which ranks the testable hypotheses in diagnostic priority **A / E /
C / I / D / G_weak / H′ / B / F** — most notably **I**
(`max_output_tokens` as an admission-time PTU reservation, not a soft
cap; inflating it for reasoning headroom silently reduces concurrency),
**G_weak** (transient cache hit dip under near-saturation PTU
utilization, recovering as load drops), and **H′** (input-side
architecture shift during single-call ReAct migration — tool
definitions, structured-output schemas, and retrieval placement
changing the cacheable prefix).

## What's Here

### Documentation

| Doc | Topic |
| --- | --- |
| [`docs/04-decision-framework.md`](docs/04-decision-framework.md) | Task → effort decision framework |
| [`docs/05-methodology.md`](docs/05-methodology.md) | How we measured (the reproducibility contract — frozen) |
| [`docs/07-cache-hit-degradation.md`](docs/07-cache-hit-degradation.md) | Hypotheses for cache hit ratio drop on reasoning models |
| [`docs/08-customer-simulation-findings.md`](docs/08-customer-simulation-findings.md) | PTU + single-call ReAct: pattern, mechanisms, leverages |
| [`docs/09-operator-guide-one-page.md`](docs/09-operator-guide-one-page.md) | Operational quick-reference (L1–L5 levers for PTU + reasoning) |
| [`docs/10-ptu-admission-controller.md`](docs/10-ptu-admission-controller.md) | Header-driven admission controller design |
| [`docs/11-multi-worker-cooldown.md`](docs/11-multi-worker-cooldown.md) | Multi-worker cooldown coordination |
| [`docs/12-prompt-cache-key-policy.md`](docs/12-prompt-cache-key-policy.md) | `prompt_cache_key` policy library + sizing runbook |
| [`docs/13-ptu-vs-payg-decision-runbook.md`](docs/13-ptu-vs-payg-decision-runbook.md) | PTU vs PAYG decision calculator + runbook |
| [`docs/14-observability-schema.md`](docs/14-observability-schema.md) | Canonical per-request / per-cell record contract |
| [`docs/15-spec-vs-inference-taxonomy.md`](docs/15-spec-vs-inference-taxonomy.md) | Two-tier citation taxonomy (Tier 1 official spec vs Tier 2 operational inference) |
| [`docs/15-spec-vs-inference-taxonomy.examples.md`](docs/15-spec-vs-inference-taxonomy.examples.md) | Worked examples of the citation taxonomy |
| [`docs/16-release-tiers-and-redaction-policy.md`](docs/16-release-tiers-and-redaction-policy.md) | Three-tier release classification and redaction rules |
| [`docs/17-foundry-packaging-relationship.md`](docs/17-foundry-packaging-relationship.md) | Relationship to Pages / Medium / arXiv / Azure AI Foundry sample |

### Code and data

- [`batch-runner/`](batch-runner/) — Python library: decision
  calculators, `prompt_cache_key` policy, observability schema,
  release-tier helpers.
- [`scripts/`](scripts/) — Measurement and analysis pipeline.
- [`benchmarks/`](benchmarks/) — Per-task measurement targets with
  sanitized run captures and per-target analysis. Tier classification
  per `docs/16`.
- [`results/`](results/) — Cross-benchmark synthesis and charts. See
  [`results/summary.md`](results/summary.md).
- [`schemas/`](schemas/) — JSON Schemas for the observability and
  release-manifest record contracts.
- [`pricing/`](pricing/) — Pricing snapshots used by the cost
  calculator (PAYG and PTU density tables).

## Methodology Summary

Three benchmark task types representative of common use cases. For
each, we sweep `reasoning_effort` across four levels and run multiple
repeats per (sample, effort) combination. We capture the full
`response.usage` object — input, cached, reasoning, and output tokens
separately — and pair token measurements with quality evaluation.

Detailed methodology: [`docs/05-methodology.md`](docs/05-methodology.md).

## Reproducing These Measurements

Requires Azure OpenAI access with a GPT-5.2 deployment.

```bash
git clone https://github.com/hyeonsangjeon/when-reasoning-pays-off.git
cd when-reasoning-pays-off

# Set up Python env
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Authenticate to Azure (Entra ID, one-time)
az login

# Configure endpoint and deployment names
cp .env.example .env
# Edit .env with your Azure OpenAI endpoint and deployment names.

# Run a benchmark
python scripts/run_benchmark.py experiments/exp001_short-factual_baseline.yaml
```

This repo uses **Entra ID authentication** — no API keys in `.env`.
Run `az login` once before any script; the cached token is
auto-refreshed by `DefaultAzureCredential`.

You do not need Azure credentials to run the
`batch-runner/tests/` unit-test suite:

```bash
pytest -q -m "not adaptive_calibration" batch-runner/tests/
```

## Status

Per-benchmark status, run reports, and per-target analyses live under
[`benchmarks/`](benchmarks/) — each directory carries its own
`RUN_REPORT.md` and `analysis.md`. The cross-benchmark synthesis is at
[`results/summary.md`](results/summary.md).

## Data publication policy

Every artifact published from this repository carries a release-tier
label per [`docs/16-release-tiers-and-redaction-policy.md`](docs/16-release-tiers-and-redaction-policy.md):

- **`RAW_PRIVATE`** — original, unmodified run output. **Forbidden in
  the public tree.** Held in a private owner-controlled archive.
- **`SANITIZED_PUBLIC`** — sanitized derivative. Same scientific
  content, sensitive fields redacted or pseudonymized.
- **`AGGREGATE_AZURE_SAMPLE`** — aggregate-only export. No per-request
  rows.

The redaction policy is **HARD: raw experiment data is never deleted**;
it is archived and linked by SHA-256 from every public derivative.

## Future surfaces

[`docs/17-foundry-packaging-relationship.md`](docs/17-foundry-packaging-relationship.md)
reserves scope for, but does not implement, three future surfaces:

- A **GitHub Pages dashboard / blog** that consumes only
  `SANITIZED_PUBLIC` and `AGGREGATE_AZURE_SAMPLE` artifacts.
- **Medium** syndication as a derivative reach channel, with canonical
  URLs pointing back at the Pages article.
- A **distinct arXiv** preprint, if and when the owner elects to
  publish one (separate scholarly manuscript, not a paste of blog
  copy).

A downstream **Azure AI Foundry sample repository** may package the
decision tools and aggregate exports for operators. The relationship
contract is in `docs/17`.

## Contributing, governance, security

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to file issues and PRs.
- [`GOVERNANCE.md`](GOVERNANCE.md) — single-owner model with documented
  escalation for frozen artifacts.
- [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) — Contributor Covenant
  2.1.
- [`SECURITY.md`](SECURITY.md) — how to report a security or
  data-leakage issue (not by opening a public issue).
- [`SUPPORT.md`](SUPPORT.md) — scope and response expectations.

## License

[MIT](LICENSE)
