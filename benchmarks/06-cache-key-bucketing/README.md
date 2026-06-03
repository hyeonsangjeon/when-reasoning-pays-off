# Benchmark 06 — `prompt_cache_key` bucketing (v2.4)

> **Status (2026-05-29, v2.4 hotfix):** Task 018 **v2.4** raises
> `runtime.concurrency` from 8 → 96 to absorb the live `gpt-5.2` P95
> TTFT ≈128 s (`0.5 TPS × ~128 s ≈ 64` in-flight at Little's-Law
> steady state; sem=96 gives ~50% headroom). All other v2.3 pins are
> preserved verbatim (sustain_tps=0.5, estimated_processed_tokens_max=11000,
> deployment_tpm_quota=500000, admitted-time telemetry, TPM/cost gates,
> PAYG-not-PTU metadata, anonymization).
>
> The v2.3 Stage 1 smoke (sem=8) saturated the dispatcher semaphore on
> both YAMLs and tripped `backlog_excessive_card1 = true`:
> inmemory card=1 P95 backlog = **2,398 ms**, realized admitted RPM =
> **22.87**; 24h card=1 P95 backlog = **111,238 ms**, realized admitted
> RPM = **13.23**; both `max_in_flight_observed_card1 = 8`. Those
> artifacts are quarantined under `runs/_v2.3_diagnostic/` with a
> DO-NOT-CITE README. The v2.4 final evidence excludes them.
>
> Pytest now covers the v2.4 happy path (`TestHeavyStubHappyPathSem96`:
> sem=96, scaled heavy stub reproducing live TTFT≈128 s; backlog stays
> nominal; `max_in_flight_observed < 96`) and the v2.3 counterfactual
> (`TestCounterfactualSem8HeavyStub`: sem=8 against the same stub
> reproduces the v2.3 saturation signature in pytest).
>
> v2.1 diagnostic artifacts remain segregated under `runs/_v2.1_diagnostic/`
> and `../../results/cache-key-bucketing/_v2.1_diagnostic/` with their
> existing DO-NOT-CITE READMEs (unchanged by v2.4).

## What changed from v2.3 → v2.4 (one knob)

Live `gpt-5.2` PAYG TTFT was much higher than v2.3 sized for. v2.3
assumed ~9 s P95 TTFT; the live deployment delivered ~128 s P95 TTFT.
At `TPS = 0.5`, Little's Law gives `in-flight = TPS × TTFT ≈ 64` —
already 8× v2.3's `sem = 8`. The dispatcher semaphore therefore
saturated in Stage 1 smoke and the `backlog_excessive` gate fired
correctly to abort the smoke.

The v2.4 hotfix is the **single permitted remediation** for that
failure mode: raise the semaphore ceiling, leave every other pin
verbatim, re-run from Stage 0. TPM math is unchanged.

| Knob | v2.3 | v2.4 | Why |
|---|---|---|---|
| `runtime.concurrency` | `8` | **`96`** | ~50% headroom over the 64-in-flight Little's-Law steady state at live P95 TTFT ≈128 s |

Everything else (sustain_tps, estimated_processed_tokens_max,
deployment_tpm_quota, dispatcher, api_version, max_output_tokens,
reasoning.effort, corpus, sweep, budget gates, namespacing, citations,
anonymization regex, PAYG metadata) is byte-identical to v2.3.

## What changed from v2.1 → v2.3 (preserved here for context)

Task 018 v2.1 used a **serial** dispatcher (concurrency=1,
sustain_tps=1.0) against a 30K-token system-prompt corpus copied from
Task 012. The methodology-auditor review found two coupled defects:

1. **Latency-vs-arrival-rate confound** — per-call TTFTs of 8-13 s
   collapsed the achieved arrival rate from the *planned* 60 RPM
   common-prefix to ~7 RPM, so the v2.1 run never exercised any
   bucket past ~7 RPM. The docs-stated **~15 RPM per-bucket overflow
   threshold** was *operationally unreachable* under those pinned
   controls. The serial dispatcher tied admission to wall-clock TTFT
   and therefore could not separate "the threshold was not crossed"
   from "the threshold cannot be crossed serially."
2. **TPM-budget overhead** — a 30K-token system prompt at TPS=1.0
   projects to `60 × 1.0 × 30512 ≈ 1.83 M` tokens/minute, which
   exceeds the deployment's 500 000 TPM quota by 3.7×. Such a YAML
   would have throttled itself under any non-serial dispatcher.

