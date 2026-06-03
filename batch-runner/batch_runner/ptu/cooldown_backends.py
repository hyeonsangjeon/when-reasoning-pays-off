"""Pluggable cooldown-coordination backends (Task 025).

This module exposes the ``CooldownBackend`` protocol consumed by
``CooldownCoordinator`` plus two reference implementations:

* ``InMemoryCooldownBackend`` — process-local, the default for
  single-host ``batch-runner/`` runs. No network calls.
* ``KeyValueCooldownBackend`` — interface example over an injectable
  client matching a minimal ``incr`` / ``expire`` / ``get`` shape.
  A Redis client is one valid choice but is supplied by the caller;
  this module does **not** import any external client library and
  ``pyproject.toml`` is not modified.

Methodology note (per Task 029 classification):
the slot-claim coordination mechanism is **operational inference**.
It does not appear in the PTU Operations Guide or in any Azure
Microsoft Learn specification; it is a runtime convention this
project adopts to avoid synchronized retry storms across workers.

Data integrity:
backends log only ``deployment_key``, ``worker_id``, ``retry_after_ms``,
``slot_offset_ms``, and ``wallclock_iso``. Backends MUST NOT log
request bodies, prompts, prompt-cache keys, or auth headers.
"""

from __future__ import annotations

import threading
from typing import Any, Protocol


class CooldownBackend(Protocol):
    """Coordination backend interface (Task 025).

    Implementations assign each ``worker_id`` contending for the same
    ``deployment_key`` a distinct time offset (in milliseconds) so that
    N workers reading the same ``retry-after-ms`` do not resume at the
    same wall-clock instant.

    The backend MUST be safe to call from concurrent workers.
    """

    def claim_slot(
        self, deployment_key: str, worker_id: str, retry_after_ms: int
    ) -> int:
        """Claim and return this worker's slot offset in milliseconds.

        The returned value is added to ``retry_after_ms`` by the
        coordinator before sleeping. Calling ``claim_slot`` multiple
        times for the same ``(deployment_key, worker_id)`` before a
        ``release_slot`` MUST return the same offset (idempotent re-claim).
        """
        ...

    def release_slot(self, deployment_key: str, worker_id: str) -> None:
        """Release this worker's slot so the next contender can rebalance."""
        ...


class InMemoryCooldownBackend:
    """Process-local coordination backend.

    Suitable for single-host multi-worker batch runs (the default for
    ``batch-runner/``). Thread-safe; uses an internal lock.

    The backend assigns the lowest unused slot index per deployment
    and multiplies by ``slot_width_ms``. With N concurrent workers,
    offsets land in ``[0, slot_width_ms * N)`` and are pairwise distinct.
    """

    def __init__(self, *, slot_width_ms: int = 100) -> None:
        if slot_width_ms <= 0:
            raise ValueError("slot_width_ms must be > 0")
        self._slot_width_ms = int(slot_width_ms)
        self._lock = threading.Lock()
        # deployment_key -> {worker_id: slot_index}
        self._claims: dict[str, dict[str, int]] = {}

    @property
    def slot_width_ms(self) -> int:
        return self._slot_width_ms

    def active_worker_count(self, deployment_key: str) -> int:
        """Return the number of currently claimed slots for a deployment.

        Exposed for ``CooldownCoordinator`` jitter strategies that need
        a worker-count estimate (e.g. ``uniform``). Not part of the
        ``CooldownBackend`` protocol surface.
        """
        with self._lock:
            return len(self._claims.get(deployment_key, {}))

    def claim_slot(
        self, deployment_key: str, worker_id: str, retry_after_ms: int
    ) -> int:
        if retry_after_ms < 0:
            raise ValueError("retry_after_ms must be >= 0")
        with self._lock:
            slots = self._claims.setdefault(deployment_key, {})
            existing = slots.get(worker_id)
            if existing is not None:
                return existing * self._slot_width_ms
            used = set(slots.values())
            slot_index = 0
            while slot_index in used:
                slot_index += 1
            slots[worker_id] = slot_index
            return slot_index * self._slot_width_ms

    def release_slot(self, deployment_key: str, worker_id: str) -> None:
        with self._lock:
            slots = self._claims.get(deployment_key)
            if not slots:
                return
            slots.pop(worker_id, None)
            if not slots:
                self._claims.pop(deployment_key, None)


class KeyValueCooldownBackend:
    """Interface example over an injectable key-value client.

    The ``client`` argument is duck-typed; it MUST expose three methods::

        client.incr(key: str) -> int
        client.expire(key: str, seconds: int) -> None
        client.get(key: str) -> str | bytes | None

    A Redis client (``redis-py``) is one valid choice, but this module
    does NOT import ``redis`` and does NOT add it to ``pyproject.toml``.
    Callers supply the client. No live Redis URL is hard-coded here.

    The default ``ttl_seconds`` (60) bounds the lifetime of stale
    counters so a crashed worker does not permanently consume a slot.
    """

    _COUNTER_FMT = "cooldown:{deployment_key}:counter"
    _WORKER_FMT = "cooldown:{deployment_key}:worker:{worker_id}"

    def __init__(
        self,
        client: Any,
        *,
        slot_width_ms: int = 100,
        ttl_seconds: int = 60,
    ) -> None:
        if slot_width_ms <= 0:
            raise ValueError("slot_width_ms must be > 0")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        for method in ("incr", "expire", "get"):
            if not callable(getattr(client, method, None)):
                raise TypeError(
                    f"client must expose callable .{method}(); "
                    "see KeyValueCooldownBackend docstring."
                )
        self._client = client
        self._slot_width_ms = int(slot_width_ms)
        self._ttl_seconds = int(ttl_seconds)

    @property
    def slot_width_ms(self) -> int:
        return self._slot_width_ms

    @staticmethod
    def _coerce_index(raw: Any) -> int | None:
        if raw is None:
            return None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("ascii", errors="replace")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def claim_slot(
        self, deployment_key: str, worker_id: str, retry_after_ms: int
    ) -> int:
        if retry_after_ms < 0:
            raise ValueError("retry_after_ms must be >= 0")
        worker_key = self._WORKER_FMT.format(
            deployment_key=deployment_key, worker_id=worker_id
        )
        cached = self._coerce_index(self._client.get(worker_key))
        if cached is not None and cached >= 0:
            self._client.expire(worker_key, self._ttl_seconds)
            return cached * self._slot_width_ms

        counter_key = self._COUNTER_FMT.format(deployment_key=deployment_key)
        counter = self._coerce_index(self._client.incr(counter_key))
        if counter is None or counter < 1:
            slot_index = 0
        else:
            slot_index = counter - 1
        self._client.expire(counter_key, self._ttl_seconds)
        try:
            self._client.set(worker_key, str(slot_index))  # type: ignore[attr-defined]
        except AttributeError:
            pass
        self._client.expire(worker_key, self._ttl_seconds)
        return slot_index * self._slot_width_ms

    def release_slot(self, deployment_key: str, worker_id: str) -> None:
        worker_key = self._WORKER_FMT.format(
            deployment_key=deployment_key, worker_id=worker_id
        )
        try:
            self._client.delete(worker_key)  # type: ignore[attr-defined]
        except AttributeError:
            self._client.expire(worker_key, 1)


__all__ = [
    "CooldownBackend",
    "InMemoryCooldownBackend",
    "KeyValueCooldownBackend",
]
