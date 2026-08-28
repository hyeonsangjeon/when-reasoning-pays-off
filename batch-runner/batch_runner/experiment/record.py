"""The one normalized output record every provider must return.

Azure OpenAI, Ollama, and the offline mock all differ in their wire responses.
This module defines the single shape the runner persists so downstream
tooling never has to branch on the provider: :class:`OutputRecord`.

It also defines the typed error hierarchy the providers raise. Every error is
*value-free*: it names what went wrong and the failing row, never the secret,
token, endpoint host, or private path that triggered it.
"""

from __future__ import annotations

import dataclasses
from typing import Any


class ProviderError(RuntimeError):
    """Base class for a provider call that could not produce a record.

    The message is safe to print and to serialize into artifacts: it must
    never contain a credential, bearer token, full endpoint URL, or private
    filesystem path.
    """

    #: Short, stable machine token stored in the record's ``error_type``.
    error_type = "provider_error"


class ProviderUnavailableError(ProviderError):
    """The provider itself could not be reached or is not installed.

    Examples: the Ollama server is not running, or the ``openai`` /
    ``azure-identity`` packages are absent for an Azure run.
    """

    error_type = "provider_unavailable"


class ModelUnavailableError(ProviderError):
    """The requested model / deployment is not available on the provider."""

    error_type = "model_unavailable"


class AuthenticationError(ProviderError):
    """Authentication with the provider failed (no secret is echoed)."""

    error_type = "authentication_failed"


class RequestTimeoutError(ProviderError):
    """The provider did not respond within the configured timeout."""

    error_type = "timeout"


class ResponseFormatError(ProviderError):
    """The provider responded, but the payload was not the expected shape."""

    error_type = "bad_response"


class BudgetNotConfirmedError(ProviderError):
    """A billed provider was asked to run without an explicit cost confirmation."""

    error_type = "budget_not_confirmed"


# Honest vocabulary for "does this provider support this metric?". A metric is
# never silently reported as ``0`` when the provider cannot measure it.
#   reported      — the service returned a real value (or an explicit 0).
#   not_reported  — supported in principle, but absent from this response.
#   not_supported — the provider has no such concept at all.
#   synthetic     — a made-up preview value (mock only); not a measurement.
METRIC_REPORTED = "reported"
METRIC_NOT_REPORTED = "not_reported"
METRIC_NOT_SUPPORTED = "not_supported"
METRIC_MODEL_DEPENDENT = "model_dependent"
METRIC_SYNTHETIC = "synthetic"


@dataclasses.dataclass(frozen=True)
class ProviderCapabilities:
    """What a provider can and cannot measure — recorded in every run.

    This exists so an artifact never has to guess why a token count is null.
    A null ``reasoning_tokens`` on Ollama means "not_supported"; on Azure it
    means "model_dependent" (only reasoning models emit it). The mock's numbers
    are "synthetic" and must never be read as evidence.
    """

    provider: str
    billed: bool
    token_usage: str  # reported | not_supported | synthetic
    reasoning_tokens: str  # reported | not_supported | model_dependent | synthetic
    cached_tokens: str  # reported | not_supported | synthetic
    notes: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "billed": self.billed,
            "token_usage": self.token_usage,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "notes": self.notes,
        }


@dataclasses.dataclass(frozen=True)
class OutputRecord:
    """One provider call outcome, normalized across every provider.

    Token counts are ``None`` when the provider does not report or does not
    support them; the run's :class:`ProviderCapabilities` says which. They are
    never fabricated as ``0``. The raw request/response text is only ever
    populated when the operator explicitly opts in via the ledger's
    ``capture_io`` flag; the default is privacy-safe.
    """

    row_id: str
    repeat_index: int
    provider: str
    model: str
    status: str  # "ok" | "error"
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None
    total_tokens: int | None = None
    finish_reason: str | None = None
    error_type: str | None = None
    error_detail: str | None = None
    # Populated only when capture_io is explicitly enabled in the ledger.
    request_text: str | None = None
    response_text: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_json(self) -> dict[str, Any]:
        """Return a JSON-serializable dict with a stable key order."""
        return {
            "row_id": self.row_id,
            "repeat_index": self.repeat_index,
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
            "finish_reason": self.finish_reason,
            "error_type": self.error_type,
            "error_detail": self.error_detail,
            "request_text": self.request_text,
            "response_text": self.response_text,
        }


__all__ = [
    "OutputRecord",
    "ProviderCapabilities",
    "METRIC_REPORTED",
    "METRIC_NOT_REPORTED",
    "METRIC_NOT_SUPPORTED",
    "METRIC_MODEL_DEPENDENT",
    "METRIC_SYNTHETIC",
    "ProviderError",
    "ProviderUnavailableError",
    "ModelUnavailableError",
    "AuthenticationError",
    "RequestTimeoutError",
    "ResponseFormatError",
    "BudgetNotConfirmedError",
]
