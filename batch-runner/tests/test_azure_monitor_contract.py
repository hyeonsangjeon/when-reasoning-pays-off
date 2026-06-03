"""Tests for the Azure Monitor correlation contract (Task 028)."""

from __future__ import annotations

import pytest

from batch_runner.observability.azure_monitor_contract import (
    AZURE_MONITOR_PTU_METRICS,
    azure_monitor_correlation_window,
)
from batch_runner.observability.schema import PTURequestRecord, hash_cache_key


# Verbatim Appendix C metric names. Order matters.
_EXPECTED_METRICS = (
    "AzureOpenAIProvisionedManagedUtilizationV2",
    "AzureOpenAIRequests",
    "AzureOpenAITimeToResponse",
    "AzureOpenAITTLTInMS",
    "AzureOpenAIContextTokensCacheMatchRate",
    "ActiveTokens",
)


def _record(ts: str = "2026-01-01T12:00:00+00:00") -> PTURequestRecord:
    return PTURequestRecord(
        request_idx=0,
        wallclock_timestamp_iso=ts,
        deployment_name_requested="ptu-A",
        response_status_code=200,
        retry_after_ms=None,
        retry_after_seconds=None,
        x_ms_region="eastus",
        x_request_id="req-1",
        x_ms_deployment_name="ptu-A",
        x_ms_spillover_from_deployment=None,
        x_ms_spillover_error=None,
        x_ratelimit_remaining_requests=None,
        prompt_tokens=10,
        completion_tokens=1,
        cached_tokens=0,
        reasoning_tokens=0,
        total_tokens=11,
        max_output_tokens_sent=128,
        prompt_cache_key_used=hash_cache_key("synthetic"),
        prompt_cache_retention_sent="in_memory",
        reasoning_effort_sent="low",
        model_id="gpt-5-mini",
        first_token_latency_ms=100.0,
        total_latency_ms=200.0,
    )


def test_metric_names_verbatim_and_in_order():
    assert AZURE_MONITOR_PTU_METRICS == _EXPECTED_METRICS


def test_metric_tuple_is_immutable():
    assert isinstance(AZURE_MONITOR_PTU_METRICS, tuple)
    with pytest.raises(TypeError):
        AZURE_MONITOR_PTU_METRICS[0] = "x"  # type: ignore[index]


def test_metric_count_is_six():
    assert len(AZURE_MONITOR_PTU_METRICS) == 6


def test_correlation_window_symmetric_padding():
    rec = _record("2026-01-01T12:00:00+00:00")
    start, end = azure_monitor_correlation_window(rec, pad_seconds=60)
    assert start == "2026-01-01T11:59:00+00:00"
    assert end == "2026-01-01T12:01:00+00:00"


def test_correlation_window_zero_pad():
    rec = _record("2026-01-01T12:00:00+00:00")
    start, end = azure_monitor_correlation_window(rec, pad_seconds=0)
    assert start == end == "2026-01-01T12:00:00+00:00"


def test_correlation_window_accepts_z_suffix():
    rec = _record("2026-01-01T12:00:00Z")
    start, end = azure_monitor_correlation_window(rec, pad_seconds=30)
    assert start == "2026-01-01T11:59:30+00:00"
    assert end == "2026-01-01T12:00:30+00:00"


def test_correlation_window_rejects_negative_pad():
    rec = _record()
    with pytest.raises(ValueError):
        azure_monitor_correlation_window(rec, pad_seconds=-1)


def test_correlation_window_default_pad_is_60s():
    rec = _record("2026-01-01T12:00:00+00:00")
    start, end = azure_monitor_correlation_window(rec)
    assert start == "2026-01-01T11:59:00+00:00"
    assert end == "2026-01-01T12:01:00+00:00"
