"""Tests for batch_runner.ptu.replay_simulator (Task 024)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from batch_runner.ptu.replay_simulator import (
    INPUT_TPM_PER_PTU,
    SOURCE_TASK013,
    SOURCE_TASK019,
    NormalizedReplayRecord,
    adapt_records,
    adapt_task013_record,
    adapt_task019_record,
    calibrate_k,
    leave_one_source_run_out,
    load_jsonl,
    recover_zero_usage_429_demand,
    replay_stream,
)
from batch_runner.ptu.utilization_model import (
    admission_cost_tokens,
    capacity_tokens,
    leak_tokens,
)


# --- Forbidden-import static guard ---------------------------------------

_FORBIDDEN = (
    "openai", "AzureOpenAI", "AsyncAzureOpenAI",
    "requests", "httpx", "aiohttp",
    "socket.socket", "socket.create_connection",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_GUARDED_FILES = [
    _REPO_ROOT / "batch-runner" / "batch_runner" / "ptu" / "utilization_model.py",
    _REPO_ROOT / "batch-runner" / "batch_runner" / "ptu" / "replay_simulator.py",
    _REPO_ROOT / "batch-runner" / "batch_runner" / "ptu" / "__init__.py",
    _REPO_ROOT / "scripts" / "replay_ptu_utilization.py",
]


def test_static_no_network_imports():
    """Replay modules and CLI must not import any HTTP/SDK/socket clients."""
    for path in _GUARDED_FILES:
        text = path.read_text(encoding="utf-8")
        for needle in _FORBIDDEN:
            # Forbid both ``import openai`` style and ``from openai import``.
            patterns = [
                rf"\bimport\s+{re.escape(needle)}\b",
                rf"\bfrom\s+{re.escape(needle)}\b",
                rf"\b{re.escape(needle)}\(",
            ]
            for pat in patterns:
                if re.search(pat, text):
                    raise AssertionError(
                        f"{path} contains forbidden network/client reference matching {pat!r}"
                    )


# --- Helpers for synthetic streams ---------------------------------------


def _accepted_record(
    t: float,
    *,
    source_run_id: str = "synth_run_a",
    model: str = "gpt-5.2",
    prompt: int = 4000,
    cached: int = 0,
    max_out: int = 1000,
    output: int = 500,
    latency_ms: float = 1000.0,
    line: int = 0,
    cell_key: str | None = None,
) -> NormalizedReplayRecord:
    return NormalizedReplayRecord(
        source_label=SOURCE_TASK013,
        source_path="synth/" + source_run_id,
        source_run_id=source_run_id,
        timestamp_seconds=t,
        model=model,
        prompt_tokens=prompt,
        cached_tokens=cached,
        max_output_tokens=max_out,
        observed_output_tokens=output,
        observed_accepted=True,
        observed_429=False,
        observed_retry_after_ms=None,
        fallback_reasons=(),
        capacity_source="declared_ptu_count",
        total_latency_ms=latency_ms,
        original_line_number=line,
        cell_key=cell_key,
    )


def _observed_429_record(
    t: float,
    *,
    source_run_id: str = "synth_run_a",
    model: str = "gpt-5.2",
    prompt: int = 4000,
    cached: int = 0,
    max_out: int = 1000,
    retry_after_ms: float | None = 1500.0,
    line: int = 0,
    cell_key: str | None = None,
) -> NormalizedReplayRecord:
    return NormalizedReplayRecord(
        source_label=SOURCE_TASK013,
        source_path="synth/" + source_run_id,
        source_run_id=source_run_id,
        timestamp_seconds=t,
        model=model,
        prompt_tokens=prompt,
        cached_tokens=cached,
        max_output_tokens=max_out,
        observed_output_tokens=None,
        observed_accepted=False,
        observed_429=True,
        observed_retry_after_ms=retry_after_ms,
        fallback_reasons=(),
        capacity_source="declared_ptu_count",
        total_latency_ms=None,
        original_line_number=line,
        cell_key=cell_key,
    )


# --- Token-bucket onset & retry-after derivation -------------------------


def test_synthetic_onset_predicted_at_expected_time():
    """Hand-computed token-bucket onset.

    Capacity at ptu_count=1 is 3400 tokens (Guide §3 gpt-5.2). With
    k_leak=0 we send admission_cost=5000 each request; first request
    accepted (util=5000 > 3400? actually first cost 5000 > 3400 so it
    would 429 immediately). Use smaller costs to test gradual fill.
    """
    # capacity = 3400. Use cost=1000 per request, k_leak=0, ptu=1.
    # After 3 requests util=3000 (<=3400, ok). 4th adds to 4000 >3400 → 429.
    records = [
        _accepted_record(0.0, prompt=1000, cached=0, max_out=0, output=500, latency_ms=100.0, line=0),
        _accepted_record(1.0, prompt=1000, cached=0, max_out=0, output=500, latency_ms=100.0, line=1),
        _accepted_record(2.0, prompt=1000, cached=0, max_out=0, output=500, latency_ms=100.0, line=2),
        _accepted_record(3.0, prompt=1000, cached=0, max_out=0, output=500, latency_ms=100.0, line=3),
    ]
    # Use tiny but positive k_leak (1e-9) so first three accepted, fourth
    # still over capacity (3400). Leak between events ~= 0.
    events = replay_stream(records, k_leak_tokens_per_ptu_per_second=1e-9, ptu_count=1.0)
    kinds = [e.kind for e in events]
    assert kinds[:3] == ["accepted", "accepted", "accepted"]
    assert kinds[3] == "predicted_429"


def test_predicted_retry_after_from_token_overshoot():
    # Setup so a single request overflows by exactly 100 tokens.
    # cap = 3400. util=3350 then cost=150 → overshoot = 100.
    # predicted_retry_after_ms = 100 / (k*ptu) * 1000.
    # With k=2, ptu=1: 100/2*1000 = 50000 ms.
    pad = _accepted_record(0.0, prompt=3350, cached=0, max_out=0, output=0, latency_ms=10.0, line=0)
    burst = _accepted_record(0.0001, prompt=150, cached=0, max_out=0, output=0, latency_ms=10.0, line=1)
    events = replay_stream([pad, burst], k_leak_tokens_per_ptu_per_second=2.0, ptu_count=1.0)
    # Second event should be predicted_429.
    burst_event = events[1]
    assert burst_event.kind == "predicted_429"
    # Some leak occurred over the 0.0001s gap, but it's negligible.
    assert burst_event.predicted_retry_after_ms is not None
    assert 49000 <= burst_event.predicted_retry_after_ms <= 51000


def test_reservation_released_on_completion():
    # Reserve 2000 max_out; observed_output=200 → release 1800 at completion.
    rec = _accepted_record(0.0, prompt=0, cached=0, max_out=2000, output=200, latency_ms=1000.0, line=0)
    follow = _accepted_record(2.0, prompt=2000, cached=0, max_out=0, output=0, latency_ms=10.0, line=1)
    events = replay_stream([rec, follow], k_leak_tokens_per_ptu_per_second=1e-9, ptu_count=1.0)
    # First event: util goes 0 → 2000.
    assert events[0].util_tokens_after == 2000.0
    # Between t=1 (completion) and t=2, 1800 should be released.
    # By t=2 util ~= 200 (just actual prompt+output input-equivalent = 0+200 used).
    # Then add follow cost=2000 → util ~= 2200.
    assert 2190 < events[1].util_tokens_after < 2210


def test_observed_429_does_not_mutate_state_in_validation_mode():
    accepted = _accepted_record(0.0, prompt=100, cached=0, max_out=0, output=0, latency_ms=100.0, line=0)
    obs_429 = _observed_429_record(1.0, prompt=9999, cached=0, max_out=0, line=1)
    # Trailing accepted that should still see util ~= 100 (not 100+9999).
    trailer = _accepted_record(2.0, prompt=50, cached=0, max_out=0, output=0, latency_ms=10.0, line=2)
    events = replay_stream(
        [accepted, obs_429, trailer],
        k_leak_tokens_per_ptu_per_second=1e-9, ptu_count=1.0,
    )
    assert events[1].kind == "observed_429"
    # Util before trailer should still be ~100 (the 429 didn't mutate it).
    assert events[2].util_tokens_before < 200


def test_task013_adapter_recovers_max_output_from_yaml():
    raw = {
        "wallclock_timestamp_iso": "2026-05-28T13:50:50Z",
        "model": "gpt-5.2",
        "usage": {"input_tokens": 21154, "input_tokens_details": {"cached_tokens": 0},
                  "output_tokens": 93},
        "real_429_observed": False,
        "total_latency_ms": 3000.0,
    }
    n = adapt_task013_record(raw, source_path="x.jsonl", line_number=0, capacity_source="declared_ptu_count")
    assert n.max_output_tokens == 1024
    assert "max_output_tokens_from_yaml" in n.fallback_reasons


def test_task019_adapter_prefers_max_output_tokens_sent():
    raw = {
        "admitted_dispatch_iso": "2026-05-31T03:41:19.725158Z",
        "model": "gpt-5.2",
        "max_output_tokens_sent": 256,
        "cell_max_output_tokens": 1024,
        "usage": {"input_tokens": 1450, "input_tokens_details": {"cached_tokens": 1280},
                  "output_tokens": 57},
        "429_observed": False,
        "cell_idx": 0,
    }
    n = adapt_task019_record(raw, source_path="y.jsonl", line_number=0, capacity_source="payg_quota_proxy")
    assert n.max_output_tokens == 256


def test_task019_adapter_falls_back_to_cell_max_output():
    raw = {
        "admitted_dispatch_iso": "2026-05-31T03:41:19.725158Z",
        "model": "gpt-5.2",
        "cell_max_output_tokens": 1024,
        "usage": {"input_tokens": 1450, "input_tokens_details": {"cached_tokens": 0},
                  "output_tokens": 57},
        "429_observed": False,
        "cell_idx": 0,
    }
    n = adapt_task019_record(raw, source_path="y.jsonl", line_number=0, capacity_source="payg_quota_proxy")
    assert n.max_output_tokens == 1024


def test_task019_first_429_metadata_marks_observed_429():
    raw = {
        "admitted_dispatch_iso": "2026-05-31T03:41:19.725158Z",
        "model": "gpt-5.2",
        "max_output_tokens_sent": 256,
        "cell_max_output_tokens": 256,
        "usage": {"input_tokens": 1450, "input_tokens_details": {"cached_tokens": 0},
                  "output_tokens": 0},
        "429_observed": False,
        "first_429_metadata": {"admitted_dispatch_iso": "2026-05-31T03:41:20.0Z"},
        "cell_idx": 0,
    }
    n = adapt_task019_record(raw, source_path="y.jsonl", line_number=0, capacity_source="payg_quota_proxy")
    assert n.observed_429 is True


def test_zero_usage_429_recovers_demand_from_neighbor():
    accepted = NormalizedReplayRecord(
        source_label=SOURCE_TASK019,
        source_path="y.jsonl",
        source_run_id="run_x",
        timestamp_seconds=1.0,
        model="gpt-5.2",
        prompt_tokens=1450,
        cached_tokens=1280,
        max_output_tokens=256,
        observed_output_tokens=57,
        observed_accepted=True,
        observed_429=False,
        observed_retry_after_ms=None,
        fallback_reasons=(),
        capacity_source="payg_quota_proxy",
        cell_key="cell0",
    )
    zero_429 = NormalizedReplayRecord(
        source_label=SOURCE_TASK019,
        source_path="y.jsonl",
        source_run_id="run_x",
        timestamp_seconds=2.0,
        model="gpt-5.2",
        prompt_tokens=0,
        cached_tokens=0,
        max_output_tokens=256,
        observed_output_tokens=None,
        observed_accepted=False,
        observed_429=True,
        observed_retry_after_ms=1500.0,
        fallback_reasons=("zero_usage_429_needs_recovery",),
        capacity_source="payg_quota_proxy",
        cell_key="cell0",
    )
    out = recover_zero_usage_429_demand([accepted, zero_429])
    assert out[1].prompt_tokens == 1450
    assert out[1].cached_tokens == 1280
    assert "demand_recovered_from_neighbor" in out[1].fallback_reasons


def test_empty_jsonl_returns_empty_list(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    assert load_jsonl(str(p)) == []
    p2 = tmp_path / "blanks.jsonl"
    p2.write_text("\n\n   \n", encoding="utf-8")
    assert load_jsonl(str(p2)) == []


def test_calibration_deterministic_and_runs():
    # Drive util above cap=3400 by stacking accepted records with cost
    # 500 each. The 7th admission (util=3000, cost=500) overshoots → the
    # simulator emits a predicted_429 event. An observed_429 record
    # follows so the pairing rule has a counterpart per stream.
    a_recs = [
        _accepted_record(float(i), source_run_id="A", prompt=500, cached=0,
                         max_out=0, output=0, latency_ms=10.0, line=i)
        for i in range(8)
    ]
    a_recs.append(_observed_429_record(10.0, source_run_id="A", prompt=500,
                                       cached=0, max_out=0, line=99))
    b_recs = [
        _accepted_record(float(i), source_run_id="B", prompt=500, cached=0,
                         max_out=0, output=0, latency_ms=10.0, line=i)
        for i in range(8)
    ]
    b_recs.append(_observed_429_record(9.0, source_run_id="B", prompt=500,
                                       cached=0, max_out=0, line=99))
    streams = [(a_recs, 1.0), (b_recs, 1.0)]
    k1, info1 = calibrate_k(streams)
    k2, info2 = calibrate_k(streams)
    # Determinism across two identical calls.
    assert k1 == k2
    assert info1["fit_status"] == info2["fit_status"]
    assert info1["fit_status"] == "ok"


def test_leave_one_source_run_out_folds_deterministic():
    a = _accepted_record(0.0, source_run_id="A", line=0)
    a_429 = _observed_429_record(1.0, source_run_id="A", line=1)
    b = _accepted_record(0.0, source_run_id="B", line=0)
    b_429 = _observed_429_record(1.0, source_run_id="B", line=1)
    streams = [("B", [b, b_429], 1.0), ("A", [a, a_429], 1.0)]
    folds = leave_one_source_run_out(streams)
    holdouts = [f[0] for f in folds]
    assert holdouts == ["A", "B"]  # sorted alphabetically


def test_replay_rejects_nonpositive_k_or_ptu():
    rec = _accepted_record(0.0, line=0)
    with pytest.raises(ValueError):
        replay_stream([rec], k_leak_tokens_per_ptu_per_second=0.0, ptu_count=1.0)
    with pytest.raises(ValueError):
        replay_stream([rec], k_leak_tokens_per_ptu_per_second=1.0, ptu_count=0.0)
