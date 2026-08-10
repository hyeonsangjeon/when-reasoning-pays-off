# Benchmark 04 — Spillover-Policy Simulation (Phase 1)

## Why this benchmark exists

This benchmark is a **controlled simulation**, not a replica of any specific
deployment. It exists to produce evidence for one narrow question:

> Under a realistic single-call ReAct workload (≈30K-input-token system prompt,
> sustained ~2 TPS for ten minutes), does a **proactive** spillover policy
> (rolling-window p95 latency threshold) minimize the saturated-window
> cache-hit dip more, less, or about the same as a **reactive** policy
> (first-token timeout / 429 trigger)?

The question is the **weak form** of Hypothesis G in
`docs/07-cache-hit-degradation.md`. The strong form — "reactive spillover
across PTU↔PAYG thrashes the cache and is the root cause of the migration-era
cache-hit drop" — was rejected by customer field measurement (PTU-only
operation also maintains cache hit ratio, so spillover is not the root
cause). The weak form survives: under sustained near-saturation load, the
cache hit ratio can dip and recover. Phase 1 measures the **shape** of that
recovery curve under two policies on one real Azure deployment, with PTU
throttle state simulated internally.

Reactive-vs-proactive is therefore a **mitigation** question in this
benchmark, not a root-cause question.

## What Phase 1 can and cannot say

This benchmark is intentionally narrow. It can speak to:

- The relative shape of cache-hit recovery curves under two policy families.
- Whether the two recovery curves are statistically distinguishable at
  Phase 1's sample size, and if so, in which direction.
- Per-policy USD spend at effort=minimal, p50/p95 first-token latency, and
  the fraction of requests routed to spillover.

It explicitly cannot:

- Claim absolute numbers transfer to another deployment, another region, or
  another tenant. One endpoint, one capacity tier, internally simulated
  throttle.
- Settle the root-cause question for any production cache-hit drop. That
  question was answered (rejected) by the customer field measurement
  referenced above and is not re-litigated here.
- Speak to PTU-specific phenomena (slot routing, native spillover headers,
  capacity-correlated cache effects) at the wire level. Those require
  Phase 2 (Task 013 — second deployment with real TPM throttling).
- Score response quality. The simulator measures policy mechanics; a
  separate later task would add a judge pass if Task 014's analysis
  motivates it.

Either Phase 1 outcome is informative. Indistinguishable recovery curves weaken
weak-form G; a difference in the predicted direction supports it and
justifies a Phase 2 measurement on a real-throttled primary.

## Position in the repo

- Methodology contract: `docs/05-methodology.md` §4 (cache handling), §6
  (cost), §7 (reproducibility). The simulator preserves every invariant
  the runner does — byte-identical prompts within a run, full `usage`
  preserved per request, timestamp + git commit hash per record,
  pricing-snapshot citation, append-only output.
- Hypothesis context: `docs/07-cache-hit-degradation.md` Hypothesis G
  (weak form). Diagnostic priority for future PTU customers:
  `A / E / C / I / D / G_weak / H′ / B / F`.
- Build Order: Phase 1 of the customer-scenario simulation series (Tasks
  012–016). Phase 2 (Task 013) replaces the internally-simulated throttle
  with a real TPM-throttled second deployment. Task 014 owns the
  analysis writeup; this directory's `analysis.md` is added there.

## What is in this directory

```
04-spillover-simulation/
├── README.md                    # this file
├── system_prompt_corpus.json    # neutral generic financial-services-assistant
│                                #   instruction snippets (~120 items)
├── user_prompts.json            # 30 short generic financial questions
└── runs/                        # per-experiment .jsonl + .summary.json
                                 #   (populated by scripts/simulate_spillover.py)
```

The corpus and user prompts are deliberately **generic**. Every snippet is
written as a neutral instruction a financial-services assistant might
receive (scope boundaries, safety guards, response format, refusal
policies). No bank name, no product name, no app name, no specific
regulator, no internal team name. The corpus is the most reviewable
artifact in this benchmark; review it before any live run.

## How the simulator builds its system prompt

`scripts/simulate_spillover.py` constructs the system prompt deterministically
from `system_prompt_corpus.json` plus a `corpus_seed` integer set in the
experiment YAML. Procedure:

1. Load the corpus as a JSON list of strings.
2. Seed `random.Random` with `corpus_seed` and shuffle the list in place.
3. Concatenate snippets in shuffled order with `\n\n` as the separator
   until the estimated token count (`len(text) // 4`) reaches
   `target_system_prompt_tokens`. If the shuffled corpus is exhausted
   before the target is reached, the loop wraps to the beginning of the
   same shuffled list and continues. Determinism is preserved.

The SHA-256 of the assembled system prompt is logged at run start and
embedded in every per-request JSONL record (`cell_metadata.system_prompt_sha256`)
so a reproducer can verify byte-identity. Both policy YAMLs share the same
`corpus_seed`, so the reactive and proactive runs use a byte-identical
system prompt; this is what makes the cross-policy comparison fair.

