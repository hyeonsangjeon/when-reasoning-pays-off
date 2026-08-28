"""Real Azure OpenAI provider in Microsoft Foundry (billed).

This makes a genuine Responses API call. It authenticates with Entra ID (Azure's
identity service) using a *refreshable bearer-token provider*: a callable that
the OpenAI client invokes before each request, so Azure Identity can cache and
refresh the token. The callable is **never** invoked at construction time and no
token, endpoint host, or secret is ever stored in an artifact or an error.

Auth reference (Microsoft Learn, Foundry v1)::

    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://ai.azure.com/.default"
    )
    client = OpenAI(base_url=f"{endpoint}/openai/v1/", api_key=token_provider)

Response contract (Microsoft Learn): read ``response.output_text``; usage exposes
``input_tokens``, ``input_tokens_details.cached_tokens``, ``output_tokens``
(already inclusive of reasoning), ``output_tokens_details.reasoning_tokens``, and
``total_tokens``. Reasoning tokens bill as output tokens and are not added twice.

Cost safety: a billed run is refused unless the ledger's budget is explicitly
confirmed with a positive hard ceiling, and it is hard-refused when running under
CI. Nothing here spends money without that explicit, auditable confirmation.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from batch_runner.experiment.ledger import RunLedger
from batch_runner.experiment.providers.base import ResolvedEndpoint
from batch_runner.experiment.record import (
    METRIC_MODEL_DEPENDENT,
    METRIC_REPORTED,
    AuthenticationError,
    BudgetNotConfirmedError,
    ModelUnavailableError,
    OutputRecord,
    ProviderCapabilities,
    ProviderUnavailableError,
    RequestTimeoutError,
    ResponseFormatError,
)

#: Entra ID audience for Microsoft Foundry data-plane calls.
FOUNDRY_AUDIENCE = "https://ai.azure.com/.default"

# A token provider is a zero-arg callable returning a bearer token string. The
# client calls it per request; we only ever pass it, never call it ourselves.
TokenProvider = Callable[[], str]
TokenProviderFactory = Callable[[], TokenProvider]
ClientFactory = Callable[..., Any]


def _default_token_provider_factory() -> TokenProvider:
    try:
        from azure.identity import (  # noqa: PLC0415 - lazy: heavy optional dep
            DefaultAzureCredential,
            get_bearer_token_provider,
        )
    except ImportError:
        raise ProviderUnavailableError(
            "the azure-identity package is required for an Azure run; "
            "install it with `pip install azure-identity`"
        ) from None
    return get_bearer_token_provider(DefaultAzureCredential(), FOUNDRY_AUDIENCE)


def _default_client_factory(*, base_url: str, api_key: TokenProvider) -> Any:
    try:
        from openai import OpenAI  # noqa: PLC0415 - lazy: heavy optional dep
    except ImportError:
        raise ProviderUnavailableError(
            "the openai package is required for an Azure run; "
            "install it with `pip install openai`"
        ) from None
    # api_key accepts a callable token provider; the client invokes it per
    # request. Do NOT call api_key() here.
    return OpenAI(base_url=base_url, api_key=api_key)


def _usage_int(usage: Any, name: str) -> int | None:
    value = getattr(usage, name, None)
    if value is None and isinstance(usage, dict):
        value = usage.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _usage_detail_int(usage: Any, group: str, name: str) -> int | None:
    details = getattr(usage, group, None)
    if details is None and isinstance(usage, dict):
        details = usage.get(group)
    if details is None:
        return None
    return _usage_int(details, name)


class AzureFoundryProvider:
    """A real, billed Azure OpenAI Responses call normalized to a record."""

    name = "azure"

    def __init__(
        self,
        *,
        ledger: RunLedger,
        endpoint: ResolvedEndpoint,
        capture_io: bool,
        client_factory: ClientFactory | None = None,
        token_provider_factory: TokenProviderFactory | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        self._ledger = ledger
        self._endpoint = endpoint
        self._capture_io = capture_io
        self._client_factory = client_factory or _default_client_factory
        self._token_provider_factory = (
            token_provider_factory or _default_token_provider_factory
        )
        self._environ = os.environ if environ is None else environ
        self._client: Any = None
        self._base_url = endpoint.base_url.rstrip("/") + "/openai/v1/"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name,
            billed=True,
            token_usage=METRIC_REPORTED,
            reasoning_tokens=METRIC_MODEL_DEPENDENT,
            cached_tokens=METRIC_REPORTED,
            notes=(
                "billed Azure OpenAI call; reasoning tokens appear only for "
                "reasoning models and are already counted within output tokens"
            ),
        )

    def prepare(self) -> None:
        """Enforce the cost gate, then build the client with a lazy token provider."""
        cost = self._ledger.execution.cost
        if not cost.billed:  # pragma: no cover - ledger forces billed for azure
            raise BudgetNotConfirmedError("azure runs must be marked billed")
        if not cost.confirmed:
            raise BudgetNotConfirmedError(
                "billed Azure run is not confirmed; set execution.cost.confirmed "
                "to true in the ledger (and pass --confirm-cost) to authorize spend"
            )
        if cost.hard_ceiling_usd is None or cost.hard_ceiling_usd <= 0:
            raise BudgetNotConfirmedError(
                "billed Azure run requires a positive execution.cost.hard_ceiling_usd"
            )
        if _is_truthy(self._environ.get("CI")):
            raise BudgetNotConfirmedError(
                "refusing to start a billed Azure run under CI; billed runs are "
                "operator-initiated only"
            )
        token_provider = self._token_provider_factory()
        # Pass the callable through untouched — the client refreshes per request.
        self._client = self._client_factory(
            base_url=self._base_url, api_key=token_provider
        )

    def run_row(self, row_id: str, repeat_index: int, prompt: str) -> OutputRecord:
        if self._client is None:  # pragma: no cover - runner always prepares first
            raise ProviderUnavailableError("azure provider was not prepared")

        kwargs: dict[str, Any] = {
            "model": self._ledger.model,
            "input": prompt,
            "max_output_tokens": self._ledger.execution.max_output_tokens,
        }
        effort = self._ledger.execution.reasoning_effort
        if effort is not None:
            kwargs["reasoning"] = {"effort": effort}

        try:
            response = self._client.responses.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - normalized to typed errors below
            raise _classify_error(exc) from None

        text = getattr(response, "output_text", None)
        if not isinstance(text, str):
            raise ResponseFormatError("azure response is missing output_text")

        usage = getattr(response, "usage", None)
        input_tokens = _usage_int(usage, "input_tokens")
        output_tokens = _usage_int(usage, "output_tokens")
        total_tokens = _usage_int(usage, "total_tokens")
        reasoning_tokens = _usage_detail_int(
            usage, "output_tokens_details", "reasoning_tokens"
        )
        cached_tokens = _usage_detail_int(
            usage, "input_tokens_details", "cached_tokens"
        )
        finish_reason = getattr(response, "status", None)
        if not isinstance(finish_reason, str):
            finish_reason = None

        return OutputRecord(
            row_id=row_id,
            repeat_index=repeat_index,
            provider=self.name,
            model=self._ledger.model,
            status="ok",
            latency_ms=0,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            cached_tokens=cached_tokens,
            total_tokens=total_tokens,
            finish_reason=finish_reason,
            request_text=prompt if self._capture_io else None,
            response_text=text if self._capture_io else None,
        )


def _is_truthy(value: str | None) -> bool:
    return bool(value) and value.strip().lower() not in {"", "0", "false", "no"}


def _classify_error(exc: Exception) -> Exception:
    """Map an SDK exception to a typed, value-free provider error."""
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    if name in {"AuthenticationError", "PermissionDeniedError"} or status in {401, 403}:
        return AuthenticationError("azure authentication failed")
    if name == "NotFoundError" or status == 404:
        return ModelUnavailableError(
            "azure deployment not found; check the deployment name in the ledger"
        )
    if name in {"APITimeoutError"} or status == 408:
        return RequestTimeoutError("azure request timed out")
    return ProviderUnavailableError(f"azure request failed ({name})")


__all__ = [
    "AzureFoundryProvider",
    "FOUNDRY_AUDIENCE",
    "TokenProvider",
    "TokenProviderFactory",
    "ClientFactory",
]
