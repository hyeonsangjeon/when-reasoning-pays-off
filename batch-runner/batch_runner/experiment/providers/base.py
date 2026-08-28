"""Provider base protocol and endpoint resolution.

``resolve_endpoint`` turns the ledger's endpoint *env-var name* into a concrete
base URL at run time, applying the safety rules that keep a run local unless it
is explicitly and safely told otherwise. It returns a value-free description of
*where* the endpoint came from (env vs default) so artifacts can record the
source without recording the host.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Protocol

from batch_runner.experiment.ledger import RunLedger
from batch_runner.experiment.record import (
    OutputRecord,
    ProviderCapabilities,
    ProviderError,
    ProviderUnavailableError,
)


@dataclass(frozen=True)
class ResolvedEndpoint:
    """A concrete base URL plus a value-free note on where it came from."""

    base_url: str
    source: str  # e.g. "OLLAMA_BASE_URL (env)" or "endpoint.default"

    @property
    def is_local(self) -> bool:
        return _is_local_url(self.base_url)


_LOCAL_HOST_RE = re.compile(
    r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d{2,5})?(/.*)?$",
    re.IGNORECASE,
)


def _is_local_url(url: str) -> bool:
    return bool(_LOCAL_HOST_RE.match(url.strip()))


class EndpointResolutionError(ProviderError):
    """The endpoint could not be resolved safely (value-free)."""

    error_type = "endpoint_unresolved"


def resolve_endpoint(
    ledger: RunLedger,
    *,
    environ: dict[str, str] | None = None,
    allow_remote: bool = False,
) -> ResolvedEndpoint:
    """Resolve the base URL for ``ledger`` from the environment or its default.

    The endpoint host is never included in raised errors or in ``source``.

    Raises:
        EndpointResolutionError: If no endpoint is configured, or a non-local
            endpoint is used without ``allow_remote`` for a local-only provider.
    """
    env = os.environ if environ is None else environ
    name = ledger.endpoint.env_var
    raw = env.get(name)
    if raw and raw.strip():
        base_url = raw.strip()
        source = f"{name} (env)"
    elif ledger.endpoint.default is not None:
        base_url = ledger.endpoint.default
        source = "endpoint.default"
    else:
        raise EndpointResolutionError(
            f"no endpoint configured: set the {name} environment variable"
        )

    if not re.match(r"^https?://", base_url, re.IGNORECASE):
        raise EndpointResolutionError(
            f"endpoint from {name} must be an http(s) URL"
        )

    # Ollama is a local-only provider by default. Refuse a non-local endpoint
    # unless the operator explicitly opted in.
    if ledger.provider == "ollama" and not _is_local_url(base_url) and not allow_remote:
        raise EndpointResolutionError(
            "ollama endpoint must be localhost (127.0.0.1/[::1]); pass "
            "--allow-remote-ollama to override for a trusted local network"
        )

    return ResolvedEndpoint(base_url=base_url, source=source)


class Provider(Protocol):
    """A provider knows how to prepare itself and execute one row."""

    name: str

    def capabilities(self) -> ProviderCapabilities:
        """Declare which metrics this provider can measure (honesty contract)."""

    def prepare(self) -> None:
        """Validate reachability/credentials up front.

        Raises a :class:`ProviderError` subclass for a *global* failure that
        should abort the whole run before any artifact is written.
        """

    def run_row(self, row_id: str, repeat_index: int, prompt: str) -> OutputRecord:
        """Execute one row and return a normalized record.

        Raises a :class:`ProviderError` subclass on a *per-row* failure; the
        runner converts that into an error record and continues.
        """


def build_provider(
    ledger: RunLedger,
    endpoint: ResolvedEndpoint,
    *,
    capture_io: bool,
) -> Provider:
    """Construct the provider named by the ledger.

    Imports of the heavy Azure/OpenAI SDK are deferred to the provider module
    so the mock and dry paths never require them.
    """
    if ledger.provider == "mock":
        from batch_runner.experiment.providers.mock import MockProvider

        return MockProvider(ledger=ledger, capture_io=capture_io)
    if ledger.provider == "ollama":
        from batch_runner.experiment.providers.ollama import OllamaProvider

        return OllamaProvider(
            ledger=ledger, endpoint=endpoint, capture_io=capture_io
        )
    if ledger.provider == "azure":
        from batch_runner.experiment.providers.azure import AzureFoundryProvider

        return AzureFoundryProvider(
            ledger=ledger, endpoint=endpoint, capture_io=capture_io
        )
    raise ProviderUnavailableError(  # pragma: no cover - ledger validation guards
        f"unknown provider {ledger.provider!r}"
    )


__all__ = [
    "Provider",
    "ResolvedEndpoint",
    "EndpointResolutionError",
    "resolve_endpoint",
    "build_provider",
]
