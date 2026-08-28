"""Deterministic offline mock provider.

The mock makes **no** network call and produces the same record for the same
input every time. It exists to preview the output shape and to let the whole
pipeline be tested with no credentials and no cost. It is a *preview*, not
evidence: its token counts and text are synthetic.

Prefer a real provider (Azure or Ollama) for any actual measurement.
"""

from __future__ import annotations

import hashlib

from batch_runner.experiment.ledger import RunLedger
from batch_runner.experiment.record import (
    METRIC_NOT_SUPPORTED,
    METRIC_SYNTHETIC,
    OutputRecord,
    ProviderCapabilities,
)


class MockProvider:
    """A deterministic, offline stand-in for a real model call."""

    name = "mock"

    def __init__(self, *, ledger: RunLedger, capture_io: bool) -> None:
        self._ledger = ledger
        self._capture_io = capture_io

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name,
            billed=False,
            token_usage=METRIC_SYNTHETIC,
            reasoning_tokens=METRIC_NOT_SUPPORTED,
            cached_tokens=METRIC_NOT_SUPPORTED,
            notes=(
                "deterministic offline preview; token counts and text are "
                "synthetic and are not a measurement"
            ),
        )

    def prepare(self) -> None:
        # Nothing to reach: the mock is always available and offline.
        return None

    def run_row(self, row_id: str, repeat_index: int, prompt: str) -> OutputRecord:
        seed = hashlib.sha256(
            f"{row_id}|{repeat_index}|{prompt}".encode("utf-8")
        ).hexdigest()
        # Deterministic, wall-clock-free synthetic metrics.
        input_tokens = max(1, len(prompt.split()))
        output_tokens = 8 + (int(seed[:2], 16) % 8)
        latency_ms = 5 + (int(seed[2:4], 16) % 20)
        response = f"[mock] deterministic preview for row {row_id}"
        return OutputRecord(
            row_id=row_id,
            repeat_index=repeat_index,
            provider=self.name,
            model=self._ledger.model,
            status="ok",
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=None,
            cached_tokens=None,
            total_tokens=input_tokens + output_tokens,
            finish_reason="stop",
            request_text=prompt if self._capture_io else None,
            response_text=response if self._capture_io else None,
        )


__all__ = ["MockProvider"]
