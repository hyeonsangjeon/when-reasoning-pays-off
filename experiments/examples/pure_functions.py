"""Network-free examples: call the pure decision/estimator functions directly.

    python experiments/examples/pure_functions.py

The Family B runners are built on small, deterministic, side-effect-free
functions. You can call them with literal inputs and read the output — no
Azure credentials, no YAML, no HTTPS. These are the clearest possible
"input -> function -> output" demonstrations in the repo, and they are the
same primitives the runners use internally (and that the unit tests pin).
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from scripts.measure_cache_key_bucketing import (  # noqa: E402
    compute_projected_tpm,
    select_bucket,
)
from scripts.simulate_spillover import (  # noqa: E402
    ReactiveObservation,
    ReactivePolicyParams,
    ReactiveState,
    reactive_decide,
)


def demo_select_bucket() -> None:
    """exp006 — which prompt_cache_key does arrival N land on?"""
    print("--- select_bucket(arrival_idx, cardinality, namespace) -> str ---")
    namespace = "benchmark06_inmemory_card08_ab12cd34"
    for arrival_idx in range(4):
        key = select_bucket(arrival_idx=arrival_idx, cardinality=8, namespace=namespace)
        print(f"  arrival {arrival_idx} (cardinality=8) -> {key}")
    # cardinality=1 collapses every arrival onto a single hot bucket:
    only = select_bucket(arrival_idx=99, cardinality=1, namespace=namespace)
    print(f"  arrival 99 (cardinality=1) -> {only}")
    print()


def demo_projected_tpm() -> None:
    """exp006/exp007 — TPM feasibility numerator (60 * tps * per-request cap)."""
    print("--- compute_projected_tpm(sustain_tps, estimated_processed_tokens_max) -> float ---")
    tpm = compute_projected_tpm(sustain_tps=1.0, estimated_processed_tokens_max=11000)
    print(f"  1.0 req/s x 11,000 tokens/req -> {tpm:,.0f} TPM")
    tpm2 = compute_projected_tpm(sustain_tps=0.33, estimated_processed_tokens_max=2158 + 16384)
    print(f"  0.33 req/s x 18,542 tokens/req -> {tpm2:,.0f} TPM")
    print()


def demo_reactive_decide() -> None:
    """exp004/exp005 — reactive router: does this request stay on primary?"""
    print("--- reactive_decide(obs, state, params) -> (route, new_state) ---")
    params = ReactivePolicyParams()  # first_token_timeout_ms = 3000 by default
    state = ReactiveState()  # fresh: on_spillover = False

    healthy = ReactiveObservation(
        request_idx=0,
        first_token_latency_ms=800.0,  # well under the 3000 ms timeout
        real_429_observed=False,
        monotonic_time_s=0.0,
    )
    route, state = reactive_decide(healthy, state, params)
    print(f"  first_token=800ms, no 429  -> route={route!r}")

    slow = ReactiveObservation(
        request_idx=1,
        first_token_latency_ms=5000.0,  # exceeds the 3000 ms timeout
        real_429_observed=False,
        monotonic_time_s=1.0,
    )
    route, state = reactive_decide(slow, state, params)
    print(f"  first_token=5000ms          -> route={route!r} (diverted to spillover)")
    print()


def main() -> int:
    demo_select_bucket()
    demo_projected_tpm()
    demo_reactive_decide()
    print("All outputs above were produced with zero network calls.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
