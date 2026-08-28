"""Real local Ollama provider (no cloud cost).

Calls Ollama's official chat endpoint over plain stdlib HTTP::

    POST {base_url}/api/chat
    {"model": ..., "messages": [{"role": "user", "content": ...}],
     "stream": false, "options": {"num_predict": ...}}

For a non-streaming response we read ``message.content`` and the usage/timing
fields Ollama returns when present: ``prompt_eval_count`` (input tokens),
``eval_count`` (output tokens), and ``total_duration`` (nanoseconds, normalized
to milliseconds here). Reference: https://docs.ollama.com/api/chat

Running locally uses your own machine's CPU/GPU: it is not billed by any cloud
provider, but it does consume local compute. Connection and model-not-found
failures are surfaced as typed, value-free errors — never turned into a
mock-style success.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable

from batch_runner.experiment.ledger import RunLedger
from batch_runner.experiment.providers.base import ResolvedEndpoint
from batch_runner.experiment.record import (
    METRIC_NOT_SUPPORTED,
    METRIC_REPORTED,
    ModelUnavailableError,
    OutputRecord,
    ProviderCapabilities,
    ProviderUnavailableError,
    RequestTimeoutError,
    ResponseFormatError,
)

# transport(url, payload_bytes, timeout_seconds) -> (status_code, body_bytes)
Transport = Callable[[str, bytes, float], "tuple[int, bytes]"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect handler that refuses to follow any redirect.

    A 3xx from a localhost Ollama could otherwise bounce the request (and its
    body) to an arbitrary remote host. We install this so urllib returns the
    redirect response itself instead of transparently following it; the caller
    then treats any 3xx as a blocked, value-free failure.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D102
        return None


def _urllib_transport(url: str, payload: bytes, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - localhost only, scheme validated upstream
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        if 300 <= code < 400:
            # A redirect that would escape localhost — refuse to follow it.
            raise ProviderUnavailableError(
                "ollama endpoint returned a redirect; refusing to follow it "
                "(a redirect could send the request off localhost)"
            ) from None
        return code, exc.read()
    except TimeoutError:
        raise RequestTimeoutError("ollama did not respond before the timeout") from None
    except urllib.error.URLError:
        # Connection refused / DNS / socket error — the server is unreachable.
        raise ProviderUnavailableError(
            "cannot reach the ollama server; is it running? "
            "start it with `ollama serve`"
        ) from None


def _ns_to_ms(value: object) -> int | None:
    if isinstance(value, (int, float)) and value >= 0:
        return int(value / 1_000_000)
    return None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


class OllamaProvider:
    """A real, local Ollama chat call normalized to :class:`OutputRecord`."""

    name = "ollama"

    def __init__(
        self,
        *,
        ledger: RunLedger,
        endpoint: ResolvedEndpoint,
        capture_io: bool,
        transport: Transport | None = None,
    ) -> None:
        self._ledger = ledger
        self._endpoint = endpoint
        self._capture_io = capture_io
        self._transport = transport or _urllib_transport
        self._url = endpoint.base_url.rstrip("/") + "/api/chat"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name,
            billed=False,
            token_usage=METRIC_REPORTED,
            reasoning_tokens=METRIC_NOT_SUPPORTED,
            cached_tokens=METRIC_NOT_SUPPORTED,
            notes=(
                "runs on local compute (not cloud-billed); Ollama reports "
                "prompt/response token counts but no reasoning or cache metrics"
            ),
        )

    def prepare(self) -> None:
        # No pre-flight socket: a down server or missing model surfaces as a
        # typed per-row error with an actionable message.
        return None

    def run_row(self, row_id: str, repeat_index: int, prompt: str) -> OutputRecord:
        payload = json.dumps(
            {
                "model": self._ledger.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"num_predict": self._ledger.execution.max_output_tokens},
            }
        ).encode("utf-8")

        started = time.monotonic()
        status, body = self._transport(
            self._url, payload, float(self._ledger.execution.timeout_seconds)
        )
        wall_ms = int((time.monotonic() - started) * 1000)

        if 300 <= status < 400:
            # A redirect must never be followed off localhost (defense in depth;
            # the default transport already refuses to follow it).
            raise ProviderUnavailableError(
                "ollama endpoint returned a redirect; refusing to follow it"
            )
        if status == 404:
            raise ModelUnavailableError(
                f"ollama model {self._ledger.model!r} is not installed; "
                f"pull it with `ollama pull {self._ledger.model}`"
            )
        if status >= 400:
            raise ProviderUnavailableError(
                f"ollama returned HTTP {status}"
            )

        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ResponseFormatError("ollama response was not valid JSON") from None
        if not isinstance(data, dict):
            raise ResponseFormatError("ollama response was not a JSON object")

        message = data.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ResponseFormatError("ollama response is missing message.content")

        content = message["content"]
        input_tokens = _int_or_none(data.get("prompt_eval_count"))
        output_tokens = _int_or_none(data.get("eval_count"))
        total = None
        if input_tokens is not None and output_tokens is not None:
            total = input_tokens + output_tokens
        latency_ms = _ns_to_ms(data.get("total_duration"))
        if latency_ms is None:
            latency_ms = wall_ms
        finish_reason = data.get("done_reason")
        if not isinstance(finish_reason, str):
            finish_reason = "stop" if data.get("done") is True else None

        return OutputRecord(
            row_id=row_id,
            repeat_index=repeat_index,
            provider=self.name,
            model=self._ledger.model,
            status="ok",
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=None,
            total_tokens=total,
            finish_reason=finish_reason,
            request_text=prompt if self._capture_io else None,
            response_text=content if self._capture_io else None,
        )


__all__ = ["OllamaProvider", "Transport"]