## Two policies, one contract

The simulator implements two policy families with explicit verbatim
contracts (see `scripts/simulate_spillover.py` for the pure-function
implementation):

- **Reactive.** Send to primary. On first-token latency exceeding
  `first_token_timeout_ms` (default 3000) OR a real 429, route subsequent
  requests to spillover. Stay on spillover for at least
  `stay_on_spillover_min_requests` (default 10). Attempt return to primary
  no more often than `health_check_interval_ms` (default 30000).
- **Proactive.** Maintain a sliding window of the last
  `latency_window_size` (default 50) first-token latencies. If p95 exceeds
  `p95_threshold_multiplier` × the warm-up baseline p95, ramp up the
  spillover fraction toward `spillover_fraction_max` over
  `measurement_window_seconds`. When p95 recovers, ramp back by
  `ramp_back_factor` per window.

Both policy functions are **pure**: they take `(observation, state, params)`
and return `(decision, new_state)`. They are unit-tested independently of
the network I/O.

## Load profile

22-minute trace per policy (verbatim from the experiment YAML):

| Phase   | Duration | TPS profile          | Purpose                                                      |
|---------|----------|----------------------|--------------------------------------------------------------|
| warmup  | 2 min    | constant 0.3 TPS     | Establish baseline p95 for the proactive policy              |
| ramp    | 10 min   | linear 0.5 → 2.5 TPS | Push the simulated primary into near-saturation              |
| sustain | 10 min   | constant 2.0 TPS     | Keep load in the saturation regime where policies differ     |

The TPS values are mechanism-exposing parameters chosen to make the
saturation regime visible. They are not customer-attributed or
deployment-attributed.

## Per-request capture schema

Every request streams a JSONL record carrying:

- `experiment_id`, `git_commit`, `timestamp_utc`, `wallclock_timestamp_iso`,
  `endpoint`, `auth_mode`, `api_version` (literal `"preview"`), `model`,
  `deployment_name`, `policy`
- `endpoint_hit` (`primary` | `spillover`), `request_idx`, `phase`,
  `relative_time_s`
- `first_token_latency_ms`, `total_latency_ms`, `retry_count`
- full `usage` object including `input_tokens_details.cached_tokens` and
  `output_tokens_details.reasoning_tokens`
- `simulated_primary_throttle_state` (`headroom` | `near_threshold` |
  `throttled`), `policy_action_taken`,
  `simulated_proactive_fraction_at_routing` (proactive only),
  `simulated_primary_recovery_state` (reactive only)
- `real_429_observed` plus `retry_after_ms` and `retry_after_seconds`
  (parsed from any 429 response headers — added per Task 011 v2
  appendix so Tasks 020 / 022 can re-use this capture)
- `prompt_cache_key` (the value forwarded to the API, if any) and
  `prompt_cache_retention`
- `cell_metadata.system_prompt_sha256`, `cell_metadata.corpus_seed`,
  `cell_metadata.policy_params_sha256`
- `pricing_snapshot_path` (the PAYG snapshot used for the running USD
  ledger)

A `*.summary.json` is emitted alongside each `*.jsonl` with aggregates:
time-weighted mean cache hit ratio per endpoint, p50/p95 first-token
latency, fraction served by spillover, total tokens, total USD spend.

## Output charts

Each policy run produces one PNG plus a sibling CSV under
`results/spillover-recovery-curves/`:

- `reactive_recovery.png` and `reactive_recovery.png.csv`
- `proactive_recovery.png` and `proactive_recovery.png.csv`

A comparison chart (`policy_comparison.png` + CSV) overlays both policies
on a single axis. Plotting style: color-blind-friendly palette, axis
labels, legend, and vertical dashed lines marking spillover events.

## How to run

Smoke (≤ $1 per policy, ≤ 3 minutes wall-clock):

```
python -m scripts.simulate_spillover \
    --experiment experiments/exp004_spillover_baseline_reactive.yaml \
    --smoke
```

Full run (≈ $2-3 per policy, 22 minutes wall-clock; hard ceiling $50):

```
python -m scripts.simulate_spillover \
    --experiment experiments/exp004_spillover_baseline_reactive.yaml
```

Dry-run (zero outbound HTTPS calls; still writes JSONL + summary):

```
python -m scripts.simulate_spillover \
    --experiment experiments/exp004_spillover_baseline_reactive.yaml \
    --dry-run --allow-dirty
```

Repeat each invocation for `exp004_spillover_proactive.yaml` to produce
the comparison data.

## Owner review gate

Before any non-private / live run against Azure: the owner reviews the
corpus for anonymization. The corpus is the most reviewable artifact in
this benchmark; live runs are blocked until that review is complete. The
dry-run path is always available without the review.
