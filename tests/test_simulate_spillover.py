"""Unit tests for ``scripts/simulate_spillover.py``.

All tests are pure: zero outbound HTTPS, zero Azure credential resolution.
The six test cases enumerated in Task 012 (Phase 1 spillover-simulator spec)
map to ``test_*`` functions below:

  1. Reactive policy triggers spillover on a 4-second first-token event
     and stays for at least ``stay_on_spillover_min_requests``.
  2. Proactive policy windows over latencies and crosses the p95 threshold
     to shift the fraction.
  3. ``--dry-run`` produces JSONL records without network access (socket
     mock asserts zero outbound HTTPS).
  4. System prompt construction is deterministic across two runs with the
     same seed (SHA-256 match) and different across seeds.
  5. Budget halt: synthetic cost ladder triggers the budget-halt code path.
  6. No env var VALUE appears in any log line or JSONL record.
"""

from __future__ import annotations

import json
import logging
import math
import pathlib
import socket
import sys
from typing import Any

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import simulate_spillover as sim  # noqa: E402

FIXTURE_PRICING_DIR = REPO_ROOT / "tests" / "fixtures" / "pricing"
CORPUS_PATH = (
    REPO_ROOT
    / "benchmarks"
    / "04-spillover-simulation"
    / "system_prompt_corpus.json"
)
USER_PROMPTS_PATH = (
    REPO_ROOT / "benchmarks" / "04-spillover-simulation" / "user_prompts.json"
)

TEST_ENDPOINT_VALUE = (
    "https://wrpo-test-endpoint.services.ai.azure.com/api/projects/test-proj"
)
TEST_DEPLOYMENT_GPT_5_2 = "test-gpt-5-2-deployment"
SECRET_ENV_NAME = "SECRET_TEST_ONLY_DEPLOYMENT"
SECRET_ENV_VALUE = "should-not-be-logged-1234abcd"


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_FOUNDRY_ENDPOINT", TEST_ENDPOINT_VALUE)
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_GPT_5_2", TEST_DEPLOYMENT_GPT_5_2)
    monkeypatch.setenv("AZURE_AUTH_MODE", "entra")
    monkeypatch.delenv("MAX_COST_PER_BENCHMARK_USD", raising=False)


def _write_synthetic_yaml(
    tmp_path: pathlib.Path,
    *,
    policy_type: str,
    experiment_id: str | None = None,
    corpus_seed: int = 4242,
    target_tokens: int = 800,
    duration_seconds: int = 60,
    warmup_duration_seconds: int = 6,
    ramp_duration_seconds: int = 18,
    sustain_duration_seconds: int = 18,
    warmup_tps: float = 0.5,
    ramp_start_tps: float = 1.0,
    ramp_end_tps: float = 1.5,
    sustain_tps: float = 1.2,
    hard_ceiling_usd: float = 5.0,
    confirmed: bool = True,
    estimated_cost_usd: float = 0.05,
) -> pathlib.Path:
    """Build a small synthetic experiment YAML pointing at the real corpus.

    The corpus + user prompts are read-only and are the ones committed
    under ``benchmarks/04-spillover-simulation/``. Tests do not synthesize
    their own corpus (that would defeat the determinism check).
    """
    exp_id = experiment_id or f"exptest_{policy_type}"
    cfg = {
        "experiment_id": exp_id,
        "description": "synthetic fixture (Task 012 unit tests; no live calls)",
        "parent_experiment": None,
        "benchmark": "04-spillover-simulation",
        "model": {
            "deployment": "${AZURE_OPENAI_DEPLOYMENT_GPT_5_2}",
            "family": "gpt-5.2",
            "version": "test-5.2",
            "endpoint_env": "AZURE_OPENAI_FOUNDRY_ENDPOINT",
            "auth_mode": "entra",
        },
        "call_params": {"max_output_tokens": 64},
        "effort": "minimal",
        "policy": {
            "type": policy_type,
            "reactive": {
                "first_token_timeout_ms": 3000,
                "stay_on_spillover_min_requests": 10,
                "health_check_interval_ms": 30000,
            },
            "proactive": {
                "latency_window_size": 50,
                "p95_threshold_multiplier": 1.5,
                "spillover_fraction_max": 0.8,
                "measurement_window_seconds": 10,
                "ramp_back_factor": 0.9,
            },
        },
        "simulation": {
            "duration_seconds": duration_seconds,
            "warmup": {
                "duration_seconds": warmup_duration_seconds,
                "tps": warmup_tps,
            },
            "load_pattern": {
                "type": "ramp_then_sustain",
                "ramp_start_tps": ramp_start_tps,
                "ramp_end_tps": ramp_end_tps,
                "ramp_duration_seconds": ramp_duration_seconds,
                "sustain_tps": sustain_tps,
                "sustain_duration_seconds": sustain_duration_seconds,
            },
            "primary": {
                "simulated_throttle_threshold_tpm": 90000,
            },
        },
        "corpus_seed": corpus_seed,
        "target_system_prompt_tokens": target_tokens,
        "system_prompt_corpus_path": str(CORPUS_PATH),
        "user_prompts_path": str(USER_PROMPTS_PATH),
        "budget": {
            "estimated_cost_usd": estimated_cost_usd,
            "hard_ceiling_usd": hard_ceiling_usd,
            "confirmed": confirmed,
        },
        "metadata": {
            "created_at": "2026-05-25",
            "git_commit": None,
            "tenant": "test",
            "consumption_model_context": "ptu",
            "architecture_context": "single_call_react",
        },
        "concurrency": 2,
    }
    exp_dir = tmp_path / "experiments"
    exp_dir.mkdir(exist_ok=True)
    path = exp_dir / f"{exp_id}.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return path


