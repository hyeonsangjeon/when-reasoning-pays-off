"""Tests for batch_runner.ptu.utilization_model (Task 024)."""

from __future__ import annotations

import pytest

from batch_runner.ptu.utilization_model import (
    INPUT_TPM_PER_PTU,
    UnknownModelError,
    admission_cost_tokens,
    capacity_tokens,
    leak_tokens,
)


def test_admission_cost_basic():
    assert admission_cost_tokens(100, 20, 200) == 280


def test_admission_cost_cached_greater_than_prompt_clamps_to_zero():
    assert admission_cost_tokens(50, 200, 64) == 64


def test_admission_cost_negative_inputs_clamped():
    assert admission_cost_tokens(-5, -10, -7) == 0
    assert admission_cost_tokens(None, None, None) == 0


def test_capacity_tokens_guide_table_gpt_5_2():
    assert capacity_tokens("gpt-5.2", 2) == 6800
    assert capacity_tokens("gpt-5.2", 1) == 3400


def test_capacity_tokens_gpt_4o():
    assert capacity_tokens("gpt-4o", 4) == 10000


def test_capacity_tokens_rejects_unknown_model():
    with pytest.raises(UnknownModelError):
        capacity_tokens("gpt-unknown", 1)


def test_capacity_tokens_rejects_nonpositive_ptu():
    with pytest.raises(ValueError):
        capacity_tokens("gpt-5.2", 0)
    with pytest.raises(ValueError):
        capacity_tokens("gpt-5.2", -1)


def test_leak_tokens_nonnegative():
    assert leak_tokens(1.0, 2.0, -5.0) == 0.0
    assert leak_tokens(0.0, 2.0, 5.0) == 0.0


def test_leak_tokens_monotonic_in_delta():
    a = leak_tokens(2.0, 1.0, 1.0)
    b = leak_tokens(2.0, 1.0, 2.0)
    assert b > a


def test_leak_tokens_monotonic_in_ptu():
    a = leak_tokens(2.0, 1.0, 1.0)
    b = leak_tokens(2.0, 4.0, 1.0)
    assert b > a


def test_leak_tokens_monotonic_in_k():
    a = leak_tokens(1.0, 2.0, 3.0)
    b = leak_tokens(5.0, 2.0, 3.0)
    assert b > a


def test_input_tpm_per_ptu_is_readonly():
    assert INPUT_TPM_PER_PTU["gpt-5.2"] == 3400
    # MappingProxyType raises TypeError on item assignment.
    with pytest.raises(TypeError):
        INPUT_TPM_PER_PTU["gpt-new"] = 1234  # type: ignore[index]
