"""Observability schema package (Task 028).

Exposes the canonical per-request and per-cell record dataclasses that
every PTU-aware script in the repo emits, plus the Azure Monitor
correlation contract (Appendix C metric registry + window derivation).
"""

from batch_runner.observability.azure_monitor_contract import (
    AZURE_MONITOR_PTU_METRICS,
    azure_monitor_correlation_window,
)
from batch_runner.observability.schema import (
    PTUCellSummary,
    PTURequestRecord,
    hash_cache_key,
)

__all__ = [
    "AZURE_MONITOR_PTU_METRICS",
    "PTUCellSummary",
    "PTURequestRecord",
    "azure_monitor_correlation_window",
    "hash_cache_key",
]
