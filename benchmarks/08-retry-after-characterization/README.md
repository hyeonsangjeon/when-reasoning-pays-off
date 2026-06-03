# Benchmark 08 — `retry-after-ms` recovery curve characterization

**Task 020.** Pure re-aggregation over existing immutable JSONL streams
from Task 013 (`benchmarks/05-dual-spillover/runs/*.jsonl`) and Task 019
(`benchmarks/07-max-output-tokens-reservation/runs/*.jsonl`).

- **Zero new LLM spend.** No API calls. No network. No client imports.
- **Read-only** over source JSONLs (allowlisted paths only).
- **Descriptive only**, scoped to these source runs. **No** causal /
  reset-formula language. **No** confidence intervals, p-values, or
  significance claims. **No** universal PTU claim.

## What this benchmark is (and is not)

This benchmark **does not** capture new `retry-after-ms` data. Tasks 013
and 019 own that capture. This benchmark re-aggregates the
`retry-after-ms` / `retry-after` values already recorded in their raw
JSONLs into an empirical distribution, and labels every event with its
**source benchmark id** so per-source provenance is never erased.

## Source-aware 429 selection

The two source streams use **different** 429 detection field names. The
aggregator must honor both — using a single shared field would silently
drop one stream.

| Source   | Benchmark id                          | 429 selector |
|----------|---------------------------------------|--------------|
| Task 013 | `05-dual-spillover`                   | `real_429_observed == true` |
| Task 019 | `07-max-output-tokens-reservation`    | `429_observed == true` **OR** `first_429_metadata` present |

Per-source counts (`counts.task013_429`, `counts.task019_429`) are
reported separately in `analysis.json` in addition to the combined view;
combined percentiles never erase per-source provenance.

## Parsing rules

`scripts/retry_after_ms_characterization.py` applies these rules
verbatim:

- `retry_after_ms` numeric → milliseconds as-is
- `retry_after` numeric → seconds × 1000
- `retry_after` non-numeric (HTTP-date / token per RFC 9110) → **skipped
  and counted** in `counts.http_date_retry_after_skipped` (never silently
  dropped). An HTTP-date branch may be added later behind an explicit
  flag.
- missing → skipped and counted in `counts.missing_retry_after_skipped`

## How to regenerate

```bash
python -m scripts.retry_after_ms_characterization \
  --benchmarks 05-dual-spillover,07-max-output-tokens-reservation \
  --out benchmarks/08-retry-after-characterization/analysis.json
```

Charts and CSVs are written to `results/retry-after-characterization/`.
This `README.md` and the sibling `analysis.md` are bootstrapped by the
same command from embedded templates in the script.

## PTU / PAYG / customer-scope caveats (carried forward)

- **Task 019 source is PAYG-throttled-quota, not direct PTU evidence.**
  Use it as a proxy for admission-control behavior; do not state PTU
  causal claims based on Task 019 data alone.
- **Task 013 source is workload-shaped and not customer-attributed.** Do
  not generalize observed `retry-after-ms` shapes to other tenants,
  regions, deployments, model versions, or time periods.
- "Operationally confirmed" framing is **not** used here. Findings are
  described as "observed in these source runs" or "consistent with the
  documented Azure guidance" only.
- Customer-facing advice is limited to: **honor the `retry-after` /
  `retry-after-ms` header Azure returns**. The observed p50 / p99 are
  descriptive context only.

## Reference

The Azure PTU concept documentation describes `retry-after-ms` (and the
HTTP-standard `retry-after`) on a 429 response as the dynamic admission
signal — there is no deterministic reset window. This benchmark
re-aggregates the values we observed in two specific source runs; it is
**not** a universal characterization of Azure behavior and does not
propose any reset formula.