v2.3 fixed both with `async_scheduled` + sustain_tps=0.5 + 11K-token
per-request cap; v2.4 inherits those fixes verbatim and adds **only**
the semaphore-resize remediation.

| Knob | v2.1 | v2.3 | v2.4 | Why |
|---|---|---|---|---|
| `runtime.dispatcher` | (implicit) serial | `async_scheduled` | `async_scheduled` | Decouples admission rate from per-call latency so the planned RPM can be **realized** independently of TTFT |
| `runtime.concurrency` | `1` | `8` | **`96`** | v2.4: ~50% headroom over live `0.5 TPS × ~128 s ≈ 64`-in-flight Little's-Law steady state |
| `runtime.sustain_tps` | `1.0` | `0.5` | `0.5` | Halves planned TPM contribution; brings projected TPM to `60 × 0.5 × 11 000 = 330 000` — safely under the 0.70 × 500 000 = 350 000 headroom ceiling |
| `request_template.estimated_processed_tokens_max` | (unset) | `11000` | `11000` | Per-request hard token cap (v2.3+) |
| `metadata.deployment_tpm_quota` | (unset) | `500000` | `500000` | Drives the TPM feasibility preflight gate (v2.3+) |
| System-prompt corpus | 30 K tokens (Task 012 copy) | ~10 K tokens (Task-018-specific) | (same as v2.3) | See *Corpus divergence* below |
| `sweep.bucket_cardinality` default | `[1, 2, 4, 8, 16]` | `[1, 8]` | `[1, 8]` | The two endpoints of interest |
| `runtime.cell_duration_seconds` (evidence) | `480` | `960` | `960` | 16 min per cell at 0.5 TPS = 480 calls/cell |

The two YAMLs (`exp006_cache_key_bucketing_inmemory.yaml` and
`exp006_cache_key_bucketing_24h.yaml`) are byte-identical in every
v2.4 pinned control; only `request_template.prompt_cache_retention`
and `runtime.washout_seconds` differ (the `24h` YAML uses
`washout_seconds=0` because waiting 24 h between cells is
operationally infeasible — see the inline rationale in that YAML).

## Hypothesis under test

Per the Azure AI Foundry prompt-caching documentation cited below,
the (prefix_hash, `prompt_cache_key`) pair selects the cache shard;
when a single bucket exceeds **~15 req/min**, the cache overflows
and the cache-hit ratio drops. v2.3 sweeps `bucket_cardinality`
across the default `[1, 8]` against a **realized** 30 RPM
per-bucket admission rate (at cardinality=1) and measures the shape
of `cache_hit_ratio_steady_state` and
`first_token_latency_ms_p95_steady_state`. With the async_scheduled
dispatcher, **realized admitted per-bucket RPM** (computed from
`admitted_dispatch_cell_elapsed_ms` timestamps post-semaphore-acquire,
not from arrival timestamps) is the authoritative observable.

## What is and is NOT measured here

| Property | This benchmark |
|---|---|
| Consumption model | **Azure OpenAI Global Standard PAYG**, single deployment |
| Throttled deployment? | **No** — runner rejects any deployment env-var name containing `THROTTLED`; Task 013's rate-limit-confound deployment is explicitly excluded |
| Simulation? | **No** — every request is a real Azure call, routed by Azure's `prompt_cache_key`+prefix-hash logic |
| PTU evidence? | **No** — single PAYG deployment; Task 022 must **not** cite this benchmark as PTU evidence |
| Architecture variant | `single_call_react` |

## File inventory

