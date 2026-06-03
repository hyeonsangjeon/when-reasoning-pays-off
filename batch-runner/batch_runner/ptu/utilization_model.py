"""Pure functions for the Task 024 PTU utilization replay simulator.

This module is intentionally side-effect free. It implements the
official-spec inputs that Task 024 v1 takes from the Azure OpenAI PTU
Operations Guide:

* **Admission reservation** (Guide §0):
  ``admission_cost_tokens = max(0, prompt_tokens - cached_tokens) + max_output_tokens``.
* **Input TPM / PTU capacity table** (Guide §3): one-minute
  input-equivalent bucket capacity is ``input_tpm_per_ptu[model] * ptu_count``.
* **Continuous leak framing** (Guide §0): the bucket drains continuously
  at a rate proportional to deployed PTU count. The numeric leak constant
  itself is *operational inference* (Task 029) and is fitted in
  ``replay_simulator``; the geometric form is the official spec.

Out of scope for v1 (per Task 024 spec): Guide §3 model-specific
output-weight ratios, reasoning-token special weighting, multi-tenant or
cross-region pools, and any live LLM call.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

# Guide §3 Input TPM / PTU table. Keys are model family names exactly as
# emitted in source-run JSONL ``model`` fields. This table is an
# official-spec input; do not mutate it at runtime.
_INPUT_TPM_PER_PTU: dict[str, int] = {
    "gpt-5.2": 3400,
    "gpt-5.2-codex": 3400,
    "gpt-5.3-codex": 3400,
    "gpt-5.1": 4750,
    "gpt-5.1-codex": 4750,
    "gpt-5": 4750,
    "gpt-4.1": 3000,
    "gpt-4o": 2500,
}

# Read-only public view so downstream callers cannot mutate the table.
INPUT_TPM_PER_PTU: Mapping[str, int] = MappingProxyType(_INPUT_TPM_PER_PTU)


class UnknownModelError(KeyError):
    """Raised when a model is not present in the Guide §3 Input TPM / PTU table."""


def admission_cost_tokens(
    prompt_tokens: int,
    cached_tokens: int,
    max_output_tokens: int,
) -> int:
    """Return Guide §0 admission-reservation cost in input-equivalent tokens.

    ``max(0, prompt_tokens - cached_tokens) + max_output_tokens``. Negative
    or missing inputs are clamped to zero; the max-output term is treated
    as a non-negative reservation.
    """
    p = max(0, int(prompt_tokens)) if prompt_tokens is not None else 0
    c = max(0, int(cached_tokens)) if cached_tokens is not None else 0
    m = max(0, int(max_output_tokens)) if max_output_tokens is not None else 0
    input_term = p - c
    if input_term < 0:
        input_term = 0
    return input_term + m


def capacity_tokens(model: str, ptu_count: float) -> float:
    """Return the one-minute input-equivalent bucket capacity.

    ``input_tpm_per_ptu[model] * ptu_count``. Capacity is **not** fitted;
    it is read directly from the Guide §3 table.
    """
    if model not in _INPUT_TPM_PER_PTU:
        raise UnknownModelError(model)
    if ptu_count is None or float(ptu_count) <= 0:
        raise ValueError("ptu_count must be positive")
    return float(_INPUT_TPM_PER_PTU[model]) * float(ptu_count)


def leak_tokens(
    k_leak_tokens_per_ptu_per_second: float,
    ptu_count: float,
    delta_seconds: float,
) -> float:
    """Return continuous leak amount in tokens.

    ``max(0, k * ptu_count * delta_seconds)``. The numeric ``k`` is
    operational inference. Negative ``delta_seconds`` clamps to zero.
    """
    if k_leak_tokens_per_ptu_per_second is None or ptu_count is None or delta_seconds is None:
        return 0.0
    amount = float(k_leak_tokens_per_ptu_per_second) * float(ptu_count) * float(delta_seconds)
    return amount if amount > 0.0 else 0.0


__all__ = [
    "INPUT_TPM_PER_PTU",
    "UnknownModelError",
    "admission_cost_tokens",
    "capacity_tokens",
    "leak_tokens",
]
