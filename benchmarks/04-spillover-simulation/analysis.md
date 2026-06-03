# Phase 1 spillover-simulation analysis

## 1. Question and design

Hypothesis G, in its weak form for this report, asks whether saturation-sensitive cache dips are mitigated better by proactive spillover than by reactive spillover in a single-call ReAct workload. Phase 1 measures two live Azure `gpt-5.2` runs driven by the same 22-minute scheduled load shape: 2 minutes warmup, 10 minutes ramp, and 10 minutes sustain. It can compare policy behavior, cache-hit recovery shape, latency, token use, and PAYG spend for this simulator.

It cannot discover the root cause of field cache degradation. Strong-form Hypothesis G was already weakened by field measurement before this simulator ran; Phase 1 only informs the weak-form mitigation comparison between the two policies.

## 2. Methodology link

Methodology: `docs/05-methodology.md` Section 8. Exact reporting guardrail: "The decision framework is built on effect size and direction across samples, not statistical significance."

## 3. Run provenance

| Field | Reactive | Proactive |
|---|---:|---:|
| Experiment YAML | `experiments/exp004_spillover_baseline_reactive.yaml` | `experiments/exp004_spillover_proactive.yaml` |
| Raw JSONL | `20260527T131115Z_exp004_spillover_baseline_reactive_reactive.jsonl` | `20260527T182353Z_exp004_spillover_proactive_proactive.jsonl` |
| Completed / scheduled requests | 2,136 / 2,136 | 2,136 / 2,136 |
| Full-run cache hit ratio | 99.0478% | 99.0942% |
| Full-run PAYG total | $17.883347 | $17.924671 |
| `dry_run` / `dirty` / halt | `false` / `false` / `null` | `false` / `false` / `null` |
| Git commit | `67542910f31cf507f2523c0fba2102df6b622f8a` | `67542910f31cf507f2523c0fba2102df6b622f8a` |
| System prompt SHA-256 | `98d3a559c54c42f683a4cc0d09e6fb4cc10034cc80203d58121edbaca606088a` | `98d3a559c54c42f683a4cc0d09e6fb4cc10034cc80203d58121edbaca606088a` |
| Corpus seed | `4242` | `4242` |
| Simulator version | `scripts/simulate_spillover.py` at the git commit above; no separate simulator version field is emitted | same |

Both runs use `api_version=preview`, `model=gpt-5.2`, `effort=low`, `target_system_prompt_tokens=30000`, and pricing snapshot `pricing/azure-openai-payg-2026-05.yaml` accessed 2026-05-19.

## 4. Architecture context

Architecture applicability: this analysis applies to single-call ReAct deployments on PTU-with-reactive-spillover; the magnitude does not transfer to multi-node orchestration.

The simulated workload uses a single large ReAct call with a roughly 30K-token-class system prompt. The primary and spillover labels are policy-routing states over one live Azure deployment, not separate customer deployments and not separate physical cache pools. This is a mechanism-exposure simulator, not a customer replica.

## 5. Headline finding

Proactive did not exceed reactive on the sustain-phase steady-state cache hit ratio. Reactive measured 99.2337%; proactive measured 99.0680%, a proactive-minus-reactive gap of -0.1657 pp. The gap is tiny and descriptive only, and it was not a broad sustained curve separation: most sustain buckets are near 99.23% for both policies, while two uncached proactive sustain requests pull its aggregate lower.

## 6. Recovery curve description

The designed load trace is 22 scheduled minutes, but the live runs completed over longer wall-clock time because requests incurred real model latency. The committed recovery CSVs and PNGs under `results/spillover-recovery-curves/` plot 60-second rolling-window cache-hit ratios against relative wall-clock seconds.

Reactive entered spillover routing at request 1 during warmup, briefly returned to primary at request 91 during ramp, re-engaged spillover at request 92, briefly returned to primary at request 1478 during sustain, and re-engaged spillover at request 1479. Its sustain cache-hit curve is visually flat around 99.23%, with sustain per-minute mean 99.2336% and std 0.0033 pp.

Proactive never engaged spillover in the raw policy-action stream (`spillover_request_fraction=0`). Its curve is also mostly flat near 99.23%, but sustain includes uncached requests at request 1307 and request 1959, producing visible one-minute dips and a sustain per-minute mean 99.0878% with std 1.2802 pp.

## 7. Per-policy aggregate stats

