# Editorial Data Inventory — Public Blogging Prep

> **Status:** planning artifact for the GitHub Pages blogging/articles section.
> No articles are published yet. This file maps the **public, sanitized** data
> and the visualization candidates available for future articles.
>
> **Sources:** public-mirror tracked files only, under `benchmarks/`,
> `results/`, `docs/`, `scripts/`, `pricing/`, `schemas/`, `release/`.
> **Excluded:** any private operator data, unpublished files, secrets, endpoint
> names, or private identifiers. Each measured number cites the exact public
> file path it comes from; no claim or number is altered.
>
> The machine-readable companion is
> [`chart-candidates.json`](./chart-candidates.json). One derived,
> aggregate-safe stub is provided at
> [`data/reasoning_effort_summary.csv`](./data/reasoning_effort_summary.csv)
> (an exact, unrounded re-projection of fields already in
> `benchmarks/<id>/analysis.json`; metric columns are left blank for cells with
> `n_used=0`).
>
> **On numbers:** inline figures may be rounded for readability and always cite
> the exact public file path that holds the full-precision value; no measured
> number or claim is altered.

## How to read the readiness flag

| Flag | Meaning |
| --- | --- |
| `ready` | A public CSV/JSON exists that a chart can be rendered from directly. |
| `needs-light-aggregation` | Public source exists but a small tidy/aggregation step is needed (numbers live in JSON or many run files). |
| `needs-new-chart` | The data exists but no chart-shaped CSV does yet; a new derivation from public inputs is required. |
| `not-ready` | Insufficient public data to chart honestly. |

## Reusable chart families

`cost-bars` · `quality-bars` · `latency-curve` · `throughput-gain` ·
`token-composition` · `ptu-payg-crossover` · `cache-hit-degradation` ·
`ttft-degradation` · `retry-after-cdf` · `spillover-recovery-timeline` ·
`replay-predicted-vs-observed` · `evidence-gate-flow` · `i18n-locale-graph`

See `chart_families[]` in `chart-candidates.json` for axis/type detail.

---

## T01 — When Reasoning Does Not Pay: Short Factual Answers

- **Public data:** `benchmarks/01-short-factual/analysis.json`,
  `results/cost-curves/benchmark-01-{cost-per-request,quality,latency,throughput-gain}.csv`
- **Charts:** cost-bars, quality-bars, latency-curve
  (`results/cost-curves/benchmark-01-*.csv`)
- **Why it exists:** quantify whether `reasoning_effort` helps a task with
  little to reason about.
- **Reader should learn:** if quality is already near ceiling, more reasoning
  buys cost and latency, not accuracy.
- **Observed (public):** cost `0.000663 → 0.005043` USD/req across
  minimal→xhigh (~7.6×); latency `1112.8 → 3088.0` ms; judge score stays
  `1.73–1.88` (no monotonic gain) — `results/cost-curves/benchmark-01-*.csv`.
- **Takeaway:** default short-factual workloads to `minimal`/`none`.
- **Readiness:** `ready`
- **Safety:** PAYG lens; pricing from `pricing/azure-openai-payg-2026-05.yaml`;
  N=20,R=3 ⇒ use std, not CI.

## T02 — When Reasoning Pays: Multi-Step Problems

- **Public data:** `benchmarks/02-multi-step-reasoning/analysis.json`,
  `results/cost-curves/benchmark-02-*.csv`
- **Charts:** quality-bars, cost-bars, latency-curve
- **Why it exists:** contrast case to T01 where intermediate reasoning is
  load-bearing.
- **Reader should learn:** how much quality a little reasoning adds, and at
  what cost.
- **Observed (public):** gpt-4o baseline judge score `1.5`; gpt-5.2 reaches
  `2.0` at none/medium/high/xhigh; the `high` cell costs `0.00125` USD/req vs
  `0.000798` baseline — `benchmarks/02-multi-step-reasoning/analysis.json`,
  `results/cost-curves/benchmark-02-cost-per-request.csv`.
- **Takeaway:** modest effort closes a real quality gap at small marginal cost.
- **Readiness:** `ready`
- **Safety:** the gpt-5.2 `minimal` cell has `n_used=0` — exclude, don't plot 0.

## T03 — Reasoning Inside a Tool-Using Agent

- **Public data:** `benchmarks/03-tool-using-agent/analysis.json`
  (incl. `tool_efficiency_breakdown`), `results/cost-curves/benchmark-03-*.csv`
