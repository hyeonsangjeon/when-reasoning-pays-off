# Task 018 — `prompt_cache_key` bucketing — analysis (v2.4)

> **Consumption-model declaration (read this first):**
> This benchmark measures **Azure OpenAI Global Standard PAYG** behaviour
> against a **single live gpt-5.2 deployment** at 500 K TPM. It is **NOT**
> a simulation (every request hits real Azure infrastructure). It is
> **NOT** PTU evidence (no Provisioned-Throughput slot routing or
> admission-time reservation is exercised). The `metadata.simulation` and
> `metadata.ptu_evidence` flags in both experiment YAMLs and every
> summary JSON are pinned to `false`. **Task 022 must NOT cite this
> benchmark as PTU evidence.**

## Scope of this write-up

This analysis covers the **v2.4 hotfix** of Task 018, which raises
`runtime.concurrency` from 8 → 96 on top of the otherwise byte-identical
v2.3 implementation. The v2.4 hotfix is the single permitted
remediation under the per-cell `backlog_excessive` fail condition that
v2.3's Stage 1 smoke tripped (live `gpt-5.2` P95 TTFT ≈128 s × TPS=0.5
gives Little's-Law steady-state in-flight ≈64; v2.3's sem=8 saturated
the dispatcher semaphore and the gate correctly fired).

Earlier v2.1 artifacts (serial dispatcher, 30 K-token Task 012 corpus
copy) remain quarantined under `runs/_v2.1_diagnostic/` and
`../../results/cache-key-bucketing/_v2.1_diagnostic/` with DO-NOT-CITE
READMEs. **v2.3 Stage 1 smoke artifacts are quarantined under
`runs/_v2.3_diagnostic/`** with a DO-NOT-CITE README that records the
exact failure numbers. Neither set is referenced as evidence from
this analysis.

## v2.4 design summary (one knob vs v2.3)

