"""Unit tests for the header-driven PTU admission controller (Task 023).

No network involvement. Sleep is injected as a no-op recorder so the
suite runs in milliseconds.
"""

from __future__ import annotations

import pytest

from batch_runner.ptu import (
    AdmissionController,
    AdmissionExhausted,
    DoubleRetryError,
    ThrottleEvent,
    WaitExceedsCeiling,
    default_jitter,
)

from .fixtures.retry_after_fixtures import (
    FakeRequest,
    FakeSDKClient,
    make_send_sequence,
    r200,
    r429_no_header,
    r429_with_ms,
    r429_with_seconds,
)


def _no_jitter(w: int) -> int:
    return w


class _SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _make_controller(**overrides):
    sleeper = overrides.pop("sleep_fn", _SleepRecorder())
    events: list[ThrottleEvent] = overrides.pop("events_sink", [])
    kwargs = dict(
        max_attempts=3,
        max_wait_ms=30_000,
        jitter_fn=_no_jitter,
        sleep_fn=sleeper,
        on_throttle=events.append,
    )
    kwargs.update(overrides)
    ctrl = AdmissionController(**kwargs)
    return ctrl, sleeper, events


# ---------------------------------------------------------------------------
# Header parsing + sleep behaviour
# ---------------------------------------------------------------------------


def test_retry_after_ms_triggers_one_sleep_and_retry():
    ctrl, sleeper, events = _make_controller()
    send = make_send_sequence([r429_with_ms(847), r200()])

    resp = ctrl.call(send, request=FakeRequest())

    assert resp.status_code == 200
    assert sleeper.calls == [pytest.approx(0.847)]
    assert len(events) == 1
    assert events[0].parsed_from_header == "retry-after-ms"
    assert events[0].wait_ms == 847
    assert events[0].decision == "sleep"
    assert events[0].attempt_idx == 1
    assert events[0].status_code == 429


def test_retry_after_seconds_fallback_multiplied_by_1000():
    ctrl, sleeper, events = _make_controller()
    send = make_send_sequence([r429_with_seconds(1), r200()])

    resp = ctrl.call(send, request=FakeRequest())

    assert resp.status_code == 200
    assert sleeper.calls == [pytest.approx(1.0)]
    assert events[0].parsed_from_header == "retry-after"
    assert events[0].wait_ms == 1000


def test_retry_after_ms_preferred_over_seconds():
    ctrl, sleeper, events = _make_controller()
    # Both headers present: -ms wins.
    resp_obj = r429_with_ms(250, **{"retry-after": "9"})
    send = make_send_sequence([resp_obj, r200()])

    ctrl.call(send, request=FakeRequest())

    assert events[0].parsed_from_header == "retry-after-ms"
    assert events[0].wait_ms == 250


def test_missing_header_treated_as_zero_wait_event():
    ctrl, sleeper, events = _make_controller()
    send = make_send_sequence([r429_no_header(), r200()])

    ctrl.call(send, request=FakeRequest())

    assert sleeper.calls == []  # zero-wait does not sleep
    assert events[0].parsed_from_header is None
    assert events[0].wait_ms == 0


# ---------------------------------------------------------------------------
# Exhaustion + ceiling
# ---------------------------------------------------------------------------


def test_persistent_429_raises_admission_exhausted():
    ctrl, sleeper, events = _make_controller(max_attempts=3)
    send = make_send_sequence([
        r429_with_ms(10),
        r429_with_ms(10),
        r429_with_ms(10),
    ])

    with pytest.raises(AdmissionExhausted):
        ctrl.call(send, request=FakeRequest())

    assert len(events) == 3
    assert events[-1].decision == "give-up"
    # Only the two non-terminal attempts sleep.
    assert sleeper.calls == [pytest.approx(0.01), pytest.approx(0.01)]


def test_wait_exceeds_ceiling_without_fallback_raises():
    ctrl, sleeper, events = _make_controller(max_wait_ms=500)
    send = make_send_sequence([r429_with_ms(60_000)])

    with pytest.raises(WaitExceedsCeiling):
        ctrl.call(send, request=FakeRequest())

    assert sleeper.calls == []
    assert events[-1].decision == "give-up"
    assert events[-1].wait_ms == 60_000
    assert events[-1].parsed_wait_ms == 60_000