- **Charts:** quality-bars, cost-bars, latency-curve
- **Why it exists:** measure reasoning value when the model selects/calls tools.
- **Reader should learn:** whether extra reasoning helps once tools do the work.
- **Observed (public):** quality near-saturated `1.85–2.0`; latency
  `2902 → 3770` ms (none→xhigh); cost ~`0.0028–0.0035` USD/req —
  `benchmarks/03-tool-using-agent/analysis.json`,
  `results/cost-curves/benchmark-03-latency.csv`.
- **Takeaway:** prefer lower effort near the quality ceiling; reserve high
  effort for hard tool-planning.
- **Readiness:** `ready` (core); tool-efficiency angle `needs-light-aggregation`.
- **Safety:** PAYG lens; the gpt-5.2 `minimal` cell has `n_used=0` — exclude it.

## T04 — The Invisible Reasoning-Token Tax

- **Public data:** `results/token-composition/benchmark-0{1,2,3}-tokens.csv`,
  `pricing/azure-openai-payg-2026-05.yaml`
- **Charts:** token-composition (stacked bar: cached / non-cached input /
  output / reasoning)
- **Why it exists:** show the billed-but-invisible reasoning tokens behind the
  cost gap a per-token-price comparison hides.
- **Reader should learn:** where the extra bill comes from when visible output
  barely changes.
- **Observed (public):** benchmark-01 `mean_reasoning_tokens` `3.66 → 311.47`
  while `mean_output_tokens` only `12.67 → 17.88` —
  `results/token-composition/benchmark-01-tokens.csv`. Reasoning is billed at
  the output rate (`reasoning_per_1m_usd = output_per_1m_usd = 14.00`) —
  `pricing/azure-openai-payg-2026-05.yaml`.
- **Takeaway:** always capture `response.usage` reasoning tokens separately.
- **Readiness:** `ready`

## T05 — PTU vs PAYG: Where the Lines Cross

- **Public data:** `results/cost-curves/benchmark-0{1,2,3}-throughput-gain.csv`,
  `pricing/azure-openai-{payg,ptu}-2026-05.yaml`,
  `pricing/ptu-density-2026-05.yaml`, `scripts/ptu_sizing.py`,
  `docs/13-ptu-vs-payg-decision-runbook.md`,
  `docs/assets/ptu-vs-payg-crossover.svg`
- **Charts:** throughput-gain (`ready`); ptu-payg-crossover line
  (`needs-new-chart`)
- **Why it exists:** reasoning tokens consume PTU capacity; teams must choose
  consumption vs reserved billing.
- **Reader should learn:** at what utilization reserved PTU beats PAYG for a
  reasoning workload.
- **Observed (public):** benchmark-01 token-shape throughput-gain factor
  `0.9846 → 0.4518` (minimal→xhigh) — at fixed token shape, higher effort serves
  fewer requests per unit baseline throughput
  (`results/cost-curves/benchmark-01-throughput-gain.csv`; the factor is
  `baseline.tokens_per_request / target.tokens_per_request`, a same-model
  token-shape proxy, **not** a cross-model PTU throughput ratio); PTU density
  gpt-5.2 `3400` vs gpt-4o `2500` input TPM/PTU
  (`pricing/ptu-density-2026-05.yaml`).
- **Takeaway:** size PTU on reasoning-inclusive token shape; run
  `scripts/ptu_sizing.py` with a measured `WorkloadShape` (which applies model
  density).
- **Readiness:** `needs-new-chart` (no USD-vs-throughput crossover CSV yet;
  throughput-gain bars are `ready`).
- **Safety:** crossover must be computed from public pricing snapshots +
  `ptu_sizing.py`; do not fabricate a crossover point.

## T06 — Cache-Hit Degradation from `prompt_cache_key` Cardinality

- **Public data:**
  `results/cache-key-bucketing/{cache_hit_ratio,ttft_p95}_vs_cardinality.csv`,
  `benchmarks/06-cache-key-bucketing/analysis.md`,
  `docs/07-cache-hit-degradation.md`, `docs/12-prompt-cache-key-policy.md`,
  `docs/assets/cache-hit-degradation.svg`
- **Charts:** cache-hit-degradation, ttft-degradation
- **Why it exists:** measure how key cardinality + retention mode affect cache
  hit ratio and TTFT.
- **Reader should learn:** whether a high-cardinality cache key quietly wrecks
  hit ratio / TTFT.
- **Observed (public):** `in_memory` cardinality 1 → TTFT p95 `106389.96` ms vs
  `24h` cardinality 1 → `9899.74` ms (retention dominates TTFT here); hit ratio
  stays `0.9334–0.9612` — `results/cache-key-bucketing/*.csv`.