| Policy | Sustain cache hit ratio | Sustain per-minute cache hit mean +/- std (std in pp) | Sustain TTFT p50 / p95 (ms) | Sustain spillover fraction | Full-run total tokens | Full-run USD | Real 429 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Reactive | 99.2337% | 99.2336% +/- 0.0033 pp | 8,490.6 / 13,617.5 | 99.9167% | 45,828,455 | $17.883347 | 0 |
| Proactive | 99.0680% | 99.0878% +/- 1.2802 pp | 7,620.9 / 12,107.8 | 0.0000% | 45,812,897 | $17.924671 | 0 |

## 8. Consumption Model Translation

### PAYG

Source: `pricing/azure-openai-payg-2026-05.yaml` accessed 2026-05-19. For `gpt-5.2`, the snapshot records $1.75 / $0.175 / $14.00 / $14.00 per 1M input / cached-input / reasoning / output tokens.

Running the reactive policy for this measured 2,136-request run cost $17.883347. Running the proactive policy cost $17.924671. The proactive-minus-reactive PAYG delta for the measured window is $0.041324; proactive was slightly more expensive despite slightly fewer total tokens because its output and reasoning-token mix was higher.

### PTU

Baseline: reactive policy under this benchmark's load shape. Target: proactive policy. Using full-run mean total tokens per request as a crude PTU pressure proxy, reactive used 21,455.27 total tokens/request and proactive used 21,447.99 total tokens/request. The target-to-baseline token ratio is 0.999661, giving a throughput-gain factor of 1.000340x for proactive versus reactive.

This factor is effectively flat and simulator-bound. It does not model PTU slot routing, does not bill PTU capacity, and does not transfer to deployments with truly separate cache pools.

## 9. Outliers / anomalies

Zero cells were excluded. The outlier rule requires `first_token_latency_ms > mean + 3*std` within sustain and a coincident logged event such as real 429, retry, route transition, or spillover engagement. Reactive had 1 high-latency sustain candidate and proactive had 8, but none coincided with the required logged event, so all were retained.

Retained non-exclusion anomalies: reactive has one zero-token sustain row; proactive has two zero-token sustain rows and two uncached sustain rows. These affect token and cache aggregates only according to their raw usage fields; they are not excluded because they do not satisfy the latency-plus-logged-event policy.

## 10. Limits of this measurement

This is a single-endpoint simulator with simulated throttle state. The primary/spillover split is a routing label over one live deployment, so absolute cache-hit numbers do not transfer to deployments with truly separate cache pools; Task 013 is the higher-fidelity follow-up for that question. No PTU billing was involved, and the PTU translation above is only a token-pressure proxy.

The run does not exercise Hypothesis I, the `max_output_tokens` admission-time reservation effect; Task 019 owns that measurement. It also does not prove or disprove customer root cause. Real environments can differ by region, tenant capacity, orchestration topology, prompt-cache controls, and retry behavior.

What this means for a PTU+ReAct customer: treat Phase 1 as a warning that reactive versus proactive policy choice did not create a meaningful steady-state cache-hit advantage in the single-endpoint simulator. If the production system has physically separate primary/spillover cache pools, Task 013 is the evidence to prioritize before using these magnitudes in capacity or cost planning.

## 11. Conclusion

Phase 1 data does not support a meaningful proactive advantage for weak-form Hypothesis G in this single-endpoint simulator. On the sustain-phase metric, proactive was -0.1657 pp below reactive, while the full-run overall cache ratio was only 0.0464 pp above reactive. Strong-form Hypothesis G was already weakened by field measurement; this simulator's contribution is the mitigation comparison, not root-cause discovery. The measured difference is tiny, descriptive only, and bounded by the shared-cache Phase 1 design.

## 12. Reproducibility footer

Re-derive the machine-readable companion with a pure JSONL aggregation: read both raw files under `benchmarks/04-spillover-simulation/runs/`, filter `phase == "sustain"` for steady-state metrics, compute cached input tokens divided by input tokens, bucket `wallclock_timestamp_iso` by minute, compute sustain TTFT p50 and p95 from sorted `first_token_latency_ms`, and apply the documented outlier rule. Cross-check totals, `git_commit`, `dirty`, `dry_run`, `system_prompt_sha256`, and `total_usd` against each `.summary.json`. The derived values are committed in `benchmarks/04-spillover-simulation/analysis.json`.
