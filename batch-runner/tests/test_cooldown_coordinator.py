"""Unit tests for the multi-worker PTU cooldown coordinator (Task 025).

No network involvement. Sleep is injected as a no-op recorder so the
suite runs in milliseconds. All RNG is seeded for reproducibility.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from batch_runner.ptu import (
    AdmissionController,
    CooldownCoordinator,
    InMemoryCooldownBackend,
    KeyValueCooldownBackend,
    ThrottleEvent,
)

from .fixtures.retry_after_fixtures import (
    FakeRequest,
    make_send_sequence,
    r200,
    r429_with_ms,
)


def _no_jitter(w: int) -> int:
    return w


class _SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _make_controller(*, sleep_fn, on_throttle=None) -> AdmissionController:
    return AdmissionController(
        max_attempts=3,
        max_wait_ms=10_000,
        on_throttle=on_throttle,
        jitter_fn=_no_jitter,
        sleep_fn=sleep_fn,
    )


def test_single_worker_invariant_zero_added_latency() -> None:
    """N=1: coordinator MUST add zero latency vs Task 023 alone."""
    sleep_recorder = _SleepRecorder()
    controller = _make_controller(sleep_fn=sleep_recorder)
    backend = InMemoryCooldownBackend(slot_width_ms=100)
    coordinator = CooldownCoordinator(
        controller=controller,
        backend=backend,
        deployment_key="dep-A",
        worker_id="w-0",
    )
    send = make_send_sequence([r429_with_ms(500), r200()])
    response = coordinator.call(send, request=FakeRequest())
    assert response.status_code == 200
    # Exactly one sleep, exactly retry-after-ms with zero offset.
    assert sleep_recorder.calls == [pytest.approx(0.5)]


def test_single_worker_invariant_matches_controller_alone() -> None:
    """Controller-alone and coordinator+1-worker MUST produce identical sleeps."""
    alone_sleeps = _SleepRecorder()
    alone_ctrl = _make_controller(sleep_fn=alone_sleeps)
    alone_ctrl.call(
        make_send_sequence([r429_with_ms(750), r200()]),
        request=FakeRequest(),
    )

    coord_sleeps = _SleepRecorder()
    coord_ctrl = _make_controller(sleep_fn=coord_sleeps)
    coordinator = CooldownCoordinator(
        controller=coord_ctrl,
        backend=InMemoryCooldownBackend(slot_width_ms=100),
        deployment_key="dep-A",
        worker_id="w-only",
    )
    coordinator.call(
        make_send_sequence([r429_with_ms(750), r200()]),
        request=FakeRequest(),
    )
    assert alone_sleeps.calls == coord_sleeps.calls


def test_n10_distinct_offsets_within_window() -> None:
    """10 workers, same retry-after-ms → 10 distinct offsets in [0, 1000)."""
    backend = InMemoryCooldownBackend(slot_width_ms=100)
    offsets: list[int] = []
    for i in range(10):
        offset = backend.claim_slot("dep-A", f"w-{i}", retry_after_ms=500)
        offsets.append(offset)
    assert len(set(offsets)) == 10
    assert all(0 <= off < 1000 for off in offsets)
    assert sorted(offsets) == [i * 100 for i in range(10)]


def test_release_rebalances_next_claim() -> None:
    """Releasing a slot frees that index for the next claim."""
    backend = InMemoryCooldownBackend(slot_width_ms=100)
    o1 = backend.claim_slot("dep-A", "w-1", 500)  # 0
    o2 = backend.claim_slot("dep-A", "w-2", 500)  # 100
    o3 = backend.claim_slot("dep-A", "w-3", 500)  # 200
    assert (o1, o2, o3) == (0, 100, 200)
    backend.release_slot("dep-A", "w-2")
    o4 = backend.claim_slot("dep-A", "w-4", 500)
    # Lowest unused index is now 1 (worker w-2's old slot).
    assert o4 == 100


def test_claim_is_idempotent_for_same_worker() -> None:
    """Re-claiming before release returns the same offset."""
    backend = InMemoryCooldownBackend(slot_width_ms=200)
    first = backend.claim_slot("dep-A", "w-1", 500)
    second = backend.claim_slot("dep-A", "w-1", 500)
    assert first == second


def test_coordinator_releases_slot_after_resume() -> None:
    """A successful resume releases the worker's slot."""
    sleep_recorder = _SleepRecorder()
    controller = _make_controller(sleep_fn=sleep_recorder)
    backend = InMemoryCooldownBackend(slot_width_ms=100)
    # Pre-claim one slot so this worker would have been assigned offset 100.
    backend.claim_slot("dep-A", "other", retry_after_ms=500)
    coordinator = CooldownCoordinator(
        controller=controller,
        backend=backend,
        deployment_key="dep-A",
        worker_id="w-self",
    )
    coordinator.call(
        make_send_sequence([r429_with_ms(500), r200()]),
        request=FakeRequest(),
    )
    # +100 ms slot offset on top of 500 ms parsed wait.
    assert sleep_recorder.calls == [pytest.approx(0.6)]
    # Self has been released; "other" remains.
    assert backend.active_worker_count("dep-A") == 1


