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
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from batch_runner.experiment.ledger import RunLedger
from batch_runner.experiment.manifest import canonical_json, sha256_bytes
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
from batch_runner.privacy import PrivacyViolation, ensure_safe_public_text

# A ``None`` payload means GET; bytes mean POST.
Transport = Callable[[str, bytes | None, float], "tuple[int, bytes]"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect handler that refuses to follow any redirect.

    A 3xx from a localhost Ollama could otherwise bounce the request (and its
    body) to an arbitrary remote host. We install this so urllib returns the
    redirect response itself instead of transparently following it; the caller
    then treats any 3xx as a blocked, value-free failure.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D102
        return None


def _urllib_transport(
    url: str, payload: bytes | None, timeout: float
) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"} if payload is not None else {},
        method="POST" if payload is not None else "GET",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect
    )
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


def _reported_text(value: object, *, label: str) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 160:
        return None
    try:
        ensure_safe_public_text(value, label=label)
    except PrivacyViolation:
        return None
    if any(ord(character) < 32 for character in value):
        return None
    return value


def _response_object(body: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ResponseFormatError(f"ollama {label} response was not valid JSON") from None
    if not isinstance(value, dict):
        raise ResponseFormatError(f"ollama {label} response was not a JSON object")
    return value


def _detail(
    tag_details: dict[str, Any],
    show_details: dict[str, Any],
    key: str,
) -> str | None:
    return _reported_text(
        tag_details.get(key, show_details.get(key)),
        label=f"ollama {key}",
    )


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
        self._fingerprint: dict[str, str | None] | None = None

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
        """Resolve the exact installed model before any prompt is submitted."""
        base = self._endpoint.base_url.rstrip("/")
        timeout = float(self._ledger.execution.timeout_seconds)
        version = self._get_object(base + "/api/version", timeout, label="version")
        tags = self._get_object(base + "/api/tags", timeout, label="tags")
        models = tags.get("models")
        if not isinstance(models, list):
            raise ResponseFormatError("ollama tags response is missing models")
        selected: dict[str, Any] | None = None
        for item in models:
            if not isinstance(item, dict):
                continue
            if (
                item.get("name") == self._ledger.model
                or item.get("model") == self._ledger.model
            ):
                selected = item
                break
        if selected is None:
            raise ModelUnavailableError(
                f"ollama model {self._ledger.model!r} is not installed; "
                f"pull it with `ollama pull {self._ledger.model}`"
            )
        digest = selected.get("digest")
        if digest is not None and (
            not isinstance(digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        ):
            raise ResponseFormatError("ollama tags response contains an invalid digest")
        expected = self._ledger.expected_model_digest
        if expected is not None and digest != expected:
            raise ModelUnavailableError(
                "ollama model digest does not match expected_model_digest; "
                "refusing before prompt submission"
            )
        show_payload = json.dumps(
            {"model": self._ledger.model, "verbose": False},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        show = self._post_object(
            base + "/api/show", show_payload, timeout, label="show"
        )
        tag_details = selected.get("details")
        show_details = show.get("details")
        if not isinstance(tag_details, dict):
            tag_details = {}
        if not isinstance(show_details, dict):
            show_details = {}
        template = show.get("template")
        model_info = show.get("model_info")
        self._fingerprint = {
            "runtime_version": _reported_text(
                version.get("version"), label="ollama runtime version"
            ),
            "tag": self._ledger.model,
            "digest": digest,
            "format": _detail(tag_details, show_details, "format"),
            "family": _detail(tag_details, show_details, "family"),
            "parameter_size": _detail(
                tag_details, show_details, "parameter_size"
            ),
            "quantization": _detail(
                tag_details, show_details, "quantization_level"
            ),
            "template_sha256": (
                sha256_bytes(canonical_json(template))
                if isinstance(template, str)
                else None
            ),
            "model_info_sha256": (
                sha256_bytes(canonical_json(model_info))
                if isinstance(model_info, dict)
                else None
            ),
        }

    def fingerprint(self) -> dict[str, str | None] | None:
        """Return the prepared, content-free runtime/model fingerprint."""
        return dict(self._fingerprint) if self._fingerprint is not None else None

    def _request_object(
        self,
        url: str,
        payload: bytes | None,
        timeout: float,
        *,
        label: str,
    ) -> dict[str, Any]:
        status, body = self._transport(url, payload, timeout)
        if 300 <= status < 400:
            raise ProviderUnavailableError(
                "ollama endpoint returned a redirect; refusing to follow it"
            )
        if status >= 400:
            raise ProviderUnavailableError(
                f"ollama {label} endpoint returned HTTP {status}"
            )
        return _response_object(body, label=label)

    def _get_object(
        self, url: str, timeout: float, *, label: str
    ) -> dict[str, Any]:
        return self._request_object(url, None, timeout, label=label)

    def _post_object(
        self, url: str, payload: bytes, timeout: float, *, label: str
    ) -> dict[str, Any]:
        return self._request_object(url, payload, timeout, label=label)

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

        data = _response_object(body, label="chat")

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
