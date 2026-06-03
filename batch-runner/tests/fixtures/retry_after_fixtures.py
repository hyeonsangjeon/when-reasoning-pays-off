"""Synthetic 429 response fixtures for the admission controller tests.

These are intentionally minimal: only ``status_code`` and ``headers``
are required by ``AdmissionController.call``. No network involvement,
no SDK objects, no auth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional


@dataclass
class FakeResponse:
    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: Optional[str] = None


def r429_with_ms(ms: int, **extra_headers: str) -> FakeResponse:
    headers = {"retry-after-ms": str(ms)}
    headers.update(extra_headers)
    return FakeResponse(status_code=429, headers=headers)


def r429_with_seconds(seconds: int, **extra_headers: str) -> FakeResponse:
    headers = {"retry-after": str(seconds)}
    headers.update(extra_headers)
    return FakeResponse(status_code=429, headers=headers)


def r429_no_header(**extra_headers: str) -> FakeResponse:
    return FakeResponse(status_code=429, headers=dict(extra_headers))


def r200(**extra_headers: str) -> FakeResponse:
    return FakeResponse(status_code=200, headers=dict(extra_headers))


class FakeRequest:
    """Stand-in for a real request object. Carries no secret material."""

    def __init__(self, name: str = "synthetic") -> None:
        self.name = name


class FakeSDKClient:
    """Stand-in for an SDK/HTTP client with a ``max_retries`` attribute."""

    def __init__(self, max_retries: int) -> None:
        self.max_retries = max_retries


def make_send_sequence(responses):
    """Return a zero-arg callable that yields ``responses`` in order.

    Raises ``IndexError`` once the sequence is exhausted, which surfaces
    test bugs immediately rather than hiding them behind another 429.
    """
    idx = {"i": 0}

    def _send():
        i = idx["i"]
        if i >= len(responses):
            raise IndexError("send() called more times than expected")
        idx["i"] += 1
        return responses[i]

    return _send
