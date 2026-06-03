# Operator Guide — Multi-Worker PTU Cooldown Coordination

> Module: `batch_runner.ptu.cooldown_coordinator`. Task 025.
> Scope: cross-worker retry-timing coordination for Azure OpenAI PTU
> 429 responses. Pairs with the Task 023 admission controller
> (`docs/10-ptu-admission-controller.md`) and the PTU Operations
> Guide §0.

## 1. The problem the Guide does not solve

The PTU Operations Guide §0 establishes header-driven recovery as the
per-request primary mechanism: when a worker receives a 429, it reads
`retry-after-ms` and resumes after that interval. Task 023 implements
this for a single worker.

The Guide is silent on a second-order question: **what happens when N
workers, each acting alone, receive 429s with the same
`retry-after-ms` value?** Each worker sleeps for the same interval and
resumes at approximately the same wall-clock instant. The PTU then
receives N coincident requests on the leak-bucket edge and emits a
fresh wave of 429s. The single-worker controller cannot prevent this;
it observes only its own request stream.

This document specifies the additive coordination layer that prevents
the herd. It does not replace the Task 023 controller; it composes
with it.

### Methodology classification

The slot-claim coordination mechanism described below is **operational
inference** per the Task 029 classification. It is not a Microsoft
Learn specification and does not appear in the PTU Operations Guide.
It is a runtime convention this project adopts when operating
multi-worker batches against a shared deployment, and it MUST be
labeled as such anywhere it is cited.

## 2. Coordination protocol

`CooldownCoordinator` wraps an `AdmissionController` and an injected
`CooldownBackend`. On every 429 the underlying controller decides to
sleep on, the coordinator:

1. Calls `backend.claim_slot(deployment_key, worker_id, retry_after_ms)`
   to obtain a per-worker time offset (milliseconds).
2. Adds that offset to the controller's parsed `retry-after-ms` value.
3. Sleeps for the augmented duration.
4. On successful resume (or on any path that exits the call), calls
   `backend.release_slot(...)` so subsequent retries can rebalance.

```
worker A   ── 429, retry-after-ms=500 ──┐
worker B   ── 429, retry-after-ms=500 ──┼─►  backend.claim_slot
worker C   ── 429, retry-after-ms=500 ──┘     │
                                              ▼
                                  A: offset=0, sleeps 500 ms
                                  B: offset=100, sleeps 600 ms
                                  C: offset=200, sleeps 700 ms
```

The default `deterministic_slot` strategy guarantees pairwise distinct
offsets across active workers contending for the same `deployment_key`.

### Surface

```python
CooldownCoordinator(
    *,
    controller: AdmissionController,
    backend: CooldownBackend,
    deployment_key: str,
    worker_id: str,
    slot_width_ms: int = 100,
    jitter_strategy: Literal[
        "deterministic_slot", "uniform", "exponential"
    ] = "deterministic_slot",
    rng: random.Random | None = None,
)

coordinator.call(send, *, request) -> response
```

The coordinator has **no** `max_attempts` argument. Retry budget,
ceiling decisions, and fallback policy remain owned by the wrapped
`AdmissionController` (the single-owner retry rule from Task 023). The
coordinator only augments sleep duration.

### Jitter strategies

| Strategy | Offset |
| --- | --- |
| `deterministic_slot` (default) | `slot_index × slot_width_ms` from backend |
| `uniform` | random in `[0, slot_width_ms × N_active)` (seeded `rng`) |
| `exponential` | exponential, mean `slot_width_ms` (seeded `rng`) |

`uniform` and `exponential` accept a `random.Random` instance for
reproducible runs; tests pass a seeded instance.

## 3. Backends

### `InMemoryCooldownBackend` (default)

Process-local, thread-safe via an internal `Lock`. Suitable for the
single-host `batch-runner/` deployment. No network calls. No external
dependency.

```python
backend = InMemoryCooldownBackend(slot_width_ms=100)
```

### `KeyValueCooldownBackend(client)` (interface example)

A reference implementation over an injectable key-value client. The
client object MUST expose:

| Method | Semantics |
| --- | --- |
| `incr(key) -> int` | atomic monotonic counter |
| `expire(key, seconds)` | TTL on the key |
| `get(key) -> str \| bytes \| None` | read |

A Redis client (`redis-py`) is one valid choice. **This project does
not import `redis` and does not add it to `pyproject.toml`.** The
client is supplied by the caller; no live URL is hard-coded. For
single-host runs, prefer `InMemoryCooldownBackend`.

## 4. Operational checklist

Use the coordinator when:

- Four or more workers (`N ≥ 4`) run concurrently against the same
  Azure OpenAI deployment, AND
- The workload is steady-state enough that synchronized 429 waves
  measurably distort measurement timing or retried-prompt spend.

Skip the coordinator when:

- A single worker is in use → the Task 023 controller alone is
  sufficient and the coordinator adds zero behavior anyway (verified
  by the N=1 invariant test).
- The deployment is PAYG-only with no per-request throttling
  guarantees → use the SDK's native retry instead and consult the
  controller's `DoubleRetryError` guidance.

### N=1 invariant

With a single worker against `InMemoryCooldownBackend`, the backend
assigns slot index 0 → offset 0 ms. The coordinator's sleep duration
equals the bare controller's. No backend round-trip on the hot path
beyond an in-memory `dict` write. The unit test
`test_single_worker_invariant_zero_added_latency` enforces this.

### Concurrency simulation

`test_cooldown_thundering_herd.py` spawns 20 simulated workers
receiving an identical 429 wave. With the coordinator and
`slot_width_ms=100`, no two workers' resume instants land within 50 ms
of each other. The control run (no coordinator) places all 20 workers
at the same instant (`C(20, 2) = 190` clustered pairs). The
coordinator's resulting clustering is zero pairs, satisfying the
`≥ 5× reduction` acceptance bar.

## 5. What this does NOT do

- Cross-region failover. The coordinator scopes to one
  `deployment_key`. Cross-region routing is a separate policy and is
  not in scope for this task.
- Tenant-level budgeting. The coordinator does not enforce
  per-tenant request caps.
- PAYG fallback. Falling back from PTU to PAYG on `WaitExceedsCeiling`
  is the wrapped controller's responsibility (its `fallback` argument).
- Native server-side spillover (Task 021). Spillover routes individual
  requests; the coordinator schedules retry timing.
- Replacing `retry-after-ms` parsing. Parsing remains in the Task 023
  controller; the coordinator consumes the parsed value via the
  `ThrottleEvent` it receives through the wrapped `on_throttle` hook.

## 6. Data integrity and secrets

Backends log only the following fields when logging is enabled by the
caller:

- `deployment_key`
- `worker_id`
- `retry_after_ms`
- `slot_offset_ms`
- `wallclock_iso`

Backends MUST NOT log request bodies, system prompts, `messages`
content, prompt-cache keys, environment variable values, or
`Authorization` headers. The `KeyValueCooldownBackend` does not
hard-code a live Redis URL; the client object is supplied by the
caller.