def test_ceiling_decided_pre_jitter_shrink_still_fallback():
    """Parsed wait just over ceiling; jitter would shrink it under.

    The admission decision must come from the parsed wait, so the
    controller still fallback/raise — it must not sleep.
    """
    fb_calls: list[object] = []

    def fb(req):
        fb_calls.append(req)
        return r200()

    # Jitter that would pull a 30_001 ms wait *below* the 30_000 ceiling.
    shrink_jitter = lambda w: max(0, w - 2_000)  # noqa: E731

    ctrl, sleeper, events = _make_controller(
        max_wait_ms=30_000,
        fallback=fb,
        jitter_fn=shrink_jitter,
    )
    send = make_send_sequence([r429_with_ms(30_001)])

    resp = ctrl.call(send, request=FakeRequest())

    assert resp.status_code == 200
    assert sleeper.calls == []  # never slept
    assert len(fb_calls) == 1
    assert events[-1].decision == "fallback"
    assert events[-1].parsed_wait_ms == 30_001


def test_ceiling_decided_pre_jitter_grow_still_sleeps():
    """Parsed wait at/under ceiling; jitter would grow it past.

    The admission decision must come from the parsed wait, so the
    controller still takes the sleep path — it must not fallback or
    raise WaitExceedsCeiling solely because jitter widened the wait.
    """
    grow_jitter = lambda w: w + 5_000  # noqa: E731

    ctrl, sleeper, events = _make_controller(
        max_wait_ms=30_000,
        jitter_fn=grow_jitter,
    )
    send = make_send_sequence([r429_with_ms(30_000), r200()])

    resp = ctrl.call(send, request=FakeRequest())

    assert resp.status_code == 200
    assert events[0].decision == "sleep"
    assert events[0].parsed_wait_ms == 30_000
    assert events[0].wait_ms == 35_000
    assert sleeper.calls == [pytest.approx(35.0)]


def test_wait_exceeds_ceiling_with_fallback_invokes_once():
    fb_calls: list[object] = []

    def fb(req):
        fb_calls.append(req)
        return r200()

    ctrl, sleeper, events = _make_controller(max_wait_ms=500, fallback=fb)
    req = FakeRequest("payg")
    send = make_send_sequence([r429_with_ms(60_000)])

    resp = ctrl.call(send, request=req)

    assert resp.status_code == 200
    assert fb_calls == [req]
    assert events[-1].decision == "fallback"
    assert sleeper.calls == []


def test_option_b_immediate_fallback_when_max_wait_zero():
    """Guide §0 Option B: max_wait_ms=0 + fallback => never sleep."""
    fb_calls: list[object] = []

    def fb(req):
        fb_calls.append(req)
        return r200()

    ctrl, sleeper, events = _make_controller(max_wait_ms=0, fallback=fb)
    send = make_send_sequence([r429_with_ms(1)])

    ctrl.call(send, request=FakeRequest())

    assert len(fb_calls) == 1
    assert sleeper.calls == []
    assert events[-1].decision == "fallback"


# ---------------------------------------------------------------------------
# Single-owner retry rule
# ---------------------------------------------------------------------------


def test_double_retry_raised_when_sdk_max_retries_positive():
    bad_client = FakeSDKClient(max_retries=2)
    with pytest.raises(DoubleRetryError):
        AdmissionController(client=bad_client, jitter_fn=_no_jitter)


def test_double_retry_accepted_when_sdk_max_retries_zero():
    good_client = FakeSDKClient(max_retries=0)
    AdmissionController(client=good_client, jitter_fn=_no_jitter)


def test_double_retry_accepted_when_client_has_no_attr():
    class _Bare:
        pass

    AdmissionController(client=_Bare(), jitter_fn=_no_jitter)


def test_double_retry_failed_closed_on_non_integer_max_retries():
    class _Weird:
        max_retries = "infinity"

    with pytest.raises(DoubleRetryError):
        AdmissionController(client=_Weird(), jitter_fn=_no_jitter)


# ---------------------------------------------------------------------------
# Event schema + secret hygiene
# ---------------------------------------------------------------------------


