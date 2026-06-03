"""Unit tests for ``scripts/measure_dual_spillover.py`` (Task 013, Phase 2).

All tests are pure: zero outbound HTTPS, zero Azure credential resolution.
The five test cases enumerated in Task 013 §Tests are covered by the
``test_*`` functions below:

  1. ``test_reactive_real_429_from_primary_triggers_spillover``: A real
     429 from the primary deployment routes the failing request and the
     next ``stay_on_spillover_min_requests`` to the spillover deployment.
     The 429 is preserved as its own JSONL record (never silently
     retried).
  2. ``test_spillover_429_retries_once_then_halts_at_one_percent``:
     Spillover-side real 429s are retried once with backoff; the
     aggregate spillover 429 rate exceeding 1% halts the run with
     ``SpilloverHalt429Error`` (CLI exit code 1).
  3. ``test_two_clients_constructed_one_per_deployment``: The runner
     constructs two distinct clients; the ``model`` field of each
     outbound request matches the deployment that owns its client.
  4. ``test_cache_pool_field_equals_deployment_used_for_every_record``:
     Every per-request JSONL record carries
     ``cache_pool == deployment_used`` (the Phase 2 cache-pool
     separation invariant).
  5. ``test_phase1_simulated_field_absent_from_phase2_records``: The
     Phase 1 schema field ``simulated_primary_throttle_state`` does NOT
     appear in any Phase 2 record (the schema-migration assertion).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import pathlib
import sys
from typing import Any

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import measure_dual_spillover as mdual  # noqa: E402

FIXTURE_PRICING_DIR = REPO_ROOT / "tests" / "fixtures" / "pricing"
CORPUS_PATH = (
    REPO_ROOT
    / "benchmarks"
    / "05-dual-spillover"
    / "system_prompt_corpus.json"
)
USER_PROMPTS_PATH = (
    REPO_ROOT / "benchmarks" / "05-dual-spillover" / "user_prompts.json"
)

TEST_ENDPOINT_VALUE = (
    "https://wrpo-test-endpoint.services.ai.azure.com/api/projects/test-proj"
)
PRIMARY_DEPLOYMENT_NAME = "test-primary-throttled"
SPILLOVER_DEPLOYMENT_NAME = "test-spillover"


# ----------------------------------------------------------------------------
# Test-only fakes
# ----------------------------------------------------------------------------


class _FakeUsage:
    """Mimics an OpenAI Responses-API usage object's model_dump()."""

    def __init__(
        self,
        *,
        input_tokens: int = 1000,
        cached_tokens: int = 0,
        output_tokens: int = 50,
        reasoning_tokens: int = 5,
    ) -> None:
        self._payload = {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": cached_tokens},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
            "total_tokens": input_tokens + output_tokens,
        }

    @property
    def output_tokens(self) -> int:
        return int(self._payload["output_tokens"])

    def model_dump(self) -> dict[str, Any]:
        return dict(self._payload)


class _FakeResponse:
    def __init__(
        self, *, usage: _FakeUsage | None = None, headers: dict[str, str] | None = None
    ) -> None:
        self.usage = usage if usage is not None else _FakeUsage()
        self.headers = dict(headers or {})


class _FakeAPIResponse:
    """Mimics the OpenAI SDK's ``AsyncAPIResponse`` (raw-response API).

    ``client.responses.with_raw_response.create(**kwargs)`` on the
    async client returns an ``AsyncAPIResponse`` with two key
    attributes:

    * ``headers`` — httpx-style headers mapping (``.get(name)``).
    * ``parse()`` — **async** in SDK ≥ 2.37; returns the typed
      response (same shape as the default ``responses.create()``).

    The fake mirrors that contract — ``parse()`` is a coroutine — so
    production code that forgets to ``await`` the parse call is caught
    by tests instead of silently returning a coroutine object.
    Errors are still raised by ``create`` directly (matching the SDK).
    """

    def __init__(
        self, *, parsed: _FakeResponse, headers: dict[str, str] | None = None
    ) -> None:
        self._parsed = parsed
        self.headers = dict(headers or {})

    async def parse(self) -> _FakeResponse:
        return self._parsed


class _FakeHttpx429Error(Exception):
    """Mimic the OpenAI SDK's 429 exception shape (status_code + response)."""

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        super().__init__("429 Too Many Requests")
        self.status_code = 429

        class _Resp:
            def __init__(self, hdrs: dict[str, str]) -> None:
                self.headers = dict(hdrs)

        self.response = _Resp(headers or {})


class _FakeClient:
    """Records every outbound `responses.create` call and replays scripted results.

    Scripts are lists of callables ``(kwargs) -> _FakeResponse | Exception``.
    The fake unpacks the next entry per call. If the script ends, raises.

    The fake exposes BOTH the legacy ``responses.create()`` shape AND the
    raw-response shape ``responses.with_raw_response.create()`` (which
    returns an ``_FakeAPIResponse`` whose ``headers`` mirror the scripted
    ``_FakeResponse.headers``). Production code uses the raw-response
    shape so success-path Azure headers (``x-ms-deployment-name``,
    ``x-ms-spillover-*``) are reliably captured; the legacy shape is
    preserved for any test that still wants to call ``create`` directly.
    """

    def __init__(self, deployment: str, script: list[Any]) -> None:
        self.deployment = deployment
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

        outer = self

        class _RespNamespace:
            async def create(_inner, **kwargs: Any) -> _FakeResponse:
                return outer._next(kwargs)

        class _RawNamespace:
            async def create(_inner, **kwargs: Any) -> _FakeAPIResponse:
                result = outer._next(kwargs)
                # ``_next`` either returns _FakeResponse or raises. The
                # exception path goes through `raise` inside `_next`, so
                # by here we have a parsed response. Wrap it.
                return _FakeAPIResponse(
                    parsed=result, headers=getattr(result, "headers", None)
                )

        ns = _RespNamespace()
        ns.with_raw_response = _RawNamespace()  # type: ignore[attr-defined]
        self.responses = ns

    def _next(self, kwargs: dict[str, Any]) -> _FakeResponse:
        """Apply the next scripted handler. Internal helper for both shapes."""
        self.calls.append(dict(kwargs))
        if not self.script:
            raise AssertionError(
                f"_FakeClient {self.deployment!r}: script exhausted; "
                f"call#{len(self.calls)} kwargs={kwargs!r}"
            )
        handler = self.script.pop(0)
        result = handler(kwargs)
        if isinstance(result, Exception):
            raise result
        return result


# ----------------------------------------------------------------------------
# YAML fixture builder
# ----------------------------------------------------------------------------


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
    stay_on_spillover_min_requests: int = 10,
) -> pathlib.Path:
    """Build a small synthetic Phase 2 YAML pointing at the real corpus."""
    exp_id = experiment_id or f"exptest_dual_{policy_type}"
    cfg = {
        "experiment_id": exp_id,
        "description": "synthetic fixture (Task 013 unit tests; no live calls)",
        "parent_experiment": None,
        "benchmark": "05-dual-spillover",
        "primary": {
            "deployment": "${AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED}",
            "deployment_name": "test-primary-throttled-name",
            "family": "gpt-5.2",
            "version": "test-5.2",
            "endpoint_env": "AZURE_OPENAI_FOUNDRY_ENDPOINT",
            "auth_mode": "entra",
            "tpm": 60000,
            "rpm": 600,
        },
        "spillover": {
            "deployment": "${AZURE_OPENAI_DEPLOYMENT_GPT_5_2}",
            "deployment_name": "test-spillover-name",
            "family": "gpt-5.2",
            "version": "test-5.2",
            "endpoint_env": "AZURE_OPENAI_FOUNDRY_ENDPOINT",
            "auth_mode": "entra",
            "tpm": 500000,
            "rpm": 5000,
        },
        "call_params": {"max_output_tokens": 64},
        "effort": "minimal",
        "policy": {
            "type": policy_type,
            "reactive_params": {
                "first_token_timeout_ms": 3000,
                "stay_on_spillover_min_requests": stay_on_spillover_min_requests,
                "health_check_interval_ms": 30000,
                "treat_real_429_as_trigger": True,
            },
            "proactive_params": {
                "latency_window_size": 50,
                "p95_threshold_multiplier": 1.5,
                "spillover_fraction_max": 0.8,
                "measurement_window_seconds": 10,
                "ramp_up_step": 0.2,
                "ramp_back_factor": 0.9,
                "real_429_observed_action": "route_to_spillover",
                "real_429_followup_requests": 5,
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
            "hypothesis_under_test": "G_weak",
            "phase": 2,
        },
        "concurrency": 2,
    }
    exp_dir = tmp_path / "experiments"
    exp_dir.mkdir(exist_ok=True)
    path = exp_dir / f"{exp_id}.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_FOUNDRY_ENDPOINT", TEST_ENDPOINT_VALUE)
    monkeypatch.setenv(
        "AZURE_OPENAI_DEPLOYMENT_GPT_5_2", SPILLOVER_DEPLOYMENT_NAME
    )
    monkeypatch.setenv(
        "AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED", PRIMARY_DEPLOYMENT_NAME
    )
    monkeypatch.setenv("AZURE_AUTH_MODE", "entra")
    monkeypatch.delenv("MAX_COST_PER_BENCHMARK_USD", raising=False)


