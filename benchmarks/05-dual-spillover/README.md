# Benchmark 05 — Dual-Endpoint Spillover Measurement (Phase 2)

## Why this benchmark exists

Phase 1 (`benchmarks/04-spillover-simulation/`) measured the shape of two
spillover-policy recovery curves on a **single** Azure deployment, with the
PTU throttle state simulated internally and both "primary" and "spillover"
routes sharing one real cache pool. The signal Phase 1 produced is real,
but its cache-pool separation is fake.

Phase 2 reproduces that property by:

1. Adding a second `gpt-5.2`-family deployment in the same Foundry resource
   under a deliberately low TPM cap, so the same sustained ~30K-token
   workload produces **real 429s** instead of a simulator-emitted throttle
   state.
2. Routing spillover traffic to a separate `gpt-5.2`-family deployment with
   high TPM headroom. Different deployment name → different cache pool by
   Azure design.
3. Reusing the Phase 1 reactive vs proactive policy logic, verbatim, against
   this two-deployment substrate.

This benchmark is:

- **NOT a simulation.** Throttling and cache-pool separation are real.
  Per-request `cached_tokens` always comes from the API response, never from
  bookkeeping.
- **NOT a replica of any specific deployment.** TPM, prompt corpus, user
  prompt pool, and load pattern are chosen to expose the mechanism. They
  are never customer- or product-attributed.
- **Phase 2 of Tasks 012–016.** Phase 1 and Phase 2 are complementary: the
  shared corpus/prompts/policy code make the curves directly comparable;
  Phase 2's higher cost buys higher fidelity on the cache-pool-separation
  property that Phase 1 cannot model.

## What Phase 2 can and cannot say

Phase 2 can speak to:

- Whether the cache-hit-recovery curves under reactive vs proactive
  policies differ when each policy decision affects a physically separate
  cache pool (the property Phase 1 could not model).
- The size of the Phase 1 ↔ Phase 2 recovery-curve gap, with two
  interpretations: a **larger** Phase 2 gap supports the cache-rebuild-cost
  framing of weak-form G; a **similar** gap shows cache-pool separation is
  a smaller factor than policy timing.
- The per-policy real 429 timeline on the primary deployment and the
  observed `retry-after-ms` distribution (re-analyzed by Task 020 into a
  recovery-curve characterization without new spend).

It explicitly cannot:

- Settle the root-cause question for any production cache-hit drop. Strong
  form G was already rejected by customer field measurement
  (`docs/07-cache-hit-degradation.md` v2 appendix); Phase 2 measures the
  **mitigation** question, not the root-cause question.
- Claim absolute numbers transfer to another tenant or region. Two
  deployments in one Foundry resource, one capacity tier each.
- Speak to native Azure-side spillover (`spilloverDeploymentName`-driven).
  Phase 2 uses a custom router in the Python client; the
  `x-ms-spillover-from-deployment` / `x-ms-deployment-name` /
  `x-ms-spillover-error` response headers are captured per-request so a
  separate Task 021 can compare native-spillover behavior against this
  custom-router data.
- Score response quality. Phase 2 measures policy mechanics; a separate
  later task would add a judge pass if Task 015's analysis motivates it.

## Position in the repo

- Methodology contract: `docs/05-methodology.md` §4 (cache handling),
  §6 (cost), §7 (reproducibility).
- Hypothesis context: `docs/07-cache-hit-degradation.md` Hypothesis G
  (weak form) and Hypothesis I (max_output_tokens as PTU reservation,
  out of scope here).
- Build Order: Phase 2 of the customer-scenario simulation series. Task
  015 owns this directory's `analysis.md`. Task 020 re-aggregates
  Phase 2 JSONL into a recovery-curve characterization. Task 021 runs
  a native-spillover variant against the same workload.

## What is in this directory

```
05-dual-spillover/
├── README.md                    # this file
├── PREFLIGHT_LOG.md             # owner pre-flight + implementer reachability check
├── system_prompt_corpus.json    # verbatim copy from benchmarks/04-spillover-simulation/
├── user_prompts.json            # verbatim copy from benchmarks/04-spillover-simulation/
└── runs/                        # per-experiment .jsonl + .summary.json
                                 #   (populated by scripts/measure_dual_spillover.py)
```

### Corpus and user-prompt-pool reuse (byte-identical to Phase 1)

The system-prompt corpus and the user-prompt pool are **verbatim copies**
of the Phase 1 files. Byte-identity at the file level lets the
deterministic system-prompt construction emit byte-identical 30K-token
system prompts in both phases (same `corpus_seed`), which is the
contract that makes Phase 1 ↔ Phase 2 comparisons valid. If the corpus
changes between phases, the comparison loses validity. Do NOT regenerate
the corpus.

