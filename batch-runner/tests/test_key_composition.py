"""Tests for batch_runner.cache.key_composition (Task 026)."""

from __future__ import annotations

import pytest

from batch_runner.cache.key_composition import (
    REASON_CONTAINS_LONG_DIGIT_RUN,
    REASON_CONTAINS_REQUEST_ID_TOKEN,
    REASON_CONTAINS_TIMESTAMP_TOKEN,
    REASON_LOOKS_LIKE_UUID,
    anti_pattern_reasons,
    assert_deterministic,
    cache_key,
)


def test_cache_key_minimal():
    assert cache_key(tenant="acme", flow="answer") == "acme:answer:en-US:v1"


def test_cache_key_with_category():
    assert (
        cache_key(tenant="contoso", flow="summarize", category="legal")
        == "contoso:summarize:en-US:v1:legal"
    )


def test_cache_key_overrides():
    assert (
        cache_key(
            tenant="northwind",
            flow="qa",
            locale="ko-KR",
            schema="v3",
            category="support",
        )
        == "northwind:qa:ko-KR:v3:support"
    )


def test_cache_key_deterministic_1000x():
    """Determinism property: same inputs -> same key, 1000 invocations."""
    first = cache_key(tenant="acme", flow="answer")
    for _ in range(1000):
        assert cache_key(tenant="acme", flow="answer") == first


def test_cache_key_rejects_empty_tenant():
    with pytest.raises(ValueError):
        cache_key(tenant="", flow="answer")


def test_cache_key_rejects_empty_flow():
    with pytest.raises(ValueError):
        cache_key(tenant="acme", flow="")


def test_cache_key_rejects_empty_category_when_given():
    with pytest.raises(ValueError):
        cache_key(tenant="acme", flow="answer", category="")


def test_assert_deterministic_passes_on_workload_key():
    assert_deterministic("agent:support:v1")
    assert_deterministic(cache_key(tenant="acme", flow="answer"))


def test_assert_deterministic_raises_on_uuid():
    with pytest.raises(ValueError) as ei:
        assert_deterministic(
            "user-12345678-1234-1234-1234-1234567890ab-uuid"
        )
    assert REASON_LOOKS_LIKE_UUID in str(ei.value)


def test_error_message_does_not_contain_key_value():
    """Error messages must NEVER echo the offending key back."""
    secret_like_key = "tenant-secretish-1717029600000-trailing"
    with pytest.raises(ValueError) as ei:
        assert_deterministic(secret_like_key)
    msg = str(ei.value)
    assert secret_like_key not in msg
    assert "1717029600000" not in msg
    assert "secretish" not in msg
    assert "trailing" not in msg


def test_assert_deterministic_raises_on_long_digit_run():
    with pytest.raises(ValueError) as ei:
        assert_deterministic("acme:answer:1717029600000")
    assert REASON_CONTAINS_LONG_DIGIT_RUN in str(ei.value)


def test_assert_deterministic_raises_on_request_id_token():
    with pytest.raises(ValueError) as ei:
        assert_deterministic("acme:request-id:abc")
    assert REASON_CONTAINS_REQUEST_ID_TOKEN in str(ei.value)


def test_assert_deterministic_raises_on_timestamp_token():
    with pytest.raises(ValueError) as ei:
        assert_deterministic("acme:answer:timestamp")
    assert REASON_CONTAINS_TIMESTAMP_TOKEN in str(ei.value)


def test_anti_pattern_reasons_clean_key_returns_empty():
    assert anti_pattern_reasons("acme:answer:en-US:v1") == []


def test_anti_pattern_reasons_uuid_and_digit_run_both():
    reasons = anti_pattern_reasons(
        "12345678-1234-1234-1234-1234567890ab-1717029600000"
    )
    assert REASON_LOOKS_LIKE_UUID in reasons
    assert REASON_CONTAINS_LONG_DIGIT_RUN in reasons


def test_anti_pattern_reasons_non_string_raises():
    with pytest.raises(TypeError):
        anti_pattern_reasons(12345)  # type: ignore[arg-type]


def test_short_digit_run_is_not_flagged():
    # Schema version like v1 / 12345 (5 digits) must not trip the
    # long-digit-run detector — only runs of 10+ digits are flagged.
    assert anti_pattern_reasons("acme:answer:v1:12345") == []