# ----------------------------------------------------------------------------
# Helper: run the measurement core with patched clients
# ----------------------------------------------------------------------------


def _install_fake_clients(
    monkeypatch: pytest.MonkeyPatch,
    *,
    primary_script: list[Any],
    spillover_script: list[Any],
) -> tuple[list[_FakeClient], dict[str, _FakeClient]]:
    """Patch ``_build_live_client`` + preflight to install scripted fakes.

    Two clients are constructed in order: primary first, then spillover.
    Their scripts are pre-populated with a 1-call preflight handler so
    the real preflight_reachability passes.
    """
    constructed: list[_FakeClient] = []
    by_role: dict[str, _FakeClient] = {}

    def _preflight_ok(kwargs: dict[str, Any]) -> _FakeResponse:
        return _FakeResponse(usage=_FakeUsage(output_tokens=4))

    primary_full_script: list[Any] = [_preflight_ok] + list(primary_script)
    spillover_full_script: list[Any] = [_preflight_ok] + list(spillover_script)

    def _factory(*, endpoint_value: str) -> Any:
        if not constructed:
            client = _FakeClient(PRIMARY_DEPLOYMENT_NAME, primary_full_script)
            by_role["primary"] = client
        else:
            client = _FakeClient(SPILLOVER_DEPLOYMENT_NAME, spillover_full_script)
            by_role["spillover"] = client
        constructed.append(client)
        return client

    monkeypatch.setattr(mdual, "_build_live_client", _factory)
    return constructed, by_role


def _read_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ----------------------------------------------------------------------------
# Test 1 — reactive real 429 from primary triggers spillover
# ----------------------------------------------------------------------------


