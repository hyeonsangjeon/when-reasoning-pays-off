"""Header-driven PTU admission controller (Task 023).

This module implements a runtime component that honours the Azure OpenAI
``retry-after-ms`` response header as the official admission signal for
the next acceptable request time (PTU Operations Guide §0).

The controller is intentionally side-effect minimal:

* It does not own the HTTP transport. Callers pass a zero-arg ``send``
  callable that returns a response with ``status_code`` and ``headers``
  attributes (mapping-like).
* It does not own retry state for any other component. The single-owner
  retry rule is enforced at construction: if an SDK/client object is
  supplied via the ``client`` keyword and that object exposes a
  positive ``max_retries`` attribute, the controller refuses to run
  (``DoubleRetryError``).
* It does not log request bodies, prompts, ``messages`` content,
  ``Authorization`` headers, environment variable values, or raw cache
  keys. Only the allow-listed safe header names are surfaced on the
  ``ThrottleEvent`` and only when present on the response.

Surface (Task 023 §"Controller surface")::

    AdmissionController(
        *,
        max_attempts=3,
        max_wait_ms=30_000,
        on_throttle=None,
        fallback=None,
        jitter_fn=default_jitter,
        clock=time.monotonic,
        # Narrow extensions documented in docs/10:
        client=None,           # for single-owner retry rule enforcement
        sleep_fn=time.sleep,   # injectable for tests; no network calls
    )

    controller.call(send, *, request) -> response
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

# Allow-list of response header names the controller may surface on the
# ThrottleEvent. Anything outside this set is dropped. Keep the list
# narrow and operationally meaningful; do not add request-side headers.
_SAFE_RESPONSE_HEADERS: tuple[str, ...] = (
    "x-request-id",
    "x-ms-region",
)

_HEADER_RETRY_AFTER_MS = "retry-after-ms"
_HEADER_RETRY_AFTER = "retry-after"


class AdmissionExhausted(RuntimeError):
    """Raised when persistent 429s exceed ``max_attempts``."""


class WaitExceedsCeiling(RuntimeError):
    """Raised when the parsed ``retry-after`` exceeds ``max_wait_ms`` and
    no fallback was configured."""


class DoubleRetryError(RuntimeError):
    """Raised at construction if an SDK client with its own auto-retry
    enabled (``max_retries > 0``) is wrapped by the controller.

    The Guide §0 caution is that retry ownership must live in exactly
    one place; running the controller over an SDK that also retries
    produces compounded waits and obscures admission accounting.
    """


@dataclass(frozen=True)
class ThrottleEvent:
    """Structured record of a single throttle decision.

    Field allow-list is fixed: anything that could leak prompt content,
    cache keys, env-var values, or auth headers is excluded by
    construction.
    """

    wait_ms: int
    attempt_idx: int
    parsed_from_header: str | None
    wallclock_iso: str
    status_code: int
    decision: str  # one of: "sleep", "fallback", "give-up"
    headers: Mapping[str, str] = field(default_factory=dict)
    # Pre-jitter admission value parsed from the header (or 0 when the
    # header was absent / unparseable). ``wait_ms`` above is the actual
    # post-jitter sleep duration; ``parsed_wait_ms`` preserves the raw
    # admission signal so Task 020-style aggregations can recover the
    # underlying retry-after-ms distribution from controller logs even
    # when jitter is enabled. Non-secret by construction.
    parsed_wait_ms: int = 0


def default_jitter(wait_ms: int) -> int:
    """Add up to +/-10% jitter to a parsed wait.

    Non-deterministic by default; tests pass ``jitter_fn=lambda w: w``
    for full determinism.
    """
    if wait_ms <= 0:
        return 0
    spread = max(1, wait_ms // 10)
    return wait_ms + random.randint(-spread, spread)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _get_header(headers: Any, name: str) -> str | None:
    """Case-insensitive header lookup against a mapping-like object."""
    if headers is None:
        return None
    lname = name.lower()
    try:
        v = headers.get(name)
        if v is not None:
            return str(v)
        v = headers.get(lname)
        if v is not None:
            return str(v)
    except AttributeError:
        pass
    try:
        items = headers.items()
    except AttributeError:
        return None
    for k, v in items:
        if isinstance(k, str) and k.lower() == lname:
            return str(v)
    return None


def _parse_wait_ms(headers: Any) -> tuple[int | None, str | None]:
    """Return (wait_ms, source) parsed from headers.

    ``retry-after-ms`` is consulted first; if absent, ``retry-after`` is
    parsed as integer seconds and multiplied by 1000. Returns
    (None, None) when neither header is present or parseable.
    """
    ms_raw = _get_header(headers, _HEADER_RETRY_AFTER_MS)
    if ms_raw is not None:
        try:
            return int(float(ms_raw)), _HEADER_RETRY_AFTER_MS
        except (TypeError, ValueError):
            pass
    sec_raw = _get_header(headers, _HEADER_RETRY_AFTER)
    if sec_raw is not None:
        try:
            return int(float(sec_raw) * 1000), _HEADER_RETRY_AFTER
        except (TypeError, ValueError):
            pass
    return None, None


def _safe_headers_view(headers: Any) -> dict[str, str]:
    """Project ``headers`` down to the allow-listed safe subset.

    Any header not in ``_SAFE_RESPONSE_HEADERS`` is dropped. Header
    values are coerced to ``str`` so the resulting dict is JSON-safe.
    """
    out: dict[str, str] = {}
    for name in _SAFE_RESPONSE_HEADERS:
        val = _get_header(headers, name)
        if val is not None:
            out[name] = val
    return out


class AdmissionController:
    """Header-driven admission controller for Azure OpenAI PTU 429s.

    See module docstring for surface and rationale.
    """

    def __init__(
        self,
        *,
        max_attempts: int = 3,
        max_wait_ms: int = 30_000,
        on_throttle: Callable[[ThrottleEvent], None] | None = None,
        fallback: Callable[[Any], Any] | None = None,
        jitter_fn: Callable[[int], int] = default_jitter,
        clock: Callable[[], float] = time.monotonic,
        client: Any = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if max_wait_ms < 0:
            raise ValueError("max_wait_ms must be >= 0")
        self._max_attempts = int(max_attempts)
        self._max_wait_ms = int(max_wait_ms)
        self._on_throttle = on_throttle
        self._fallback = fallback
        self._jitter_fn = jitter_fn
        self._clock = clock
        self._sleep_fn = sleep_fn
        self._enforce_single_owner_retry(client)

    @staticmethod
    def _enforce_single_owner_retry(client: Any) -> None:
        """Refuse to wrap an SDK client that has its own retry enabled.

        Detection rule (narrow on purpose): if ``client`` is not None
        and exposes a ``max_retries`` attribute whose integer value is
        strictly positive, raise ``DoubleRetryError``. ``max_retries
        == 0`` (SDK explicitly disabled) or absent attribute are both
        accepted.
        """
        if client is None:
            return
        max_retries = getattr(client, "max_retries", None)
        if max_retries is None:
            return
        try:
            if int(max_retries) > 0:
                raise DoubleRetryError(
                    "AdmissionController refuses to wrap a client with "
                    "max_retries > 0; consolidate retry ownership in "
                    "one place (PTU Operations Guide §0)."
                )
        except (TypeError, ValueError):
            # Non-integer max_retries: do not silently accept; fail
            # closed so misconfigured clients surface early.
            raise DoubleRetryError(
                "AdmissionController could not interpret client "
                "max_retries; set it to 0 explicitly."
            )

    def _emit(self, event: ThrottleEvent) -> None:
        if self._on_throttle is None:
            return
        try:
            self._on_throttle(event)
        except Exception:
            # Never let an observer raise into the caller's hot path.
            pass

    def _emit_decision(
        self,
        *,
        decision: str,
        wait_ms: int,
        parsed_wait_ms: int,
        attempt_idx: int,
        source: str | None,
        status: int,
        safe_headers: dict[str, str],
    ) -> None:
        self._emit(ThrottleEvent(
            wait_ms=wait_ms,
            attempt_idx=attempt_idx,
            parsed_from_header=source,
            wallclock_iso=_iso_now(),
            status_code=status,
            decision=decision,
            headers=safe_headers,
            parsed_wait_ms=parsed_wait_ms,
        ))

    def call(self, send: Callable[[], Any], *, request: Any) -> Any:
        """Execute ``send`` honouring 429 admission headers.

        The ``send`` callable must accept zero arguments and return a
        response object exposing ``status_code: int`` and ``headers``
        (mapping-like). It must not raise on 429 — the controller
        inspects the response.
        """
        for attempt_idx in range(1, self._max_attempts + 1):
            response = send()
            status = int(getattr(response, "status_code", 0))
            if status != 429:
                return response

            headers = getattr(response, "headers", None)
            parsed_ms, source = _parse_wait_ms(headers)
            # If header missing entirely, treat as zero-wait so we still
            # surface a structured event but do not block.
            parsed_wait_ms = max(0, int(parsed_ms)) if parsed_ms is not None else 0
            safe_headers = _safe_headers_view(headers)

            # Admission policy (fallback / give-up / sleep) is decided
            # from the *pre-jitter* parsed wait. Jitter only perturbs
            # the eventual sleep duration; it must not flip the
            # ceiling decision in either direction.
            if parsed_wait_ms > self._max_wait_ms:
                emit_kwargs = dict(
                    wait_ms=parsed_wait_ms,
                    parsed_wait_ms=parsed_wait_ms,
                    attempt_idx=attempt_idx,
                    source=source,
                    status=status,
                    safe_headers=safe_headers,
                )
                if self._fallback is not None:
                    self._emit_decision(decision="fallback", **emit_kwargs)
                    return self._fallback(request)
                self._emit_decision(decision="give-up", **emit_kwargs)
                raise WaitExceedsCeiling(
                    f"retry wait {parsed_wait_ms} ms exceeds ceiling "
                    f"{self._max_wait_ms} ms and no fallback configured"
                )

            # Admission accepted. Jitter applies only to the sleep
            # duration. Clamp to non-negative so a jitter implementation
            # cannot drive sleep below zero.
            wait_ms = max(0, int(self._jitter_fn(parsed_wait_ms)))
            emit_kwargs = dict(
                wait_ms=wait_ms,
                parsed_wait_ms=parsed_wait_ms,
                attempt_idx=attempt_idx,
                source=source,
                status=status,
                safe_headers=safe_headers,
            )

            if attempt_idx >= self._max_attempts:
                self._emit_decision(decision="give-up", **emit_kwargs)
                raise AdmissionExhausted(
                    f"persistent 429 across {self._max_attempts} attempts"
                )

            self._emit_decision(decision="sleep", **emit_kwargs)
            if wait_ms > 0:
                self._sleep_fn(wait_ms / 1000.0)

        # Defensive: loop always returns or raises above. If we somehow
        # exit it, treat as exhausted rather than returning a 429.
        raise AdmissionExhausted(
            "admission loop completed without resolution"
        )
