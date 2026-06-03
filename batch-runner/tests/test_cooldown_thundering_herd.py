"""Concurrency simulation: thundering-herd dispersion (Task 025).

Simulates N workers each receiving a 429 with the same retry-after-ms.
Without coordination, resume timestamps cluster at one instant; with
the cooldown coordinator, they disperse across the slot window. Tests
are deterministic — no real wall-clock waits, no real threading races
in assertions; we use a virtual clock + Python threads only to verify
that the InMemoryCooldownBackend's lock yields distinct slots under
concurrent ``claim_slot`` calls.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from batch_runner.ptu import (
    AdmissionController,
    CooldownCoordinator,
    InMemoryCooldownBackend,
)

from .fixtures.retry_after_fixtures import (
    FakeRequest,
    make_send_sequence,
    r200,
    r429_with_ms,
)


class _VirtualClock:
    """Records each worker's scheduled sleep duration instead of waiting.

    An optional ``barrier`` makes the recorded sleep also act as a
    rendezvous: a worker only returns from ``sleep`` after all peers
    have arrived. This keeps each worker's slot *claimed* through the
    duration of the wave, which is what allows the InMemoryCooldownBackend
    to assign distinct indices instead of recycling slot 0 serially.
    """

    def __init__(self, *, barrier: threading.Barrier | None = None) -> None:
        self.lock = threading.Lock()
        self.sleeps: dict[str, list[float]] = {}
        self._current_worker = threading.local()
        self._barrier = barrier

    def bind(self, worker_id: str) -> None:
        self._current_worker.wid = worker_id

    def sleep(self, seconds: float) -> None:
        wid = getattr(self._current_worker, "wid", "?")
        with self.lock:
            self.sleeps.setdefault(wid, []).append(float(seconds))
        if self._barrier is not None:
            self._barrier.wait(timeout=5.0)


def _no_jitter(w: int) -> int:
    return w


def _run_worker(
    *,
    worker_id: str,
    coordinator: CooldownCoordinator | None,
    controller: AdmissionController,
    clock: _VirtualClock,
    retry_after_ms: int,
) -> None:
    clock.bind(worker_id)
    send = make_send_sequence([r429_with_ms(retry_after_ms), r200()])
    if coordinator is None:
        controller.call(send, request=FakeRequest(worker_id))
    else:
        coordinator.call(send, request=FakeRequest(worker_id))


def _resume_timestamps(clock: _VirtualClock) -> list[float]:
    """Each worker resumes at sleep_duration seconds after t=0 (same wave)."""
    out: list[float] = []
    for wid, calls in clock.sleeps.items():
        # One sleep per worker (single 429 then 200).
        assert len(calls) == 1, (wid, calls)
        out.append(calls[0])
    return out


def _clustering_within(values: list[float], window_seconds: float) -> int:
    """Count pairs of workers whose resume instants are within ``window``."""
    sorted_v = sorted(values)
    pairs = 0
    for i, v in enumerate(sorted_v):
        for w in sorted_v[i + 1 :]:
            if w - v <= window_seconds:
                pairs += 1
            else:
                break
    return pairs


def _make_controller(clock: _VirtualClock) -> AdmissionController:
    return AdmissionController(
        max_attempts=3,
        max_wait_ms=30_000,
        jitter_fn=_no_jitter,
        sleep_fn=clock.sleep,
    )


def _spawn_wave(
    *,
    n_workers: int,
    use_coordinator: bool,
    deployment_key: str = "dep-shared",
    slot_width_ms: int = 100,
) -> _VirtualClock:
    """Run N workers concurrently against a single 429 wave; return the clock."""
    barrier = threading.Barrier(n_workers)
    clock = _VirtualClock(barrier=barrier)
    backend = InMemoryCooldownBackend(slot_width_ms=slot_width_ms)

    threads: list[threading.Thread] = []
    for i in range(n_workers):
        wid = f"w-{i:02d}"
        controller = _make_controller(clock)
        coordinator = (
            CooldownCoordinator(
                controller=controller,
                backend=backend,
                deployment_key=deployment_key,
                worker_id=wid,
                slot_width_ms=slot_width_ms,
            )
            if use_coordinator
            else None
        )
        threads.append(
            threading.Thread(
                target=_run_worker,
                kwargs=dict(
                    worker_id=wid,
                    coordinator=coordinator,
                    controller=controller,
                    clock=clock,
                    retry_after_ms=500,
                ),
            )
        )
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
        assert not t.is_alive(), f"thread {t.name} stuck"
    return clock


@pytest.mark.parametrize("n_workers", [20])
def test_coordinator_disperses_resume_timestamps(n_workers: int) -> None:
    """20 workers, identical 429: assert pairwise dispersion under slot_width=100."""
    clock = _spawn_wave(n_workers=n_workers, use_coordinator=True)
    resumes = _resume_timestamps(clock)
    assert len(resumes) == n_workers
    # All offsets in [500 ms, 500 ms + n * slot_width_ms) = [0.5, 2.5) s.
    assert all(0.5 <= r < 0.5 + n_workers * 0.1 + 1e-9 for r in resumes)
    # Pairwise distinct.
    assert len(set(resumes)) == n_workers
    # No two workers within 50 ms (slot_width=100 ⇒ minimum spacing 100 ms).
    close_pairs = _clustering_within(resumes, window_seconds=0.05)
    assert close_pairs == 0, (
        f"expected zero pairs within 50 ms; got {close_pairs}: {sorted(resumes)}"
    )


def test_control_run_without_coordinator_clusters() -> None:
    """Control: no coordinator → all 20 workers resume at the same instant."""
    n_workers = 20
    clock = _spawn_wave(n_workers=n_workers, use_coordinator=False)
    resumes = _resume_timestamps(clock)
    assert len(resumes) == n_workers
    assert all(r == pytest.approx(0.5) for r in resumes)
    control_pairs = _clustering_within(resumes, window_seconds=0.05)
    expected_pairs = n_workers * (n_workers - 1) // 2
    assert control_pairs == expected_pairs


def test_coordinator_vs_control_dispersion_ratio() -> None:
    """The coordinator must produce >=5x less clustering than the control."""
    n_workers = 20
    ctrl_clock = _spawn_wave(
        n_workers=n_workers, use_coordinator=False, deployment_key="dep-ctrl"
    )
    coord_clock = _spawn_wave(
        n_workers=n_workers, use_coordinator=True, deployment_key="dep-coord"
    )
    control_pairs = _clustering_within(
        _resume_timestamps(ctrl_clock), window_seconds=0.05
    )
    coord_pairs = _clustering_within(
        _resume_timestamps(coord_clock), window_seconds=0.05
    )
    # With slot_width=100ms and 50ms window, coord_pairs is exactly zero.
    assert control_pairs >= 5 * max(1, coord_pairs)
    assert control_pairs > 0
