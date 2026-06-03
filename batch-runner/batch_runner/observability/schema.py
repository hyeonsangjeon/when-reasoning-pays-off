"""Canonical PTU observability schema (Task 028).

This module defines the *single* per-request and per-cell record shape
that every PTU-aware script in this repo emits going forward. Downstream
aggregators (Task 020, future analytics) rely on it.

Two record types are exposed:

* :class:`PTURequestRecord` — one row per Azure OpenAI request.
* :class:`PTUCellSummary` — one row per measurement cell window.

Each field carries a *category* tag in the generated JSON Schema:

* ``official_spec`` — name and semantics come from the Azure OpenAI PTU
  Operations Guide (Appendix A — HTTP 429 response headers, Appendix B —
  spillover headers, Appendix C — Azure Monitor metric names) or the
  OpenAI ``usage`` block.
* ``operational_inference`` — repo convention (e.g. ``cell_id``,
  ``request_idx``); not lifted verbatim from any vendor spec.

Privacy / secret rules enforced here:

* No ``api_key``, ``Authorization`` header, ``messages`` payload,
  ``system_prompt``, or request body content fields exist.
* ``prompt_cache_key_used`` is a **stable hash** of the raw key. The
  helper :func:`hash_cache_key` returns 16 lowercase hex characters
  (truncated SHA-256). The raw key is never stored.

No network calls, no environment-variable reads, no Azure / OpenAI
client instantiation happen at import time or at any time in this
module.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional


CATEGORY_OFFICIAL = "official_spec"
CATEGORY_INFERENCE = "operational_inference"


# Per-field metadata: (category, source citation, optional-on-PTU flag).
# The source citation is a short pointer to the Guide section; it is NOT
# the verbatim Guide text.
_REQUEST_FIELD_META: dict[str, dict[str, Any]] = {
    # Identity (repo convention).
    "request_idx": {
        "category": CATEGORY_INFERENCE,
        "source": "repo convention",
    },
    "wallclock_timestamp_iso": {
        "category": CATEGORY_INFERENCE,
        "source": "repo convention",
    },
    "deployment_name_requested": {
        "category": CATEGORY_INFERENCE,
        "source": "repo convention (client-side request target)",
    },
    # Azure response — Appendix A (HTTP 429) + Appendix B (spillover).
    "response_status_code": {
        "category": CATEGORY_OFFICIAL,
        "source": "HTTP/1.1 status line",
    },
    "retry_after_ms": {
        "category": CATEGORY_OFFICIAL,
        "source": "Guide Appendix A — retry-after-ms (contract-reliable on 429)",
        "header_name": "retry-after-ms",
    },
    "retry_after_seconds": {
        "category": CATEGORY_OFFICIAL,
        "source": "Guide Appendix A — retry-after (contract-reliable on 429)",
        "header_name": "retry-after",
    },
    "x_ms_region": {
        "category": CATEGORY_OFFICIAL,
        "source": "Guide Appendix A — observability header",
        "header_name": "x-ms-region",
    },
    "x_request_id": {
        "category": CATEGORY_OFFICIAL,
        "source": "Guide Appendix A — observability header",
        "header_name": "x-request-id",
    },
    "x_ms_deployment_name": {
        "category": CATEGORY_OFFICIAL,
        "source": "Guide Appendix B — actual server deployment",
        "header_name": "x-ms-deployment-name",
    },
    "x_ms_spillover_from_deployment": {
        "category": CATEGORY_OFFICIAL,
        "source": "Guide Appendix B — spillover origin",
        "header_name": "x-ms-spillover-from-deployment",
    },
    "x_ms_spillover_error": {
        "category": CATEGORY_OFFICIAL,
        "source": "Guide Appendix B — spillover failure code",
        "header_name": "x-ms-spillover-error",
    },
    "x_ratelimit_remaining_requests": {
        "category": CATEGORY_OFFICIAL,
        "source": (
            "Guide Appendix A — x-ratelimit-* headers are described for "
            "Standard deployments; OPTIONAL on PTU path."
        ),
        "header_name": "x-ratelimit-remaining-requests",
        "optional_on_ptu": True,
    },
    # OpenAI usage block.
    "prompt_tokens": {
        "category": CATEGORY_OFFICIAL,
        "source": "OpenAI usage.prompt_tokens",
    },
    "completion_tokens": {
        "category": CATEGORY_OFFICIAL,
        "source": "OpenAI usage.completion_tokens",
    },
    "cached_tokens": {
        "category": CATEGORY_OFFICIAL,
        "source": "OpenAI usage.prompt_tokens_details.cached_tokens",
    },
    "reasoning_tokens": {
        "category": CATEGORY_OFFICIAL,
        "source": "OpenAI usage.completion_tokens_details.reasoning_tokens",
    },
    "total_tokens": {
        "category": CATEGORY_OFFICIAL,
        "source": "OpenAI usage.total_tokens",
    },
    # Request parameters that influence admission.
    "max_output_tokens_sent": {
        "category": CATEGORY_OFFICIAL,
        "source": "OpenAI request parameter — max_output_tokens",
    },
    "prompt_cache_key_used": {
        "category": CATEGORY_OFFICIAL,
        "source": (
            "Guide §1 — prompt_cache_key routing parameter; "
            "ALWAYS stored as hash via hash_cache_key, never raw."
        ),
        "string_constraints": {
            "pattern": "^[0-9a-f]{16}$",
            "minLength": 16,
            "maxLength": 16,
        },
    },
    "prompt_cache_retention_sent": {
        "category": CATEGORY_OFFICIAL,
        "source": "OpenAI request parameter — prompt_cache_retention",
    },
    "reasoning_effort_sent": {
        "category": CATEGORY_OFFICIAL,
        "source": "OpenAI request parameter — reasoning.effort",
    },
    "model_id": {
        "category": CATEGORY_OFFICIAL,
        "source": "OpenAI request parameter — model",
    },
    # Latency (operational measurement).
    "first_token_latency_ms": {
        "category": CATEGORY_INFERENCE,
        "source": "client-side measurement",
    },
    "total_latency_ms": {
        "category": CATEGORY_INFERENCE,
        "source": "client-side measurement",
    },
}


_CELL_FIELD_META: dict[str, dict[str, Any]] = {
    "cell_id": {
        "category": CATEGORY_INFERENCE,
        "source": "repo convention (Task 013 cell taxonomy)",
    },
    "cell_label": {
        "category": CATEGORY_INFERENCE,
        "source": "repo convention",
    },
    "window_start_iso": {
        "category": CATEGORY_INFERENCE,
        "source": "client-side window boundary",
    },
    "window_end_iso": {
        "category": CATEGORY_INFERENCE,
        "source": "client-side window boundary",
    },
    "deployment_name": {
        "category": CATEGORY_INFERENCE,
        "source": "repo convention",
    },
    "request_count": {
        "category": CATEGORY_INFERENCE,
        "source": "aggregation over PTURequestRecord",
    },
    "real_429_count": {
        "category": CATEGORY_INFERENCE,
        "source": "aggregation over PTURequestRecord.response_status_code == 429",
    },
    "mean_cached_fraction": {
        "category": CATEGORY_INFERENCE,
        "source": "aggregation over cached_tokens / prompt_tokens",
    },
    "p50_ttft_ms": {
        "category": CATEGORY_INFERENCE,
        "source": "aggregation over first_token_latency_ms",
    },
    "p95_ttft_ms": {
        "category": CATEGORY_INFERENCE,
        "source": "aggregation over first_token_latency_ms",
    },
    "p99_ttft_ms": {
        "category": CATEGORY_INFERENCE,
        "source": "aggregation over first_token_latency_ms",
    },
    "mean_retry_after_ms_on_429": {
        "category": CATEGORY_INFERENCE,
        "source": "aggregation over retry_after_ms on 429 responses",
    },
    "azure_monitor_metrics_to_query": {
        "category": CATEGORY_OFFICIAL,
        "source": (
            "Guide Appendix C — frozen list of metric names that MUST be "
            "queried on the same time window when interpreting this cell."
        ),
    },
}


@dataclass(frozen=True)
class PTURequestRecord:
    """Canonical per-request observability record (Task 028)."""

    # Identity (operational inference — repo convention).
    request_idx: int
    wallclock_timestamp_iso: str
    deployment_name_requested: str

    # Azure response — official spec (Appendix A / B).
    response_status_code: int
    retry_after_ms: Optional[float]
    retry_after_seconds: Optional[float]
    x_ms_region: Optional[str]
    x_request_id: Optional[str]
    x_ms_deployment_name: Optional[str]
    x_ms_spillover_from_deployment: Optional[str]
    x_ms_spillover_error: Optional[str]
    # OPTIONAL on PTU path per Appendix A.
    x_ratelimit_remaining_requests: Optional[int]

    # OpenAI usage block — official spec.
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    total_tokens: int

    # Request parameters that influence admission.
    max_output_tokens_sent: Optional[int]
    # Hash, never raw — see hash_cache_key().
    prompt_cache_key_used: Optional[str]
    prompt_cache_retention_sent: Optional[Literal["in_memory", "24h"]]
    reasoning_effort_sent: Optional[Literal["minimal", "low", "medium", "high"]]
    model_id: str

    # Latency (operational inference).
    first_token_latency_ms: Optional[float]
    total_latency_ms: float

    def __post_init__(self) -> None:
        # Enforce hash-only storage for prompt_cache_key_used.
        # None is preserved. An already-valid 16-char lowercase hex digest
        # is preserved unchanged. Anything else is normalized via
        # hash_cache_key(). The dataclass is frozen, so use
        # object.__setattr__ to mutate during construction only.
        raw = self.prompt_cache_key_used
        if raw is None:
            return
        if not isinstance(raw, str):
            raise TypeError(
                "prompt_cache_key_used must be a string or None"
            )
        if _HEX_DIGEST_RE.fullmatch(raw):
            return
        object.__setattr__(
            self, "prompt_cache_key_used", hash_cache_key(raw)
        )


@dataclass(frozen=True)
class PTUCellSummary:
    """Canonical per-cell aggregation record (Task 028).

    ``azure_monitor_metrics_to_query`` is the frozen Appendix C list:
    all six metrics MUST be queried on the same time window when
    interpreting this cell. See
    :data:`batch_runner.observability.azure_monitor_contract.AZURE_MONITOR_PTU_METRICS`.
    """

    cell_id: str
    cell_label: str
    window_start_iso: str
    window_end_iso: str
    deployment_name: str
    request_count: int
    real_429_count: int
    mean_cached_fraction: float
    p50_ttft_ms: float
    p95_ttft_ms: float
    p99_ttft_ms: float
    mean_retry_after_ms_on_429: Optional[float]
    azure_monitor_metrics_to_query: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Hashing helper for prompt_cache_key
# ---------------------------------------------------------------------------

_HASH_LEN = 16
_HEX_DIGEST_RE = re.compile(r"[0-9a-f]{16}")


def hash_cache_key(raw: str) -> str:
    """Return a 16-char lowercase hex digest of *raw*.

    The raw ``prompt_cache_key`` value can carry tenant or customer
    identifiers. This helper hashes it with SHA-256 and truncates to
    16 hex characters, giving a stable, non-reversible token suitable
    for logging.

    Raises ``TypeError`` if *raw* is not a string.
    """
    if not isinstance(raw, str):
        raise TypeError("prompt_cache_key must be a string before hashing")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return digest[:_HASH_LEN]


# ---------------------------------------------------------------------------
# JSON Schema emission
# ---------------------------------------------------------------------------

_PY_TO_JSON = {
    int: "integer",
    float: "number",
    str: "string",
    bool: "boolean",
}


def _type_to_schema(annotation: Any) -> dict[str, Any]:
    """Translate a dataclass field annotation to a JSON Schema fragment.

    Hand-rolled to avoid pulling in a new dependency.
    Supports: ``int``, ``float``, ``str``, ``bool``, ``Optional[X]``,
    ``Literal[...]``, ``tuple[X, ...]``, ``list[X]``.
    """
    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", ())

    # Optional[X] is Union[X, None]; handle both typing.Union and PEP 604.
    if _is_union(annotation):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1 and type(None) in args:
            inner = _type_to_schema(non_none[0])
            t = inner.get("type")
            if isinstance(t, str):
                inner["type"] = [t, "null"]
            elif isinstance(t, list) and "null" not in t:
                inner["type"] = [*t, "null"]
            else:
                inner["type"] = ["null"]
            # If the inner schema constrains values via enum (e.g. from
            # Literal[...]), allow JSON null too so Draft 7 validators
            # accept None for Optional[Literal[...]] fields.
            if "enum" in inner and None not in inner["enum"]:
                inner["enum"] = [*inner["enum"], None]
            return inner

    # Literal[...].
    if _is_literal(annotation):
        values = list(args)
        return {"type": "string", "enum": values}

    # list[X] / tuple[X, ...].
    if origin in (list, tuple):
        item_schema = _type_to_schema(args[0]) if args else {}
        return {"type": "array", "items": item_schema}

    if annotation in _PY_TO_JSON:
        return {"type": _PY_TO_JSON[annotation]}

    # Fallback — accept anything.
    return {}


def _is_union(annotation: Any) -> bool:
    import types
    import typing

    origin = getattr(annotation, "__origin__", None)
    if origin is typing.Union:
        return True
    if hasattr(types, "UnionType") and isinstance(annotation, types.UnionType):
        return True
    return False


def _is_literal(annotation: Any) -> bool:
    import typing

    return getattr(annotation, "__origin__", None) is typing.Literal


def _build_schema(
    dc: type,
    title: str,
    description: str,
    field_meta: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    import typing

    hints = typing.get_type_hints(dc)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for f in fields(dc):
        annotation = hints.get(f.name, f.type)
        prop = _type_to_schema(annotation)
        meta = field_meta.get(f.name, {})
        prop["description"] = meta.get("source", "")
        prop["category"] = meta.get("category", CATEGORY_INFERENCE)
        if "header_name" in meta:
            prop["header_name"] = meta["header_name"]
        if meta.get("optional_on_ptu"):
            prop["optional_on_ptu"] = True
        for key, value in meta.get("string_constraints", {}).items():
            prop[key] = value
        properties[f.name] = prop
        # Optional[X] => not required; everything else required.
        is_optional = _is_union(annotation) and type(None) in getattr(
            annotation, "__args__", ()
        )
        if not is_optional:
            required.append(f.name)
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": title,
        "description": description,
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def build_request_record_schema() -> dict[str, Any]:
    """Return the JSON Schema dict for :class:`PTURequestRecord`."""
    # Resolve string annotations to real types.
    import typing

    typing.get_type_hints(PTURequestRecord)
    return _build_schema(
        PTURequestRecord,
        title="PTURequestRecord",
        description=(
            "Canonical per-request PTU observability record (Task 028). "
            "Header field names match Azure OpenAI PTU Operations Guide "
            "Appendix A and B verbatim."
        ),
        field_meta=_REQUEST_FIELD_META,
    )


def build_cell_summary_schema() -> dict[str, Any]:
    """Return the JSON Schema dict for :class:`PTUCellSummary`."""
    import typing

    typing.get_type_hints(PTUCellSummary)
    return _build_schema(
        PTUCellSummary,
        title="PTUCellSummary",
        description=(
            "Canonical per-cell aggregation record (Task 028). "
            "azure_monitor_metrics_to_query is the frozen Appendix C list."
        ),
        field_meta=_CELL_FIELD_META,
    )


def write_schema_files(out_dir: Path) -> tuple[Path, Path]:
    """Emit both JSON Schema files into *out_dir*."""
    out_dir.mkdir(parents=True, exist_ok=True)
    req_path = out_dir / "ptu_request_record.schema.json"
    cell_path = out_dir / "ptu_cell_summary.schema.json"
    req_path.write_text(
        json.dumps(build_request_record_schema(), indent=2, sort_keys=False) + "\n"
    )
    cell_path.write_text(
        json.dumps(build_cell_summary_schema(), indent=2, sort_keys=False) + "\n"
    )
    return req_path, cell_path


def utc_iso_now() -> str:
    """Return current UTC time in ISO 8601 (no network call)."""
    return datetime.now(timezone.utc).isoformat()