| Path | Role |
|---|---|
| `system_prompt_corpus.json` | **NEW v2.3** Task-018-specific ~10 K-token corpus on corporate-treasury operations (see *Corpus divergence*); SHA-256 below |
| `user_prompts.json` | 25 user prompts, byte-identical copy of `benchmarks/04-spillover-simulation/user_prompts.json` (SHA-256 below) |
| `runs/.gitkeep` | Placeholder so the runs directory tracks under Git even when empty |
| `runs/{TIMESTAMP}_{experiment_id}_{stage}.jsonl` | One-line-per-request JSONL with the v2.3 dispatcher telemetry fields |
| `runs/{TIMESTAMP}_{experiment_id}_{stage}.jsonl.summary.json` | Per-cell + run-level summary including the dual Citations block, `tpm_feasibility`, `backlog_excessive_any`, and (for smoke runs) the hoisted `*_card1` first-class fields |
| `runs/{TIMESTAMP}_{experiment_id}_{stage}.jsonl.partial.summary.json` | Written only when the mid-run budget gate halts cleanly |
| `runs/_v2.1_diagnostic/` | Quarantined v2.1 (serial-dispatcher, 30 K corpus) artifacts. See its README — **DO NOT CITE in final analysis** |
| `runs/_v2.3_diagnostic/` | Quarantined v2.3 (sem=8) dry-run + Stage 1 smoke artifacts that tripped `backlog_excessive_card1 = true` on both YAMLs. See its README — **DO NOT CITE in final analysis**; v2.4 artifacts only |
| `analysis.md` | v2.4 honest write-up; cites only v2.4 artifacts |
| `../../results/cache-key-bucketing/cache_hit_ratio_vs_cardinality.{png,csv}` | **v2.4 chart** — cache-hit ratio across the v2.4 cardinality sweep, one line per `prompt_cache_retention` mode; rendered by `python -m scripts.plot_cache_key_bucketing` from the latest Stage 2 evidence summaries |
| `../../results/cache-key-bucketing/ttft_p95_vs_cardinality.{png,csv}` | **v2.4 chart** — TTFT p95 (ms) across the v2.4 cardinality sweep, one line per `prompt_cache_retention` mode; same renderer |
| `../../results/cache-key-bucketing/_v2.1_diagnostic/` | Quarantined v2.1 (serial-dispatcher) PNGs and README — **DO NOT CITE in final analysis** |

### Corpus divergence (v2.3 only)

The v2.3 corpus is intentionally **not** byte-identical to
`benchmarks/04-spillover-simulation/system_prompt_corpus.json` (the
30 K-token Task 012/004/013 corpus). The TPM-feasibility preflight
requires `60 × sustain_tps × estimated_processed_tokens_max ≤ 0.70 ×
deployment_tpm_quota`. With the deployment's 500 000 TPM quota and
the v2.3 pinned `sustain_tps=0.5`, the per-request cap is bounded
above by `0.70 × 500 000 / 60 / 0.5 ≈ 11 666` tokens — well below
the 30 K corpus's roughly 30 512-token footprint. The v2.3 corpus
is 50 paragraphs (~21 K characters → ~5 250 tokens of textual
content; effective `system_prompt` post `_build_system_prompt`
≈9 549 tokens after the target_system_prompt_tokens=9500 cap is
applied). The user-prompt set is unchanged so cache-hit signals
from the prefix portion remain comparable to Tasks 012/013, with
the explicit caveat that absolute TTFT/cost numbers differ.

## Corpus SHA-256s

```text
c169e4d5eb8abff5e1d85289b9f50cd41edd49ad470d5120b19f206bc79762af  system_prompt_corpus.json (v2.3 NEW)
45f4a95b5cfe208a3555683fe22c6eb74f1b0e46d621723b1ea1480713ab3087  user_prompts.json (byte-identical to benchmarks/04-spillover-simulation/)
```

Re-verify the unchanged file:

```bash
diff -q benchmarks/06-cache-key-bucketing/user_prompts.json \
        benchmarks/04-spillover-simulation/user_prompts.json
```

The system-prompt corpus is **not** expected to diff against the
Task 012/013 copy — that's the entire point of v2.3.

## v2.4 pinned controls (enforced at YAML load time)

`scripts/measure_cache_key_bucketing.load_experiment` aborts at
load time if any of these mismatch the YAML. Narrowing the sweep
or raising the YAML budget ceiling is permitted when preflight
trips; lowering ANY pinned control is forbidden. v2.4 itself is the
permitted **semaphore-resize remediation** (sem 8 → 96) under the
per-cell `backlog_excessive` fail condition triggered by v2.3 Stage 1
smoke — it is not a violation of the no-lowering rule.