SHA-256 contract (logged at commit time; the implementer's pre-flight
verifies these match):

| File                          | SHA-256                                                          |
|-------------------------------|------------------------------------------------------------------|
| `system_prompt_corpus.json`   | `6a8ab5a3cb1ad3dace030a82ec1327496b39e65b77a627714a27c39017ca19e3` |
| `user_prompts.json`           | `45f4a95b5cfe208a3555683fe22c6eb74f1b0e46d621723b1ea1480713ab3087` |

These match the Phase 1 source files at commit time. To verify:

```
shasum -a 256 benchmarks/05-dual-spillover/system_prompt_corpus.json \
              benchmarks/05-dual-spillover/user_prompts.json
```

## Two real deployments, two real cache pools

The runner constructs two `AsyncOpenAI` clients against the same Foundry
v1 endpoint base and the same Entra ID credential. They differ only in
the `model` argument passed at request time:

- **Primary**: `${AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED}` (newly
  created at 60K TPM / 600 RPM, GlobalStandard PAYG, Entra ID,
  intermittent real 429s under the workload).
- **Spillover**: `${AZURE_OPENAI_DEPLOYMENT_GPT_5_2}` (existing 500K TPM /
  5000 RPM deployment, unchanged from benchmark 01 use, expected to see
  zero real 429s under the workload).

Because Azure caches per deployment name, the two routes have separate
cache pools — the property Phase 1 could not model. The per-request
JSONL record carries the `cache_pool` field equal to the
`deployment_used` value for every record.

The TPM and prompt sizing are deliberate workload-shaping parameters
chosen to expose the mechanism (60K TPM / 30K-token system prompt /
sustained 2 TPS produces a gross input rate approaching the cap so the
primary throttles when cache is cold). They are not attributed to any
customer's deployment.

## Two policies, one contract (verbatim from Phase 1)

The runner imports the pure-function `reactive_decide` and
`proactive_decide` primitives directly from `scripts/simulate_spillover.py`
unchanged. Behavior is byte-identical to Phase 1 inside the policy
functions; the differences are at the orchestration layer (two clients,
real 429 handling, additional captures).

- **Reactive.** Send to primary. On a real 429 OR a first-token latency
  exceeding `first_token_timeout_ms` (default 3000), route the failing
  request AND the next `stay_on_spillover_min_requests` (default 10) to
  spillover. Attempt return to primary no more often than
  `health_check_interval_ms` (default 30000). Real 429s are **never
  silently retried** — they are the signal this benchmark exists to
  measure.
- **Proactive.** Maintain a sliding window of the last
  `latency_window_size` (default 50) first-token latencies. If p95
  exceeds `p95_threshold_multiplier` × the warm-up baseline p95, ramp
  the spillover fraction toward `spillover_fraction_max` over
  `measurement_window_seconds`. When p95 recovers, ramp back by
  `ramp_back_factor` per window. On a real 429 from primary, the
  failing request retries on spillover (per
  `real_429_observed_action: route_to_spillover`) AND the next
  `real_429_followup_requests` (default 5) are routed to spillover.
  The 429 is logged as its own observation, not as a latency
  measurement.

Spillover-side real 429s are anomalies. They are retried **once** with
exponential backoff. If the aggregate spillover-side 429 rate exceeds
**1%** of spillover requests, the runner halts with a non-zero exit code
(signal that the spillover TPM is misconfigured).

## Load profile (verbatim from Phase 1)

22-minute trace per policy:

| Phase   | Duration | TPS profile          | Purpose                                                      |
|---------|----------|----------------------|--------------------------------------------------------------|
| warmup  | 2 min    | constant 0.3 TPS     | Establish baseline p95 for the proactive policy              |
| ramp    | 10 min   | linear 0.5 → 2.5 TPS | Push the throttled primary into real 429 territory           |
| sustain | 10 min   | constant 2.0 TPS     | Keep load in the saturation regime where policies differ     |

The TPS values are mechanism-exposing parameters chosen to make the
saturation regime visible on a 60K-TPM primary. They are not
customer-attributed or deployment-attributed.

## Per-request capture schema (Phase 2 delta from Phase 1)

Every request streams a JSONL record carrying:

- `experiment_id`, `git_commit`, `timestamp_utc`, `wallclock_timestamp_iso`,
  `endpoint`, `auth_mode`, `api_version` (literal `"preview"`),
  `model`, `policy`