def test_throttle_event_carries_safe_headers_only():
    ctrl, _sleeper, events = _make_controller()
    resp = r429_with_ms(
        100,
        **{
            "x-request-id": "abc123",
            "x-ms-region": "eastus",
            "authorization": "Bearer SECRET",
            "api-key": "SECRET",
            "set-cookie": "session=SECRET",
        },
    )
    send = make_send_sequence([resp, r200()])

    ctrl.call(send, request=FakeRequest())

    headers = dict(events[0].headers)
    assert headers == {"x-request-id": "abc123", "x-ms-region": "eastus"}
    # Hard assertion: no auth/key/cookie leaked under any casing.
    flat = " ".join(f"{k}={v}" for k, v in headers.items()).lower()
    for needle in ("authorization", "bearer", "api-key", "secret", "cookie"):
        assert needle not in flat


def test_throttle_event_fields_present():
    ctrl, _sleeper, events = _make_controller()
    send = make_send_sequence([r429_with_ms(50), r200()])

    ctrl.call(send, request=FakeRequest())

    ev = events[0]
    assert isinstance(ev, ThrottleEvent)
    assert ev.wait_ms == 50
    assert ev.attempt_idx == 1
    assert ev.parsed_from_header == "retry-after-ms"
    assert ev.status_code == 429
    assert ev.decision == "sleep"
    # ISO-8601 with millisecond precision and tz info.
    assert "T" in ev.wallclock_iso and (
        ev.wallclock_iso.endswith("+00:00") or ev.wallclock_iso.endswith("Z")
    )


def test_on_throttle_observer_exception_does_not_propagate():
    def bad_observer(_ev):
        raise RuntimeError("observer blew up")

    ctrl, _sleeper, _events = _make_controller(on_throttle=bad_observer)
    send = make_send_sequence([r429_with_ms(10), r200()])

    resp = ctrl.call(send, request=FakeRequest())
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Jitter
# ---------------------------------------------------------------------------


def test_jitter_is_deterministic_when_identity():
    """Identity jitter function => sleep equals parsed wait exactly."""
    ctrl, sleeper, events = _make_controller(jitter_fn=lambda w: w)
    send = make_send_sequence([r429_with_ms(1234), r200()])

    ctrl.call(send, request=FakeRequest())

    assert sleeper.calls == [pytest.approx(1.234)]
    assert events[0].wait_ms == 1234


def test_jitter_default_stays_within_plus_minus_ten_percent():
    base = 1000
    samples = [default_jitter(base) for _ in range(200)]
    for s in samples:
        assert base - 100 <= s <= base + 100


def test_jitter_default_zero_input_returns_zero():
    assert default_jitter(0) == 0
    assert default_jitter(-5) == 0


def test_parsed_wait_ms_preserved_when_jitter_changes_sleep():
    """`parsed_wait_ms` must reflect the raw header value even when
    jitter changes the actual sleep duration. Task 020 aggregations
    depend on recovering the underlying admission signal from logs.
    """
    perturb = lambda w: w + 137  # noqa: E731
    ctrl, sleeper, events = _make_controller(jitter_fn=perturb)
    send = make_send_sequence([r429_with_ms(1000), r200()])

    ctrl.call(send, request=FakeRequest())

    assert events[0].parsed_wait_ms == 1000
    assert events[0].wait_ms == 1137
    assert sleeper.calls == [pytest.approx(1.137)]


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


def test_invalid_max_attempts_rejected():
    with pytest.raises(ValueError):
        AdmissionController(max_attempts=0, jitter_fn=_no_jitter)


def test_invalid_max_wait_ms_rejected():
    with pytest.raises(ValueError):
        AdmissionController(max_wait_ms=-1, jitter_fn=_no_jitter)


# ---------------------------------------------------------------------------
# Non-429 short-circuit
# ---------------------------------------------------------------------------


def test_non_429_response_returned_immediately():
    ctrl, sleeper, events = _make_controller()
    send = make_send_sequence([r200()])

    resp = ctrl.call(send, request=FakeRequest())

    assert resp.status_code == 200
    assert sleeper.calls == []
    assert events == []