| Pinned control | Required value | Rationale |
|---|---|---|
| `runtime.dispatcher` | `async_scheduled` | Decouples planned admission rate from per-call TTFT |
| `runtime.concurrency` | **`96`** (v2.4) | ~50% headroom over the 64-in-flight Little's-Law steady state at live P95 TTFT ≈ 128 s; v2.3's pin of 8 saturated and tripped `backlog_excessive` on both YAMLs |
| `runtime.sustain_tps` | `0.5` | Half-Hz arrival rate per the v2.3 TPM-feasibility analysis (unchanged in v2.4) |
| `request_template.estimated_processed_tokens_max` | `11000` | Per-request hard cap; over-cap records rejected at construction (no HTTP send) |
| `request_template.max_output_tokens` | `512` | Keeps Azure's reservation-style admission logic out of the picture; Task 019 owns output-size sweeps |
| `request_template.reasoning.effort` | `low` | Smallest non-zero gpt-5.2-supported effort |
| `request_template.prompt_cache_retention` | `in_memory` or `24h` | Only varying field between the two YAMLs |
| `client.api_version` | `preview` | Foundry v1 API literal |
| `metadata.deployment_tpm_quota` | `500000` | Drives the TPM feasibility preflight gate |
| `metadata.consumption_model_context` | `paygo_standard` | PAYG declaration; downstream readers cannot mis-cite as PTU evidence |
| `metadata.simulation` | `false` | This is a live measurement |
| `metadata.ptu_evidence` | `false` | PAYG-only benchmark |
| Deployment env-var name | must NOT contain `THROTTLED` | Task 013's throttled deployment confounds bucketing with saturation |

## Preflight gates (all run BEFORE the live HTTP client is constructed)

| Gate | Pass condition | Abort error class |
|---|---|---|
| Pricing-snapshot freshness | snapshot < 90 days old | `PricingSnapshotStaleError` |
| USD preflight | `projected_usd ≤ 0.9 × hard_ceiling_usd` | `BudgetExceededError` |
| **NEW v2.3** TPM feasibility | `60 × sustain_tps × estimated_processed_tokens_max ≤ 0.70 × deployment_tpm_quota` | `TpmFeasibilityAbortError` |
| Token cap (at construction, per record) | `int((sys_chars + user_chars)/4) + max_output_tokens ≤ estimated_processed_tokens_max` | record-level `TokenCapAbortError` (recorded as `failed=true, failure_reason="token_cap_exceeded"`) |
| Mid-run USD | `cumulative_usd ≤ 0.85 × hard_ceiling_usd` after each cell | clean halt; partial summary written, exit 0 |

The TPM feasibility gate is the **new** v2.3 preflight that
prevents misconfigured YAMLs from ever opening a live HTTP client.
With current pins it computes:

```text
projected_tpm     = 60 × 0.5 × 11000 = 330000
quota_ceiling     = 0.70 × 500000     = 350000
passed (330000 ≤ 350000) → True
```

If you change `sustain_tps`, `estimated_processed_tokens_max`, or
`deployment_tpm_quota`, the gate recomputes. **Never** bypass it
by reducing `concurrency` — concurrency is orthogonal to TPM
admission control.

## Dispatcher (v2.4 `async_scheduled`, sem=96 — same machinery as v2.3)

The dispatcher schedules arrival `i` at wall-clock time
`cell_t0 + i / sustain_tps`. Per record we capture:

| Field | When captured | Role |
|---|---|---|
| `scheduled_dispatch_cell_elapsed_ms` | immediately after the pacer's `asyncio.sleep` returns, BEFORE `sem.acquire()` | The instant the dispatcher *wanted* to dispatch |
| `admitted_dispatch_cell_elapsed_ms` | immediately after `sem.acquire()` returns, BEFORE the HTTP `responses.create` call | The instant the dispatcher *actually* dispatched |
| `dispatch_backlog_ms` | `admitted - scheduled` | Delta — captures semaphore saturation |
| `in_flight_at_dispatch` | snapshot BEFORE incrementing the in-flight counter | Per-spec: "exclusive of the just-admitted call" |
| `request_concurrency` | constant, echoed from `runtime.concurrency` | Audit trail |
| `request_sustain_tps` | constant, echoed from `runtime.sustain_tps` | Audit trail |
| `request_estimated_processed_tokens` | per-record (int) | Used by the token-cap gate |
| `dispatcher_kind` | constant `"async_scheduled"` | Audit trail |

After the cell completes, per-bucket and common-prefix RPM are
**rebuilt from `admitted_dispatch_cell_elapsed_ms`** (admitted
order, not arrival order), giving the authoritative observable:
`realized_admitted_per_bucket_rpm` and
`realized_admitted_common_prefix_rpm`. JSONL is written sorted by
admitted timestamp.

### `backlog_excessive` gate

After every cell:

