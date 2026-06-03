# Observability schema — canonical PTU record contract

**Task 028 anchor.** This document defines the single canonical
per-request and per-cell record shape that every PTU-aware script in
this repo emits. Downstream aggregators (Task 020, future analytics)
and audits (Task 029 category labeling) rely on it.

The schema is generated from
`batch_runner/observability/schema.py` and emitted as JSON Schema files
under `schemas/`. The Azure Monitor correlation contract lives in
`batch_runner/observability/azure_monitor_contract.py`.

---

## 1. Purpose

Before Task 028, each measurement script (013, 019, 021, …) defined its
own keyset. Task 020's aggregator had to translate between shapes.
Adding more measurement tasks would multiply that cost. Task 028 fixes
the shape once: header field names match the Azure OpenAI PTU
Operations Guide Appendix A and B verbatim, Azure Monitor metric names
match Appendix C verbatim, and every field carries an
``official_spec`` vs ``operational_inference`` tag per Task 029.

---

## 2. Per-request schema — `PTURequestRecord`

One record per Azure OpenAI request. Emitted as a JSON Lines row.

### Identity fields (operational inference)

| Field | Type | Notes |
|---|---|---|
| `request_idx` | int | Monotonic index within a measurement run. Repo convention. |
| `wallclock_timestamp_iso` | string (ISO 8601) | UTC. |
| `deployment_name_requested` | string | What the client targeted, before any spillover. |

### Azure response headers (official spec — Appendix A / B)

Header names are case-sensitive lowercase (HTTP/2 style) and match the
Guide verbatim. The JSON Schema attaches a `header_name` annotation on
each.

| Field | Type | `header_name` | Appendix | Notes |
|---|---|---|---|---|
| `response_status_code` | int | — | HTTP status line | |
| `retry_after_ms` | number \| null | `retry-after-ms` | A | Contract-reliable on 429. Coexists with `retry_after_seconds` — both fields, no normalization. |
| `retry_after_seconds` | number \| null | `retry-after` | A | Contract-reliable on 429. |
| `x_ms_region` | string \| null | `x-ms-region` | A | Observability. |
| `x_request_id` | string \| null | `x-request-id` | A | Observability. |
| `x_ms_deployment_name` | string \| null | `x-ms-deployment-name` | B | Actual server (may differ from requested on spillover). |
| `x_ms_spillover_from_deployment` | string \| null | `x-ms-spillover-from-deployment` | B | Spillover origin. |
| `x_ms_spillover_error` | string \| null | `x-ms-spillover-error` | B | Spillover failure code. |
| `x_ratelimit_remaining_requests` | int \| null | `x-ratelimit-remaining-requests` | A | **Optional on PTU path.** The Guide describes `x-ratelimit-*` for Standard deployments; they are not contract-guaranteed on PTU. The schema flags this with `optional_on_ptu: true`. |

### OpenAI usage block (official spec)

| Field | Type | Source |
|---|---|---|
| `prompt_tokens` | int | `usage.prompt_tokens` |
| `completion_tokens` | int | `usage.completion_tokens` |
| `cached_tokens` | int | `usage.prompt_tokens_details.cached_tokens` |
| `reasoning_tokens` | int | `usage.completion_tokens_details.reasoning_tokens` |
| `total_tokens` | int | `usage.total_tokens` |

### Request parameters that influence admission (official spec)

| Field | Type | Notes |
|---|---|---|
| `max_output_tokens_sent` | int \| null | The value passed in the request. |
| `prompt_cache_key_used` | string \| null | **Hash, never raw.** See §5. |
| `prompt_cache_retention_sent` | enum: `in_memory` \| `24h` \| null | |
| `reasoning_effort_sent` | enum: `minimal` \| `low` \| `medium` \| `high` \| null | |
| `model_id` | string | The `model` field sent on the request. |

### Latency (operational inference)

| Field | Type | Notes |
|---|---|---|
| `first_token_latency_ms` | number \| null | Client-side. May be null for non-streaming. |
| `total_latency_ms` | number | Client-side wall time. |

---

## 3. Per-cell summary schema — `PTUCellSummary`

One record per measurement cell window (see Task 013 cell taxonomy).

| Field | Type | Category | Notes |
|---|---|---|---|
| `cell_id` | string | inference | Repo cell id. |
| `cell_label` | string | inference | Human-readable. |
| `window_start_iso` | string | inference | UTC. |
| `window_end_iso` | string | inference | UTC. |
| `deployment_name` | string | inference | Deployment under measurement. |
| `request_count` | int | inference | Number of `PTURequestRecord` rows in the window. |
| `real_429_count` | int | inference | Rows with `response_status_code == 429`. |
| `mean_cached_fraction` | number | inference | `cached_tokens / prompt_tokens`, averaged. |
| `p50_ttft_ms` | number | inference | Percentile of `first_token_latency_ms`. |
| `p95_ttft_ms` | number | inference | |
| `p99_ttft_ms` | number | inference | |
| `mean_retry_after_ms_on_429` | number \| null | inference | Null if no 429s in the window. |
| `azure_monitor_metrics_to_query` | array of string | **official_spec** | Frozen Appendix C list — see §4. |

---

## 4. Azure Monitor correlation contract

`batch_runner.observability.azure_monitor_contract` exposes:

```python
AZURE_MONITOR_PTU_METRICS = (
    "AzureOpenAIProvisionedManagedUtilizationV2",
    "AzureOpenAIRequests",
    "AzureOpenAITimeToResponse",
    "AzureOpenAITTLTInMS",
    "AzureOpenAIContextTokensCacheMatchRate",
    "ActiveTokens",
)

def azure_monitor_correlation_window(record, *, pad_seconds=60) -> (start_iso, end_iso): ...
```

**All six metrics MUST be queried on the same time window** when
interpreting a `PTURequestRecord`. The window is derived from the
record's `wallclock_timestamp_iso` with a symmetric pad (default 60 s)
on each side — wide enough to land inside Azure Monitor's 1-minute
aggregate bucket regardless of small clock skew.

The helper is **pure**: no Azure SDK import, no network call, no
credential read. Operators (or downstream offline tools) perform the
actual query using the returned window.

`AzureOpenAIRequests` is queried with the `StatusCode` and
`IsSpillover` dimensions per Appendix C.

---

## 5. What MUST NOT be in the schema

The schema deliberately omits the following. Tests in
`batch-runner/tests/test_observability_schema.py` enforce these.

- **Authentication.** No `api_key`, no `Authorization` header field.
- **Request body content.** No `messages`, no `system_prompt`, no
  `prompt`, no `content`, no `request_body`.
- **Environment variable values.** No fields that mirror env-var values.
- **Raw `prompt_cache_key`.** The Guide §1 promotes
  `prompt_cache_key` to a routing parameter; the raw value can carry
  tenant or customer identifiers. The schema stores only a stable
  **hash** via `hash_cache_key(raw)`, which returns a 16-character
  lowercase hex digest (truncated SHA-256). Enforcement is field-level:
  `PTURequestRecord.__post_init__` normalizes any non-digest string
  through `hash_cache_key`, and the emitted JSON Schema constrains
  the field to `^[0-9a-f]{16}$` (length 16). Tests in
  `batch-runner/tests/test_observability_schema.py` cover raw-input
  normalization, already-valid-digest preservation, and the `None`
  case.

---

## 6. Migration path

Existing measurement scripts (013, 019, 021) MAY adopt the canonical
schema in a follow-up minor revision; this task ships schema, tests,
and doc only, with no breaking change to existing record shapes.
Until adoption, Task 020's aggregator translates older shapes using
the mapping below.

### Mapping table (older record → canonical field)

| Older record key | Originating task | Canonical field |
|---|---|---|
| `endpoint_hit` | Task 013 | `x_ms_deployment_name` |
| `deployment_requested` | Task 013 | `deployment_name_requested` |
| `spillover_origin` | Task 013 | `x_ms_spillover_from_deployment` |
| `spillover_err` | Task 013 | `x_ms_spillover_error` |
| `region` | Task 013 | `x_ms_region` |
| `request_id` | Task 013 / 021 | `x_request_id` |
| `429_observed` | Task 019 | `response_status_code == 429` |
| `retry_after_ms` | Task 019 / 020 | `retry_after_ms` (unchanged) |
| `retry_after` | Task 019 | `retry_after_seconds` |
| `ratelimit_remaining` | Task 019 | `x_ratelimit_remaining_requests` |
| `usage.prompt_tokens` | Task 013+ | `prompt_tokens` |
| `usage.completion_tokens` | Task 013+ | `completion_tokens` |
| `usage.prompt_tokens_details.cached_tokens` | Task 023 | `cached_tokens` |
| `usage.completion_tokens_details.reasoning_tokens` | Task 023 | `reasoning_tokens` |
| `usage.total_tokens` | Task 013+ | `total_tokens` |
| `max_output_tokens` | Task 019 | `max_output_tokens_sent` |
| `cache_key` (raw) | Task 024 | `prompt_cache_key_used` (must be hashed) |
| `cache_retention` | Task 024 | `prompt_cache_retention_sent` |
| `reasoning_effort` | Task 023 | `reasoning_effort_sent` |
| `model` | Task 013+ | `model_id` |
| `ttft_ms` | Task 013+ | `first_token_latency_ms` |
| `latency_ms` / `wall_ms` | Task 013+ | `total_latency_ms` |

For aggregator output, the old per-window summary key `n_429` maps to
`real_429_count`; `p50_latency_ms` (TTFT) maps to `p50_ttft_ms`, etc.

---

## 7. Compliance with Task 029

Every field in both records carries a `category` tag in the emitted
JSON Schema:

- `official_spec` — name and semantics come from the Guide
  (Appendix A / B / C) or the OpenAI `usage` block.
- `operational_inference` — repo convention (e.g. `cell_id`,
  `request_idx`, latency measurements).

The schema files therefore satisfy Task 029's per-field labeling
requirement and serve as the worked example for future schemas.

---

## 8. Privacy & secrets — enforcement summary

- `prompt_cache_key_used` is a 16-hex-char SHA-256 prefix; raw key
  never written.
- No auth fields exist (`api_key`, `Authorization`).
- No request body content fields exist (`messages`, `system_prompt`,
  `prompt`, `content`, `request_body`).
- No environment variable values are echoed.
- No Azure / OpenAI SDK imports in the observability package; no
  network calls; no credential reads. The package is import-safe in
  any environment.