- **Takeaway:** retention mode drives steady-state TTFT more than cardinality in
  these cells; keep the cacheable prefix stable.
- **Readiness:** `ready`
- **Safety:** PAYG, not PTU (`metadata.ptu_evidence=false`); only two cardinality
  points (1, 8) per retention — label as sparse.

## T07 — What Retry-After Actually Looks Like on 429s

- **Public data:**
  `results/retry-after-characterization/retry_after_ms_percentiles.csv`,
  `.../retry_after_ms_events.csv`,
  `benchmarks/08-retry-after-characterization/analysis.json`
- **Charts:** retry-after-cdf (CDF + histogram + percentile table)
- **Why it exists:** ground backoff/admission logic in observed Retry-After-ms.
- **Reader should learn:** how long the service actually says to wait on a 429.
- **Observed (public):** overall n=193; p50 `43.0` ms, p90 `50.8` ms, p99
  `16921.12` ms, max `17258.0` ms (long tail) —
  `results/retry-after-characterization/retry_after_ms_percentiles.csv`.
  Mechanism split: task013 burst p50 `43.0` ms vs task019 reservation p50
  `3.0` ms — `benchmarks/08-retry-after-characterization/analysis.json`.
- **Takeaway:** size backoff for the p99 tail, not the median.
- **Readiness:** `ready`
- **Safety:** single-tenant, source-run-scoped, descriptive only; not
  customer-attributed; not direct PTU evidence.

## T08 — Proactive vs Reactive Spillover Recovery

- **Public data:** `benchmarks/04-spillover-simulation/analysis.json`,
  `benchmarks/05-dual-spillover/README.md`,
  `results/spillover-recovery-curves/*.png.csv`,
  `results/dual-spillover-curves/{policy_comparison_aggregate,real_429_timeline}.png.csv`
- **Charts:** spillover-recovery-timeline; 429-timeline
- **Why it exists:** compare a proactive admission/spillover policy vs a reactive
  one under burst load.
- **Reader should learn:** whether spilling proactively recovers cache hit ratio
  / avoids 429s better than reacting.
- **Observed (public):** proactive run `real_429_count=0`, overall
  `cache_hit_ratio=0.99094`; aggregate proactive−reactive cache-hit delta
  `+0.046` pp (near parity) — `benchmarks/04-spillover-simulation/analysis.json`.
- **Takeaway:** in this simulation the proactive policy avoided 429s entirely at
  near-identical steady-state hit ratio.
- **Readiness:** `ready`
- **Safety:** B04 is simulation; B05 hits real infra but workload-shaped, not
  customer-attributed. Time-series CSVs are 2–4k rows — downsample to render.

## T09 — Replaying a PTU Admission Controller

- **Public data:** `benchmarks/10-replay-validation/calibration.json`,
  `.../validation.md`,
  `results/replay-validation/predicted_vs_observed_{429_timestamps,retry_after_ms}.png`,
  `scripts/replay_ptu_utilization.py`, `docs/10-ptu-admission-controller.md`
- **Charts:** replay-predicted-vs-observed (scatter)
- **Why it exists:** validate an offline admission model (reserve at admission,
  leak continuously) against observed 429 timing/Retry-After.
- **Reader should learn:** whether a simple leak-rate model predicts 429 onset.
- **Observed (public):** in-sample confusion matrix tp=60, fp=5, fn=107,
  tn=4267 over n=4439; fitted `k_leak_tokens_per_ptu_per_second=3.9103` —
  `benchmarks/10-replay-validation/calibration.json` (high precision, low
  in-sample recall).
- **Takeaway:** a continuous-leak model catches some onsets precisely but misses
  many (high FN); a planning proxy, not a guarantee.
- **Readiness:** `needs-light-aggregation` (residuals are in JSON; only PNGs
  exist for the scatter — no paired CSV).
- **Safety:** proxy, not direct PTU-pool observation.

## T10 — `max_output_tokens` and the Admission Boundary

- **Public data:** `benchmarks/07-max-output-tokens-reservation/analysis.md`,
  `.../live-calibration-smoke-evidence-final.md`,
  `.../live-v2.5-adaptive-contrast.md`,
  `benchmarks/exp007_max_output_tokens_sweep/runs/*.summary.json`,
  `scripts/analyze_max_output_tokens_sweep.py`