def test_reactive_real_429_from_primary_triggers_spillover(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real 429 from the primary deployment routes the failing request
    AND the next ``stay_on_spillover_min_requests`` to spillover.

    The 429 itself is recorded as a JSONL row (NOT silently retried).
    """
    exp_path = _write_synthetic_yaml(
        tmp_path,
        policy_type="reactive",
        duration_seconds=20,
        warmup_duration_seconds=2,
        ramp_duration_seconds=8,
        sustain_duration_seconds=8,
        warmup_tps=0.5,
        ramp_start_tps=0.5,
        ramp_end_tps=1.0,
        sustain_tps=1.0,
        stay_on_spillover_min_requests=5,
    )
    benchmarks_root = tmp_path / "benchmarks"
    bench_dir = benchmarks_root / "05-dual-spillover"
    (bench_dir / "runs").mkdir(parents=True)

    # Primary script: 3 healthy responses, then a 429, then more healthy.
    # The 429 should never be reached as a "primary call" again until
    # min_requests on spillover have elapsed.
    primary_script: list[Any] = [
        lambda kw: _FakeResponse(usage=_FakeUsage()),
        lambda kw: _FakeResponse(usage=_FakeUsage()),
        lambda kw: _FakeResponse(usage=_FakeUsage()),
        lambda kw: _FakeHttpx429Error(
            headers={"retry-after-ms": "2500", "retry-after": "3"}
        ),
        # Continue offering responses if accidentally re-called.
        *[lambda kw: _FakeResponse(usage=_FakeUsage()) for _ in range(50)],
    ]
    # Spillover script: all healthy.
    spillover_script: list[Any] = [
        lambda kw: _FakeResponse(usage=_FakeUsage()) for _ in range(50)
    ]
    constructed, by_role = _install_fake_clients(
        monkeypatch,
        primary_script=primary_script,
        spillover_script=spillover_script,
    )

    cfg = mdual.load_experiment(exp_path)

    import os
    cwd_before = os.getcwd()
    os.chdir(tmp_path)
    try:
        mdual.run_measurement(
            cfg=cfg,
            benchmarks_root=benchmarks_root,
            pricing_dir=FIXTURE_PRICING_DIR,
            dry_run=False,
            smoke=False,
            allow_dirty=True,
        )
    finally:
        os.chdir(cwd_before)

    # Locate the JSONL.
    jsonl_files = sorted((bench_dir / "runs").glob("*.jsonl"))
    assert len(jsonl_files) == 1
    records = _read_jsonl(jsonl_files[0])

    # Find the 429 record. It must exist as its own row (sub_request_role
    # = primary_429) — the 429 is NOT silently retried away.
    p429_rows = [
        r
        for r in records
        if r.get("real_429_observed") is True
        and r.get("endpoint_hit") == "primary"
    ]
    assert len(p429_rows) >= 1, (
        "expected at least one primary-side real_429_observed=true row; "
        "real 429 was silently retried away"
    )
    p429_row = p429_rows[0]
    assert p429_row["sub_request_role"] == "primary_429"
    # The 429 row carries the parsed retry-after-ms (2500.0).
    assert p429_row["retry_after_ms"] == 2500.0
    assert p429_row["retry_after_seconds"] == 3.0
    # primary_429_count_running_total is monotonically incremented.
    assert p429_row["primary_429_count_running_total"] >= 1

    # The follow-up reactive routing: the 5 immediately-following
    # primary_request rows after the 429 must all be on spillover.
    p429_idx_in_records = records.index(p429_row)
    followup = records[p429_idx_in_records + 1: p429_idx_in_records + 6]
    # The first follow-up is the re-issue of the same request_idx on
    # spillover (sub_request_role=primary_request).
    assert followup[0]["endpoint_hit"] == "spillover", followup[0]
    assert followup[0]["request_idx"] == p429_row["request_idx"], (
        "expected the failing primary request to be re-issued on spillover"
    )
    # Subsequent stay_on_spillover_min_requests rows also route to
    # spillover (regardless of their reactive_decide view, because the
    # follow-up counter forces it).
    for r in followup[1:]:
        assert r["endpoint_hit"] == "spillover", r

    # The 429 is NOT silently retried away on primary: the primary
    # client's call log shows the 429 was a single attempt; the runner
    # never re-issued the same request on the primary client.
    primary_call_count = len(by_role["primary"].calls)
    # 1 preflight + up to 4 healthy calls before the 429 + 1 (the 429
    # itself). After that, the next 5 requests are routed to spillover,
    # so the primary client receives no additional calls in that window.
    assert primary_call_count <= 6, (
        f"primary client received more calls than expected — sign of a "
        f"silent retry: {primary_call_count}"
    )


# ----------------------------------------------------------------------------
# Test 2 — spillover 429 retries once then halts at >1%
# ----------------------------------------------------------------------------


def test_spillover_429_retries_once_then_halts_at_one_percent(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spillover-side 429s retry once. Aggregate spillover 429 rate
    exceeding 1% halts the run via ``SpilloverHalt429Error`` (exit 1).
    """
    # We need a setup where lots of requests route to spillover so the
    # halt fires after spillover_request_count >= MIN. A primary-only
    # 429 storm gets us there: every primary call 429s → every retry
    # routes to spillover → spillover 429s every time. After
    # SPILLOVER_REAL_429_MIN_REQUESTS spillover requests, if > 1% of
    # those 429'd, the halt fires. We make 100% of spillover requests
    # 429 so the halt is unambiguous.
    exp_path = _write_synthetic_yaml(
        tmp_path,
        policy_type="reactive",
        duration_seconds=120,
        warmup_duration_seconds=5,
        ramp_duration_seconds=20,
        sustain_duration_seconds=90,
        warmup_tps=1.0,
        ramp_start_tps=1.0,
        ramp_end_tps=2.0,
        sustain_tps=2.0,
        stay_on_spillover_min_requests=2,
    )
    benchmarks_root = tmp_path / "benchmarks"
    bench_dir = benchmarks_root / "05-dual-spillover"
    (bench_dir / "runs").mkdir(parents=True)

    primary_script: list[Any] = [
        lambda kw: _FakeHttpx429Error(headers={"retry-after-ms": "10"})
        for _ in range(500)
    ]
    spillover_script: list[Any] = [
        lambda kw: _FakeHttpx429Error(headers={"retry-after-ms": "10"})
        for _ in range(500)
    ]
    constructed, by_role = _install_fake_clients(
        monkeypatch,
        primary_script=primary_script,
        spillover_script=spillover_script,
    )

    cfg = mdual.load_experiment(exp_path)

    import os
    cwd_before = os.getcwd()
    os.chdir(tmp_path)
    try:
        with pytest.raises(mdual.SpilloverHalt429Error):
            mdual.run_measurement(
                cfg=cfg,
                benchmarks_root=benchmarks_root,
                pricing_dir=FIXTURE_PRICING_DIR,
                dry_run=False,
                smoke=False,
                allow_dirty=True,
            )
    finally:
        os.chdir(cwd_before)

    # Spillover client receives exactly two calls per "spillover attempt"
    # request: the initial attempt and the one retry. The
    # SPILLOVER_429_MAX_RETRIES constant is 1.
    assert mdual.SPILLOVER_429_MAX_RETRIES == 1
    # Spillover should have been called at least 2*(MIN_REQUESTS) times
    # (initial + retry per spillover request) before the halt fired.
    spillover_calls = len(by_role["spillover"].calls)
    # Subtract 1 for the preflight call.
    spillover_real_calls = spillover_calls - 1
    expected_min = 2 * mdual.SPILLOVER_REAL_429_MIN_REQUESTS
    assert spillover_real_calls >= expected_min, (
        f"spillover client received only {spillover_real_calls} real calls; "
        f"expected at least {expected_min} (2 per spillover request)"
    )


# ----------------------------------------------------------------------------
# Test 3 — two clients constructed, distinguishable by `model` field
# ----------------------------------------------------------------------------


def test_two_clients_constructed_one_per_deployment(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner constructs TWO clients. Every call to the primary
    client carries ``model == primary_deployment``; every call to the
    spillover client carries ``model == spillover_deployment``.
    """
    exp_path = _write_synthetic_yaml(
        tmp_path,
        policy_type="reactive",
        duration_seconds=15,
        warmup_duration_seconds=3,
        ramp_duration_seconds=6,
        sustain_duration_seconds=6,
        warmup_tps=1.0,
        ramp_start_tps=1.0,
        ramp_end_tps=1.5,
        sustain_tps=1.0,
        stay_on_spillover_min_requests=3,
    )
    benchmarks_root = tmp_path / "benchmarks"
    bench_dir = benchmarks_root / "05-dual-spillover"
    (bench_dir / "runs").mkdir(parents=True)

    # Primary script: a 429 after 2 healthy responses so we exercise both clients.
    primary_script: list[Any] = [
        lambda kw: _FakeResponse(usage=_FakeUsage()),
        lambda kw: _FakeResponse(usage=_FakeUsage()),
        lambda kw: _FakeHttpx429Error(),
        *[lambda kw: _FakeResponse(usage=_FakeUsage()) for _ in range(50)],
    ]
    spillover_script: list[Any] = [
        lambda kw: _FakeResponse(usage=_FakeUsage()) for _ in range(50)
    ]
    constructed, by_role = _install_fake_clients(
        monkeypatch,
        primary_script=primary_script,
        spillover_script=spillover_script,
    )

    cfg = mdual.load_experiment(exp_path)

    import os
    cwd_before = os.getcwd()
    os.chdir(tmp_path)
    try:
        mdual.run_measurement(
            cfg=cfg,
            benchmarks_root=benchmarks_root,
            pricing_dir=FIXTURE_PRICING_DIR,
            dry_run=False,
            smoke=False,
            allow_dirty=True,
        )
    finally:
        os.chdir(cwd_before)

    # Exactly two clients constructed (one per deployment).
    assert len(constructed) == 2, (
        f"expected 2 clients constructed (one per deployment); got "
        f"{len(constructed)}"
    )
    assert "primary" in by_role and "spillover" in by_role
    # The two client objects are distinct Python instances.
    assert by_role["primary"] is not by_role["spillover"]

    # Every primary client call (after the preflight ping) carries
    # model == primary_deployment.
    primary_calls = by_role["primary"].calls[1:]  # skip preflight
    for c in primary_calls:
        assert c["model"] == PRIMARY_DEPLOYMENT_NAME, (
            f"primary client received a call with model={c['model']!r}; "
            f"expected {PRIMARY_DEPLOYMENT_NAME!r}"
        )

    # Every spillover client call (after preflight) carries
    # model == spillover_deployment.
    spillover_calls = by_role["spillover"].calls[1:]
    assert spillover_calls, (
        "expected at least one spillover call after primary 429"
    )
    for c in spillover_calls:
        assert c["model"] == SPILLOVER_DEPLOYMENT_NAME, (
            f"spillover client received a call with model={c['model']!r}; "
            f"expected {SPILLOVER_DEPLOYMENT_NAME!r}"
        )

    # Pre-flight ping calls also follow the deployment-to-client
    # mapping (each preflight pings its own deployment).
    assert by_role["primary"].calls[0]["model"] == PRIMARY_DEPLOYMENT_NAME
    assert by_role["spillover"].calls[0]["model"] == SPILLOVER_DEPLOYMENT_NAME


# ----------------------------------------------------------------------------
# Test 4 — cache_pool == deployment_used for every record
# ----------------------------------------------------------------------------


def test_cache_pool_field_equals_deployment_used_for_every_record(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Phase 2 cache-pool separation invariant: every per-request
    record has ``cache_pool == deployment_used``.
    """
    exp_path = _write_synthetic_yaml(
        tmp_path,
        policy_type="reactive",
        duration_seconds=15,
        warmup_duration_seconds=3,
        ramp_duration_seconds=6,
        sustain_duration_seconds=6,
        warmup_tps=1.0,
        ramp_start_tps=1.0,
        ramp_end_tps=1.5,
        sustain_tps=1.0,
        stay_on_spillover_min_requests=3,
    )
    benchmarks_root = tmp_path / "benchmarks"
    bench_dir = benchmarks_root / "05-dual-spillover"
    (bench_dir / "runs").mkdir(parents=True)

    # Mix of primary success + 429 so the records include both endpoints.
    primary_script: list[Any] = [
        lambda kw: _FakeResponse(usage=_FakeUsage()),
        lambda kw: _FakeHttpx429Error(),
        *[lambda kw: _FakeResponse(usage=_FakeUsage()) for _ in range(50)],
    ]
    spillover_script: list[Any] = [
        lambda kw: _FakeResponse(usage=_FakeUsage()) for _ in range(50)
    ]
    _install_fake_clients(
        monkeypatch,
        primary_script=primary_script,
        spillover_script=spillover_script,
    )

    cfg = mdual.load_experiment(exp_path)

    import os
    cwd_before = os.getcwd()
    os.chdir(tmp_path)
    try:
        mdual.run_measurement(
            cfg=cfg,
            benchmarks_root=benchmarks_root,
            pricing_dir=FIXTURE_PRICING_DIR,
            dry_run=False,
            smoke=False,
            allow_dirty=True,
        )
    finally:
        os.chdir(cwd_before)

    jsonl_files = sorted((bench_dir / "runs").glob("*.jsonl"))
    assert len(jsonl_files) == 1
    records = _read_jsonl(jsonl_files[0])
    assert records, "expected at least one record"
    # Every single record has cache_pool == deployment_used.
    for r in records:
        assert "cache_pool" in r, f"missing cache_pool: {r!r}"
        assert "deployment_used" in r, f"missing deployment_used: {r!r}"
        assert r["cache_pool"] == r["deployment_used"], (
            f"cache_pool ({r['cache_pool']!r}) != deployment_used "
            f"({r['deployment_used']!r}) in record at request_idx="
            f"{r.get('request_idx')!r}"
        )
        # And the value is one of the two deployment names (not the
        # generic family name).
        assert r["deployment_used"] in (
            PRIMARY_DEPLOYMENT_NAME,
            SPILLOVER_DEPLOYMENT_NAME,
        ), f"unexpected deployment_used: {r['deployment_used']!r}"


# ----------------------------------------------------------------------------
# Test 5 — Phase 1 simulated_primary_throttle_state absent from Phase 2 records
# ----------------------------------------------------------------------------


def test_phase1_simulated_field_absent_from_phase2_records(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``simulated_primary_throttle_state`` (Phase 1 schema field) MUST
    NOT appear in any Phase 2 per-request record — it would be
    misleading next to real measurements.
    """
    exp_path = _write_synthetic_yaml(
        tmp_path,
        policy_type="proactive",
        duration_seconds=15,
        warmup_duration_seconds=3,
        ramp_duration_seconds=6,
        sustain_duration_seconds=6,
        warmup_tps=1.0,
        ramp_start_tps=1.0,
        ramp_end_tps=1.5,
        sustain_tps=1.0,
    )
    benchmarks_root = tmp_path / "benchmarks"
    bench_dir = benchmarks_root / "05-dual-spillover"
    (bench_dir / "runs").mkdir(parents=True)

    # All healthy.
    primary_script: list[Any] = [
        lambda kw: _FakeResponse(usage=_FakeUsage()) for _ in range(80)
    ]
    spillover_script: list[Any] = [
        lambda kw: _FakeResponse(usage=_FakeUsage()) for _ in range(80)
    ]
    _install_fake_clients(
        monkeypatch,
        primary_script=primary_script,
        spillover_script=spillover_script,
    )

    cfg = mdual.load_experiment(exp_path)

    import os
    cwd_before = os.getcwd()
    os.chdir(tmp_path)
    try:
        mdual.run_measurement(
            cfg=cfg,
            benchmarks_root=benchmarks_root,
            pricing_dir=FIXTURE_PRICING_DIR,
            dry_run=False,
            smoke=False,
            allow_dirty=True,
        )
    finally:
        os.chdir(cwd_before)

    jsonl_files = sorted((bench_dir / "runs").glob("*.jsonl"))
    assert len(jsonl_files) == 1
    records = _read_jsonl(jsonl_files[0])
    assert records, "expected at least one record"
    for r in records:
        # The Phase 1 simulator field must be ABSENT (not present-with-None).
        assert "simulated_primary_throttle_state" not in r, (
            "Phase 1 simulator field present in Phase 2 record: "
            f"simulated_primary_throttle_state={r['simulated_primary_throttle_state']!r}"
        )
    # Belt and suspenders: textual search across the JSONL.
    raw_text = jsonl_files[0].read_text(encoding="utf-8")
    assert "simulated_primary_throttle_state" not in raw_text, (
        "Phase 1 field name found in raw Phase 2 JSONL"
    )


# ----------------------------------------------------------------------------
# Test 6 — live --smoke gate: primary must see >= 1 real 429
# ----------------------------------------------------------------------------


def _patch_smoke_durations(
    monkeypatch: pytest.MonkeyPatch,
    *,
    duration_seconds: int = 18,
    sustain_seconds: int = 6,
) -> None:
    """Shrink the SMOKE override constants so smoke unit tests stay fast.

    Production: 180s total / 60s sustain. Tests: a few seconds. The
    override is applied via attribute monkeypatch so it auto-reverts
    at end of test.
    """
    monkeypatch.setattr(mdual, "SMOKE_DURATION_SECONDS", duration_seconds)
    monkeypatch.setattr(
        mdual, "SMOKE_SUSTAIN_DURATION_SECONDS", sustain_seconds
    )


def test_smoke_criteria_fails_when_primary_observes_no_429(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live ``--smoke`` requires primary_real_429_count >= 1.

    If the throttled primary deployment never 429s during the smoke
    window, the workload shape is too light or the TPM is misconfigured.
    Either way, downstream Phase 2 conclusions would be invalid. The
    runner halts with ``SmokeCriteriaError``; the CLI maps to
    ``EXIT_SMOKE`` (5).
    """
    _patch_smoke_durations(monkeypatch)
    exp_path = _write_synthetic_yaml(
        tmp_path,
        policy_type="reactive",
        duration_seconds=18,
        warmup_duration_seconds=3,
        ramp_duration_seconds=6,
        sustain_duration_seconds=6,
        warmup_tps=1.0,
        ramp_start_tps=1.0,
        ramp_end_tps=1.0,
        sustain_tps=1.0,
        stay_on_spillover_min_requests=3,
    )
    benchmarks_root = tmp_path / "benchmarks"
    bench_dir = benchmarks_root / "05-dual-spillover"
    (bench_dir / "runs").mkdir(parents=True)

    # Every primary call returns 200 OK; the smoke gate must fire because
    # the throttled primary never throttled.
    primary_script: list[Any] = [
        lambda kw: _FakeResponse(usage=_FakeUsage()) for _ in range(80)
    ]
    spillover_script: list[Any] = [
        lambda kw: _FakeResponse(usage=_FakeUsage()) for _ in range(80)
    ]
    _install_fake_clients(
        monkeypatch,
        primary_script=primary_script,
        spillover_script=spillover_script,
    )

    cfg = mdual.load_experiment(exp_path)

    import os
    cwd_before = os.getcwd()
    os.chdir(tmp_path)
    try:
        with pytest.raises(mdual.SmokeCriteriaError) as excinfo:
            mdual.run_measurement(
                cfg=cfg,
                benchmarks_root=benchmarks_root,
                pricing_dir=FIXTURE_PRICING_DIR,
                dry_run=False,
                smoke=True,
                allow_dirty=True,
            )
    finally:
        os.chdir(cwd_before)
    msg = str(excinfo.value)
    assert "SMOKE_PRIMARY_NO_429" in msg, msg
    assert "primary_real_429_count >= 1" in msg, msg


# ----------------------------------------------------------------------------
# Test 7 — live --smoke gate: spillover must see exactly 0 real 429s
# ----------------------------------------------------------------------------


def test_smoke_criteria_fails_when_spillover_observes_429(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live ``--smoke`` requires spillover_real_429_count == 0.

    The strict-zero gate fires BEFORE the runtime ``> 1%`` halt guard
    so misconfigured spillover TPM is caught before a full run is
    launched. Achieved here with a single primary 429 (satisfying the
    primary criterion) followed by a spillover 429 (failing the
    spillover criterion).
    """
    _patch_smoke_durations(monkeypatch, duration_seconds=12, sustain_seconds=4)
    exp_path = _write_synthetic_yaml(
        tmp_path,
        policy_type="reactive",
        duration_seconds=12,
        warmup_duration_seconds=2,
        ramp_duration_seconds=4,
        sustain_duration_seconds=4,
        warmup_tps=0.5,
        ramp_start_tps=0.5,
        ramp_end_tps=1.0,
        sustain_tps=0.5,
        stay_on_spillover_min_requests=2,
    )
    benchmarks_root = tmp_path / "benchmarks"
    bench_dir = benchmarks_root / "05-dual-spillover"
    (bench_dir / "runs").mkdir(parents=True)

    # Primary: success, then ONE 429, then more success.
    primary_script: list[Any] = [
        lambda kw: _FakeResponse(usage=_FakeUsage()),
        lambda kw: _FakeHttpx429Error(headers={"retry-after-ms": "10"}),
        *[lambda kw: _FakeResponse(usage=_FakeUsage()) for _ in range(40)],
    ]
    # Spillover: 429 on first contact, success on retry; that single
    # 429 is enough to fail the strict-zero smoke gate even though the
    # runtime > 1% guard has not yet fired (too few spillover requests).
    spillover_script: list[Any] = [
        lambda kw: _FakeHttpx429Error(headers={"retry-after-ms": "10"}),
        *[lambda kw: _FakeResponse(usage=_FakeUsage()) for _ in range(40)],
    ]
    _install_fake_clients(
        monkeypatch,
        primary_script=primary_script,
        spillover_script=spillover_script,
    )

    cfg = mdual.load_experiment(exp_path)

    import os
    cwd_before = os.getcwd()
    os.chdir(tmp_path)
    try:
        with pytest.raises(mdual.SmokeCriteriaError) as excinfo:
            mdual.run_measurement(
                cfg=cfg,
                benchmarks_root=benchmarks_root,
                pricing_dir=FIXTURE_PRICING_DIR,
                dry_run=False,
                smoke=True,
                allow_dirty=True,
            )
    finally:
        os.chdir(cwd_before)
    msg = str(excinfo.value)
    assert "SMOKE_SPILLOVER_NONZERO_429" in msg, msg
    assert "spillover_real_429_count == 0" in msg, msg


# ----------------------------------------------------------------------------
# Test 8 — live --smoke success path passes both criteria
# ----------------------------------------------------------------------------


def test_smoke_criteria_passes_when_primary_429_and_spillover_clean(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live ``--smoke`` returns normally when both criteria hold:

    * primary observes >= 1 real 429
    * spillover observes 0 real 429s
    """
    _patch_smoke_durations(monkeypatch, duration_seconds=12, sustain_seconds=4)
    exp_path = _write_synthetic_yaml(
        tmp_path,
        policy_type="reactive",
        duration_seconds=12,
        warmup_duration_seconds=2,
        ramp_duration_seconds=4,
        sustain_duration_seconds=4,
        warmup_tps=0.5,
        ramp_start_tps=0.5,
        ramp_end_tps=1.0,
        sustain_tps=0.5,
        stay_on_spillover_min_requests=2,
    )
    benchmarks_root = tmp_path / "benchmarks"
    bench_dir = benchmarks_root / "05-dual-spillover"
    (bench_dir / "runs").mkdir(parents=True)

    primary_script: list[Any] = [
        lambda kw: _FakeResponse(usage=_FakeUsage()),
        lambda kw: _FakeHttpx429Error(headers={"retry-after-ms": "10"}),
        *[lambda kw: _FakeResponse(usage=_FakeUsage()) for _ in range(40)],
    ]
    spillover_script: list[Any] = [
        lambda kw: _FakeResponse(usage=_FakeUsage()) for _ in range(40)
    ]
    _install_fake_clients(
        monkeypatch,
        primary_script=primary_script,
        spillover_script=spillover_script,
    )

    cfg = mdual.load_experiment(exp_path)

    import os
    cwd_before = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = mdual.run_measurement(
            cfg=cfg,
            benchmarks_root=benchmarks_root,
            pricing_dir=FIXTURE_PRICING_DIR,
            dry_run=False,
            smoke=True,
            allow_dirty=True,
        )
    finally:
        os.chdir(cwd_before)

    assert result.primary_real_429_count >= 1
    assert result.spillover_real_429_count == 0
    assert result.halt_reason is None


# ----------------------------------------------------------------------------
# Test 9 — --smoke + --dry-run does NOT enforce the live criteria
# ----------------------------------------------------------------------------


def test_smoke_criteria_not_enforced_in_dry_run(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--smoke`` + ``--dry-run`` skips the live criteria gate.

    The gate is defined only for live runs (``smoke=True`` and
    ``dry_run=False``). A dry-run smoke produces no real 429s by
    design, so enforcing the gate would make ``--dry-run --smoke``
    impossible — but that combination is the normal way to validate
    the YAML before spending tenant budget.
    """
    _patch_smoke_durations(monkeypatch, duration_seconds=12, sustain_seconds=4)
    exp_path = _write_synthetic_yaml(
        tmp_path,
        policy_type="reactive",
        duration_seconds=12,
        warmup_duration_seconds=2,
        ramp_duration_seconds=4,
        sustain_duration_seconds=4,
        warmup_tps=0.5,
        ramp_start_tps=0.5,
        ramp_end_tps=1.0,
        sustain_tps=0.5,
    )
    benchmarks_root = tmp_path / "benchmarks"
    bench_dir = benchmarks_root / "05-dual-spillover"
    (bench_dir / "runs").mkdir(parents=True)

    cfg = mdual.load_experiment(exp_path)

    import os
    cwd_before = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = mdual.run_measurement(
            cfg=cfg,
            benchmarks_root=benchmarks_root,
            pricing_dir=FIXTURE_PRICING_DIR,
            dry_run=True,
            smoke=True,
            allow_dirty=True,
        )
    finally:
        os.chdir(cwd_before)

    # Dry-run produces zero 429s on both sides; the gate is not enforced.
    assert result.primary_real_429_count == 0
    assert result.spillover_real_429_count == 0


# ----------------------------------------------------------------------------
# Test 10 — CLI: main() returns EXIT_SMOKE=5 on smoke criteria failure
# ----------------------------------------------------------------------------


def test_cli_main_exits_5_on_smoke_criteria_failure(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``main()`` returns ``EXIT_SMOKE`` (5) when smoke criteria fail.

    The CLI must distinguish smoke-criteria failure from budget /
    spillover halt so an operator's smoke pipeline can tell why the
    run did not progress to a full benchmark.
    """
    _patch_smoke_durations(monkeypatch, duration_seconds=12, sustain_seconds=4)
    exp_path = _write_synthetic_yaml(
        tmp_path,
        policy_type="reactive",
        duration_seconds=12,
        warmup_duration_seconds=2,
        ramp_duration_seconds=4,
        sustain_duration_seconds=4,
        warmup_tps=0.5,
        ramp_start_tps=0.5,
        ramp_end_tps=1.0,
        sustain_tps=0.5,
    )
    benchmarks_root = tmp_path / "benchmarks"
    bench_dir = benchmarks_root / "05-dual-spillover"
    (bench_dir / "runs").mkdir(parents=True)

    primary_script: list[Any] = [
        lambda kw: _FakeResponse(usage=_FakeUsage()) for _ in range(40)
    ]
    spillover_script: list[Any] = [
        lambda kw: _FakeResponse(usage=_FakeUsage()) for _ in range(40)
    ]
    _install_fake_clients(
        monkeypatch,
        primary_script=primary_script,
        spillover_script=spillover_script,
    )

    import os
    cwd_before = os.getcwd()
    os.chdir(tmp_path)
    try:
        rc = mdual.main(
            [
                "--experiment",
                str(exp_path),
                "--smoke",
                "--allow-dirty",
                "--benchmarks-root",
                str(benchmarks_root),
                "--pricing-dir",
                str(FIXTURE_PRICING_DIR),
                "--log-level",
                "ERROR",
            ]
        )
    finally:
        os.chdir(cwd_before)

    assert rc == mdual.EXIT_SMOKE == 5, (
        f"expected EXIT_SMOKE=5 on smoke-criteria failure; got {rc}"
    )


# ----------------------------------------------------------------------------
# Test 11 — success-response headers (x-ms-deployment-name, x-ms-spillover-*)
# are captured via the SDK's with_raw_response API
# ----------------------------------------------------------------------------


def test_success_response_headers_captured_via_with_raw_response(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Azure ``x-ms-*`` headers on 2xx responses land in JSONL records.

    Reasoning: ``client.responses.create()`` returns a parsed response
    without HTTP headers. The OpenAI SDK exposes them only via the
    raw-response API (``client.responses.with_raw_response.create()``).
    The Phase 2 contract requires capturing
    ``x-ms-deployment-name`` (reveals Azure-side native spillover),
    ``x-ms-spillover-from-deployment``, and ``x-ms-spillover-error``
    on every request, not just on 429s. This test verifies the success
    path captures them by scripting fake responses that carry these
    headers and reading them back from the on-disk JSONL.
    """
    exp_path = _write_synthetic_yaml(
        tmp_path,
        policy_type="reactive",
        duration_seconds=10,
        warmup_duration_seconds=2,
        ramp_duration_seconds=3,
        sustain_duration_seconds=3,
        warmup_tps=1.0,
        ramp_start_tps=1.0,
        ramp_end_tps=1.0,
        sustain_tps=1.0,
        stay_on_spillover_min_requests=2,
    )
    benchmarks_root = tmp_path / "benchmarks"
    bench_dir = benchmarks_root / "05-dual-spillover"
    (bench_dir / "runs").mkdir(parents=True)

    # Every primary success carries the three x-ms-* headers. We
    # specifically include both x-ms-deployment-name (always emitted by
    # Azure) and the spillover-tracking headers (emitted when Azure-side
    # native spillover routed the request) so the parser is exercised
    # on all three.
    def _ok_with_headers(kw: dict[str, Any]) -> _FakeResponse:
        return _FakeResponse(
            usage=_FakeUsage(),
            headers={
                "x-ms-deployment-name": PRIMARY_DEPLOYMENT_NAME,
                "x-ms-spillover-from-deployment": "",
                "x-ms-spillover-error": "",
            },
        )

    def _ok_spillover_headers(kw: dict[str, Any]) -> _FakeResponse:
        # Mimic an Azure-side native spillover signal on a spillover
        # endpoint hit, in case the runtime ever observes one.
        return _FakeResponse(
            usage=_FakeUsage(),
            headers={
                "x-ms-deployment-name": SPILLOVER_DEPLOYMENT_NAME,
                "x-ms-spillover-from-deployment": PRIMARY_DEPLOYMENT_NAME,
                "x-ms-spillover-error": "throttled",
            },
        )

    primary_script: list[Any] = [_ok_with_headers for _ in range(40)]
    spillover_script: list[Any] = [_ok_spillover_headers for _ in range(40)]
    _install_fake_clients(
        monkeypatch,
        primary_script=primary_script,
        spillover_script=spillover_script,
    )

    cfg = mdual.load_experiment(exp_path)

    import os
    cwd_before = os.getcwd()
    os.chdir(tmp_path)
    try:
        mdual.run_measurement(
            cfg=cfg,
            benchmarks_root=benchmarks_root,
            pricing_dir=FIXTURE_PRICING_DIR,
            dry_run=False,
            smoke=False,
            allow_dirty=True,
        )
    finally:
        os.chdir(cwd_before)

    jsonl_files = sorted((bench_dir / "runs").glob("*.jsonl"))
    assert len(jsonl_files) == 1
    records = _read_jsonl(jsonl_files[0])
    # Every record (no 429s in this script) has real_429_observed=False
    # and the three x-ms-* fields populated from the success-path
    # headers. The Phase 2 contract is that the headers are captured
    # on success responses, not just on 429s.
    primary_rows = [r for r in records if r["endpoint_hit"] == "primary"]
    assert primary_rows, "expected at least one primary success record"
    for r in primary_rows:
        assert r["real_429_observed"] is False
        assert r["x_ms_deployment_name"] == PRIMARY_DEPLOYMENT_NAME, (
            f"x-ms-deployment-name not captured on success row: {r!r}"
        )
        assert "x_ms_spillover_from_deployment" in r
        assert "x_ms_spillover_error" in r


def test_success_response_headers_captured_for_spillover_endpoint(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same header-capture contract for the spillover endpoint.

    Verifies that ``_call_spillover_with_retry`` also routes the
    successful raw-response headers into the JSONL record (separate
    code path from primary). Asserts that
    ``x-ms-spillover-from-deployment`` and ``x-ms-spillover-error``
    populate as scripted on a success response.
    """
    exp_path = _write_synthetic_yaml(
        tmp_path,
        policy_type="reactive",
        duration_seconds=10,
        warmup_duration_seconds=2,
        ramp_duration_seconds=3,
        sustain_duration_seconds=3,
        warmup_tps=1.0,
        ramp_start_tps=1.0,
        ramp_end_tps=1.0,
        sustain_tps=1.0,
        stay_on_spillover_min_requests=3,
    )
    benchmarks_root = tmp_path / "benchmarks"
    bench_dir = benchmarks_root / "05-dual-spillover"
    (bench_dir / "runs").mkdir(parents=True)

    # Primary: success then 429 then success. The 429 routes the next
    # 3 requests to spillover, exercising the spillover success path.
    primary_script: list[Any] = [
        lambda kw: _FakeResponse(usage=_FakeUsage()),
        lambda kw: _FakeHttpx429Error(headers={"retry-after-ms": "10"}),
        *[lambda kw: _FakeResponse(usage=_FakeUsage()) for _ in range(40)],
    ]

    def _ok_spillover_headers(kw: dict[str, Any]) -> _FakeResponse:
        return _FakeResponse(
            usage=_FakeUsage(),
            headers={
                "x-ms-deployment-name": SPILLOVER_DEPLOYMENT_NAME,
                "x-ms-spillover-from-deployment": "",
                "x-ms-spillover-error": "",
            },
        )

    spillover_script: list[Any] = [_ok_spillover_headers for _ in range(40)]
    _install_fake_clients(
        monkeypatch,
        primary_script=primary_script,
        spillover_script=spillover_script,
    )

    cfg = mdual.load_experiment(exp_path)

    import os
    cwd_before = os.getcwd()
    os.chdir(tmp_path)
    try:
        mdual.run_measurement(
            cfg=cfg,
            benchmarks_root=benchmarks_root,
            pricing_dir=FIXTURE_PRICING_DIR,
            dry_run=False,
            smoke=False,
            allow_dirty=True,
        )
    finally:
        os.chdir(cwd_before)

    jsonl_files = sorted((bench_dir / "runs").glob("*.jsonl"))
    assert len(jsonl_files) == 1
    records = _read_jsonl(jsonl_files[0])
    spillover_success_rows = [
        r
        for r in records
        if r["endpoint_hit"] == "spillover"
        and r["real_429_observed"] is False
    ]
    assert spillover_success_rows, (
        "expected at least one spillover success record after the "
        "primary 429 routed follow-ups to spillover"
    )
    for r in spillover_success_rows:
        assert r["x_ms_deployment_name"] == SPILLOVER_DEPLOYMENT_NAME, (
            f"x-ms-deployment-name not captured on spillover success "
            f"row: {r!r}"
        )


def test_create_with_raw_response_helper_unwraps_apiresponse() -> None:
    """Direct unit test for the ``_create_with_raw_response`` helper.

    The helper must:

    1. Prefer ``client.responses.with_raw_response.create(**kwargs)``
       when available.
    2. Call ``.parse()`` on the returned APIResponse and return the
       parsed response as element 0.
    3. Return the APIResponse's ``.headers`` as element 1 (not the
       parsed response's headers — those are typically absent).
    """
    fake_response = _FakeResponse(usage=_FakeUsage(), headers={})
    api_resp_headers = {
        "x-ms-deployment-name": "test-deployment",
        "x-ms-spillover-from-deployment": "test-source",
    }
    script: list[Any] = [lambda kw: fake_response]
    client = _FakeClient("test-deployment", script)
    # Inject explicit raw-response headers (not the same as the parsed
    # response's headers attribute) so we can prove the helper reads
    # from the APIResponse, not the parsed response.

    original_create = client.responses.with_raw_response.create  # type: ignore[attr-defined]

    async def _patched_create(**kwargs: Any) -> _FakeAPIResponse:
        resp = await original_create(**kwargs)
        return _FakeAPIResponse(parsed=await resp.parse(), headers=api_resp_headers)

    client.responses.with_raw_response.create = _patched_create  # type: ignore[attr-defined]

    import asyncio

    response, raw_headers = asyncio.run(
        mdual._create_with_raw_response(
            client=client, call_kwargs={"model": "x", "input": "y"}
        )
    )
    assert response is fake_response
    # Awaiting the async ``parse()`` must surface the typed response so
    # downstream ``response.usage`` access works. Guard against the
    # regression where a missing ``await`` silently returns a coroutine
    # object (which would not have a ``.usage`` attribute).
    assert not inspect.isawaitable(response), (
        "_create_with_raw_response returned an awaitable instead of the "
        "parsed response — parse() result was not awaited"
    )
    assert response.usage is fake_response.usage, (
        "Parsed response's usage object did not survive the raw-response "
        "unwrap; reasoning/output token capture would break."
    )
    assert raw_headers is not None
    assert raw_headers.get("x-ms-deployment-name") == "test-deployment"
    assert (
        raw_headers.get("x-ms-spillover-from-deployment") == "test-source"
    )


# ----------------------------------------------------------------------------
# Regression test — Foundry v1 + gpt-5.2 preflight kwargs contract
# ----------------------------------------------------------------------------


def test_preflight_reachability_uses_min_legal_kwargs() -> None:
    """``preflight_reachability`` must call each client with the minimum
    Foundry-v1-legal kwargs the production model accepts.

    Two regressions are guarded:

    1. ``max_output_tokens >= 16``: Foundry v1 Responses API rejects
       ``max_output_tokens < 16`` with HTTP 400
       ``integer_below_min_value``. Earlier preflight code used
       ``max_output_tokens=8`` and 400'd before the workload loop ever
       started.
    2. ``reasoning == {"effort": "low"}``: gpt-5.2-2025-12-11 rejects
       ``effort="minimal"`` at dispatch with HTTP 400
       ``unsupported_value`` (supported: ``none / low / medium / high /
       xhigh``). The smallest non-zero effort the production model
       accepts is ``low`` and it matches the effort the workload loop
       uses, so the preflight exercises the same code path as the
       measurement itself.

    A drift of either kwarg would silently re-introduce the 400 the
    methodology auditor flagged in Task 015 Phase 2 pre-run.
    """
    primary = _FakeClient(
        PRIMARY_DEPLOYMENT_NAME,
        [lambda kw: _FakeResponse(usage=_FakeUsage(output_tokens=3))],
    )
    spillover = _FakeClient(
        SPILLOVER_DEPLOYMENT_NAME,
        [lambda kw: _FakeResponse(usage=_FakeUsage(output_tokens=3))],
    )

    import asyncio

    results = asyncio.run(
        mdual.preflight_reachability(
            primary_client=primary,
            primary_deployment=PRIMARY_DEPLOYMENT_NAME,
            spillover_client=spillover,
            spillover_deployment=SPILLOVER_DEPLOYMENT_NAME,
        )
    )
    assert results["primary_reachable"] is True
    assert results["spillover_reachable"] is True

    for client, role, deployment in (
        (primary, "primary", PRIMARY_DEPLOYMENT_NAME),
        (spillover, "spillover", SPILLOVER_DEPLOYMENT_NAME),
    ):
        assert len(client.calls) == 1, (
            f"{role}: expected exactly 1 preflight call, "
            f"got {len(client.calls)}"
        )
        call = client.calls[0]
        assert call.get("model") == deployment, (
            f"{role}: preflight call routed to wrong deployment "
            f"(model={call.get('model')!r}, expected {deployment!r})"
        )
        max_out = call.get("max_output_tokens")
        assert isinstance(max_out, int) and max_out >= 16, (
            f"{role}: preflight must request max_output_tokens >= 16 "
            f"(Foundry v1 minimum; got {max_out!r})"
        )
        assert call.get("reasoning") == {"effort": "low"}, (
            f"{role}: preflight must pass reasoning={{'effort': 'low'}} "
            f"(gpt-5.2 rejects 'minimal'; got {call.get('reasoning')!r})"
        )


# ----------------------------------------------------------------------------
# Long-run auth hardening — refreshable Entra ID token provider wiring
# ----------------------------------------------------------------------------
#
# Phase 2 attempt at commit f3cc669 exited with
# ``openai.AuthenticationError: 401 - Access token ... expired`` at
# ``request_idx=202`` after ~23 minutes of wall clock — the bearer
# token captured at process start by the *synchronous*
# ``azure.identity.get_bearer_token_provider`` was eagerly resolved
# (``token_provider()``) into a static JWT string and embedded in
# ``AsyncOpenAI.api_key``. The OpenAI SDK then re-sent that one literal
# Bearer header per request with no refresh hook.
#
# The fix mirrors the earlier Task 014 hardening in
# ``scripts/simulate_spillover.py``: use ``azure.identity.aio`` and
# pass the async callable itself into ``AsyncOpenAI(api_key=...)``.
# The OpenAI SDK awaits the callable per request via
# ``_refresh_api_key`` so the Bearer header is always fresh.
#
# These tests prove the wiring is correct **offline** — they never
# touch real Entra ID and they never open a socket. Five tests
# parallel the equivalent suite for ``simulate_spillover.py``:
#
#   1. ``test_build_live_client_uses_refreshable_token_provider``
#   2. ``test_build_live_client_provider_refreshes_per_call``
#   3. ``test_build_live_client_does_not_embed_static_token_string``
#   4. ``test_build_live_client_no_outbound_https``
#   5. ``test_build_live_client_uses_aio_identity_not_sync``


def _install_fake_aio_identity(
    monkeypatch: pytest.MonkeyPatch,
    *,
    token_values: list[str] | None = None,
) -> dict[str, Any]:
    """Inject fake ``azure.identity.aio`` symbols into the import path.

    Captures ``DefaultAzureCredential`` construction and the audience
    scope passed to ``get_bearer_token_provider`` so tests can assert
    them. ``token_values`` (defaults to a single value) controls what
    successive awaits of the provider return — proving the provider
    is invoked per-request, not per-client.
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


@pytest.fixture
def _socket_guard(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Replace ``socket.socket`` with a guard that records and refuses connects.

    Returns a list shared with the test so assertions can inspect any
    attempted connection. A real connect attempt would raise — proving
    no outbound HTTPS was made.
    """
    import socket as _socket

    attempted: list[tuple[Any, ...]] = []

    class _GuardedSocket(_socket.socket):
        def connect(self, address: Any) -> None:  # type: ignore[override]
            attempted.append(("connect", address))
            raise AssertionError(
                f"_build_live_client made a socket.connect to "
                f"{address!r}; this is forbidden in unit tests"
            )

        def connect_ex(self, address: Any) -> int:  # type: ignore[override]
            attempted.append(("connect_ex", address))
            raise AssertionError(
                f"_build_live_client made a socket.connect_ex to "
                f"{address!r}; this is forbidden in unit tests"
            )

    monkeypatch.setattr(_socket, "socket", _GuardedSocket)
    return attempted


def test_build_live_client_uses_refreshable_token_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_build_live_client`` must pass a *callable* (refreshable) provider
    to ``AsyncOpenAI``, never a pre-resolved static token string.

    Verified by inspecting the resulting client:
      * ``client.api_key`` is empty (no static token embedded).
      * ``client._api_key_provider`` is set and is callable.
      * The provider is an async callable
        (``Callable[[], Awaitable[str]]``) — this is the contract
        ``AsyncOpenAI`` awaits per request to refresh the Bearer header.
    """
    state = _install_fake_aio_identity(monkeypatch)

    client = mdual._build_live_client(endpoint_value=TEST_ENDPOINT_VALUE)

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
    assert inspect.iscoroutinefunction(client._api_key_provider), (
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

    Under the Task 015 blocker hotfix the raw provider is wrapped with
    bounded retry + ``asyncio.Lock`` stampede protection, but the
    wrapper intentionally does NOT add its own outer fixed-window
    cache. Token reuse + refresh-near-expiry is delegated entirely to
    ``azure.identity.aio``'s internal credential cache (which knows
    the real ``exp``). Therefore every wrapper call invokes the
    underlying provider: three SDK refresh calls draw three distinct
    fresh tokens from the fake provider, and ``provider_calls == 3``.
    """
    state = _install_fake_aio_identity(
        monkeypatch,
        token_values=["fake-bearer-AAAA", "fake-bearer-BBBB", "fake-bearer-CCCC"],
    )

    client = mdual._build_live_client(endpoint_value=TEST_ENDPOINT_VALUE)

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
    assert state["provider_calls"] == 3, (
        f"expected underlying provider to be invoked once per refresh "
        f"(no outer caching at the wrapper layer); got "
        f"{state['provider_calls']} call(s)"
    )
    # And the most recently refreshed token is the one the client will
    # send on its next request.
    assert client.api_key == "fake-bearer-CCCC"


def test_robust_token_provider_retries_on_transient_cli_timeout() -> None:
    """A transient ``CredentialUnavailableError`` (Azure CLI subprocess
    timeout) must be retried with bounded exponential backoff, then
    the eventually-successful token returned to the caller. This is
    the core Task 015 blocker fix: a single transient timeout in a
    multi-thousand-request run must NOT abort the run.
    """
    from azure.identity import CredentialUnavailableError

    call_count = {"n": 0}
    sleeps: list[float] = []

    async def _flaky_underlying() -> str:
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise CredentialUnavailableError(
                message="Timed out waiting for Azure CLI"
            )
        return "fake-bearer-AFTER-RETRY"

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    provider = mdual._make_robust_token_provider(
        _flaky_underlying,
        max_retries=5,
        base_backoff_seconds=1.0,
        max_backoff_seconds=30.0,
        sleeper=_fake_sleep,
    )

    import asyncio as _asyncio

    token = _asyncio.run(provider())
    assert token == "fake-bearer-AFTER-RETRY", (
        f"transient retry did not return the recovered token: {token!r}"
    )
    assert call_count["n"] == 3, (
        f"expected 3 underlying calls (2 failures + 1 success), "
        f"got {call_count['n']}"
    )
    # Backoffs are exponential, starting at base_backoff_seconds=1.0,
    # capped at max_backoff_seconds=30.0. Two retries → two sleeps:
    # 1.0s before retry #1, 2.0s before retry #2.
    assert sleeps == [1.0, 2.0], (
        f"backoff schedule deviates from exponential 1s, 2s, ...: {sleeps!r}"
    )


def test_robust_token_provider_exhausts_after_bounded_retries() -> None:
    """When transient ``CredentialUnavailableError`` keeps firing past
    ``max_retries``, the wrapper must re-raise the original
    exception (NOT silently return ``None`` or a stale cached value
    from a different fetch). Silent failure here would surface as
    a confusing downstream 401 instead of the real CLI-timeout
    root cause.
    """
    from azure.identity import CredentialUnavailableError

    call_count = {"n": 0}
    sleeps: list[float] = []

    async def _always_timing_out() -> str:
        call_count["n"] += 1
        raise CredentialUnavailableError(
            message="Timed out waiting for Azure CLI"
        )

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    provider = mdual._make_robust_token_provider(
        _always_timing_out,
        max_retries=3,
        base_backoff_seconds=1.0,
        max_backoff_seconds=30.0,
        sleeper=_fake_sleep,
    )

    import asyncio as _asyncio

    with pytest.raises(CredentialUnavailableError):
        _asyncio.run(provider())

    assert call_count["n"] == 4, (
        f"expected max_retries+1=4 total attempts, got {call_count['n']}"
    )
    # Three retries → three backoffs: 1s, 2s, 4s. (No sleep after
    # the final failed attempt — we just re-raise.)
    assert sleeps == [1.0, 2.0, 4.0], (
        f"backoff schedule deviates from exponential 1s, 2s, 4s: {sleeps!r}"
    )


def test_robust_token_provider_retries_on_asyncio_timeout_error() -> None:
    """``asyncio.TimeoutError`` from the underlying provider chain is
    also retried (some failure modes surface as a generic asyncio
    timeout rather than a CLI-specific
    ``CredentialUnavailableError``).
    """
    call_count = {"n": 0}

    async def _flaky_underlying() -> str:
        call_count["n"] += 1
        if call_count["n"] < 2:
            raise asyncio.TimeoutError("async wait exceeded")
        return "fake-bearer-AFTER-ASYNC-TIMEOUT"

    async def _fake_sleep(_seconds: float) -> None:
        return None

    provider = mdual._make_robust_token_provider(
        _flaky_underlying,
        max_retries=3,
        base_backoff_seconds=0.5,
        sleeper=_fake_sleep,
    )

    import asyncio as _asyncio

    token = _asyncio.run(provider())
    assert token == "fake-bearer-AFTER-ASYNC-TIMEOUT"
    assert call_count["n"] == 2


def test_robust_token_provider_does_not_retry_non_transient_errors() -> None:
    """Non-transient errors (e.g. configuration or programming bugs)
    must surface immediately — silent retry would hide real failure
    modes behind the bounded backoff wall clock.
    """
    call_count = {"n": 0}

    class _ConfigurationError(RuntimeError):
        pass

    async def _bad_config() -> str:
        call_count["n"] += 1
        raise _ConfigurationError("bad scope")

    async def _fake_sleep(_seconds: float) -> None:
        return None

    provider = mdual._make_robust_token_provider(
        _bad_config,
        max_retries=5,
        sleeper=_fake_sleep,
    )

    import asyncio as _asyncio

    with pytest.raises(_ConfigurationError):
        _asyncio.run(provider())
    assert call_count["n"] == 1, (
        f"non-transient error retried {call_count['n']} times; "
        f"must propagate on first attempt"
    )


def test_robust_token_provider_does_not_cache_calls_underlying_every_time() -> None:
    """The wrapper must NOT add its own outer fixed-window cache.

    Token reuse + refresh-near-expiry is delegated to
    ``azure.identity.aio``'s internal credential cache (which knows
    the real Entra ID ``exp``). A fixed-window outer cache would
    risk re-caching an aged token that ``azure.identity`` returned
    from its internal cache while still valid, then extending the
    wrapper's perceived freshness past the real Azure-side expiry
    → 401 mid-run. This test pins that behaviour: every wrapper
    invocation must reach ``underlying`` exactly once.
    """
    call_count = {"n": 0}
    values = ["fake-bearer-FRESH-1", "fake-bearer-FRESH-2", "fake-bearer-FRESH-3"]

    async def _underlying() -> str:
        i = call_count["n"]
        call_count["n"] += 1
        return values[i] if i < len(values) else values[-1]

    async def _fake_sleep(_seconds: float) -> None:
        return None

    provider = mdual._make_robust_token_provider(
        _underlying,
        max_retries=5,
        sleeper=_fake_sleep,
    )

    import asyncio as _asyncio

    async def _drive() -> tuple[str, str, str]:
        a = await provider()
        b = await provider()
        c = await provider()
        return a, b, c

    a, b, c = _asyncio.run(_drive())
    assert (a, b, c) == (
        "fake-bearer-FRESH-1",
        "fake-bearer-FRESH-2",
        "fake-bearer-FRESH-3",
    ), (
        f"wrapper appears to be caching ({a!r} {b!r} {c!r}); "
        f"every call must reach underlying so the wrapper never "
        f"extends a token's perceived freshness past underlying's "
        f"say-so"
    )
    assert call_count["n"] == 3, (
        f"expected 3 underlying calls (one per wrapper call), got "
        f"{call_count['n']}"
    )


def test_robust_token_provider_does_not_recache_aged_underlying_token() -> None:
    """Reviewer-scenario regression test: when ``underlying`` returns
    the SAME bearer string across many calls (i.e. ``azure.identity``
    is serving from its internal cache, the issued token is still
    inside its real Entra ID validity window), the wrapper must NOT
    extend that token's perceived freshness past underlying's say-so.

    Concretely: if the wrapper had its own fixed-window outer cache,
    then at e.g. wrapper-cache refresh time (minute 40 of a 60-minute
    Entra TTL) it could read the SAME token from underlying, treat it
    as "freshly fetched", and cache it for another window — past the
    real Azure expiry → 401. The correct design is: never cache
    bearer strings at this layer, trust only the underlying.

    Verified by: many sequential wrapper calls + an underlying that
    always returns the same string → ``call_count`` equals wrapper
    call count (no skips), all returned tokens equal the underlying's
    string (no synthesised value), and we never observe a transition
    from "fresh fetch" to "served from outer cache".
    """
    underlying_calls = {"n": 0}
    aged_token = "fake-bearer-AGED-BUT-STILL-INSIDE-EXP"

    async def _underlying_returning_same_token() -> str:
        underlying_calls["n"] += 1
        return aged_token

    async def _fake_sleep(_seconds: float) -> None:
        return None

    provider = mdual._make_robust_token_provider(
        _underlying_returning_same_token,
        max_retries=5,
        sleeper=_fake_sleep,
    )

    import asyncio as _asyncio

    async def _drive_many_calls() -> list[str]:
        results: list[str] = []
        for _ in range(25):
            results.append(await provider())
        return results

    results = _asyncio.run(_drive_many_calls())
    assert len(results) == 25
    assert all(t == aged_token for t in results), (
        "wrapper synthesised a value different from underlying's "
        f"return: {set(results)!r}"
    )
    assert underlying_calls["n"] == 25, (
        f"wrapper served {25 - underlying_calls['n']} call(s) from "
        f"an outer cache — this re-introduces the stale-token risk "
        f"flagged by the final review. Every call must reach "
        f"underlying so the wrapper never extends a token's "
        f"perceived freshness past azure.identity's say-so."
    )


def test_robust_token_provider_no_static_token_regression() -> None:
    """Defense-in-depth: every wrapper call must fetch from the
    underlying provider (proving the long-run safety property — the
    wrapper never embeds a static one-shot token, even when the
    underlying provider rotates the token across calls).
    """
    fetched: list[str] = []

    async def _underlying() -> str:
        value = f"fake-bearer-#{len(fetched)}"
        fetched.append(value)
        return value

    async def _fake_sleep(_seconds: float) -> None:
        return None

    provider = mdual._make_robust_token_provider(
        _underlying,
        max_retries=2,
        sleeper=_fake_sleep,
    )

    import asyncio as _asyncio

    async def _drive() -> list[str]:
        results: list[str] = []
        for _ in range(5):
            results.append(await provider())
        return results

    results = _asyncio.run(_drive())
    # Every wrapper call reaches underlying → each call returns the
    # next fake value. If the wrapper had silently embedded the first
    # token as a static value this list would be all-equal.
    assert results == [
        "fake-bearer-#0",
        "fake-bearer-#1",
        "fake-bearer-#2",
        "fake-bearer-#3",
        "fake-bearer-#4",
    ], (
        f"wrapper did not refresh on every call — static-token bug "
        f"may have regressed: {results!r}"
    )
    assert len(fetched) == 5


def test_build_live_client_does_not_embed_static_token_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense-in-depth: even if a future refactor accidentally calls
    the provider eagerly, this test catches the regression.

    We watch the fake provider's invocation count: ``_build_live_client``
    must *not* call the provider during construction (only the SDK
    does, per-request).
    """
    state = _install_fake_aio_identity(monkeypatch)
    client = mdual._build_live_client(endpoint_value=TEST_ENDPOINT_VALUE)
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
    _ = mdual._build_live_client(endpoint_value=TEST_ENDPOINT_VALUE)
    assert _socket_guard == [], (
        f"_build_live_client attempted a socket connect: {_socket_guard}"
    )


def test_build_live_client_uses_aio_identity_not_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fix specifically uses ``azure.identity.aio`` (async surface)
    because ``AsyncOpenAI``'s per-request refresh hook awaits the
    provider. Using the sync ``azure.identity`` surface would force
    ``api_key=token_provider()`` (the old static-token bug) or require
    a custom async wrapper. This test pins the import path so a
    future refactor cannot silently regress.
    """
    import importlib

    src = importlib.import_module("scripts.measure_dual_spillover")
    body = inspect.getsource(src._build_live_client)
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
        "bug. Pass the callable itself: "
        "AsyncOpenAI(api_key=token_provider)."
    )
