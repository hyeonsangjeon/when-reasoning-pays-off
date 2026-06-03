# Operator Guide — PTU Admission Controller (Header-Driven)

> Module: `batch_runner.ptu.admission_controller`. Task 023.
> Scope: one client-side component that honours `retry-after-ms` on
> Azure OpenAI 429 responses. Pairs with the PTU Operations Guide §0.

## 1. Why this exists

The Azure OpenAI PTU Operations Guide §0 names `retry-after-ms` as the
official admission signal for the next acceptable request time on a
throttled PTU deployment. This module is the deployable code path that
honours that signal explicitly and logs every decision, so a reviewer
can answer "why did this client sleep that long?" from logs alone
instead of inferring SDK retry behaviour. It is the runtime complement
to Task 020's offline characterization of the same header.

## 2. Three options the Guide names

The Guide §0 catalogues three deployable patterns:

- **A. SDK Defaults** — let the `openai` Python SDK auto-retry honour
  `retry-after`. Minimal code; opaque to audit.
- **B. PAYG Fallback** — read `retry-after-ms` and immediately route to
  a fallback (e.g., PAYG) endpoint when the wait exceeds a ceiling.
- **C. Native Spillover** — server-side `spilloverDeploymentName`
  routing, owned by Task 021. Out of scope here.

This module implements **A** and **B**. Use it when your caller cannot
rely on native spillover (cross-region, cross-tenant, PAYG-only target,
or audit posture requires explicit retry accounting).

## 3. Usage

The recipes below use the default jitter for clarity. Tests pass
`jitter_fn=lambda w: w` for determinism; production callers typically
leave the default in place.

### 3.1 Synchronous — Option A (explicit, single-owner retry)

The recipe below disables SDK auto-retry (`max_retries=0`) so the
controller is the sole retry owner. Leaving SDK auto-retry enabled is
a configuration error — see §4.

```python
from batch_runner.ptu import AdmissionController

ctrl = AdmissionController(
    max_attempts=3,
    max_wait_ms=30_000,
    on_throttle=audit_log.append,
    client=sdk_client,            # must have max_retries == 0
)

def send():
    return sdk_client.post(url, json=payload)

response = ctrl.call(send, request=payload)
```

### 3.2 Asynchronous wrap

The controller itself is synchronous on purpose (one decision per
attempt, blocking on a sleep). To use it from `async` code, run the
`call` in a thread executor:

```python
import asyncio

async def admit_async(send, *, request):
    return await asyncio.to_thread(ctrl.call, send, request=request)
```

Do not call `ctrl.call` from inside the event loop directly; the
injected `sleep_fn` defaults to `time.sleep`.

### 3.3 With PAYG fallback — Option B

```python
def to_payg(request):
    return payg_client.post(payg_url, json=request)

ctrl = AdmissionController(
    max_attempts=1,
    max_wait_ms=0,                # never sleep; fall back immediately
    fallback=to_payg,
    on_throttle=audit_log.append,
)

response = ctrl.call(send, request=payload)
```

`max_wait_ms=0` together with a configured `fallback` is the exact
"immediate PAYG fallback" mapping called out in Guide §0 Option B.

## 4. Single-owner retry rule

> Retry ownership must live in exactly one place. Running the
> controller over an SDK that also retries produces compounded waits
> and obscures admission accounting.

The controller refuses to wrap an SDK/client object whose
`max_retries` attribute is a positive integer; passing one raises
`DoubleRetryError` at construction. If you cannot set the SDK's
`max_retries` to `0`, do not pass that client to the controller — use
the controller XOR the SDK's auto-retry, never both. A client without
a `max_retries` attribute is accepted unchanged.

## 5. What this module does NOT do

- It does not coordinate retries across worker processes — Task 025
  owns the multi-worker layer.
- It does not re-characterize the `retry-after-ms` distribution —
  Task 020 owns the descriptive statistics; this module only honours
  whatever value the header carries.
- It does not implement native server-side spillover — Task 021 owns
  that comparison.
- It does not own PAYG endpoint selection, capacity planning, or rate
  budgeting — Task 027 owns the decision calculator. The `fallback`
  parameter is the seam where those decisions plug in.
- It does not own observability schema. See §7 below.

## 6. Compliance with `docs/05-methodology.md` §7

Every retry decision the controller takes is emitted as one
`ThrottleEvent` to the caller-supplied `on_throttle` callback. The
event records: the pre-jitter parsed wait (`parsed_wait_ms`), the
actual post-jitter sleep duration (`wait_ms`), attempt index, which
header the wait was parsed from (`retry-after-ms` or `retry-after`),
wall-clock ISO timestamp, HTTP status, the decision (`sleep` /
`fallback` / `give-up`), and an allow-listed subset of safe response
headers.

The controller deliberately omits from its logs and event surface:

- Request body, system prompt, `messages` content.
- `Authorization` header and any bearer token.
- API keys and `api-key` headers.
- Environment variable values.
- Raw `prompt_cache_key` values.
- Endpoint hostnames, tenant or subscription IDs.

The only response headers the controller will surface are
`x-request-id` and `x-ms-region`, and only when present.

## 7. Provisional log keys (Task 028 not yet APPROVE'd)

Task 028 will define the canonical observability schema for all PTU
runtime components. Until that schema lands, the controller's
`ThrottleEvent` fields are **provisional** and listed here for review:

| Key                  | Type    | Notes                                       |
|----------------------|---------|---------------------------------------------|
| `wait_ms`            | int     | Actual sleep duration after jitter; clamped to `[0, ...]`. |
| `parsed_wait_ms`     | int     | Pre-jitter admission value parsed from the header (0 if absent). Preserves the raw `retry-after-ms` signal for Task 020-style aggregation. |
| `attempt_idx`        | int     | 1-based; matches the `send()` call index.   |
| `parsed_from_header` | str?    | `"retry-after-ms"`, `"retry-after"`, or null.|
| `wallclock_iso`      | str     | UTC, millisecond precision, ISO-8601.       |
| `status_code`        | int     | HTTP status (always `429` today).           |
| `decision`           | str     | One of `sleep`, `fallback`, `give-up`. Decided from `parsed_wait_ms`, not from the jittered value. |
| `headers`            | map     | Safe subset only: `x-request-id`, `x-ms-region`. |

If Task 028 APPROVE's a different schema, the controller is expected
to migrate; the `on_throttle` seam is the migration point.

## 8. Typed exceptions

- `AdmissionExhausted` — persistent 429 across `max_attempts`.
- `WaitExceedsCeiling` — parsed wait exceeded `max_wait_ms` and no
  `fallback` was configured.
- `DoubleRetryError` — single-owner retry rule violated at
  construction.

## 9. Non-goals (restated)

This module is a small, auditable runtime seam. It does not chase
exotic SDK behaviour, it does not invent a new backoff policy beyond
"honour the header plus jitter," and it does not extend the Guide's
three named options. New behaviour belongs in new tasks.
