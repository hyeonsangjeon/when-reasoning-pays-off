# Phase 2 dual-endpoint spillover analysis

## 1. Question and design

Hypothesis G, in its weak form for this report, asks whether saturation-sensitive cache dips are mitigated better by proactive spillover than by reactive spillover. Phase 1 (`benchmarks/04-spillover-simulation/`) answered that question on a single live Azure deployment whose primary/spillover split was a routing label over one shared cache pool. Phase 2 reproduces the same 22-minute scheduled load shape (2 minutes warmup, 10 minutes ramp from 0.5 to 2.5 TPS, 10 minutes sustain at 2.5 TPS) against **two real `gpt-5.2` deployments**: a primary capped at 60,000 TPM so the ~30K-token workload throttles into **real 429s**, and a spillover at 500,000 TPM with headroom to absorb the overflow. Different deployment name means a different cache pool by Azure design, so Phase 2 can finally measure the property Phase 1 could not: what each policy costs when its routing decisions land on physically separate cache pools.

It still cannot discover the root cause of field cache degradation. Strong-form Hypothesis G was already weakened by field measurement before this run; Phase 2 only sharpens the weak-form mitigation comparison between the two policies.

## 2. Methodology link

Methodology: `docs/05-methodology.md` Section 8. Exact reporting guardrail: "The decision framework is built on effect size and direction across samples, not statistical significance." Cache handling (§4), cost (§6), and reproducibility (§7) govern the token, dollar, and provenance fields below.

## 3. Run provenance

| Field | Reactive | Proactive |
|---|---:|---:|
| Experiment YAML | `experiments/exp005_dual_spillover_reactive.yaml` | `experiments/exp005_dual_spillover_proactive.yaml` |
| Raw JSONL | `20260528T135034Z_exp005_dual_spillover_reactive_reactive.jsonl` | `20260528T183310Z_exp005_dual_spillover_proactive_proactive.jsonl` |
| Completed / scheduled requests | 2,136 / 2,136 | 2,303 / 2,136 |
| Full-run cache hit ratio | 99.0479% | 98.2112% |
| Full-run PAYG total | $17.895660 | $18.806026 |
| Real 429 count (primary) | 0 | 167 |
| `dry_run` / `dirty` / halt | `false` / `false` / `null` | `false` / `false` / `null` |
| Git commit | `9a266efec53ca9e1e86c8f9a1b45808808d656d8` | `9a266efec53ca9e1e86c8f9a1b45808808d656d8` |
| System prompt SHA-256 | `98d3a559c54c42f683a4cc0d09e6fb4cc10034cc80203d58121edbaca606088a` | `98d3a559c54c42f683a4cc0d09e6fb4cc10034cc80203d58121edbaca606088a` |
| Policy params SHA-256 | `144683e7f764d5b69b5bf5004927032d938d3a727b0e1da26d4cc49bc404f076` | `678c908ced201ee6ef8b5f0be007931cdfb62b030f58dfa3f48b9fd6cf837ec5` |
| Corpus seed | `4242` | `4242` |
| Runner version | `scripts/measure_dual_spillover.py` at the git commit above | same |

Both runs use `api_version=preview`, `model=gpt-5.2` version `2025-12-11`, `effort=low`, `max_output_tokens=1024`, `target_system_prompt_tokens=30000`, and pricing snapshot `pricing/azure-openai-payg-2026-05.yaml` accessed 2026-05-19. The proactive run logged 167 more completed rows than it scheduled because each real 429 is recorded as its own failed-attempt row; those rows carry zero usage tokens and do not change token or cache aggregates.

## 4. Architecture context

Architecture applicability: this analysis applies to single-call ReAct deployments fronted by a client-side router across two real Azure deployments with separate cache pools. The magnitude does not transfer to multi-node orchestration, to native Azure-side spillover (`spilloverDeploymentName`-driven), or to a single deployment.

The workload is one large ReAct call with a roughly 30K-token-class system prompt. Unlike Phase 1, the primary (`ptu-deploy-throttled`, 60,000 TPM / 600 RPM) and spillover (`gpt-5.2`, 500,000 TPM / 5,000 RPM) are distinct deployments in one Foundry resource, so each policy decision affects a physically separate cache pool. TPM caps are workload-shaping parameters, not customer- or product-attributed references.

