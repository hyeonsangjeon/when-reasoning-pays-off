"""Fail-closed cost preflight for a billed Azure sample run."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

from batch_runner.experiment.ledger import RunLedger
from scripts._azure_pricing import (
    Gpt52Rates,
    PricingSelection,
    resolve_pinned_payg_snapshot,
    select_price_record,
)

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
    pricing_snapshot_path: str
    pricing_snapshot_sha256: str
    pricing_source_url: str
    pricing_archive_url: str | None
    pricing_accessed_date: str
    pricing_currency: str
    pricing_model_family: str
    pricing_model_version: str
    pricing_geography: str
    pricing_region: str
    pricing_deployment_type: str
    pricing_sku: str
    pricing_price_key: str
    pricing_meters: dict[str, str]
    input_rate_usd_per_1m_tokens: float
    cached_input_rate_usd_per_1m_tokens: float
    reasoning_rate_usd_per_1m_tokens: float | None
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
            "pricing_snapshot_path": self.pricing_snapshot_path,
            "pricing_snapshot_sha256": self.pricing_snapshot_sha256,
            "pricing_source_url": self.pricing_source_url,
            "pricing_archive_url": self.pricing_archive_url,
            "pricing_accessed_date": self.pricing_accessed_date,
            "pricing_currency": self.pricing_currency,
            "pricing_model_family": self.pricing_model_family,
            "pricing_model_version": self.pricing_model_version,
            "pricing_geography": self.pricing_geography,
            "pricing_region": self.pricing_region,
            "pricing_deployment_type": self.pricing_deployment_type,
            "pricing_sku": self.pricing_sku,
            "pricing_price_key": self.pricing_price_key,
            "pricing_meters": dict(self.pricing_meters),
            "input_rate_usd_per_1m_tokens": self.input_rate_usd_per_1m_tokens,
            "cached_input_rate_usd_per_1m_tokens": (
                self.cached_input_rate_usd_per_1m_tokens
            ),
            "reasoning_rate_usd_per_1m_tokens": (
                self.reasoning_rate_usd_per_1m_tokens
            ),
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


def resolve_ledger_pricing(ledger: RunLedger) -> PricingSelection:
    """Resolve and validate every safe pricing dimension in an Azure ledger."""
    pricing = ledger.execution.cost.pricing
    if pricing is None:
        raise ValueError("Azure pricing selection is missing")
    snapshot = resolve_pinned_payg_snapshot(
        snapshot_id=pricing.snapshot_id,
        snapshot_path=pricing.snapshot_path,
        snapshot_sha256=pricing.snapshot_sha256,
    )
    return select_price_record(
        snapshot,
        price_key=pricing.price_key,
        model_family=pricing.model_family,
        model_version=pricing.model_version,
        geography=pricing.geography,
        region=pricing.region,
        deployment_type=pricing.deployment_type,
        currency=pricing.currency,
    )


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
    selection = resolve_ledger_pricing(ledger)
    snapshot = selection.snapshot
    record = selection.record
    rates = record.rates
    input_rate = Decimal(str(rates.input_per_1m_usd))
    cached_input_rate = Decimal(str(rates.cached_input_per_1m_usd))
    output_rate = Decimal(str(rates.output_per_1m_usd))
    reasoning_rate = (
        Decimal(str(rates.reasoning_per_1m_usd))
        if isinstance(rates, Gpt52Rates)
        else None
    )
    million = Decimal(1_000_000)
    estimated_input = Decimal(input_tokens) * input_rate / million
    estimated_output = Decimal(output_tokens) * output_rate / million
    estimated_usd = estimated_input + estimated_output
    return CostPreflight(
        planned_requests=planned_requests,
        max_output_tokens_per_request=max_output,
        estimated_input_tokens=input_tokens,
        estimated_output_tokens=output_tokens,
        pricing_snapshot_id=str(snapshot.snapshot_id),
        pricing_snapshot_path=ledger.execution.cost.pricing.snapshot_path,
        pricing_snapshot_sha256=str(snapshot.snapshot_sha256),
        pricing_source_url=snapshot.source_url,
        pricing_archive_url=snapshot.archive_url,
        pricing_accessed_date=snapshot.accessed_date,
        pricing_currency=snapshot.currency,
        pricing_model_family=record.model_family,
        pricing_model_version=record.model_version,
        pricing_geography=record.geography,
        pricing_region=record.region,
        pricing_deployment_type=record.deployment_type,
        pricing_sku=record.sku,
        pricing_price_key=record.price_key,
        pricing_meters=dict(record.meters),
        input_rate_usd_per_1m_tokens=float(input_rate),
        cached_input_rate_usd_per_1m_tokens=float(cached_input_rate),
        reasoning_rate_usd_per_1m_tokens=(
            float(reasoning_rate) if reasoning_rate is not None else None
        ),
        output_rate_usd_per_1m_tokens=float(output_rate),
        estimated_input_usd=float(estimated_input),
        estimated_output_usd=float(estimated_output),
        estimated_usd=float(estimated_usd),
        hard_ceiling_usd=ledger.execution.cost.hard_ceiling_usd,
    )


__all__ = [
    "CostPreflight",
    "estimate_azure_cost",
    "resolve_ledger_pricing",
]
