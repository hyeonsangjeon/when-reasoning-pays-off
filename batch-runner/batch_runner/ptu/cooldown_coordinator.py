"""Multi-worker PTU cooldown coordinator (Task 025).

This module layers a coordination protocol on top of the Task 023
``AdmissionController`` so that N workers each receiving a 429 with the
same ``retry-after-ms`` value do NOT resume at the same wall-clock
instant. Without coordination, N workers reading the identical header
synchronize their retries onto the PTU's leak-bucket edge and trigger
a fresh 429 wave (a thundering herd).

Methodology note (per Task 029 classification):
the slot-claim coordination mechanism implemented here is
**operational inference**. The Azure OpenAI PTU Operations Guide (§0)
recommends header-driven recovery as the per-request primary mechanism
but is silent on cross-worker retry-timing coordination. The slot-claim
behavior added by ``CooldownCoordinator`` is therefore *not* a
Microsoft Learn specification and is *not* derived from the Guide; it
is a runtime convention this project adopts and documents in
``docs/11-multi-worker-cooldown.md``.

Single-owner retry rule:
the coordinator does NOT own a retry budget. There is no
``max_attempts`` constructor parameter. The wrapped
``AdmissionController`` remains the only component that decides whether
to retry, when to give up, and how to fall back. The coordinator only
augments the sleep duration with a per-worker slot offset.

N=1 invariant:
with one worker against any backend that returns a zero offset for the
sole active worker (``InMemoryCooldownBackend`` does so by construction),
the coordinator adds zero latency relative to the bare controller. This
is verified by ``test_cooldown_coordinator.py``.
"""

from __future__ import annotations

import random
from typing import Any, Callable, Literal

from .admission_controller import AdmissionController, ThrottleEvent
from .cooldown_backends import CooldownBackend

JitterStrategy = Literal["uniform", "exponential", "deterministic_slot"]

_VALID_STRATEGIES: tuple[str, ...] = (
    "deterministic_slot",
    "uniform",
    "exponential",
)


class CooldownCoordinator:
    """Wraps an ``AdmissionController`` with cross-worker slot coordination.

    Parameters
    ----------
    controller:
        The Task 023 admission controller. Retry budget, ``retry-after``
        parsing, and ceiling decisions remain owned by this object.
    backend:
        A ``CooldownBackend`` implementation that assigns per-worker
        slot offsets keyed on ``deployment_key``.
    deployment_key:
        Identifier for the Azure OpenAI deployment shared across
        workers. Workers coordinating against the same deployment MUST
        use the same key.
    worker_id:
        Identifier for this worker. MUST be unique across workers
        coordinating against the same ``deployment_key``.
    slot_width_ms:
        Width of each slot in milliseconds. With N active workers,
        deterministic offsets land in ``[0, slot_width_ms * N)``.
    jitter_strategy:
        ``"deterministic_slot"`` (default): use the backend's slot
        offset directly. ``"uniform"``: replace with a random offset
        in ``[0, slot_width_ms * N)``. ``"exponential"``: replace with
        an exponentially distributed offset with mean ``slot_width_ms``.
    rng:
        Optional ``random.Random`` instance for ``"uniform"`` and
        ``"exponential"`` strategies. Pass a seeded ``Random`` for
        reproducible tests.
    """

    def __init__(
        self,
        *,
        controller: AdmissionController,
        backend: CooldownBackend,
        deployment_key: str,
        worker_id: str,
        slot_width_ms: int = 100,
        jitter_strategy: JitterStrategy = "deterministic_slot",
        rng: random.Random | None = None,
    ) -> None:
        if not deployment_key:
            raise ValueError("deployment_key must be a non-empty string")
        if not worker_id:
            raise ValueError("worker_id must be a non-empty string")
        if slot_width_ms < 0:
            raise ValueError("slot_width_ms must be >= 0")
        if jitter_strategy not in _VALID_STRATEGIES:
            raise ValueError(
                f"jitter_strategy must be one of {_VALID_STRATEGIES}"
            )
        self._controller = controller
        self._backend = backend
        self._deployment_key = deployment_key
        self._worker_id = worker_id
        self._slot_width_ms = int(slot_width_ms)
        self._jitter_strategy = jitter_strategy
        self._rng = rng if rng is not None else random.Random()

    @property
    def deployment_key(self) -> str:
        return self._deployment_key

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def jitter_strategy(self) -> str:
        return self._jitter_strategy

    def _active_worker_count(self) -> int:
        count_fn = getattr(self._backend, "active_worker_count", None)
        if callable(count_fn):
            try:
                return max(1, int(count_fn(self._deployment_key)))
            except Exception:
                return 1
        return 1

    def _apply_strategy(self, raw_offset_ms: int) -> int:
        if self._jitter_strategy == "deterministic_slot":
            return max(0, int(raw_offset_ms))
        n_workers = self._active_worker_count()
        if self._jitter_strategy == "uniform":
            upper = max(1, self._slot_width_ms * n_workers)
            return int(self._rng.random() * upper)
        # exponential
        mean_ms = max(1, self._slot_width_ms)
        return max(0, int(self._rng.expovariate(1.0 / mean_ms)))

    def call(self, send: Callable[[], Any], *, request: Any) -> Any:
        """Execute ``send`` with cross-worker slot coordination.

        Mirrors ``AdmissionController.call`` but adds, on each 429
        sleep, a backend-assigned slot offset so that this worker's
        resume instant is dispersed away from peer workers seeing the
        same ``retry-after-ms``.
        """
        ctrl = self._controller
        orig_sleep_fn = ctrl._sleep_fn  # noqa: SLF001 — intentional wrap
        orig_on_throttle = ctrl._on_throttle  # noqa: SLF001

        pending = {"offset_ms": 0, "claimed": False}

        def wrapped_on_throttle(event: ThrottleEvent) -> None:
            if event.decision == "sleep":
                raw = self._backend.claim_slot(
                    self._deployment_key,
                    self._worker_id,
                    int(event.parsed_wait_ms),
                )
                pending["offset_ms"] = self._apply_strategy(int(raw))
                pending["claimed"] = True
            if orig_on_throttle is not None:
                try:
                    orig_on_throttle(event)
                except Exception:
                    pass

        def wrapped_sleep(seconds: float) -> None:
            extra_ms = pending["offset_ms"]
            pending["offset_ms"] = 0
            total = float(seconds) + (extra_ms / 1000.0)
            if total > 0:
                orig_sleep_fn(total)

        ctrl._sleep_fn = wrapped_sleep  # noqa: SLF001
        ctrl._on_throttle = wrapped_on_throttle  # noqa: SLF001
        try:
            return ctrl.call(send, request=request)
        finally:
            ctrl._sleep_fn = orig_sleep_fn  # noqa: SLF001
            ctrl._on_throttle = orig_on_throttle  # noqa: SLF001
            if pending["claimed"]:
                try:
                    self._backend.release_slot(
                        self._deployment_key, self._worker_id
                    )
                except Exception:
                    pass


__all__ = [
    "CooldownCoordinator",
    "JitterStrategy",
]