# ----------------------------------------------------------------------------
# Spec test 1 — reactive policy: 4s first-token triggers spillover and stays
# ----------------------------------------------------------------------------


def test_reactive_triggers_spillover_and_stays_min_requests() -> None:
    """A single first-token > 3000ms flips state and traffic stays on
    spillover for at least ``stay_on_spillover_min_requests`` requests.
    """
    params = sim.ReactivePolicyParams(
        first_token_timeout_ms=3000.0,
        stay_on_spillover_min_requests=10,
        health_check_interval_ms=30000.0,
    )
    state = sim.ReactiveState()

    # First 5 requests OK (under threshold) — stay on primary.
    for i in range(5):
        obs = sim.ReactiveObservation(
            request_idx=i,
            first_token_latency_ms=400.0,
            real_429_observed=False,
            monotonic_time_s=float(i),
        )
        decision, state = sim.reactive_decide(obs, state, params)
        assert decision == "primary"
        assert state.on_spillover is False

    # Request #5 has a 4-second first-token event — flips to spillover.
    obs = sim.ReactiveObservation(
        request_idx=5,
        first_token_latency_ms=4000.0,
        real_429_observed=False,
        monotonic_time_s=5.0,
    )
    decision, state = sim.reactive_decide(obs, state, params)
    assert decision == "spillover"
    assert state.on_spillover is True
    assert state.spillover_started_idx == 5
    assert state.requests_on_spillover == 1

    # Next 9 requests are OK but we should remain on spillover (min stay
    # = 10 and the health-check interval has not elapsed).
    for i in range(6, 15):
        obs = sim.ReactiveObservation(
            request_idx=i,
            first_token_latency_ms=400.0,
            real_429_observed=False,
            monotonic_time_s=5.0 + (i - 5) * 0.5,  # well under 30s interval
        )
        decision, state = sim.reactive_decide(obs, state, params)
        assert decision == "spillover", (
            f"request_idx={i}: expected to stay on spillover until "
            f"min_requests met; got {decision}"
        )

    # Now meet both conditions: requests_on_spillover >= 10 AND health
    # interval elapsed. Healthy first-token latency returns to primary.
    obs = sim.ReactiveObservation(
        request_idx=15,
        first_token_latency_ms=400.0,
        real_429_observed=False,
        monotonic_time_s=5.0 + 60.0,  # 60 seconds past divergence
    )
    decision, state = sim.reactive_decide(obs, state, params)
    assert decision == "primary"
    assert state.on_spillover is False


# ----------------------------------------------------------------------------
# Spec test 2 — proactive policy: window crossing p95 threshold ramps fraction
# ----------------------------------------------------------------------------


