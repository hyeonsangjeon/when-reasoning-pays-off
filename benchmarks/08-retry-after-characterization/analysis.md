# Task 020 — `retry-after-ms` characterization (analysis)

> Decision-grade narrative — descriptive of **these source runs only**.
> No CIs, no p-values, no significance language, no causal /
> reset-formula language. No universal PTU claim.

## TL;DR

**Recommendation:** honor the `retry-after-ms` (or `retry-after`) header
that Azure returns on every 429. Do not substitute a fixed-window timer.
In these source runs the observed p50 ≈ `43.0` ms and p99 ≈
`16921.1` ms (combined; see per-source breakdown below). These are
**descriptive context only** — do not generalize across tenants,
regions, deployments, model versions, or time periods.

> **Caveat (lead):** results are **imbalanced** (one source ≥ 80% of events). Treat percentiles as shape-only context, not as calibrated targets. Follow-up captures needed to lift this caveat: (a) additional Task 013 Phase 2 proactive runs with primary-overcommit configuration that produces ≥ 50 real_429 events with retry-after headers; (b) additional Task 019 calibration runs whose explored TPS envelope crosses the deployment 429 admission ceiling so 429s with retry-after headers are observed at non-trivial depth.

## What we measured

Re-aggregation of the `retry-after-ms` / `retry-after` field already
captured on every 429 in two existing source streams:

- Task 013 — `benchmarks/05-dual-spillover/runs/*.jsonl` (dual-endpoint
  burst load; 429 selector `real_429_observed == true`)
- Task 019 — `benchmarks/07-max-output-tokens-reservation/runs/*.jsonl`
  (max_output_tokens reservation sweep on a PAYG-throttled deployment;
  429 selector `429_observed == true` OR `first_429_metadata` present)

Counts (`counts` block in `analysis.json`):

| metric | value |
|---|---|
| files_scanned | 12 |
| records_scanned | 8526 |
| total_429 | 193 |
| task013_429 | 167 |
| task019_429 | 26 |
| http_date_retry_after_skipped | 0 |
| unparseable_retry_after_ms_skipped | 0 |
| missing_retry_after_skipped | 0 |

Sparse flag: `False` (threshold: total_429 < 50).
Imbalanced flag: `True` (threshold: one source ≥ 80%).

## Findings

### Distribution of `retry-after-ms` (descriptive)

Empirical percentiles (linear-interpolated), in milliseconds:

| scope   | count | min | p10 | p50 | p90 | p99 | max |
|---------|-------|-----|-----|-----|-----|-----|-----|
| overall | 193 | 1.0 | 3.0 | 43.0 | 50.8 | 16921.1 | 17258.0 |
| task013 (burst) | 167 | 1.0 | 4.0 | 43.0 | 49.0 | 16983.3 | 17258.0 |
| task019 (reservation) | 26 | 1.0 | 2.0 | 3.0 | 60.0 | 60.0 | 60.0 |

Charts:

- `results/retry-after-characterization/retry_after_ms_histogram.png` —
  source-labeled histogram overlay
- `results/retry-after-characterization/retry_after_ms_cdf.png` —
  empirical CDF per source plus combined

CSVs:

- `results/retry-after-characterization/retry_after_ms_events.csv`
- `results/retry-after-characterization/retry_after_ms_percentiles.csv`

### Correlation with overshoot

`correlation_with_overshoot.status = 'not_computable'`. Reason: no numeric projected/admitted utilization proxy with a calibrated capacity denominator is present in Task 013 v2 records; Task 019 v2 records expose arrival_rpm_at_request_time but no calibrated capacity denominator (selected_peak_tps null in available calibration outcomes), so overshoot-above-100% cannot be computed for these source runs

No scatter plot is emitted because the proxy is not computable for these source runs.


## Interpretation

These are **two different 429 mechanisms**:

- Task 013 429s come from primary-deployment burst overload in a
  dual-endpoint experiment. Workload-shaped; not customer-attributed.
- Task 019 429s come from PAYG admission control on a throttled
  deployment exercised by a `max_output_tokens` reservation sweep. PAYG
  throttled-quota is a **proxy** for admission-control behavior, not
  direct PTU evidence.

Quantization / continuity answer: **Observed retry-after values appear clustered / integer-ms quantized, not continuous, in these source runs.** Overall,
41 unique values appeared across
193 events (unique ratio
0.2; integer-ms share
1.0). Task 013 was classified
as `clustered / integer-ms quantized`; Task 019 was classified as
`clustered / integer-ms quantized`. This is a descriptive observation
about these source runs only and not a universal property of the service.

The observed shape is **consistent with the documented Azure guidance**
that there is no fixed reset window, but it does **not** "operationally
confirm" any universal PTU behavior.

## Decision

For a customer's retry wrapper, the four operational answers are:

1. In these source runs, observed `retry-after-ms` values look
   **clustered / integer-ms quantized**, not like a smooth continuous
   distribution.
2. **Honor the `retry-after-ms` (or `retry-after`) header Azure returns
   on every 429.** Do not substitute a fixed timer.
3. Treat the observed p50 / p99 above as descriptive context only.
   Calibrate retry behavior against your own traffic.
4. Do not infer a deterministic reset window or a universal PTU formula
   from this data. None is supported by these source runs.

## Limitations

- Single tenant, single region, snapshot in time. Source runs scoped.
- Task 019 source is PAYG-throttled-quota, not direct PTU.
- Task 013 source is workload-shaped, not customer-attributed.
- No CIs / no significance tests by methodology rule (§8). Percentiles
  are point estimates.
- `correlation_with_overshoot` is not computable for these source runs:
  Task 013 v2 records expose no numeric per-record projected/admitted
  utilization proxy; Task 019 records expose `arrival_rpm_at_request_time`
  but no calibrated capacity denominator (`selected_peak_tps` is null in
  the available calibration outcomes), so overshoot-above-100% cannot be
  computed.
- HTTP-date `retry-after` headers (per RFC 9110) are skipped and
  counted; an explicit HTTP-date parsing branch may be added later
  behind a flag.