```text
backlog_excessive = (p95(dispatch_backlog_ms) > 1500) OR (max(dispatch_backlog_ms) > 5000)
```

A `True` here is a hard Stage-1 / Stage-2 failure: it means the
semaphore is saturated and the planned admission cadence is not
being realized. Failed records (token cap, transport exception,
post-retry rate-limited) are excluded from latency and cache
aggregates but counted in `n_records` / `n_failed_records`.

## Namespace + bucket key format

```text
benchmark06_{retention_tag}_card{NN}_{run_id_short}_bucket_{idx:03d}
```

Anonymization audit regex:

```text
^benchmark06_(inmemory|24h)_card\d{2}_[a-f0-9]{4,8}_bucket_\d{3}$
```

## How to reproduce

### Stage 0 — dry-run (no network)

```bash
source .env
python -m scripts.measure_cache_key_bucketing \
  --experiment experiments/exp006_cache_key_bucketing_inmemory.yaml \
  --dry-run --allow-dirty
python -m scripts.measure_cache_key_bucketing \
  --experiment experiments/exp006_cache_key_bucketing_24h.yaml \
  --dry-run --allow-dirty
```

### Stage 1 — smoke (2 cells × 60 calls/cell ≈ 120 s/cell at 0.5 TPS)

```bash
source .env
# AZURE_RESOURCE_URI is the env-var name for the Azure resource
# audience (e.g. set in .env, not committed). Using a placeholder
# rather than a literal resource URI keeps the audit recipe clean.
az account get-access-token --resource "${AZURE_RESOURCE_URI}" -o none
python -m scripts.measure_cache_key_bucketing \
  --experiment experiments/exp006_cache_key_bucketing_inmemory.yaml \
  --smoke --allow-dirty
python -m scripts.measure_cache_key_bucketing \
  --experiment experiments/exp006_cache_key_bucketing_24h.yaml \
  --smoke --allow-dirty
```

**Stage 1 gates** (both must pass for both YAMLs):

1. `realized_admitted_per_bucket_rpm_card1 ≥ 15`
2. `backlog_excessive_card1 == false`

### Stage 2 — evidence (2 cells × 480 calls/cell at 0.5 TPS = 16 min/cell)

```bash
source .env
az account get-access-token --resource "${AZURE_RESOURCE_URI}" -o none
python -m scripts.measure_cache_key_bucketing \
  --experiment experiments/exp006_cache_key_bucketing_inmemory.yaml \
  --allow-dirty
python -m scripts.measure_cache_key_bucketing \
  --experiment experiments/exp006_cache_key_bucketing_24h.yaml \
  --allow-dirty
```

Stage 2 should only be run after Stage 1 clears both gates for
both YAMLs.

### Anonymization audit (must return zero matches)

Per the task spec under `.internal/tasks/`, the audit covers five
checks. The literal greps are not reproduced verbatim here because
doing so would itself trip the host-name and auth-header greps;
see the task-spec **Test / Verification Plan** section for the
canonical command lines. The checks at a high level:

