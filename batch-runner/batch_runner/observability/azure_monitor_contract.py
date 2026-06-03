"""Azure Monitor correlation contract for PTU records (Task 028).

The Azure OpenAI PTU Operations Guide Appendix C names six Azure Monitor
metrics that observability-grade analysis MUST query on the same time
window when interpreting a per-request record:

1. ``AzureOpenAIProvisionedManagedUtilizationV2``
2. ``AzureOpenAIRequests`` (split by ``StatusCode`` and ``IsSpillover``)
3. ``AzureOpenAITimeToResponse``
4. ``AzureOpenAITTLTInMS``
5. ``AzureOpenAIContextTokensCacheMatchRate``
6. ``ActiveTokens``

This module exposes the frozen metric-name tuple and a pure helper that
derives a ``(start_iso, end_iso)`` window from a
:class:`~batch_runner.observability.schema.PTURequestRecord`.

There is **no Azure SDK import here**, **no network call**, and **no
credential read**. Actually querying Azure Monitor is operator work;
this module only enforces that downstream consumers ask for the right
metrics on the right window.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from batch_runner.observability.schema import PTURequestRecord


# Verbatim Appendix C names. ORDER MUST NOT CHANGE — downstream
# fixtures and dashboards rely on it.
AZURE_MONITOR_PTU_METRICS: tuple[str, ...] = (
    "AzureOpenAIProvisionedManagedUtilizationV2",
    "AzureOpenAIRequests",
    "AzureOpenAITimeToResponse",
    "AzureOpenAITTLTInMS",
    "AzureOpenAIContextTokensCacheMatchRate",
    "ActiveTokens",
)


# Default symmetric padding around the request wallclock timestamp. Azure
# Monitor metrics are 1-minute aggregates; a 60-second pad on each side
# guarantees the request lands inside the queried window even if its
# wallclock and the Azure clock differ by a few seconds.
_DEFAULT_PAD_SECONDS = 60


def azure_monitor_correlation_window(
    record: "PTURequestRecord",
    *,
    pad_seconds: int = _DEFAULT_PAD_SECONDS,
) -> tuple[str, str]:
    """Return ``(start_iso, end_iso)`` for the Appendix C metric query.

    The window is derived from the record's
    ``wallclock_timestamp_iso`` plus a symmetric pad (default 60s).
    Both endpoints are UTC ISO 8601 strings.

    The helper is pure: it does not call Azure Monitor, only computes
    the time bounds an operator (or a downstream offline tool) must
    use when querying.
    """
    if pad_seconds < 0:
        raise ValueError("pad_seconds must be non-negative")

    base = _parse_iso_utc(record.wallclock_timestamp_iso)
    delta = timedelta(seconds=pad_seconds)
    start = base - delta
    end = base + delta
    return _format_iso_utc(start), _format_iso_utc(end)


def _parse_iso_utc(value: str) -> datetime:
    """Parse an ISO 8601 timestamp. Accepts ``Z`` suffix for UTC."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()