def test_uniform_strategy_seeded_is_reproducible() -> None:
    """Uniform strategy with a seeded RNG produces deterministic offsets."""
    backend = InMemoryCooldownBackend(slot_width_ms=100)
    # Pre-claim 4 workers so n_workers = 5 when this worker claims (upper=500).
    for i in range(4):
        backend.claim_slot("dep-A", f"other-{i}", retry_after_ms=500)

    sleeps_a = _SleepRecorder()
    coord_a = CooldownCoordinator(
        controller=_make_controller(sleep_fn=sleeps_a),
        backend=backend,
        deployment_key="dep-A",
        worker_id="w-uniform-a",
        slot_width_ms=100,
        jitter_strategy="uniform",
        rng=random.Random(1234),
    )
    coord_a.call(
        make_send_sequence([r429_with_ms(500), r200()]),
        request=FakeRequest(),
    )

    sleeps_b = _SleepRecorder()
    coord_b = CooldownCoordinator(
        controller=_make_controller(sleep_fn=sleeps_b),
        backend=backend,
        deployment_key="dep-A",
        worker_id="w-uniform-b",
        slot_width_ms=100,
        jitter_strategy="uniform",
        rng=random.Random(1234),
    )
    coord_b.call(
        make_send_sequence([r429_with_ms(500), r200()]),
        request=FakeRequest(),
    )
    assert sleeps_a.calls == sleeps_b.calls
    # Offset bounded by slot_width * n_workers (500 ms) over baseline 500 ms.
    extra = sleeps_a.calls[0] - 0.5
    assert 0.0 <= extra < 0.5 + 1e-9


def test_uniform_strategy_offset_within_window() -> None:
    backend = InMemoryCooldownBackend(slot_width_ms=100)
    sleeps = _SleepRecorder()
    coord = CooldownCoordinator(
        controller=_make_controller(sleep_fn=sleeps),
        backend=backend,
        deployment_key="dep-A",
        worker_id="w-uni",
        slot_width_ms=100,
        jitter_strategy="uniform",
        rng=random.Random(7),
    )
    coord.call(
        make_send_sequence([r429_with_ms(500), r200()]),
        request=FakeRequest(),
    )
    # Only this worker active → n=1 → upper = 100 ms.
    extra = sleeps.calls[0] - 0.5
    assert 0.0 <= extra < 0.1 + 1e-9


def test_coordinator_passes_through_200() -> None:
    """No 429 → no backend claim, no extra sleep, response returned."""
    sleeps = _SleepRecorder()
    backend = InMemoryCooldownBackend(slot_width_ms=100)
    coord = CooldownCoordinator(
        controller=_make_controller(sleep_fn=sleeps),
        backend=backend,
        deployment_key="dep-A",
        worker_id="w-passthru",
    )
    resp = coord.call(make_send_sequence([r200()]), request=FakeRequest())
    assert resp.status_code == 200
    assert sleeps.calls == []
    assert backend.active_worker_count("dep-A") == 0


def test_coordinator_has_no_retry_budget_parameter() -> None:
    """Coordinator MUST NOT own a retry budget (no max_attempts param)."""
    import inspect

    sig = inspect.signature(CooldownCoordinator.__init__)
    forbidden = {"max_attempts", "max_retries", "retries", "retry_budget"}
    assert not (set(sig.parameters) & forbidden)


