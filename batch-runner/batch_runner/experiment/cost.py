"""Conservative pre-flight cost estimation for a billed Azure sample run.

The ledger's ``hard_ceiling_usd`` is only meaningful if it is *enforced*. This
module produces an intentionally conservative upper-bound estimate of what a
billed run could cost, so the runner can refuse — before any network call — a
plan that could exceed the ceiling. It never talks to a pricing service; it
uses a single pinned constant chosen to over-estimate real gpt-5.2 pricing so
the guard errs on the side of refusing.

The estimate is deliberately pessimistic:

* input tokens are approximated from the prompt text at ~3 characters/token
  (fewer characters per token ⇒ a larger, safer token count);
* every request is assumed to emit the full ``max_output_tokens``;
* the per-token price is a flat ceiling that exceeds current gpt-5.2 rates.

None of these values are a bill. They exist only to keep a $0.01 ceiling from
launching a large call.
"""

from __future__ import annotations

import dataclasses
import math

from batch_runner.experiment.ledger import RunLedger

#: Flat, conservative USD-per-token ceiling used for the pre-flight guard only.
#: This is ~$60 per million tokens — well above current gpt-5.2 input/output
#: rates — so the estimate is an upper bound, never an under-count. It is NOT a
#: quoted price and must not be presented as one.
AZURE_CONSERVATIVE_USD_PER_TOKEN = 6e-5

#: Assumed characters per token when approximating input length (pessimistic).
_CHARS_PER_TOKEN = 3

#: A small fixed per-request overhead added to the input estimate.
_INPUT_TOKEN_OVERHEAD = 8


@dataclasses.dataclass(frozen=True)
class CostPreflight:
    """A conservative, pre-network cost plan for a billed run."""

    planned_requests: int
    max_output_tokens_per_request: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    usd_per_token: float
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
            "conservative_usd_per_token": self.usd_per_token,
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
    return math.ceil(len(prompt) / _CHARS_PER_TOKEN) + _INPUT_TOKEN_OVERHEAD


def estimate_azure_cost(ledger: RunLedger, prompts: list[str]) -> CostPreflight:
    """Return a conservative :class:`CostPreflight` for ``prompts``.

    ``prompts`` are the actual selected prompt strings (one per row); each is
    executed ``repeats`` times. The estimate assumes the maximum output for
    every request.
    """
    repeats = ledger.execution.repeats
    max_output = ledger.execution.max_output_tokens
    planned_requests = len(prompts) * repeats
    input_tokens = sum(_estimate_input_tokens(p) for p in prompts) * repeats
    output_tokens = planned_requests * max_output
    estimated_usd = (input_tokens + output_tokens) * AZURE_CONSERVATIVE_USD_PER_TOKEN
    return CostPreflight(
        planned_requests=planned_requests,
        max_output_tokens_per_request=max_output,
        estimated_input_tokens=input_tokens,
        estimated_output_tokens=output_tokens,
        usd_per_token=AZURE_CONSERVATIVE_USD_PER_TOKEN,
        estimated_usd=estimated_usd,
        hard_ceiling_usd=ledger.execution.cost.hard_ceiling_usd,
    )


__all__ = [
    "AZURE_CONSERVATIVE_USD_PER_TOKEN",
    "CostPreflight",
    "estimate_azure_cost",
]
