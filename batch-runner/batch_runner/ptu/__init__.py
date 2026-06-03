"""Public surface for the PTU admission controller (Task 023).

Re-exports the controller class, typed exceptions, and event dataclass so
callers can write::

    from batch_runner.ptu import AdmissionController, ThrottleEvent
"""

from .admission_controller import (
    AdmissionController,
    AdmissionExhausted,
    DoubleRetryError,
    ThrottleEvent,
    WaitExceedsCeiling,
    default_jitter,
)
from .cooldown_backends import (
    CooldownBackend,
    InMemoryCooldownBackend,
    KeyValueCooldownBackend,
)
from .cooldown_coordinator import (
    CooldownCoordinator,
    JitterStrategy,
)
from .replay_simulator import (
    NormalizedReplayRecord,
    ReplayEvent,
    SOURCE_TASK013,
    SOURCE_TASK019,
    adapt_records,
    adapt_task013_record,
    adapt_task019_record,
    calibrate_k,
    leave_one_source_run_out,
    load_jsonl,
    recover_zero_usage_429_demand,
    replay_stream,
)
from .utilization_model import (
    INPUT_TPM_PER_PTU,
    admission_cost_tokens,
    capacity_tokens,
    leak_tokens,
)

__all__ = [
    "AdmissionController",
    "AdmissionExhausted",
    "CooldownBackend",
    "CooldownCoordinator",
    "DoubleRetryError",
    "InMemoryCooldownBackend",
    "JitterStrategy",
    "KeyValueCooldownBackend",
    "ThrottleEvent",
    "WaitExceedsCeiling",
    "default_jitter",
    "INPUT_TPM_PER_PTU",
    "NormalizedReplayRecord",
    "ReplayEvent",
    "SOURCE_TASK013",
    "SOURCE_TASK019",
    "adapt_records",
    "adapt_task013_record",
    "adapt_task019_record",
    "admission_cost_tokens",
    "calibrate_k",
    "capacity_tokens",
    "leak_tokens",
    "leave_one_source_run_out",
    "load_jsonl",
    "recover_zero_usage_429_demand",
    "replay_stream",
]