def test_proactive_ramps_fraction_when_p95_exceeds_threshold() -> None:
    """After warm-up, sustained high latencies pull p95 above
    ``p95_threshold_multiplier`` × baseline, ramping the spillover
    fraction up toward the cap **progressively over multiple windows**
    (not in a single jump). Recovery ramps it back down once p95
    returns to baseline.
    """
    ramp_up_step = 0.2
    params = sim.ProactivePolicyParams(
        latency_window_size=50,
        p95_threshold_multiplier=1.5,
        spillover_fraction_max=0.8,
        measurement_window_seconds=10.0,
        ramp_up_step=ramp_up_step,
        ramp_back_factor=0.9,
    )
    state = sim.ProactiveState()

    # Warm-up: 30 healthy observations at ~400 ms.
    for i in range(30):
        obs = sim.ProactiveObservation(
            request_idx=i,
            first_token_latency_ms=400.0 + i,  # tiny noise
            monotonic_time_s=float(i),
            in_warmup=True,
        )
        fraction, state = sim.proactive_decide(obs, state, params)
        assert fraction == 0.0

    # First non-warmup observation locks the baseline_p95.
    obs = sim.ProactiveObservation(
        request_idx=30,
        first_token_latency_ms=420.0,
        monotonic_time_s=30.0,
        in_warmup=False,
    )
    fraction, state = sim.proactive_decide(obs, state, params)
    assert state.baseline_p95_ms is not None
    assert state.baseline_p95_ms > 0
    baseline = state.baseline_p95_ms
    # Baseline-locking observation must not also bump the fraction.
    assert fraction == 0.0

    # Feed elevated latencies AND advance the monotonic clock past the
    # measurement window so the fraction re-evaluates ONCE per window.
    elevated_latency = baseline * 3.0  # well above 1.5x threshold
    base_t = 30.0
    fractions_per_window: list[float] = []
    # 30 elevated windows is far more than needed to hit the cap at
    # step=0.2 (4 windows). Capture the fraction at each window.
    for j in range(1, 31):
        obs = sim.ProactiveObservation(
            request_idx=30 + j,
            first_token_latency_ms=elevated_latency,
            monotonic_time_s=base_t + j * 11.0,  # 11s > 10s window
            in_warmup=False,
        )
        last_fraction, state = sim.proactive_decide(obs, state, params)
        fractions_per_window.append(last_fraction)

    # Multi-window ramp assertions. The very first elevated observation
    # may or may not breach the p95 threshold depending on where the
    # single high latency lands in the sorted 32-element window
    # (interpolation edge case), so we drop the leading zeros and assert
    # ramp shape on the rising prefix.
    first_breach_idx = next(
        (i for i, f in enumerate(fractions_per_window) if f > 0), None
    )
    assert first_breach_idx is not None, (
        "proactive policy never ramped under sustained elevated load"
    )
    rising = fractions_per_window[first_breach_idx:]

    # 1. The first non-zero fraction equals ramp_up_step exactly
    #    (proves the ramp adds in fixed steps, not a 0 → cap jump).
    assert rising[0] == pytest.approx(ramp_up_step), (
        f"first non-zero fraction expected {ramp_up_step}; "
        f"got {rising[0]} (full series {fractions_per_window!r})"
    )

    # 2. Cap must take strictly more than one window to reach. This is
    #    the core regression guard against 0 → max in one step.
    n_windows_to_cap = math.ceil(
        params.spillover_fraction_max / ramp_up_step
    )
    assert n_windows_to_cap > 1, (
        "ramp_up_step must be < spillover_fraction_max so the ramp is "
        "multi-window — fixture invariant violated"
    )

    # 3. Pre-cap fractions are strictly monotonically increasing across
    #    consecutive windows.
    pre_cap = rising[:n_windows_to_cap]
    assert len(pre_cap) == n_windows_to_cap, (
        f"not enough rising windows captured to verify ramp: {pre_cap!r}"
    )
    for i in range(1, len(pre_cap)):
        assert pre_cap[i] > pre_cap[i - 1], (
            f"pre-cap fractions not strictly increasing at window {i}: "
            f"{pre_cap}"
        )
        # Each step adds ramp_up_step exactly until the cap.
        expected = min(
            params.spillover_fraction_max,
            pre_cap[i - 1] + ramp_up_step,
        )
        assert pre_cap[i] == pytest.approx(expected), (
            f"window {i} stepped by != ramp_up_step: prev={pre_cap[i-1]} "
            f"curr={pre_cap[i]} expected={expected}"
        )

    # 4. The cap is reached on the n-th ramp window and not earlier.
    assert pre_cap[-1] == pytest.approx(params.spillover_fraction_max), (
        f"cap not reached on expected window: pre_cap={pre_cap}"
    )

    # 5. After the cap, fraction stays at the cap (does not overshoot).
    for f in rising[n_windows_to_cap:]:
        assert f == pytest.approx(params.spillover_fraction_max), (
            f"fraction drifted past cap or fell back under sustained load: {f}"
        )

    last_fraction = fractions_per_window[-1]
    assert last_fraction == pytest.approx(params.spillover_fraction_max)

    # Recovery: feed many healthy observations, enough to fully refresh
    # the latency_window_size=50 sliding window past the elevated data.
    # The fraction should ramp back down by ramp_back_factor per window
    # once p95 drops below threshold.
    base_t2 = base_t + 30 * 11.0
    n_recovery = 80  # > window_size=50 so window flushes the spike
    for j in range(1, n_recovery + 1):
        obs = sim.ProactiveObservation(
            request_idx=60 + j,
            first_token_latency_ms=baseline * 0.5,
            monotonic_time_s=base_t2 + j * 11.0,
            in_warmup=False,
        )
        last_fraction, state = sim.proactive_decide(obs, state, params)
    assert last_fraction < 0.5, (
        f"proactive fraction did not ramp back; got {last_fraction}"
    )


