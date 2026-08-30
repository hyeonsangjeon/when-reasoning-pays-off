"""Fail-closed cost preflight for a billed Azure sample run."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

from batch_runner.experiment.ledger import RunLedger

#: A small fixed per-request overhead added to the input estimate.
_INPUT_TOKEN_OVERHEAD = 8


@dataclasses.dataclass(frozen=True)
class CostPreflight:
    """A conservative, pre-network cost plan for a billed run."""

    planned_requests: int
    max_output_tokens_per_request: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    pricing_snapshot_id: str
    pricing_model: str
    input_rate_usd_per_1m_tokens: float
    output_rate_usd_per_1m_tokens: float
    estimated_input_usd: float
    estimated_output_usd: float
    estimated_usd: float
    hard_ceiling_usd: float

    @property
    def within_ceiling(self) -> bool:
        return self.estimated_usd <= self.hard_ceiling_usd

    def to_json(self) -> dict[str, object]:
        return {
            "planned_requests": self.planned_requests,
            "max_output_tokens_per_request": self.max_output_tokens_per_request,
            "estimated_input_tokens": self.estimated_input_tokens,
            "estimated_output_tokens": self.estimated_output_tokens,
            "pricing_snapshot_id": self.pricing_snapshot_id,
            "pricing_model": self.pricing_model,
            "input_rate_usd_per_1m_tokens": self.input_rate_usd_per_1m_tokens,
            "output_rate_usd_per_1m_tokens": self.output_rate_usd_per_1m_tokens,
            "estimated_input_usd": round(self.estimated_input_usd, 6),
            "estimated_output_usd": round(self.estimated_output_usd, 6),
            "estimated_usd": round(self.estimated_usd, 6),
            "hard_ceiling_usd": self.hard_ceiling_usd,
            "within_ceiling": self.within_ceiling,
        }

    def plan_line(self) -> str:
        """A one-line, value-free summary to show before a billed run."""
        return (
            f"cost plan: {self.planned_requests} request(s), "
            f"<= {self.max_output_tokens_per_request} output tokens each; "
            f"conservative estimate ${self.estimated_usd:.4f} "
            f"(ceiling ${self.hard_ceiling_usd:.2f})"
        )


def _estimate_input_tokens(prompt: str) -> int:
    # UTF-8 bytes are a conservative tokenizer-independent upper bound.
    return len(prompt.encode("utf-8")) + _INPUT_TOKEN_OVERHEAD


def estimate_azure_cost(
    ledger: RunLedger, prompts: list[str], *, repeats: int | None = None
) -> CostPreflight:
    """Return a conservative :class:`CostPreflight` for ``prompts``.

    By default, ``prompts`` contains one string per selected row and the ledger's
    repeat count is applied. A retry caller may instead pass one string per
    actual attempt with ``repeats=1``. The estimate assumes the maximum output
    for every request.
    """
    effective_repeats = ledger.execution.repeats if repeats is None else repeats
    if effective_repeats < 1:
        raise ValueError("repeats must be at least one")
    max_output = ledger.execution.max_output_tokens
    planned_requests = len(prompts) * effective_repeats
    input_tokens = (
        sum(_estimate_input_tokens(p) for p in prompts) * effective_repeats
    )
    output_tokens = planned_requests * max_output
    cost = ledger.execution.cost
    if any(
        value is None
        for value in (
            cost.pricing_snapshot_id,
            cost.pricing_model,
            cost.input_per_1m_usd,
            cost.output_per_1m_usd,
        )
    ):
        raise ValueError("Azure pricing assumptions are incomplete")
    input_rate = Decimal(str(cost.input_per_1m_usd))
    output_rate = Decimal(str(cost.output_per_1m_usd))
    million = Decimal(1_000_000)
    estimated_input = Decimal(input_tokens) * input_rate / million
    estimated_output = Decimal(output_tokens) * output_rate / million
    estimated_usd = estimated_input + estimated_output
    return CostPreflight(
        planned_requests=planned_requests,
        max_output_tokens_per_request=max_output,
        estimated_input_tokens=input_tokens,
        estimated_output_tokens=output_tokens,
        pricing_snapshot_id=str(cost.pricing_snapshot_id),
        pricing_model=str(cost.pricing_model),
        input_rate_usd_per_1m_tokens=float(input_rate),
        output_rate_usd_per_1m_tokens=float(output_rate),
        estimated_input_usd=float(estimated_input),
        estimated_output_usd=float(estimated_output),
        estimated_usd=float(estimated_usd),
        hard_ceiling_usd=ledger.execution.cost.hard_ceiling_usd,
    )


__all__ = [
    "CostPreflight",
    "estimate_azure_cost",
]
