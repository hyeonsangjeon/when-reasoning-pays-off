# `_v2.3_diagnostic/` — Quarantined v2.3 dry-run + smoke artifacts

**Status:** **EXCLUDED from final evidence.** These artifacts are kept on
disk for forensic / regression-attribution purposes only. They MUST NOT
be cited as Task 018 evidence.

## Why these artifacts are quarantined

Task 018 v2.3 pinned `runtime.concurrency = 8` against an assumed
`gpt-5.2` time-to-first-token (TTFT) of ~9 s. The live deployment's P95
TTFT measured ~128 s in Stage 1 smoke (~14× the assumption), giving a
Little's-Law steady-state in-flight count of `0.5 TPS × ~128 s ≈ 64`.
With sem=8, the dispatcher semaphore saturated immediately and the
backlog regression-detection logic (per-cell P95 backlog > 1500 ms OR
max backlog > 5000 ms ⇒ `backlog_excessive = true`) tripped on both
YAMLs' Stage 1 smoke.

Per the v2.3 acceptance criteria, a smoke that trips
`backlog_excessive = true` MUST NOT be promoted to evidence. The
v2.4 hotfix raises `runtime.concurrency` to 96 — ~50 % headroom above
the 64-in-flight Little's-Law steady state — and re-runs both stages
from scratch. The v2.3 artifacts below are preserved verbatim so the
exact failure signature stays reproducible.

## Failure signature (live Stage 1 smoke, v2.3)

Numbers below are read directly from the on-disk `*.summary.json`
files in this directory; reviewers can confirm them by `jq` over the
files.

### `exp006_cache_key_bucketing_inmemory` smoke

| field                                            | value                |
|--------------------------------------------------|----------------------|
| `realized_admitted_per_bucket_rpm_card1`         | **22.87**            |
| `realized_admitted_common_prefix_rpm_card1`      | 22.87                |
| `p95_dispatch_backlog_ms_card1`                  | **2,398 ms**         |
| `max_dispatch_backlog_ms_card1`                  | 3,948 ms             |
| `max_in_flight_observed_card1`                   | **8** (= sem)        |
| `backlog_excessive_card1`                        | **true**             |
| card=8 realized admitted RPM                     | 3.00                 |
| card=8 P95 backlog                               | 31,459 ms            |

### `exp006_cache_key_bucketing_24h` smoke

| field                                            | value                |
|--------------------------------------------------|----------------------|
| `realized_admitted_per_bucket_rpm_card1`         | **13.23**            |
| `realized_admitted_common_prefix_rpm_card1`      | 13.23                |
| `p95_dispatch_backlog_ms_card1`                  | **111,238 ms**       |
| `max_dispatch_backlog_ms_card1`                  | 119,546 ms           |
| `max_in_flight_observed_card1`                   | **8** (= sem)        |
| `backlog_excessive_card1`                        | **true**             |
| card=8 realized admitted RPM                     | 3.20                 |
| card=8 P95 backlog                               | 4,683 ms             |

Three diagnostic signatures co-occur on both YAMLs:

1. `max_in_flight_observed_card1 == 8` (the v2.3 semaphore ceiling)
2. `backlog_excessive_card1 == true` (per-cell backlog regression)
3. `realized_admitted_per_bucket_rpm_card1 < 15` (Stage 1 gate failure
   on the 24h YAML; gate-bordering on the inmemory YAML)

## What v2.4 changed

* `runtime.concurrency: 8 → 96` in both YAMLs and
  `scripts/measure_cache_key_bucketing.py:CONCURRENCY_PINNED`.
* Everything else (sustain_tps=0.5, estimated_processed_tokens_max=11000,
  deployment_tpm_quota=500000, TPM/cost gates, admitted-time telemetry,
  PAYG-not-PTU metadata, anonymization, dispatcher='async_scheduled',
  api_version='preview', max_output_tokens=512, reasoning.effort='low',
  cell isolation namespace, citations) is preserved verbatim.
* TPM math unchanged: `60 × 0.5 × 11000 = 330,000 ≤ 0.70 × 500,000 =
  350,000` ✓.

## Files

```
20260529T103513Z_exp006_cache_key_bucketing_inmemory_dry-run.jsonl{,.summary.json}
20260529T103519Z_exp006_cache_key_bucketing_24h_dry-run.jsonl{,.summary.json}
20260529T103610Z_exp006_cache_key_bucketing_inmemory_smoke.jsonl{,.summary.json}
20260529T104555Z_exp006_cache_key_bucketing_24h_smoke.jsonl{,.summary.json}
```

The dry-run artifacts (which never touched Azure) are quarantined for
provenance: they were generated on the same v2.3 working tree as the
failed smokes and reference the v2.3 controls in
`pinned_confounds_echo`. The v2.4 Stage 0 dry-runs replace them as
authoritative pinning fixtures.

## Related quarantines

* `../_v2.1_diagnostic/` — v2.1 concurrency=1 / sustain_tps=1.0 /
  30 K-token corpus diagnostics, kept for the same forensic reason.
* No `_v2.2_diagnostic/`: v2.2 was an in-spec proposal that never ran
  live before v2.3 superseded it.