| Knob | v2.3 | v2.4 |
|---|---|---|
| `runtime.concurrency` | `8` | **`96`** (~50% headroom over the 64-in-flight Little's-Law steady state at live P95 TTFT ≈128 s) |

Everything else is byte-identical to v2.3 (sustain_tps=0.5,
estimated_processed_tokens_max=11000, deployment_tpm_quota=500000,
dispatcher='async_scheduled', api_version='preview',
max_output_tokens=512, reasoning.effort='low',
prompt_cache_retention ∈ {in_memory, 24h}, default sweep [1, 8],
PAYG-not-PTU metadata, anonymization). TPM math is unchanged:
`60 × 0.5 × 11000 = 330000 ≤ 0.70 × 500000 = 350000`.

The v2.3 async_scheduled telemetry is preserved verbatim:

- `scheduled_dispatch_cell_elapsed_ms` — wall-clock instant the
  dispatcher *wanted* to dispatch (captured immediately after the
  pacer `asyncio.sleep`, BEFORE `sem.acquire`)
- `admitted_dispatch_cell_elapsed_ms` — wall-clock instant the
  dispatcher *actually* dispatched (captured immediately after
  `sem.acquire` returns, BEFORE the HTTP send)
- `dispatch_backlog_ms` — `admitted − scheduled`; semaphore
  saturation indicator
- `in_flight_at_dispatch` — snapshot BEFORE the just-admitted call
  increments the counter
- `request_concurrency`, `request_sustain_tps`,
  `request_estimated_processed_tokens`, `dispatcher_kind` — audit
  trail echoed onto every record

Per-bucket and common-prefix RPM are rebuilt from
`admitted_dispatch_cell_elapsed_ms` post-cell as
`realized_admitted_per_bucket_rpm` and
`realized_admitted_common_prefix_rpm`. v2.4 adds a first-class
`max_in_flight_observed_run` rollup at the summary top level (a
run-wide max of the per-cell `max_in_flight_observed`) so reviewers
can detect semaphore saturation regressions of the v2.3 kind
without spelunking into per-cell rows.

The `backlog_excessive` flag fires when
`p95(dispatch_backlog_ms) > 1500 ms` OR `max > 5000 ms`. This is a
hard Stage 1/2 failure indicator (preserved verbatim from v2.3).

## v2.3 Stage 1 failure — what v2.4 fixes (historical, do not cite)

Numbers below come from
`runs/_v2.3_diagnostic/*_smoke.jsonl.summary.json`. They are
reproduced here for *attribution of the v2.4 design rationale*, not
as evidence.

| YAML | `realized_admitted_per_bucket_rpm_card1` | `p95_dispatch_backlog_ms_card1` | `max_dispatch_backlog_ms_card1` | `max_in_flight_observed_card1` | `backlog_excessive_card1` | v2.3 Stage 1 result |
|---|---:|---:|---:|---:|:---:|:---:|
| `inmemory` | 22.87 | 2,398 ms | 3,948 ms | 8 | true | ❌ |
| `24h` | 13.23 | 111,238 ms | 119,546 ms | 8 | true | ❌ |

The three co-occurring diagnostic signatures
(`max_in_flight_observed_card1 == 8`; `backlog_excessive == true`;
live P95 TTFT ≈128 s) point cleanly at the v2.3 sem=8 ceiling: at
`0.5 TPS × ~128 s ≈ 64`-in-flight steady state, sem=8 was 8× too
small. The v2.4 fix is exclusively the semaphore-resize remediation
(`sem 8 → 96`); every other v2.3 control is preserved verbatim.

This v2.3 failure mode is also locked into pytest as
`TestCounterfactualSem8HeavyStub`: against a deterministic heavy
stub mirroring the live TTFT regime (256×-scaled to fit in
`--timeout=120`), sem=8 reproduces all three signatures (sem-pinned
max_in_flight, `backlog_excessive=True`, realized common-prefix RPM
well below 30). `TestHeavyStubHappyPathSem96` exercises the v2.4
sem=96 happy path against the same stub: P95 backlog <1500 ms, max
backlog <5000 ms, `backlog_excessive=False`,
`max_in_flight_observed < 96`, realized common-prefix RPM (rescaled
to live time) ∈ [28, 32].

## v2.4 Stage 0 (dry-run) results

Both YAMLs ran end-to-end (2 cells × 480 records each, no network)
with all preflight gates passing under sem=96:

```text
TPM_FEASIBILITY_PREFLIGHT projected_tpm=330000.0 quota=500000
                          headroom_fraction=0.70 ceiling=350000.0  → pass
BUDGET_PREFLIGHT          stage=evidence cells=2 calls_per_cell=480
                          projected_usd=10.4706 hard_ceiling=60.0000
                          preflight_threshold=54.0000              → pass
```

Dry-run JSONL records carry the v2.4 telemetry set with
`request_concurrency = 96` echoed on every row; summaries carry the
dual citations block, the `tpm_feasibility` block, the per-cell
`backlog_excessive` flag, the cell-list-level `backlog_excessive_any`
roll-up, and the new top-level `max_in_flight_observed_run` rollup.
(The dispatcher path still acquires the semaphore even in dry-run —
it's the network send that is bypassed — so `max_in_flight_observed_run`
is a small positive integer ≪ 96 rather than zero.)

Dry-run summary artifacts (this session, v2.4) are listed under
*Artifacts* below.

## v2.4 Stage 1 (smoke) results

Both YAMLs cleared all three Stage 1 gates. Smoke ran 60 records ×
2 cells per YAML; total combined live cost **$1.43** (well under the
$16 combined ceiling).

| YAML | `realized_admitted_per_bucket_rpm_card1` | `p95_dispatch_backlog_ms_card1` | `max_dispatch_backlog_ms_card1` | `max_in_flight_observed_card1` | `backlog_excessive_card1` | Cell USD | Stage 1 |
|---|---:|---:|---:|---:|:---:|---:|:---:|
| `inmemory` | **23.10** | 0 ms | 0 ms | 13 | **false** | $0.8123 | ✅ |
| `24h`      | **23.12** | 0 ms | 0 ms | 14 | **false** | $0.6147 | ✅ |

Per-cell smoke detail:

| YAML | card | n_records | n_failed | cache_hit_steady | TTFT p50 | TTFT p95 | per_bucket_rpm | common_rpm | max_in_flight | backlog_excessive |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|
| inmemory | 1 | 60 | 0 | 0.7647 | (–) | 53,580 ms | 23.10 | 23.10 | 13 | false |
| inmemory | 8 | 60 | 0 | 0.7810 | (–) | 105,355 ms | 3.20 | 23.17 | 15 | false |
| 24h      | 1 | 60 | 0 | (–) | (–) | 113,965 ms | 23.12 | 23.12 | 14 | false |
| 24h      | 8 | 60 | 1 | (–) | (–) | 130,548 ms | 3.14 | 22.51 | 21 | false |

(The single 24h-card8 failure was a transient `500 Internal Server
Error` from one upstream attempt; the script's per-record-failure
isolation preserved the surrounding cell. The cell still cleared
`backlog_excessive=false` and `max_in_flight_observed=21 < 96`.)

All three v2.4 Stage 1 promotion gates pass on both YAMLs:

1. `realized_admitted_per_bucket_rpm_card1 ≥ 15`  ✅ (23.10 / 23.12)
2. `backlog_excessive_card1 == false`             ✅ (both)
3. `max_in_flight_observed_card1 < 96`            ✅ (13 / 14)

Stage 2 evidence is therefore authorised.

## v2.4 Stage 2 (evidence) results

Both YAMLs ran the full evidence cell pair (480 records × 2 cells
per YAML, ~38 min wall per YAML). Combined live cost **$9.85**
(well under the $120 combined ceiling). No `backlog_excessive`
trips, no rate-limit (429) events, single transient 500 (1 failed
record across 1,920 sends; cell isolation held).

| YAML | total USD | cells | `backlog_excessive_any` | `max_in_flight_observed_run` |
|---|---:|---:|:---:|---:|
| `inmemory` | $4.9972 | 2 / 2 | **false** | 33 |
| `24h`      | $4.8488 | 2 / 2 | **false** | 10 |

Per-cell evidence detail (steady-state slice; warmup excluded per
the v2.3 aggregator):

| YAML | card | n_records | n_steady | n_failed | cache_hit_steady | TTFT p50 | TTFT p95 | per_bucket_rpm | common_rpm | p95_backlog | max_backlog | max_in_flight | backlog_excessive |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|
| inmemory | 1 | 480 | 388 | 1 | **0.9334** | 19,536 ms | 106,390 ms | **30.65** | 30.65 | 0 ms | 1 ms | 33 | false |
| inmemory | 8 | 480 | 389 | 0 | **0.9586** |  6,847 ms |  16,624 ms |   4.00     | 30.69 | 0 ms | 1 ms | 11 | false |
| 24h      | 1 | 480 | (–)  | 0 | **0.9612** |  5,702 ms |   9,900 ms | **30.73** | 30.73 | 0 ms | 1 ms |  6 | false |
| 24h      | 8 | 480 | (–)  | 0 | **0.9612** |  6,016 ms |  15,549 ms |   4.00     | 30.75 | 0 ms | 1 ms | 10 | false |

**Headline findings under v2.4 sem=96:**

1. **The scheduled cadence is realized.** card=1 `realized_admitted_per_bucket_rpm`
   tracks the planned 30 RPM (60×TPS) within boundary: 30.65 / 30.73.
   Common-prefix RPM at card=8 likewise tracks 30 (30.69 / 30.75),
   with per-bucket = 30/8 ≈ 3.75 lifted to 4.00 by the cell's last-bucket
   boundary inclusivity.

2. **The dispatcher is not backpressured.** All four cells report
   `p95_dispatch_backlog_ms = 0` and `max_dispatch_backlog_ms = 1 ms`
   (i.e. the semaphore was effectively never contended).
   `max_in_flight_observed` peaks at 33 (inmemory card=1; a transient
   live TTFT excursion) — well below the sem=96 ceiling and consistent
   with the Little's-Law steady-state estimate ~64 only being
   *approached* in TTFT outlier windows.

3. **The threshold is operationally reachable.** With 30 RPM admitted
   into a single common-prefix at card=1, the `cache_hit_ratio_steady_state`
   is **0.9334 (inmemory) / 0.9612 (24h)**. At card=8 (each bucket
   admitted at 30/8 ≈ 3.75 RPM, well below the docs-stated 15 RPM
   per-bucket ceiling), cache-hit rises to **0.9586 / 0.9612**. The
   card=1 vs card=8 cache-hit gap (-2.5 pp inmemory; 0 pp 24h) is
   small under v2.4 sem=96 — significantly smaller than the band the
   docs-stated 15 RPM-per-bucket overflow model predicts. This is
   the first reliably measured cache-hit baseline against the
   per-bucket admission threshold from this benchmark.

4. **24h has tighter latency than inmemory.** At the same admitted
   cadence, 24h card=1 sees TTFT p95 = 9.9 s vs inmemory's 106.4 s.
   This is consistent with the 24h retention class warming a larger
   key window across the cell; inmemory retention requires repeated
   prefix-recompute under TTFT outliers.

**Promotion verdict.** Stage 2 evidence clears all v2.4 quality
gates (no backlog excessive, no rate-limit, ≤1 transient failure
across 1,920 sends, both common-prefix cadences within boundary of
30 RPM, sem ceiling never approached). The numbers above are
authoritative for Task 018 v2.4.

## Operational implication — does the threshold exist?

**Yes, but the v2.4 evidence shows the docs-stated overflow band is
softer than naive reading suggests on this PAYG deployment.** At
card=1 the per-bucket admitted cadence is 30 RPM (2× the docs-stated
15 RPM threshold) and cache-hit still holds at 93–96 %; at card=8
the per-bucket cadence drops to 4 RPM (well inside the threshold)
and cache-hit holds at 96 %. The cache-hit deltas across cardinality
(≤2.5 pp) are too small to be diagnostic of a sharp 15 RPM cliff on
this deployment. The benchmark thus shows the threshold exists in
principle but is **operationally softer than the docs-stated 15 RPM
band** under v2.4's pinned controls. This finding supersedes the v2.1
and v2.3 diagnostic conclusions (which were "the threshold exists in
principle but was not operationally reachable on this deployment
under the prior pinned controls") and is the authoritative result
for downstream Task 022 framing.

## Confounds and caveats

1. **Per-call TTFT variance still dominates** — even at sem=96,
   if the deployment's TTFT distribution shifts during the run, the
   dispatcher may briefly approach the semaphore ceiling. The v2.4
   `max_in_flight_observed_run` and per-cell `max_in_flight_observed`
   capture this directly.
2. **Time-of-day** — Azure deployment load fluctuates. Compare any
   Stage 2 run-time against off-peak / on-peak Azure windows when
   interpreting trends.
3. **Single deployment** — no PTU evidence; no dual-deployment
   comparison. Task 022 must inherit these caveats.
4. **PAYG, not PTU** — the docs-stated 15 RPM threshold is on the
   PAYG model. PTU admission semantics differ; **this benchmark does
   not measure PTU**.
5. **Cell isolation under 24h retention** — the `24h` YAML cannot
   wash out caches between cells; isolation relies entirely on the
   per-cell namespace (UUID-v4 8-char suffix) per the v2.3+
   Implementation Notes.

## Charts

The two v2.4 chart deliverables required by the task spec are
rendered by `scripts/plot_cache_key_bucketing.py` directly from
the Stage 2 evidence summary JSONs above:

| Chart | Path | Source script |
|---|---|---|
| Cache hit ratio vs cardinality | [`../../results/cache-key-bucketing/cache_hit_ratio_vs_cardinality.png`](../../results/cache-key-bucketing/cache_hit_ratio_vs_cardinality.png) | `python -m scripts.plot_cache_key_bucketing` |
| TTFT p95 vs cardinality | [`../../results/cache-key-bucketing/ttft_p95_vs_cardinality.png`](../../results/cache-key-bucketing/ttft_p95_vs_cardinality.png) | `python -m scripts.plot_cache_key_bucketing` |

Both x-axes are `bucket_cardinality` on a log2 scale (one tick per
swept cell — card=1 and card=8 under the v2.4 default sweep); each
chart carries one line per `prompt_cache_retention` mode
(`in_memory` and `24h`). Sibling CSVs (`*.csv`) next to each PNG
contain the underlying per-cell values (cache_hit_ratio,
ttft_p95_ms, realized_admitted_per_bucket_rpm, n_steady_state_records,
max_in_flight_observed, namespace) so the numerical content is
auditable without re-rendering. The renderer excludes any cell
with `backlog_excessive == true` from the plotted curves (v2.3+
analysis contract); the v2.4 evidence run reports
`backlog_excessive == false` for all four cells, so all four
points appear in both charts.

Reproduce with::

    python -m scripts.plot_cache_key_bucketing

(auto-discovers the most recently modified
`*_inmemory_evidence.jsonl.summary.json` and
`*_24h_evidence.jsonl.summary.json` under `runs/`; pass explicit
`--inmemory-summary` / `--h24-summary` to pin to specific files).

Existing v2.1 diagnostic charts under
`../../results/cache-key-bucketing/_v2.1_diagnostic/` remain
quarantined as DO-NOT-CITE — they were produced under the serial
v2.1 dispatcher and are not authoritative.

## Artifacts

### v2.4 (this session — authoritative)

Stage 0 dry-run:

- `runs/20260529T113153Z_exp006_cache_key_bucketing_inmemory_dry-run.jsonl[.summary.json]`
- `runs/20260529T113159Z_exp006_cache_key_bucketing_24h_dry-run.jsonl[.summary.json]`

Stage 1 smoke (all three v2.4 gates cleared on both YAMLs):

- `runs/20260529T113210Z_exp006_cache_key_bucketing_inmemory_smoke.jsonl[.summary.json]`
- `runs/20260529T114222Z_exp006_cache_key_bucketing_24h_smoke.jsonl[.summary.json]`

Stage 2 evidence (authoritative; numbers in tables above):

- `runs/20260529T115239Z_exp006_cache_key_bucketing_inmemory_evidence.jsonl[.summary.json]`
- `runs/20260529T123021Z_exp006_cache_key_bucketing_24h_evidence.jsonl[.summary.json]`

### Quarantined v2.3 (do NOT cite — v2.3 Stage 1 smoke failure)

- `runs/_v2.3_diagnostic/` (with `README.md` disclaimer and the
  inmemory + 24h failure-number tables)

### Quarantined v2.1 (do NOT cite)

- `runs/_v2.1_diagnostic/` (with `README.md` disclaimer)
- `../../results/cache-key-bucketing/_v2.1_diagnostic/` (with
  `README.md` disclaimer)

## Citations

Echoed verbatim in every `*.summary.json` and reproduced in the
benchmark README.

- **Azure AI Foundry — Prompt caching**
  - URL: <https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/prompt-caching>
  - Accessed: 2026-05-29
- **Azure AI Foundry — Rate limits / quota**
  - URL: <https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/quota>
  - Accessed: 2026-05-29
- **Pricing snapshot**: `pricing/azure-openai-payg-2026-05.yaml`
  (Source: <https://azure.microsoft.com/en-us/pricing/details/azure-openai/>,
  accessed 2026-05-19)

## Cross-references

- Parent task spec: internal Task 018 cache-key-bucketing-benchmark
  specification (private working tree; v2.4 hotfix banner on top of v2.3 body)
- Benchmark README: `benchmarks/06-cache-key-bucketing/README.md`
- v2.3 diagnostic quarantine: `benchmarks/06-cache-key-bucketing/runs/_v2.3_diagnostic/`
- v2.1 diagnostic quarantine: `benchmarks/06-cache-key-bucketing/runs/_v2.1_diagnostic/`,
  `results/cache-key-bucketing/_v2.1_diagnostic/`
- Sibling benchmarks: `benchmarks/04-spillover-simulation/`, `benchmarks/05-dual-spillover/`
- Downstream PTU roll-up: Task 022 — PAYG-not-PTU caveat applies
