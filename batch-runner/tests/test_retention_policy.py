"""Tests for batch_runner.cache.retention_policy (Task 026)."""

from __future__ import annotations

import pytest

from batch_runner.cache.retention_policy import (
    EXTENDED_RETENTION_SUPPORTED_MODELS,
    ImplicitInMemoryError,
    UnknownRetentionValueError,
    default_retention,
    ensure_explicit,
)


def test_supported_models_set_contains_guide_table():
    expected = {
        "gpt-5.4",
        "gpt-5.3-codex",
        "gpt-5.2",
        "gpt-5.1-codex-max",
        "gpt-5.1",
        "gpt-5.1-codex",
        "gpt-5.1-codex-mini",
        "gpt-5.1-chat",
        "gpt-5",
        "gpt-5-codex",
        "gpt-4.1",
    }
    assert EXTENDED_RETENTION_SUPPORTED_MODELS == expected


def test_supported_models_is_frozen():
    with pytest.raises(AttributeError):
        EXTENDED_RETENTION_SUPPORTED_MODELS.add("gpt-future")  # type: ignore[attr-defined]


def test_default_retention_gpt_5_2_is_in_memory():
    assert default_retention("gpt-5.2") == "in_memory"


def test_default_retention_unknown_model_raises_keyerror():
    with pytest.raises(KeyError):
        default_retention("gpt-unknown")


def test_ensure_explicit_raises_on_none_for_in_memory_default():
    with pytest.raises(ImplicitInMemoryError):
        ensure_explicit("gpt-5.2", None)


def test_ensure_explicit_returns_24h_when_passed():
    assert ensure_explicit("gpt-5.2", "24h") == "24h"


def test_ensure_explicit_returns_in_memory_when_passed():
    assert ensure_explicit("gpt-5.2", "in_memory") == "in_memory"


def test_ensure_explicit_rejects_unknown_retention_value():
    with pytest.raises(UnknownRetentionValueError):
        ensure_explicit("gpt-5.2", "permanent")


def test_ensure_explicit_unknown_model_raises_keyerror():
    with pytest.raises(KeyError):
        ensure_explicit("gpt-unknown", "24h")


def test_implicit_in_memory_error_is_value_error():
    # Catchability: callers may want to catch ValueError broadly.
    assert issubclass(ImplicitInMemoryError, ValueError)


def test_unknown_retention_value_error_is_value_error():
    assert issubclass(UnknownRetentionValueError, ValueError)


def test_all_listed_models_default_to_in_memory_per_guide():
    # The Guide §2 "must be explicit" rule applies because all listed
    # models default to in_memory.
    for model in EXTENDED_RETENTION_SUPPORTED_MODELS:
        assert default_retention(model) == "in_memory"


def test_ensure_explicit_does_not_leak_model_id_into_unknown_value_msg():
    # Hygiene: error messages should not be a debug dump.
    try:
        ensure_explicit("gpt-5.2", "weird-value")
    except UnknownRetentionValueError as exc:
        assert "weird-value" not in str(exc)