def test_coordinator_does_not_swallow_controller_give_up() -> None:
    """Persistent 429s still raise AdmissionExhausted from the controller."""
    from batch_runner.ptu import AdmissionExhausted

    sleeps = _SleepRecorder()
    controller = AdmissionController(
        max_attempts=2,
        max_wait_ms=10_000,
        jitter_fn=_no_jitter,
        sleep_fn=sleeps,
    )
    coord = CooldownCoordinator(
        controller=controller,
        backend=InMemoryCooldownBackend(slot_width_ms=100),
        deployment_key="dep-A",
        worker_id="w-exhaust",
    )
    with pytest.raises(AdmissionExhausted):
        coord.call(
            make_send_sequence([r429_with_ms(100), r429_with_ms(100)]),
            request=FakeRequest(),
        )


def test_throttle_observer_still_invoked() -> None:
    """User-supplied on_throttle callback still receives ThrottleEvent."""
    seen: list[ThrottleEvent] = []

    def observer(ev: ThrottleEvent) -> None:
        seen.append(ev)

    sleeps = _SleepRecorder()
    controller = _make_controller(sleep_fn=sleeps, on_throttle=observer)
    coord = CooldownCoordinator(
        controller=controller,
        backend=InMemoryCooldownBackend(slot_width_ms=100),
        deployment_key="dep-A",
        worker_id="w-obs",
    )
    coord.call(
        make_send_sequence([r429_with_ms(300), r200()]),
        request=FakeRequest(),
    )
    assert len(seen) == 1
    assert seen[0].decision == "sleep"
    assert seen[0].parsed_wait_ms == 300


def test_coordinator_rejects_invalid_strategy() -> None:
    with pytest.raises(ValueError):
        CooldownCoordinator(
            controller=_make_controller(sleep_fn=_SleepRecorder()),
            backend=InMemoryCooldownBackend(),
            deployment_key="dep-A",
            worker_id="w-x",
            jitter_strategy="bogus",  # type: ignore[arg-type]
        )


def test_coordinator_rejects_empty_identifiers() -> None:
    backend = InMemoryCooldownBackend()
    controller = _make_controller(sleep_fn=_SleepRecorder())
    with pytest.raises(ValueError):
        CooldownCoordinator(
            controller=controller,
            backend=backend,
            deployment_key="",
            worker_id="w-x",
        )
    with pytest.raises(ValueError):
        CooldownCoordinator(
            controller=controller,
            backend=backend,
            deployment_key="dep-A",
            worker_id="",
        )


# -- KeyValueCooldownBackend (interface example) ----------------------------


class _FakeKVClient:
    """In-process fake matching the KeyValueCooldownBackend client shape."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expires: dict[str, int] = {}

    def incr(self, key: str) -> int:
        raw = self.store.get(key, "0")
        try:
            current = int(raw)
        except ValueError:
            current = 0
        current += 1
        self.store[key] = str(current)
        return current

    def expire(self, key: str, seconds: int) -> None:
        self.expires[key] = int(seconds)

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value

    def delete(self, key: str) -> None:
        self.store.pop(key, None)
        self.expires.pop(key, None)


def test_kv_backend_assigns_distinct_offsets() -> None:
    client = _FakeKVClient()
    backend = KeyValueCooldownBackend(client, slot_width_ms=100)
    offsets = [
        backend.claim_slot("dep-A", f"w-{i}", retry_after_ms=500)
        for i in range(5)
    ]
    assert offsets == [0, 100, 200, 300, 400]


def test_kv_backend_idempotent_reclaim_uses_cache() -> None:
    client = _FakeKVClient()
    backend = KeyValueCooldownBackend(client, slot_width_ms=100)
    first = backend.claim_slot("dep-A", "w-1", 500)
    second = backend.claim_slot("dep-A", "w-1", 500)
    assert first == second == 0


def test_kv_backend_rejects_client_missing_methods() -> None:
    class Bad:
        pass

    with pytest.raises(TypeError):
        KeyValueCooldownBackend(Bad())


def test_kv_backend_does_not_import_redis() -> None:
    import sys

    # Confirm: importing the backend module must not pull in 'redis'.
    redis_present = "redis" in sys.modules
    # Backend itself does not depend on redis being importable.
    backend = KeyValueCooldownBackend(_FakeKVClient(), slot_width_ms=50)
    assert backend.slot_width_ms == 50
    # Whatever was already loaded by the runtime, this test does not add it.
    assert ("redis" in sys.modules) == redis_present


def test_module_docstring_labels_operational_inference() -> None:
    from batch_runner.ptu import cooldown_coordinator as mod

    assert "operational inference" in (mod.__doc__ or "").lower()