1. **API tokens** — long `sk-` prefixed tokens or `Bearer <token>` literals.
2. **Endpoint host-names** — Azure OpenAI / Cognitive-Services /
   API-Management host-name forms (see task-spec grep #2).
3. **Literal env-var values** — `AZURE_OPENAI_*` assignments to a
   literal value (assignments using `${VAR}` are excluded).
4. **Auth-header literals** — header-name-then-separator pairs
   used as request-header assignments (see task-spec grep #4).
5. **Schema check** — every JSONL `prompt_cache_key_used` matches
   the namespace regex above.

All five checks must report zero matches before this benchmark is
considered shippable.

## v2.3 Stage 1 outcome (superseded by v2.4) — historical record

This section is preserved unchanged from the v2.3 README for forensic
attribution. The artifacts referenced here are quarantined under
`runs/_v2.3_diagnostic/` and **must not** be cited as final evidence.

Stage 0 dry-run passed cleanly on both YAMLs. Stage 1 smoke
**executed without auth or transport-class failures** (60 + 60 records
per YAML, no `failed=true`, no halts) but **did not clear the v2.3
backlog gate** on either YAML under the Azure-deployment TTFT
distribution at the time of the v2.3 run:

| YAML | `realized_admitted_per_bucket_rpm_card1` | RPM ≥ 15 gate | `p95_dispatch_backlog_ms_card1` | `max_dispatch_backlog_ms_card1` | `max_in_flight_observed_card1` | `backlog_excessive_card1` | v2.3 Stage 1 result |
|---|---:|:---:|---:|---:|:---:|:---:|:---:|
| `inmemory` | 22.87 | ✅ pass | 2,398 ms | 3,948 ms | 8 | ❌ excessive | ❌ |
| `24h`      | 13.23 | ❌ fail | 111,238 ms | 119,546 ms | 8 | ❌ excessive | ❌ |

The three co-occurring diagnostic signatures
(`max_in_flight_observed_card1 == 8` = the v2.3 semaphore ceiling;
`backlog_excessive_card1 == true`; live P95 TTFT ≈128 s observed in
the underlying JSONL) point cleanly at semaphore saturation, not at
the deployment being unavailable. Little's Law at `0.5 TPS × ~128 s ≈ 64`
in-flight makes `sem = 8` an 8× undersize.

**v2.4 remediation:** raise `runtime.concurrency` to `96` (~50%
headroom over 64-in-flight steady state); preserve every other v2.3
pin; re-run from Stage 0. See *v2.4 Stage 1 outcome* below for the
v2.4 result.

## v2.4 Stage 1 outcome (2026-05-29 session)

Stage 1 cleared all three v2.4 gates on **both** YAMLs:

| YAML | `realized_admitted_per_bucket_rpm_card1` | `backlog_excessive_card1` | `max_in_flight_observed_card1` | Cell USD | Stage 1 |
|---|---:|:---:|---:|---:|:---:|
| `inmemory` | **23.10** | **false** | 13 | $0.8123 | ✅ |
| `24h`      | **23.12** | **false** | 14 | $0.6147 | ✅ |

Stage 2 evidence was therefore authorised and run (both YAMLs to
completion under budget; combined live cost $9.85 / $120 ceiling).
The authoritative Stage 2 per-cell numbers, headline findings, and
artifact paths live in `analysis.md`. Headline: card=1 admitted
per-bucket RPM = **30.65 (inmemory) / 30.73 (24h)** (matches scheduled
30 RPM); cache-hit at card=1 = **93.34 % / 96.12 %**; no
`backlog_excessive` trips; `max_in_flight_observed_run` peaks at 33
(well below the sem=96 ceiling, validating the v2.4 sizing).

## Citations

The dual Citations block is echoed verbatim into every
`*.summary.json` and referenced from `analysis.md`. Sources of
truth are the Microsoft Learn pages named in the v2.3 spec.

- **Azure AI Foundry — Prompt caching**
  - URL: <https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/prompt-caching>
  - Accessed date: **2026-05-29**
  - Claims cited:
    1. `prompt_cache_key` combined with prefix hash influences routing.
    2. **~15 req/min per (prefix_hash, prompt_cache_key)** is the documented per-bucket overflow threshold.
    3. `24h` retention is available when `prompt_cache_retention='24h'`.
    4. `in_memory` retention is the default when `prompt_cache_retention` is unset.
- **Azure AI Foundry — Rate limits / quota**
  - URL: <https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/quota>
  - Accessed date: **2026-05-29**
  - Claims cited:
    1. Deployment TPM quota is enforced as a sliding window over estimated processed tokens (input + max_output).
    2. Exceeding the sliding window returns HTTP 429 with `retry-after`.
    3. Operator must keep projected TPM safely below quota to avoid throttling that would confound per-bucket RPM observation.
- **Pricing snapshot**
  - Path: `pricing/azure-openai-payg-2026-05.yaml`
  - Source URL: <https://azure.microsoft.com/en-us/pricing/details/azure-openai/>
  - Accessed date: **2026-05-19**

## Cross-references

- Parent task spec: `.internal/tasks/018-cache-key-bucketing-benchmark.md` (v2.4 hotfix banner on top of v2.3 body)
- Sibling benchmark (single-deployment baseline): `benchmarks/04-spillover-simulation/`
- Sibling benchmark (dual-deployment spillover): `benchmarks/05-dual-spillover/`
- Downstream consumer (PTU-comparison roll-up): Task 022 — must cite this only with the PAYG-not-PTU caveat
- Quarantined v2.1 diagnostic artifacts: `runs/_v2.1_diagnostic/`, `../../results/cache-key-bucketing/_v2.1_diagnostic/` — **NOT for citation**
- Quarantined v2.3 diagnostic artifacts: `runs/_v2.3_diagnostic/` — **NOT for citation**