- `endpoint_hit` (`primary` | `spillover`), `request_idx`, `phase`,
  `relative_time_s`
- `first_token_latency_ms`, `total_latency_ms`, `retry_count`
- Full `usage` object including `input_tokens_details.cached_tokens` and
  `output_tokens_details.reasoning_tokens`
- **`deployment_used`** — the actual deployment name sent as the `model`
  field (`ptu-deploy-throttled` or `gpt-5.2`)
- **`cache_pool`** — equals `deployment_used` for every record, since
  Azure caches per-deployment
- **`real_429_observed`** (bool) — whether the request observed a real 429
- **`primary_429_count_running_total`** (int) — monotonic 429 count on
  primary
- **`primary_health_check_state`** (reactive only) — `on_spillover` |
  `primary`
- **`retry_after_ms`** / **`retry_after_seconds`** — parsed from 429
  response headers
- **`x_ms_spillover_from_deployment`** / **`x_ms_deployment_name`** /
  **`x_ms_spillover_error`** — captured from response headers on every
  request (Phase 2 uses a custom router, but these headers reveal whether
  the request also went through any Azure-side native spillover)
- **`prompt_cache_key`** (the value forwarded to the API, if any) and
  **`prompt_cache_retention`**
- `policy_action_taken`,
  `simulated_proactive_fraction_at_routing` (proactive only)
- `cell_metadata.system_prompt_sha256`, `cell_metadata.corpus_seed`,
  `cell_metadata.policy_params_sha256`
- `pricing_snapshot_path` (the PAYG snapshot used for the running USD
  ledger)

**Phase 1 → Phase 2 schema migration.** The Phase 1 field
`simulated_primary_throttle_state` is **omitted** in Phase 2 records — it
would be misleading next to real measurements. The runner's unit tests
assert its absence.

A `*.summary.json` is emitted alongside each `*.jsonl` with aggregates:
time-weighted mean cache hit ratio per endpoint, p50/p95 first-token
latency, fraction served by spillover, total tokens, total USD spend,
real 429 totals per endpoint.

## Output charts

Each policy run emits four PNGs (and sibling CSVs) under
`results/dual-spillover-curves/`:

1. `reactive_per_endpoint.png` — cache hit ratio over time, one line per
   endpoint (`primary` vs `spillover`).
2. `proactive_per_endpoint.png` — same, for the proactive run.
3. `policy_comparison_aggregate.png` — time-weighted total cache hit ratio
   (both endpoints combined) reactive vs proactive on one axis.
4. `real_429_timeline.png` — per-minute real 429 count for both policies
   (primary endpoint only; spillover-side 429s are flagged as anomalies
   in the summary).

Plotting style: color-blind-friendly palette (Wong, 2011), axis labels,
legend, no decoration that depends on grayscale.

## How to run

### Dry-run (zero outbound HTTPS; still writes JSONL + summary)

```
python -m scripts.measure_dual_spillover \
    --experiment experiments/exp005_dual_spillover_reactive.yaml \
    --dry-run --allow-dirty
```

### Smoke (≤ $1 per policy, ~3 minutes wall-clock)

```
python -m scripts.measure_dual_spillover \
    --experiment experiments/exp005_dual_spillover_reactive.yaml \
    --smoke
```

Smoke confirms:

- Primary observes ≥ 1 real 429 (signal that the workload-shaping
  parameters are right).
- Spillover observes 0 real 429s. If spillover real 429 fraction > 1%,
  the runner halts with non-zero exit.
- Policy routing fires as expected (`endpoint_hit == "spillover"`
  appears in the record stream).

### Full run (≈ $3–6 per policy, 22 minutes wall-clock; hard ceiling $75)

```
python -m scripts.measure_dual_spillover \
    --experiment experiments/exp005_dual_spillover_reactive.yaml
```

Repeat each invocation for `exp005_dual_spillover_proactive.yaml` to
produce the comparison data. The comparison chart is emitted after the
second run finds its sibling-policy JSONL in `runs/`.

## Pre-flight reachability gate

Before any non-dry-run invocation, the runner executes a one-request
reachability check against each deployment (`ptu-deploy-throttled` and
`gpt-5.2`). On failure, the run aborts with exit code 2 and the
operator must verify the manual pre-flight checklist in
`PREFLIGHT_LOG.md`. The reachability check is part of every live
run (smoke and full) and cannot be silently bypassed.

## Owner review gate

Same as Phase 1: the corpus and user-prompt pool are the most
reviewable artifacts. Live runs are blocked until the owner confirms
anonymization review of these byte-identical-to-Phase-1 files. The
dry-run path is always available without that review.