def test_proactive_ramp_up_step_validated_in_yaml(
    tmp_path: pathlib.Path,
) -> None:
    """``policy.proactive.ramp_up_step`` must be > 0 and < cap.

    A ramp_up_step equal to or greater than spillover_fraction_max would
    collapse the multi-window ramp into a single 0 → cap jump, which the
    policy contract forbids. Verify the YAML loader rejects that.
    """
    # Build a baseline valid YAML, then mutate ramp_up_step.
    base = _write_synthetic_yaml(tmp_path, policy_type="proactive")
    raw = yaml.safe_load(base.read_text(encoding="utf-8"))
    raw["policy"]["proactive"]["ramp_up_step"] = 0.8  # equals cap
    bad = tmp_path / "experiments" / "bad_ramp_eq_cap.yaml"
    bad.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="ramp_up_step"):
        sim.load_experiment(bad)

    raw["policy"]["proactive"]["ramp_up_step"] = 0.0
    bad2 = tmp_path / "experiments" / "bad_ramp_zero.yaml"
    bad2.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="ramp_up_step"):
        sim.load_experiment(bad2)


# ----------------------------------------------------------------------------
# Spec test 3 — dry-run makes zero outbound HTTPS calls
# ----------------------------------------------------------------------------


@pytest.fixture
def _socket_guard(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Replace socket.socket with a guard that records and refuses connects.

    Returns a list shared with the test so assertions can inspect any
    attempted connection. A real connect attempt would raise — proving
    no outbound HTTPS was made.
    """
    attempted: list[tuple[Any, ...]] = []

    class _GuardedSocket(socket.socket):
        def connect(self, address: Any) -> None:  # type: ignore[override]
            attempted.append(("connect", address))
            raise AssertionError(
                f"dry-run made a socket.connect to {address!r}; this is forbidden"
            )

        def connect_ex(self, address: Any) -> int:  # type: ignore[override]
            attempted.append(("connect_ex", address))
            raise AssertionError(
                f"dry-run made a socket.connect_ex to {address!r}; forbidden"
            )

    monkeypatch.setattr(socket, "socket", _GuardedSocket)
    return attempted


def test_dry_run_zero_https(
    tmp_path: pathlib.Path, _socket_guard: list[tuple[Any, ...]]
) -> None:
    """``--dry-run`` produces JSONL + summary without any socket.connect."""
    exp_path = _write_synthetic_yaml(
        tmp_path,
        policy_type="reactive",
        duration_seconds=20,
        warmup_duration_seconds=4,
        ramp_duration_seconds=8,
        sustain_duration_seconds=8,
        warmup_tps=0.5,
        ramp_start_tps=0.5,
        ramp_end_tps=1.0,
        sustain_tps=0.6,
    )
    benchmarks_root = tmp_path / "benchmarks"
    bench_dir = benchmarks_root / "04-spillover-simulation"
    (bench_dir / "runs").mkdir(parents=True)

    cfg = sim.load_experiment(exp_path)

    import os
    cwd_before = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = sim.run_simulation(
            cfg=cfg,
            benchmarks_root=benchmarks_root,
            pricing_dir=FIXTURE_PRICING_DIR,
            dry_run=True,
            smoke=False,
            allow_dirty=True,
        )
    finally:
        os.chdir(cwd_before)

    assert _socket_guard == [], (
        f"dry-run attempted a socket connect: {_socket_guard}"
    )
    assert result.jsonl_path.is_file()
    assert result.summary_path.is_file()
    assert result.cells_written > 0
    # All records have dry_run=true and zero-usage.
    with result.jsonl_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            assert rec["dry_run"] is True
            assert rec["usage"]["input_tokens"] == 0
            assert rec["api_version"] == "preview"
            assert rec["model"] == "gpt-5.2"
            assert rec["endpoint_hit"] in ("primary", "spillover")


# ----------------------------------------------------------------------------
# Spec test 4 — system prompt construction is deterministic per-seed
# ----------------------------------------------------------------------------


def test_system_prompt_deterministic_per_seed(tmp_path: pathlib.Path) -> None:
    """Same corpus + same seed → byte-identical system prompt; differ by seed."""
    p1 = sim.build_system_prompt(CORPUS_PATH, corpus_seed=4242, target_tokens=2000)
    p2 = sim.build_system_prompt(CORPUS_PATH, corpus_seed=4242, target_tokens=2000)
    p3 = sim.build_system_prompt(CORPUS_PATH, corpus_seed=9999, target_tokens=2000)

    assert sim.sha256_text(p1) == sim.sha256_text(p2)
    assert sim.sha256_text(p1) != sim.sha256_text(p3)
    # Sanity: at the chars/4 heuristic, the assembled prompt should be at
    # least the target_tokens × 4 characters minus the last appended snippet.
    assert len(p1) >= 2000 * 4 - 600


# ----------------------------------------------------------------------------
# Spec test 5 — budget halt triggers BudgetHaltError → exit code 1
# ----------------------------------------------------------------------------


def test_budget_halt_raises_and_exit_code_1(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A synthetic cost ladder above the hard ceiling halts mid-run."""
    exp_path = _write_synthetic_yaml(
        tmp_path,
        policy_type="reactive",
        duration_seconds=30,
        warmup_duration_seconds=4,
        ramp_duration_seconds=10,
        sustain_duration_seconds=10,
        warmup_tps=0.5,
        ramp_start_tps=1.0,
        ramp_end_tps=2.0,
        sustain_tps=1.5,
        hard_ceiling_usd=0.0001,
        estimated_cost_usd=0.0001,
    )
    benchmarks_root = tmp_path / "benchmarks"
    bench_dir = benchmarks_root / "04-spillover-simulation"
    (bench_dir / "runs").mkdir(parents=True)

    # Patch ``payg_cost_per_call`` so the synthetic ladder reports
    # non-zero spend even though we're in dry-run mode. We patch the
    # *as imported by the simulator module* binding.
    class _FakeBreakdown:
        usd_per_request = 1.0  # one cell already exceeds 0.0001 ceiling

    def _fake_payg(*args: Any, **kwargs: Any) -> Any:
        return _FakeBreakdown()

    monkeypatch.setattr(sim, "payg_cost_per_call", _fake_payg)

    cfg = sim.load_experiment(exp_path)

    import os
    cwd_before = os.getcwd()
    os.chdir(tmp_path)
    try:
        with pytest.raises(sim.BudgetHaltError):
            # dry_run=False with stubbed live call. We patch the live
            # call to return zero-usage immediately (no network).
            async def _stub_call(**kwargs: Any) -> Any:
                return (
                    {
                        "input_tokens": 100,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens": 50,
                        "output_tokens_details": {"reasoning_tokens": 5},
                        "total_tokens": 150,
                    },
                    50.0,
                    50.0,
                    0,
                    False,
                    None,
                    None,
                )

            monkeypatch.setattr(sim, "_live_call", _stub_call)
            # Stub out client construction so we don't import azure-identity.
            monkeypatch.setattr(
                sim,
                "_build_live_client",
                lambda *, endpoint_value: object(),
            )
            sim.run_simulation(
                cfg=cfg,
                benchmarks_root=benchmarks_root,
                pricing_dir=FIXTURE_PRICING_DIR,
                dry_run=False,
                smoke=False,
                allow_dirty=True,
            )
    finally:
        os.chdir(cwd_before)

    # Now exercise the CLI's exit code mapping with the same setup, but
    # rebuild a fresh experiment YAML so the JSONL target collision check
    # does not abort the second run.
    exp_path2 = _write_synthetic_yaml(
        tmp_path,
        policy_type="reactive",
        experiment_id="exptest_reactive_v2",
        duration_seconds=30,
        warmup_duration_seconds=4,
        ramp_duration_seconds=10,
        sustain_duration_seconds=10,
        warmup_tps=0.5,
        ramp_start_tps=1.0,
        ramp_end_tps=2.0,
        sustain_tps=1.5,
        hard_ceiling_usd=0.0001,
        estimated_cost_usd=0.0001,
    )

    async def _stub_call_2(**kwargs: Any) -> Any:
        return (
            {
                "input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 50,
                "output_tokens_details": {"reasoning_tokens": 5},
                "total_tokens": 150,
            },
            50.0,
            50.0,
            0,
            False,
            None,
            None,
        )

    monkeypatch.setattr(sim, "_live_call", _stub_call_2)
    monkeypatch.setattr(
        sim, "_build_live_client", lambda *, endpoint_value: object()
    )
    os.chdir(tmp_path)
    try:
        code = sim.main(
            [
                "--experiment",
                str(exp_path2),
                "--allow-dirty",
                "--benchmarks-root",
                str(benchmarks_root),
                "--pricing-dir",
                str(FIXTURE_PRICING_DIR),
            ]
        )
    finally:
        os.chdir(cwd_before)
    assert code == sim.EXIT_BUDGET == 1


# ----------------------------------------------------------------------------
# Spec test 6 — no env var VALUE in logs or JSONL records
# ----------------------------------------------------------------------------


def test_no_env_var_value_leaks(
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deployment env var VALUE never appears in any log line nor in
    JSONL records (other than ``deployment_name`` which is the resolved
    value, by design — that field is the public audit-trail name)."""
    monkeypatch.setenv(SECRET_ENV_NAME, SECRET_ENV_VALUE)
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_GPT_5_2", SECRET_ENV_VALUE)
    exp_path = _write_synthetic_yaml(
        tmp_path,
        policy_type="reactive",
        duration_seconds=10,
        warmup_duration_seconds=2,
        ramp_duration_seconds=4,
        sustain_duration_seconds=4,
        warmup_tps=0.5,
        ramp_start_tps=0.5,
        ramp_end_tps=1.0,
        sustain_tps=0.5,
    )
    benchmarks_root = tmp_path / "benchmarks"
    bench_dir = benchmarks_root / "04-spillover-simulation"
    (bench_dir / "runs").mkdir(parents=True)

    cfg = sim.load_experiment(exp_path)

    import os
    cwd_before = os.getcwd()
    os.chdir(tmp_path)
    caplog.set_level(logging.DEBUG)
    try:
        result = sim.run_simulation(
            cfg=cfg,
            benchmarks_root=benchmarks_root,
            pricing_dir=FIXTURE_PRICING_DIR,
            dry_run=True,
            smoke=False,
            allow_dirty=True,
        )
    finally:
        os.chdir(cwd_before)

    # No log line contains the secret value.
    for rec in caplog.records:
        msg = rec.getMessage()
        assert SECRET_ENV_VALUE not in msg, (
            f"env var VALUE leaked into log: {msg!r}"
        )
        # SECRET_ENV_NAME (the NAME) is allowed in log messages — only
        # the VALUE is forbidden. The runner does name lookups, never
        # value-string formatting.

    # No JSONL record contains the secret value as a raw substring
    # OTHER than the legitimate ``deployment_name`` field. Tear the
    # record apart and check every other key.
    with result.jsonl_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            for k, v in rec.items():
                if k == "deployment_name":
                    continue
                serialized = json.dumps(v, sort_keys=True)
                assert SECRET_ENV_VALUE not in serialized, (
                    f"env var VALUE leaked into JSONL field {k!r}: "
                    f"{serialized!r}"
                )


# ----------------------------------------------------------------------------
# Spec extension — comparison chart emission requires sibling-policy JSONL
# ----------------------------------------------------------------------------


def test_policy_comparison_chart_skipped_without_sibling(
    tmp_path: pathlib.Path,
) -> None:
    """Single-policy run (no sibling JSONL) emits the per-policy chart
    but skips the comparison chart and reports None on the result.
    """
    exp_path = _write_synthetic_yaml(
        tmp_path,
        policy_type="reactive",
        duration_seconds=20,
        warmup_duration_seconds=4,
        ramp_duration_seconds=8,
        sustain_duration_seconds=8,
        warmup_tps=0.5,
        ramp_start_tps=0.5,
        ramp_end_tps=1.0,
        sustain_tps=0.6,
    )
    benchmarks_root = tmp_path / "benchmarks"
    bench_dir = benchmarks_root / "04-spillover-simulation"
    (bench_dir / "runs").mkdir(parents=True)

    cfg = sim.load_experiment(exp_path)

    import os
    cwd_before = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = sim.run_simulation(
            cfg=cfg,
            benchmarks_root=benchmarks_root,
            pricing_dir=FIXTURE_PRICING_DIR,
            dry_run=True,
            smoke=False,
            allow_dirty=True,
        )
    finally:
        os.chdir(cwd_before)

    assert result.chart_path is not None
    assert result.chart_path.is_file()
    assert result.comparison_chart_path is None
    assert result.comparison_chart_csv_path is None
    # No comparison artifact on disk yet.
    chart_dir = tmp_path / "results" / "spillover-recovery-curves"
    assert not (chart_dir / "policy_comparison.png").exists()


def test_policy_comparison_chart_emitted_for_dry_run_pair(
    tmp_path: pathlib.Path,
) -> None:
    """Running reactive then proactive in the same benchmark runs_dir
    (both --dry-run) produces ``policy_comparison.png`` + sibling CSV
    after the second run completes.
    """
    benchmarks_root = tmp_path / "benchmarks"
    bench_dir = benchmarks_root / "04-spillover-simulation"
    (bench_dir / "runs").mkdir(parents=True)

    exp_reactive = _write_synthetic_yaml(
        tmp_path,
        policy_type="reactive",
        experiment_id="exptest_pair_reactive",
        duration_seconds=20,
        warmup_duration_seconds=4,
        ramp_duration_seconds=8,
        sustain_duration_seconds=8,
        warmup_tps=0.5,
        ramp_start_tps=0.5,
        ramp_end_tps=1.0,
        sustain_tps=0.6,
    )
    exp_proactive = _write_synthetic_yaml(
        tmp_path,
        policy_type="proactive",
        experiment_id="exptest_pair_proactive",
        duration_seconds=20,
        warmup_duration_seconds=4,
        ramp_duration_seconds=8,
        sustain_duration_seconds=8,
        warmup_tps=0.5,
        ramp_start_tps=0.5,
        ramp_end_tps=1.0,
        sustain_tps=0.6,
    )

    import os
    cwd_before = os.getcwd()
    os.chdir(tmp_path)
    try:
        cfg1 = sim.load_experiment(exp_reactive)
        result1 = sim.run_simulation(
            cfg=cfg1,
            benchmarks_root=benchmarks_root,
            pricing_dir=FIXTURE_PRICING_DIR,
            dry_run=True,
            smoke=False,
            allow_dirty=True,
        )
        # First run sees no sibling — comparison still None.
        assert result1.comparison_chart_path is None

        cfg2 = sim.load_experiment(exp_proactive)
        result2 = sim.run_simulation(
            cfg=cfg2,
            benchmarks_root=benchmarks_root,
            pricing_dir=FIXTURE_PRICING_DIR,
            dry_run=True,
            smoke=False,
            allow_dirty=True,
        )
    finally:
        os.chdir(cwd_before)

    # Second run finds the reactive sibling and emits the comparison.
    assert result2.comparison_chart_path is not None
    assert result2.comparison_chart_csv_path is not None
    assert result2.comparison_chart_path.is_file()
    assert result2.comparison_chart_csv_path.is_file()
    # Chart and sibling CSV live under results/spillover-recovery-curves
    # rooted at the cwd at run time (tmp_path).
    expected = tmp_path / "results" / "spillover-recovery-curves" / "policy_comparison.png"
    assert result2.comparison_chart_path.resolve() == expected.resolve()
    # CSV header sanity.
    csv_text = result2.comparison_chart_csv_path.read_text(encoding="utf-8")
    assert csv_text.startswith("series,t_seconds,value")
    assert "reactive" in csv_text and "proactive" in csv_text


# ----------------------------------------------------------------------------
# Long-run auth hardening — refreshable Entra ID token provider wiring
# ----------------------------------------------------------------------------
#
# The previous implementation of ``_build_live_client`` called
# ``token_provider()`` at client construction time and embedded the
# resulting static token string in ``api_key``. Entra ID bearer tokens
# have a ~60 minute TTL, so any long run (e.g. the 22 minute load shape
# repeated across reactive + proactive policies with retries) could 401
# silently mid-stream. The fix uses ``azure.identity.aio`` and passes
# the async callable itself into ``AsyncOpenAI(api_key=...)``; the
# OpenAI SDK awaits the callable before every Responses API call.
#
# These tests prove the wiring is correct **offline** — they never touch
# real Entra ID and they never open a socket.


def _install_fake_aio_identity(
    monkeypatch: pytest.MonkeyPatch,
    *,
    token_values: list[str] | None = None,
) -> dict[str, Any]:
    """Inject fake ``azure.identity.aio`` symbols into the import path.

    Captures ``DefaultAzureCredential`` construction and the audience
    scope passed to ``get_bearer_token_provider`` so tests can assert
    them. ``token_values`` (defaults to a single value) controls what
    successive awaits of the provider return — proving the provider is
    invoked per-request, not per-client.
    """
    import types

    fake_aio = types.ModuleType("azure.identity.aio")
    state: dict[str, Any] = {
        "credential_instances": 0,
        "scopes_passed": [],
        "provider_calls": 0,
    }
    values = list(token_values or ["fake-bearer-AAAA"])

    class _FakeAioCredential:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            state["credential_instances"] += 1
            state["credential_args"] = (args, kwargs)

        async def close(self) -> None:
            pass

    def _fake_get_bearer_token_provider(credential: Any, *scopes: str) -> Any:
        state["scopes_passed"].append(tuple(scopes))
        state["credential_passed"] = credential

        async def _provider() -> str:
            i = state["provider_calls"]
            state["provider_calls"] += 1
            return values[i] if i < len(values) else values[-1]

        return _provider

    fake_aio.DefaultAzureCredential = _FakeAioCredential
    fake_aio.get_bearer_token_provider = _fake_get_bearer_token_provider
    monkeypatch.setitem(sys.modules, "azure.identity.aio", fake_aio)
    return state


def test_build_live_client_uses_refreshable_token_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_build_live_client`` must pass a *callable* (refreshable) provider
    to ``AsyncOpenAI``, never a pre-resolved static token string.

    Verified by inspecting the resulting client:
      * ``client.api_key`` is empty (no static token embedded).
      * ``client._api_key_provider`` is set and is callable.
      * The provider is an async callable (``Callable[[], Awaitable[str]]``)
        — this is the contract ``AsyncOpenAI`` awaits per request to
        refresh the Bearer header.
    """
    state = _install_fake_aio_identity(monkeypatch)

    client = sim._build_live_client(endpoint_value=TEST_ENDPOINT_VALUE)

    # api_key attribute is empty: no static one-shot token embedded.
    assert client.api_key == "", (
        f"static token leaked into api_key (long-run 401 risk): "
        f"{client.api_key!r}"
    )
    # Callable provider attached for per-request refresh.
    assert client._api_key_provider is not None, (
        "AsyncOpenAI client has no _api_key_provider — long runs will 401"
    )
    assert callable(client._api_key_provider)
    import inspect as _inspect
    assert _inspect.iscoroutinefunction(client._api_key_provider), (
        "token provider must be async (Callable[[], Awaitable[str]]); "
        "synchronous callables are accepted by the sync OpenAI client only"
    )
    # Audience scope is the Foundry v1 audience, not the classic
    # cognitiveservices.azure.com scope (which produces 401 against
    # Foundry v1).
    assert state["scopes_passed"], "get_bearer_token_provider was not called"
    assert state["scopes_passed"][0] == ("https://ai.azure.com/.default",), (
        f"wrong audience scope: {state['scopes_passed'][0]!r}"
    )
    # Credential was constructed via DefaultAzureCredential (not an
    # api-key path).
    assert state["credential_instances"] == 1
    # base_url preserved (Foundry v1 surface).
    assert str(client.base_url).startswith(TEST_ENDPOINT_VALUE)
    assert str(client.base_url).endswith("/openai/v1/")


def test_build_live_client_provider_refreshes_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK's ``_refresh_api_key`` hook must produce a *new* token
    on each invocation when the provider is wired correctly.

    This is the core long-run safety property: even if the first token
    expires, the next request triggers a fresh token via the provider.
    """
    state = _install_fake_aio_identity(
        monkeypatch,
        token_values=["fake-bearer-AAAA", "fake-bearer-BBBB", "fake-bearer-CCCC"],
    )

    client = sim._build_live_client(endpoint_value=TEST_ENDPOINT_VALUE)
    # First refresh — picks up the first token from the provider.
    import asyncio as _asyncio

    async def _refresh_three_times() -> tuple[str, str, str]:
        a = await client._refresh_api_key()
        b = await client._refresh_api_key()
        c = await client._refresh_api_key()
        return a, b, c

    t1, t2, t3 = _asyncio.run(_refresh_three_times())
    assert (t1, t2, t3) == (
        "fake-bearer-AAAA",
        "fake-bearer-BBBB",
        "fake-bearer-CCCC",
    ), (
        "_refresh_api_key did not draw fresh values from the provider — "
        "long runs would keep using a stale, eventually-expired token"
    )
    assert state["provider_calls"] == 3
    # And the most recently refreshed token is the one the client will
    # send on its next request.
    assert client.api_key == "fake-bearer-CCCC"


def test_build_live_client_does_not_embed_static_token_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense-in-depth: even if a future refactor accidentally calls
    the provider eagerly, this test catches the regression.

    We watch the fake provider's invocation count: ``_build_live_client``
    must *not* call the provider during construction (only the SDK does,
    per-request).
    """
    state = _install_fake_aio_identity(monkeypatch)
    client = sim._build_live_client(endpoint_value=TEST_ENDPOINT_VALUE)
    assert state["provider_calls"] == 0, (
        "_build_live_client invoked the token provider at construction "
        "time — this re-introduces the static one-shot token bug "
        "(static token would expire mid-run)"
    )
    # And api_key remains empty until the SDK awaits the provider.
    assert client.api_key == ""


def test_build_live_client_no_outbound_https(
    monkeypatch: pytest.MonkeyPatch,
    _socket_guard: list[tuple[Any, ...]],
) -> None:
    """Constructing the live client must not open any socket. All real
    HTTPS happens only when the SDK actually issues a Responses call.
    """
    _install_fake_aio_identity(monkeypatch)
    _ = sim._build_live_client(endpoint_value=TEST_ENDPOINT_VALUE)
    assert _socket_guard == [], (
        f"_build_live_client attempted a socket connect: {_socket_guard}"
    )


def test_build_live_client_uses_aio_identity_not_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fix specifically uses ``azure.identity.aio`` (async surface)
    because ``AsyncOpenAI``'s per-request refresh hook awaits the
    provider. Using the sync ``azure.identity`` surface would force
    ``api_key=token_provider()`` (the old static-token bug) or require a
    custom async wrapper. This test pins the import path.
    """
    import importlib

    src = importlib.import_module("scripts.simulate_spillover")
    import inspect as _inspect

    body = _inspect.getsource(src._build_live_client)
    assert "azure.identity.aio" in body, (
        "Auth fix must use azure.identity.aio (async surface) so the "
        "OpenAI SDK can await the token provider before each request. "
        "Sync azure.identity will silently re-introduce the static "
        "one-shot token bug."
    )
    # And the callable form (api_key=token_provider) is used, not the
    # eager-resolution form (api_key=token_provider()).
    assert "api_key=token_provider)" in body
    assert "api_key=token_provider()" not in body, (
        "Eager-resolution form re-introduces the static one-shot token "
        "bug. Pass the callable itself: AsyncOpenAI(api_key=token_provider)."
    )
