"""Deterministic ``prompt_cache_key`` composition (Task 026).

Implements the key-composition policy from the Azure OpenAI PTU
Operations Guide §1 ("prompt_cache_key Bucketing Guide"):

* Identical workload inputs MUST produce identical keys (determinism).
* Keys are workload-oriented (tenant + flow + locale + schema +
  optional category), NOT per-request identifiers.
* Per-request entropy (UUIDs, request ids, timestamps) explodes
  cardinality and defeats the routing hint; the Guide §1 ANTI-PATTERN
  table calls this out explicitly.

This module is intentionally free of ``time``, ``random``, ``datetime``,
and ``uuid`` imports so that the composition path has no nondeterminism
budget and so static review can confirm it.

Error policy: when ``assert_deterministic`` raises, the error message
contains only short reason categories (e.g. ``"looks_like_uuid"``,
``"contains_long_digit_run"``). The offending key value is never
included in the exception message or logged here; reproducing the key
is the caller's responsibility on the caller's side of the trust
boundary.
"""

from __future__ import annotations

import re

# Reason categories surfaced by anti_pattern_reasons / assert_deterministic.
# Keep these as short identifiers — they are safe to log; key values are
# not.
REASON_LOOKS_LIKE_UUID = "looks_like_uuid"
REASON_CONTAINS_LONG_DIGIT_RUN = "contains_long_digit_run"
REASON_CONTAINS_REQUEST_ID_TOKEN = "contains_request_id_token"
REASON_CONTAINS_TIMESTAMP_TOKEN = "contains_timestamp_token"
REASON_CONTAINS_HIGH_ENTROPY_HEX = "contains_high_entropy_hex_run"

# Canonical UUID shape: 8-4-4-4-12 hex characters with dashes.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# 10+ consecutive digits — unix timestamps (10 digits), millisecond
# timestamps (13 digits), and most numeric request ids land here.
_LONG_DIGIT_RUN_RE = re.compile(r"\d{10,}")

# 24+ hex characters in a single run — covers undashed UUIDs and most
# request-id hex digests.
_HIGH_ENTROPY_HEX_RE = re.compile(r"[0-9a-fA-F]{24,}")

# Token-level signal: substring labels operators commonly attach to
# per-request identifiers. Matched case-insensitively, on a word/segment
# boundary to avoid flagging legitimate tokens like "request_handler".
_REQUEST_ID_TOKENS = (
    "request-id",
    "request_id",
    "requestid",
    "reqid",
    "req-id",
    "trace-id",
    "trace_id",
    "traceid",
    "correlation-id",
    "correlation_id",
    "correlationid",
)

_TIMESTAMP_TOKENS = (
    "timestamp",
    "epoch",
    "unixtime",
    "unix-time",
    "unix_time",
)


def cache_key(
    *,
    tenant: str,
    flow: str,
    locale: str = "en-US",
    schema: str = "v1",
    category: str | None = None,
) -> str:
    """Compose a deterministic ``prompt_cache_key`` per Guide §1.

    Parameters are keyword-only to force callers to name the workload
    axes; positional ordering is intentionally not part of the public
    contract.

    Determinism guarantee: given the same ``(tenant, flow, locale,
    schema, category)`` tuple this function returns the same string on
    every call, in every process, on every host. No time source, no
    random source, no hostname lookup participates.

    The returned key is a colon-joined string. Callers SHOULD pass
    workload-stable identifiers (tenant slug, flow name, schema
    version) and MUST NOT pass per-request entropy (request ids,
    UUIDs, timestamps); use ``assert_deterministic`` at the call site
    if untrusted input is mixed in.
    """
    if not isinstance(tenant, str) or not tenant:
        raise ValueError("tenant must be a non-empty string")
    if not isinstance(flow, str) or not flow:
        raise ValueError("flow must be a non-empty string")
    if not isinstance(locale, str) or not locale:
        raise ValueError("locale must be a non-empty string")
    if not isinstance(schema, str) or not schema:
        raise ValueError("schema must be a non-empty string")
    if category is not None and (not isinstance(category, str) or not category):
        raise ValueError("category, when given, must be a non-empty string")

    parts = [tenant, flow, locale, schema]
    if category:
        parts.append(category)
    return ":".join(parts)


def anti_pattern_reasons(key: str) -> list[str]:
    """Return a list of short reason codes if ``key`` looks per-request.

    Non-raising. Returns an empty list when the key looks workload-
    stable. Reason codes are safe to log; the input ``key`` itself is
    not echoed back into the reason strings.
    """
    if not isinstance(key, str):
        raise TypeError("key must be a string")

    reasons: list[str] = []

    if _UUID_RE.search(key):
        reasons.append(REASON_LOOKS_LIKE_UUID)

    if _LONG_DIGIT_RUN_RE.search(key):
        reasons.append(REASON_CONTAINS_LONG_DIGIT_RUN)

    if _HIGH_ENTROPY_HEX_RE.search(key) and REASON_LOOKS_LIKE_UUID not in reasons:
        reasons.append(REASON_CONTAINS_HIGH_ENTROPY_HEX)

    lowered = key.lower()
    if any(tok in lowered for tok in _REQUEST_ID_TOKENS):
        reasons.append(REASON_CONTAINS_REQUEST_ID_TOKEN)
    if any(tok in lowered for tok in _TIMESTAMP_TOKENS):
        reasons.append(REASON_CONTAINS_TIMESTAMP_TOKEN)

    return reasons


def assert_deterministic(key: str) -> None:
    """Raise ``ValueError`` if ``key`` looks per-request.

    The raised exception message contains only the comma-joined reason
    codes (e.g. ``"looks_like_uuid,contains_long_digit_run"``). The
    ``key`` value itself is NOT included in the message — callers that
    need the value for debugging must log it themselves on their side
    of the trust boundary.
    """
    reasons = anti_pattern_reasons(key)
    if reasons:
        raise ValueError(
            "prompt_cache_key fails determinism policy: " + ",".join(reasons)
        )