## 5. Headline finding

Proactive again did not beat reactive, and the gap is now wider than Phase 1. On the sustain-phase steady-state cache hit ratio, reactive measured 99.2337% and proactive measured 98.2414%, a proactive-minus-reactive gap of -0.9923 pp. Phase 1's sustain gap on the shared-pool simulator was -0.1657 pp, so introducing physically separate cache pools widened the gap roughly six-fold. Reactive also cost less ($17.895660 versus $18.806026, a $0.910366 delta) and produced zero real 429s, while proactive absorbed 167 real 429s on the throttled primary. The direction is the same as Phase 1; the higher-fidelity substrate made the cost of the proactive policy visible instead of near-zero.

## 6. Recovery curve description

The designed load trace is 22 scheduled minutes, but the live runs completed over longer wall-clock time because requests incurred real model latency plus, for proactive, real 429 backoffs. The committed CSVs and PNGs under `results/dual-spillover-curves/` plot 60-second rolling-window cache-hit ratios against relative wall-clock seconds, per endpoint and aggregate, plus a per-minute real-429 timeline.

Reactive entered spillover routing at request 1 and rode it for almost the whole run: 48 route transitions in total, but 98.8764% of full-run requests and 98.7500% of sustain requests landed on the high-headroom spillover deployment, concentrating traffic into one warm cache pool. Its sustain cache-hit curve is visually flat around 99.23%, with sustain per-minute mean 99.2337% and std 0.0026 pp across 145 buckets.

Proactive kept probing the throttled primary to balance load: 334 route transitions, and only 43.3348% of full-run requests (67.9468% of sustain requests) spilled over. The primary could not sustain the load and returned 167 real 429s (13 in ramp, 154 in sustain). Splitting traffic left both pools cooler than reactive's single pool — in sustain, proactive's primary cache measured 96.7534% and its spillover cache 98.6942%, versus reactive's 99.2336% concentrated spillover pool. The proactive sustain curve is noticeably rougher, with per-minute mean 98.2900% and std 4.3726 pp across 179 buckets, because the 429 failure rows and the split-pool cache misses punch through it.

## 7. Per-policy aggregate stats

| Policy | Sustain cache hit ratio | Sustain per-minute cache hit mean +/- std (std in pp) | Sustain TTFT p50 / p95 (ms) | Sustain spillover fraction | Full-run total tokens | Full-run USD | Real 429 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Reactive | 99.2337% | 99.2337% +/- 0.0026 pp | 7,153.9 / 11,817.2 | 98.7500% | 45,850,205 | $17.895660 | 0 |
| Proactive | 98.2414% | 98.2900% +/- 4.3726 pp | 7,926.6 / 13,431.7 | 67.9468% | 45,851,851 | $18.806026 | 167 |

Full-run first-token latency from each `.summary.json` tells the same story more sharply: reactive p50/p95 was 7,383.3 / 11,967.2 ms, while proactive p50/p95 was 10,239.96 / 34,631.71 ms — the proactive p95 blows out because ramp-and-sustain 429 backoffs stack onto real model latency.

## 8. Consumption Model Translation

### PAYG

Source: `pricing/azure-openai-payg-2026-05.yaml` accessed 2026-05-19. For `gpt-5.2`, the snapshot records $1.75 / $0.175 / $14.00 / $14.00 per 1M input / cached-input / reasoning / output tokens.

Running the reactive policy for this measured 2,136-request schedule cost $17.895660. Running the proactive policy cost $18.806026. The proactive-minus-reactive PAYG delta for the measured window is $0.910366. The two runs consumed nearly identical total tokens (reactive 45,850,205; proactive 45,851,851), so the delta is almost entirely a cache-mix effect: proactive's 0.8367 pp lower full-run cache hit ratio moved roughly 380K input tokens from the $0.175/1M cached rate to the $1.75/1M uncached rate, and its slightly higher output-token count added the remainder. Cache misses, not extra work, made proactive more expensive.

### PTU

Baseline: reactive policy under this benchmark's load shape. Target: proactive policy. Using full-run mean total tokens per request as a crude PTU pressure proxy, reactive used 21,465.45 total tokens/request across 2,136 completed requests and proactive used 19,909.62 total tokens/request across 2,303 completed requests; the proactive figure is diluted by its 167 zero-token 429 rows and is not a like-for-like throughput signal.