- **Charts:** admission-boundary line (`needs-light-aggregation`)
- **Why it exists:** probe how `max_output_tokens` reservation interacts with the
  throttled PAYG admission layer (a PTU-admission proxy).
- **Reader should learn:** whether raising `max_output_tokens` reserves capacity
  and triggers 429s earlier.
- **Observed (public):** task019 reservation-driven 429s have a tight Retry-After
  distribution (p50 `3.0` ms, max `60.0` ms) —
  `benchmarks/08-retry-after-characterization/analysis.json`.
- **Takeaway:** `max_output_tokens` is a reservation, not just a cap; it can move
  the 429 onset on a capacity-bound deployment. Verify on your own deployment.
- **Readiness:** `needs-light-aggregation` (smoke-tier sweep summaries need
  aggregating).
- **Safety:** PAYG-throttled proxy, explicitly **not** PTU evidence (analysis.md
  banner).

## T11 — How the Data Is Sanitized: Evidence Gates and Release Tiers (meta)

- **Public data:** `docs/16-release-tiers-and-redaction-policy.md`,
  `docs/14-observability-schema.md`,
  `schemas/{ptu_request_record,ptu_cell_summary,raw_archive_manifest,redaction_rules}.schema.json`,
  `release/public_sanitized_manifest.json`,
  `scripts/sanitize_public_artifacts.py`,
  `docs/assets/release-tiers-redaction-boundary.svg`
- **Charts:** evidence-gate-flow (hand-authored SVG)
- **Why it exists:** explain SANITIZED_PUBLIC / RAW_PRIVATE /
  AGGREGATE_AZURE_SAMPLE tiering and per-artifact redaction provenance.
- **Reader should learn:** how public numbers can be faithful yet leak nothing
  private.
- **Observed (public):** `release/public_sanitized_manifest.json` carries one
  provenance entry per published artifact (sha256, redaction_rules_sha256,
  tier=SANITIZED_PUBLIC).
- **Takeaway:** a verifiable redaction boundary (`--verify`) makes the public
  surface auditable.
- **Readiness:** `ready`
- **Safety:** never render any RAW_PRIVATE sample.

## T12 — Publishing a Multilingual Research Site (meta, optional)

- **Public data:** `docs/i18n.md`, `docs/{en,ko,ja,zh-CN,hi}/index.html`,
  `docs/assets/i18n-locale-hreflang-graph.svg`, `docs/validate.sh`
- **Charts:** i18n-locale-graph (hand-authored SVG)
- **Why it exists:** document the locale set, hreflang/canonical metadata, and
  the stale-translation source-hash gate.
- **Reader should learn:** how to keep five translated pages from silently
  drifting.
- **Observed (public):** `docs/validate.sh` recomputes the EN `<main>`
  source-content sha256 and fails any locale page whose recorded
  `i18n:source-content-sha256` differs.
- **Takeaway:** a per-page source-content hash turns "is this stale?" into a
  deterministic CI check.
- **Readiness:** `ready`

---

## Readiness summary

| Topic | Primary chart family | Readiness |
| --- | --- | --- |
| T01 short-factual | cost-bars / quality-bars | ready |
| T02 multi-step | quality-bars / cost-bars | ready |
| T03 tool-agent | quality-bars / cost-bars | ready |
| T04 reasoning-token-tax | token-composition | ready |
| T05 PTU/PAYG crossover | throughput-gain / ptu-payg-crossover | needs-new-chart |
| T06 cache bucketing | cache-hit / ttft-degradation | ready |
| T07 retry-after | retry-after-cdf | ready |
| T08 spillover recovery | spillover-recovery-timeline | ready |
| T09 admission replay | replay-predicted-vs-observed | needs-light-aggregation |
| T10 max_output_tokens | admission-boundary line | needs-light-aggregation |
| T11 evidence gate (meta) | evidence-gate-flow | ready |
| T12 i18n (meta) | i18n-locale-graph | ready |

## Operator-takeaway thread (the storyline spine)

1. Reasoning is billed but invisible — **measure it** (T04).
2. It does **not** help short/factual work (T01); it **does** help genuine
   multi-step work (T02) and is near-neutral once tools dominate (T03).
3. Reasoning tokens consume **PTU capacity**, shifting the PTU↔PAYG decision
   (T05).
4. Operational levers around it: cache-key stability (T06), Retry-After-aware
   backoff (T07), proactive spillover (T08), admission modelling (T09), and
   `max_output_tokens` reservation (T10).
5. All of it is published through an auditable redaction boundary (T11) on a
   multilingual site (T12).