This proxy does not model PTU slot routing and does not bill PTU capacity. The measurement that matters for a PTU tenant is the real-429 count: reactive drew zero, proactive drew 167. On a real PTU deployment those 429s are the throttle signal, and a policy that keeps feeding the throttled endpoint pays for them in latency and retries.

## 9. Outliers / anomalies

Zero cells were excluded. The outlier rule requires `first_token_latency_ms > mean + 3*std` within sustain and a coincident logged event such as real 429, retry, or route transition. Reactive had 6 high-latency sustain candidates, of which 1 coincided with a route transition; proactive had 18, of which 5 coincided with a 429 or route transition. Excluding the coincident candidates shifts sustain TTFT by less than 1% for both policies (reactive p50 7,153.9 to 7,146.3 ms; proactive p50 7,926.6 to 7,910.5 ms), so all rows are retained and the tables above use the full sustain set.

Retained non-exclusion anomalies: proactive has 154 zero-input sustain rows (the real-429 failure attempts, which carry no usage tokens) and 12 uncached sustain rows; reactive has none of either. These affect token and cache aggregates only through their raw usage fields and are not excluded, because they are the phenomenon the benchmark exists to measure, not a measurement fault.

## 10. Limits of this measurement

This is two deployments in one Foundry resource, one capacity tier each, so absolute cache-hit and dollar numbers do not transfer to another tenant, region, or capacity plan. Phase 2 measures the mitigation question, not the root-cause question: it cannot settle why any production cache-hit drop happened, and strong-form Hypothesis G was already rejected by field measurement (`docs/07-cache-hit-degradation.md` v2 appendix). It uses a custom Python-client router, so it does not speak to native Azure-side spillover; the `x-ms-spillover-from-deployment` / `x-ms-deployment-name` / `x-ms-spillover-error` response headers are captured per request so a separate native-spillover run can be compared later. It does not score response quality, and it does not exercise the `max_output_tokens` admission-time reservation effect (Hypothesis I).

What this means for a PTU+ReAct customer: when a primary deployment throttles and a high-headroom spillover pool exists, the simple reactive rule — spill the failing request and the next several to the spillover pool, then let that one pool stay warm — beat the proactive rule that kept probing the throttled primary to balance load. With physically separate cache pools, splitting traffic to protect the primary rebuilt cache in two pools at once and still paid the throttle penalty. If a production system has separate primary/spillover cache pools, prefer concentrating spillover traffic over pre-emptively balancing it, and re-measure if the workload shape changes.

## 11. Conclusion

Phase 2 data does not support a meaningful proactive advantage for weak-form Hypothesis G, and it strengthens the Phase 1 verdict. On the sustain metric, proactive was -0.9923 pp below reactive (versus -0.1657 pp in the shared-pool Phase 1), it cost $0.910366 more over the measured window, and it took 167 real 429s to reactive's zero. The mechanism is now visible: because the two deployments hold separate cache pools, the proactive policy's traffic-splitting kept both pools cooler than reactive's single concentrated pool, and its insistence on the throttled primary converted into real 429s, latency, and cost. Per the pre-registered reading in this benchmark's README, a larger Phase 2 gap supports the cache-rebuild-cost framing of weak-form G: cache-pool separation is not a smaller factor than policy timing here — it is what makes the simple policy win.

## 12. Reproducibility footer

Re-derive the machine-readable companion with a pure JSONL aggregation: read both raw files under `benchmarks/05-dual-spillover/runs/`, filter `phase == "sustain"` for steady-state metrics, compute `canonical_cached_tokens` divided by `canonical_input_tokens`, bucket `wallclock_timestamp_iso` by minute, compute sustain TTFT p50 and p95 from sorted `first_token_latency_ms`, count `real_429_observed` by phase and endpoint, and apply the documented outlier rule. Cross-check full-run totals, `git_commit`, `dirty`, `dry_run`, `system_prompt_sha256`, `policy_params_sha256`, and `total_usd` against each `.summary.json`. The derived values are committed in `benchmarks/05-dual-spillover/analysis.json`.
