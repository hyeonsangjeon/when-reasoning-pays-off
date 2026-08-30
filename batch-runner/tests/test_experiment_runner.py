"""Contract and acceptance tests for the five-minute experiment runner.

Everything here is offline: the mock provider makes no call, and the Ollama and
Azure providers are exercised with injected fake transports/clients. A network
guard fixture proves no socket is opened.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

from batch_runner.experiment.catalog import build_catalog, load_packaged_catalog
from batch_runner.experiment.dataset import DatasetError, load_dataset
from batch_runner.experiment.ledger import LedgerError, parse_ledger
from batch_runner.experiment.providers.azure import AzureFoundryProvider
from batch_runner.experiment.providers.base import (
    EndpointResolutionError,
    ResolvedEndpoint,
    resolve_endpoint,
)
from batch_runner.experiment.providers.ollama import OllamaProvider
from batch_runner.experiment.record import (
    BudgetNotConfirmedError,
    ModelUnavailableError,
    OutputRecord,
    ProviderCapabilities,
    ProviderUnavailableError,
    ResponseNotCompletedError,
)
from batch_runner.experiment.runner import (
    EXIT_OK,
    EXIT_PARTIAL,
    ExperimentOutputConflict,
    retry_failed_run,
    run_ledger,
    sample_banner,
)

_RESOURCES = "batch_runner.experiment.resources"
_FIXED_CLOCK = lambda: 1_700_000_000.0  # noqa: E731 - test clock
_FIXED_RANDOM = lambda _n: "0123abcd"  # noqa: E731 - test randomness


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _base_ledger(provider: str) -> dict[str, Any]:
    led: dict[str, Any] = {
        "schema_version": "1.0.0",
        "experiment": {"id": f"t-{provider}", "purpose": "unit test"},
        "provider": provider,
        "model": "mock-preview" if provider == "mock" else "test-model",
        "endpoint": {"env_var": "TEST_ENDPOINT", "default": "http://localhost:11434"},
        "auth": {"mode": "none", "env_vars": []},
        "input": {
            "path": "sample.jsonl",
            "format": "jsonl",
            "row_shape": {"required_fields": {"id": "string", "input": "string"}},
            "max_records": 50,
            "sample_selector": "first",
        },
        "execution": {
            "max_samples": 3,
            "concurrency": 1,
            "timeout_seconds": 60,
            "max_output_tokens": 128,
            "repeats": 1,
            "capture_io": True,
            "cost": {"billed": False, "confirmed": False},
        },
        "output": {
            "dir": "out",
            "artifacts": [
                "run.json",
                "records.jsonl",
                "summary.md",
                "manifest.json",
                "artifacts.sha256",
            ],
        },
        "provenance": {"method_id": "experiment-runner", "method_version": "1.0.0"},
    }
    if provider == "ollama":
        led["model"] = "qwen2.5:0.5b"
        led["endpoint"] = {
            "env_var": "OLLAMA_BASE_URL",
            "default": "http://localhost:11434",
        }
    if provider == "azure":
        led["model"] = "gpt-5.2"
        led["endpoint"] = {"env_var": "AZURE_OPENAI_FOUNDRY_ENDPOINT"}
        led["auth"] = {"mode": "entra", "env_vars": []}
        led["execution"]["reasoning_effort"] = "none"
        led["execution"]["cost"] = {
            "billed": True,
            "confirmed": True,
            "estimated_usd": 0.05,
            "hard_ceiling_usd": 1.0,
            "pricing_snapshot_id": "test-pricing-2026-03-08",
            "pricing_model": "gpt-5.2",
            "input_per_1m_usd": 2.0,
            "output_per_1m_usd": 20.0,
        }
    return led


def _workspace(tmp_path: Path, rows: list[dict[str, Any]] | None = None) -> Path:
    rows = rows or [
        {"id": "q1", "input": "What is 2+2? Answer with a number."},
        {"id": "q2", "input": "Name the capital of France."},
    ]
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "sample.jsonl").write_text(text, encoding="utf-8")
    return tmp_path


def _latest_run(workspace: Path) -> Path:
    pointer = json.loads((workspace / "out" / "latest.json").read_text())
    return workspace / "out" / pointer["run_path"]


def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_a: Any, **_k: Any) -> None:
        raise AssertionError("network access attempted in a test")

    monkeypatch.setattr(socket.socket, "connect", blocked)


# --------------------------------------------------------------------------
# Catalog coverage / no-drift
# --------------------------------------------------------------------------
def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_catalog_covers_every_exp_yaml_exactly_once():
    repo = _repo_root()
    yaml_files = sorted(
        p.name
        for p in (repo / "experiments").glob("exp*.yaml")
        if p.name != "_template.yaml"
    )
    assert len(yaml_files) == 20, yaml_files
    catalog = build_catalog(repo_root=repo)
    covered = [e["config_path"].split("/")[-1] for e in catalog["experiments"]]
    assert sorted(covered) == yaml_files
    assert len(covered) == len(set(covered)) == 20


def test_packaged_catalog_matches_freshly_built():
    repo = _repo_root()
    built = build_catalog(repo_root=repo)
    packaged = load_packaged_catalog()
    assert packaged["experiment_count"] == built["experiment_count"] == 20
    built_ids = [e["experiment_id"] for e in built["experiments"]]
    packaged_ids = [e["experiment_id"] for e in packaged["experiments"]]
    assert built_ids == packaged_ids


def test_catalog_no_orphan_families():
    catalog = load_packaged_catalog()
    for entry in catalog["experiments"]:
        assert entry["family"], entry
        assert entry["read_benchmark"], entry
        assert entry["execute"]["provider"] == "azure"


# --------------------------------------------------------------------------
# Ledger: fail-closed, secret-safe, discriminated
# --------------------------------------------------------------------------
def test_ledger_rejects_unknown_field():
    led = _base_ledger("mock")
    led["surprise"] = 1
    with pytest.raises(LedgerError):
        parse_ledger(led)


def test_ledger_rejects_secret_shaped_model():
    led = _base_ledger("mock")
    # Build the secret-shaped value at runtime so no literal key-like token
    # appears in tracked source (the public-surface grep forbids such literals).
    led["model"] = "sk-" + ("a" * 20) + "0123456789012345"
    with pytest.raises(LedgerError):
        parse_ledger(led)


def test_ledger_azure_forbids_endpoint_default():
    led = _base_ledger("azure")
    led["endpoint"]["default"] = "http://localhost:11434"
    with pytest.raises(LedgerError):
        parse_ledger(led)


def test_ledger_ollama_rejects_nonlocal_default():
    led = _base_ledger("ollama")
    led["endpoint"]["default"] = "http://evil.example.com:11434"
    with pytest.raises(LedgerError):
        parse_ledger(led)


def test_ledger_reasoning_effort_is_azure_only():
    led = _base_ledger("ollama")
    led["execution"]["reasoning_effort"] = "high"
    with pytest.raises(LedgerError):
        parse_ledger(led)


def test_ledger_max_samples_not_above_max_records():
    led = _base_ledger("mock")
    led["execution"]["max_samples"] = 999
    led["input"]["max_records"] = 10
    with pytest.raises(LedgerError):
        parse_ledger(led)


# --------------------------------------------------------------------------
# Dataset shape
# --------------------------------------------------------------------------
def test_dataset_loads_jsonl(tmp_path: Path):
    _workspace(tmp_path)
    led = parse_ledger(_base_ledger("mock"))
    ds = load_dataset(tmp_path / "sample.jsonl", led.input)
    assert ds.total_records == 2
    assert ds.selected(selector="first", limit=1)[0]["id"] == "q1"


def test_dataset_missing_required_field_fails(tmp_path: Path):
    _workspace(tmp_path, rows=[{"id": "q1"}])
    led = parse_ledger(_base_ledger("mock"))
    with pytest.raises(DatasetError):
        load_dataset(tmp_path / "sample.jsonl", led.input)


# --------------------------------------------------------------------------
# Mock provider: deterministic structure under a fixed clock
# --------------------------------------------------------------------------
def test_mock_run_is_byte_deterministic(tmp_path: Path, monkeypatch):
    _no_network(monkeypatch)
    led = parse_ledger(_base_ledger("mock"))
    ws1 = _workspace(tmp_path / "a")
    ws2 = _workspace(tmp_path / "b")
    r1 = run_ledger(
        led, base_dir=ws1, clock=_FIXED_CLOCK, random_hex=_FIXED_RANDOM
    )
    r2 = run_ledger(
        led, base_dir=ws2, clock=_FIXED_CLOCK, random_hex=_FIXED_RANDOM
    )
    assert r1.exit_code == r2.exit_code == EXIT_OK
    assert r1.run_json_path.read_text() == r2.run_json_path.read_text()
    assert r1.records_path.read_text() == r2.records_path.read_text()


def test_mock_capabilities_never_fabricate_zero(tmp_path: Path):
    led = parse_ledger(_base_ledger("mock"))
    run_ledger(led, base_dir=_workspace(tmp_path), clock=_FIXED_CLOCK)
    run_dir = _latest_run(tmp_path)
    run_json = json.loads((run_dir / "run.json").read_text())
    assert run_json["usage"]["reasoning_tokens"] is None
    assert run_json["usage"]["cached_tokens"] is None
    assert run_json["capabilities"]["reasoning_tokens"] == "not_supported"
    rec = json.loads((run_dir / "records.jsonl").read_text().splitlines()[0])
    assert rec["reasoning_tokens"] is None


def test_sample_banner_present(tmp_path: Path):
    led = parse_ledger(_base_ledger("mock"))
    run_ledger(led, base_dir=_workspace(tmp_path), clock=_FIXED_CLOCK)
    run_json = json.loads((_latest_run(tmp_path) / "run.json").read_text())
    assert "not the published benchmark" in run_json["banner"]


_BANNER_TAIL = (
    "not the published benchmark; no quality judge or comparable "
    "reasoning-effort sweep"
)


def test_banner_is_provider_specific():
    # mock is an OFFLINE preview; ollama/azure are LIVE samples. Both keep the
    # shared honesty tail so no run is mistaken for the published benchmark.
    mock_banner = sample_banner("mock")
    assert "illustrative offline preview" in mock_banner
    assert "illustrative live sample" not in mock_banner
    assert _BANNER_TAIL in mock_banner

    for live in ("ollama", "azure"):
        live_banner = sample_banner(live)
        assert "illustrative live sample" in live_banner
        assert "illustrative offline preview" not in live_banner
        assert _BANNER_TAIL in live_banner


def test_mock_run_json_uses_offline_preview_banner(tmp_path: Path):
    led = parse_ledger(_base_ledger("mock"))
    run_ledger(led, base_dir=_workspace(tmp_path), clock=_FIXED_CLOCK)
    run_json = json.loads((_latest_run(tmp_path) / "run.json").read_text())
    assert "illustrative offline preview" in run_json["banner"]
    assert "illustrative live sample" not in run_json["banner"]


# --------------------------------------------------------------------------
# Azure quickstart ledger: cost-safe reasoning effort, honest auth metadata
# --------------------------------------------------------------------------
def _packaged_azure_ledger_text() -> str:
    from importlib import resources

    return (
        resources.files(_RESOURCES).joinpath("ledger.azure.yaml").read_text()
    )


def test_azure_quickstart_reasoning_effort_is_explicit_none():
    import yaml

    text = _packaged_azure_ledger_text()
    data = yaml.safe_load(text)
    effort = data["execution"]["reasoning_effort"]
    # Never implicit (null): an unset effort can pick a non-lowest default.
    assert effort == "none"


def test_azure_quickstart_never_advertises_minimal():
    # The packaged gpt-5.2 sample must not mention or set `minimal` anywhere,
    # since gpt-5.2 rejects it and it is not a value to suggest.
    text = _packaged_azure_ledger_text()
    assert "minimal" not in text.lower()
    data = __import__("yaml").safe_load(text)
    assert data["execution"]["reasoning_effort"] != "minimal"


def test_ledger_rejects_minimal_for_gpt_5_2():
    led = _base_ledger("azure")
    led["model"] = "gpt-5.2"
    led["execution"]["reasoning_effort"] = "minimal"
    with pytest.raises(LedgerError):
        parse_ledger(led)


def test_ledger_allows_minimal_for_other_models():
    led = _base_ledger("azure")
    led["model"] = "some-other-model"
    led["execution"]["reasoning_effort"] = "minimal"
    parse_ledger(led)  # not gpt-5.2 -> permissive, no raise


def test_azure_quickstart_endpoint_not_in_auth_env_vars():
    data = __import__("yaml").safe_load(_packaged_azure_ledger_text())
    endpoint_var = data["endpoint"]["env_var"]
    assert data["auth"]["env_vars"] == []
    assert endpoint_var not in data["auth"]["env_vars"]


def test_ledger_rejects_endpoint_var_listed_as_auth():
    led = _base_ledger("azure")
    led["auth"]["env_vars"] = [led["endpoint"]["env_var"]]
    with pytest.raises(LedgerError):
        parse_ledger(led)


def test_no_network_during_mock_run(tmp_path: Path, monkeypatch):
    _no_network(monkeypatch)
    led = parse_ledger(_base_ledger("mock"))
    result = run_ledger(led, base_dir=_workspace(tmp_path), clock=_FIXED_CLOCK)
    assert result.exit_code == EXIT_OK


# --------------------------------------------------------------------------
# Ollama provider: localhost restriction + fake transport
# --------------------------------------------------------------------------
def _ollama_ok_body() -> bytes:
    return json.dumps(
        {
            "message": {"role": "assistant", "content": "4"},
            "done": True,
            "done_reason": "stop",
            "total_duration": 2_000_000_000,
            "prompt_eval_count": 11,
            "eval_count": 3,
        }
    ).encode("utf-8")


def test_ollama_refuses_nonlocal_endpoint(monkeypatch):
    led = parse_ledger(_base_ledger("ollama"))
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://remote.example.com:11434")
    with pytest.raises(EndpointResolutionError):
        resolve_endpoint(led, environ={"OLLAMA_BASE_URL": "http://remote.example.com:11434"})


def test_ollama_fake_transport_normalizes(monkeypatch):
    _no_network(monkeypatch)
    led = parse_ledger(_base_ledger("ollama"))
    ep = ResolvedEndpoint(base_url="http://localhost:11434", source="endpoint.default")

    def transport(url: str, payload: bytes, timeout: float) -> tuple[int, bytes]:
        assert url == "http://localhost:11434/api/chat"
        return 200, _ollama_ok_body()

    prov = OllamaProvider(ledger=led, endpoint=ep, capture_io=True, transport=transport)
    rec = prov.run_row("q1", 0, "What is 2+2?")
    assert rec.status == "ok"
    assert rec.input_tokens == 11 and rec.output_tokens == 3
    assert rec.reasoning_tokens is None
    assert rec.latency_ms == 2000  # 2e9 ns -> 2000 ms
    assert rec.response_text == "4"


def test_ollama_model_missing_is_actionable(monkeypatch):
    led = parse_ledger(_base_ledger("ollama"))
    ep = ResolvedEndpoint(base_url="http://localhost:11434", source="endpoint.default")

    def transport(url: str, payload: bytes, timeout: float) -> tuple[int, bytes]:
        return 404, b"{}"

    prov = OllamaProvider(ledger=led, endpoint=ep, capture_io=True, transport=transport)
    with pytest.raises(ModelUnavailableError) as exc:
        prov.run_row("q1", 0, "hi")
    assert "ollama pull" in str(exc.value)


def test_ollama_connection_error_is_not_success(monkeypatch):
    led = parse_ledger(_base_ledger("ollama"))
    ep = ResolvedEndpoint(base_url="http://localhost:11434", source="endpoint.default")

    def transport(url: str, payload: bytes, timeout: float) -> tuple[int, bytes]:
        raise ConnectionRefusedError("no server")

    prov = OllamaProvider(ledger=led, endpoint=ep, capture_io=True, transport=transport)
    with pytest.raises(ConnectionRefusedError):
        prov.run_row("q1", 0, "hi")


# --------------------------------------------------------------------------
# Azure provider: billed gate + refreshable token provider (never eager)
# --------------------------------------------------------------------------
class _FakeDetails:
    def __init__(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeUsage:
    def __init__(self) -> None:
        self.input_tokens = 11
        self.output_tokens = 50  # already includes reasoning
        self.total_tokens = 61
        self.output_tokens_details = _FakeDetails(reasoning_tokens=20)
        # input_tokens_details deliberately absent -> cached must be null


class _FakeResponse:
    def __init__(self) -> None:
        self.output_text = "the answer"
        self.usage = _FakeUsage()
        self.status = "completed"


def _azure_provider(monkeypatch, environ: dict[str, str] | None = None):
    counters = {"factory": 0, "token": 0}

    def token_provider() -> str:
        counters["token"] += 1
        return f"tok-{counters['token']}"

    def token_provider_factory():
        counters["factory"] += 1
        return token_provider

    holder: dict[str, Any] = {}

    class _FakeResponses:
        def create(self, **kwargs: Any) -> _FakeResponse:
            # Simulate the OpenAI client calling api_key() per request.
            holder["api_key"]()
            holder.setdefault("create_kwargs", []).append(kwargs)
            return _FakeResponse()

    def client_factory(*, base_url: str, api_key, **kwargs: Any):  # noqa: ANN001
        holder["api_key"] = api_key
        holder["client_kwargs"] = kwargs
        holder["base_url"] = base_url
        client = type("C", (), {})()
        client.responses = _FakeResponses()
        return client

    led = parse_ledger(_base_ledger("azure"))
    ep = ResolvedEndpoint(
        base_url="https://x/", source="AZURE_OPENAI_FOUNDRY_ENDPOINT (env)"
    )
    prov = AzureFoundryProvider(
        ledger=led,
        endpoint=ep,
        capture_io=True,
        client_factory=client_factory,
        token_provider_factory=token_provider_factory,
        environ=environ or {},
    )

    def _api_key_getter():
        return holder["api_key"]

    counters["holder"] = holder
    return prov, counters, _api_key_getter


def test_azure_token_provider_not_called_at_construction(monkeypatch):
    prov, counters, _ = _azure_provider(monkeypatch)
    assert counters["factory"] == 0
    assert counters["token"] == 0


def test_azure_token_provider_refreshes_per_request(monkeypatch):
    prov, counters, get_api_key = _azure_provider(monkeypatch)
    prov.prepare()
    # Factory was used once to obtain the provider; the token itself is not
    # fetched until the client makes a request.
    assert counters["factory"] == 1
    assert counters["token"] == 0
    # The client received the CALLABLE, not a resolved string.
    assert callable(get_api_key())
    rec1 = prov.run_row("q1", 0, "hi")
    rec2 = prov.run_row("q2", 0, "yo")
    assert counters["token"] == 2  # one refresh per request
    assert rec1.status == rec2.status == "ok"


def test_azure_usage_normalization_no_double_count(monkeypatch):
    prov, _, _ = _azure_provider(monkeypatch)
    prov.prepare()
    rec = prov.run_row("q1", 0, "hi")
    assert rec.output_tokens == 50  # reasoning already included, not added
    assert rec.reasoning_tokens == 20
    assert rec.cached_tokens is None  # missing nested detail -> null, not 0


def test_azure_refuses_unconfirmed(monkeypatch):
    led_dict = _base_ledger("azure")
    led_dict["execution"]["cost"]["confirmed"] = False
    led2 = parse_ledger(led_dict)
    ep = ResolvedEndpoint(base_url="https://x/", source="env")
    prov = AzureFoundryProvider(
        ledger=led2,
        endpoint=ep,
        capture_io=True,
        client_factory=lambda **_k: None,
        token_provider_factory=lambda: (lambda: "t"),
        environ={},
    )
    with pytest.raises(BudgetNotConfirmedError):
        prov.prepare()


def test_azure_hard_refuses_ci(monkeypatch):
    prov, _, _ = _azure_provider(monkeypatch, environ={"CI": "true"})
    with pytest.raises(BudgetNotConfirmedError):
        prov.prepare()


# --------------------------------------------------------------------------
# run_ledger integration: partial failure, owned output, evidence isolation
# --------------------------------------------------------------------------
class _FlakyProvider:
    """Deterministic provider that fails one specific row."""

    name = "mock"

    def __init__(self, fail_id: str) -> None:
        self._fail_id = fail_id

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name,
            billed=False,
            token_usage="synthetic",
            reasoning_tokens="not_supported",
            cached_tokens="not_supported",
        )

    def prepare(self) -> None:
        return None

    def run_row(self, row_id: str, repeat_index: int, prompt: str) -> OutputRecord:
        if row_id == self._fail_id:
            raise ProviderUnavailableError("boom")
        return OutputRecord(
            row_id=row_id,
            repeat_index=repeat_index,
            provider=self.name,
            model="mock-1",
            status="ok",
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
            reasoning_tokens=None,
            cached_tokens=None,
            total_tokens=2,
            finish_reason="stop",
            response_text="ok",
        )


def test_partial_failure_preserves_rows_and_exits_nonzero(tmp_path: Path):
    led_dict = _base_ledger("mock")
    led = parse_ledger(led_dict)
    ws = _workspace(tmp_path)
    result = run_ledger(
        led,
        base_dir=ws,
        clock=_FIXED_CLOCK,
        provider_builder=lambda *_a, **_k: _FlakyProvider("q2"),
    )
    assert result.exit_code == EXIT_PARTIAL
    assert result.ok_count == 1
    assert result.error_count == 1
    assert any(f.row_id == "q2" for f in result.failures)
    lines = (result.records_path).read_text().splitlines()
    assert len(lines) == 2  # completed row + failure record both preserved


def test_output_refuses_foreign_directory(tmp_path: Path):
    led = parse_ledger(_base_ledger("mock"))
    ws = _workspace(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "important.txt").write_text("do not touch")
    with pytest.raises(ExperimentOutputConflict):
        run_ledger(led, base_dir=ws, out_dir=foreign, clock=_FIXED_CLOCK)
    assert (foreign / "important.txt").read_text() == "do not touch"


def test_output_refuses_evidence_tree(tmp_path: Path):
    # A sample/preview run must never be able to write inside the published
    # benchmark or results evidence trees (which feed claim-integrity checks),
    # no matter how deep the path segment appears. Nothing may be created.
    led = parse_ledger(_base_ledger("mock"))
    ws = _workspace(tmp_path)
    for evidence in (
        tmp_path / "benchmarks" / "01" / "out",
        tmp_path / "results" / "runs" / "out",
        tmp_path / "nested" / "benchmarks" / "deep",
        tmp_path / "nested" / "results" / "deep",
    ):
        with pytest.raises(ExperimentOutputConflict):
            run_ledger(led, base_dir=ws, out_dir=evidence, clock=_FIXED_CLOCK)
        assert not evidence.exists(), (
            f"guard must refuse BEFORE creating {evidence}"
        )


def test_azure_run_json_has_no_secret_endpoint(tmp_path: Path):
    led = parse_ledger(_base_ledger("azure"))
    ws = _workspace(tmp_path)

    def builder(*_a, **_k):
        return _FlakyProvider("__none__")

    env = {"AZURE_OPENAI_FOUNDRY_ENDPOINT": "https://secret-host.example.com/openai/v1/"}
    run_ledger(
        led,
        base_dir=ws,
        environ=env,
        confirm_cost=True,
        clock=_FIXED_CLOCK,
        provider_builder=builder,
    )
    text = (_latest_run(tmp_path) / "run.json").read_text()
    # The resolved host must never be serialized; only the env-var NAME and the
    # fixed public audience URL may appear.
    assert "secret-host.example.com" not in text
    endpoint = json.loads(text)["endpoint"]
    assert "base_url" not in endpoint
    assert endpoint["endpoint_env_var"] == "AZURE_OPENAI_FOUNDRY_ENDPOINT"


def test_packaged_sample_matches_committed():
    from importlib import resources

    pkg = resources.files("batch_runner.experiment.resources")
    packaged = (pkg / "sample.jsonl").read_text()
    committed = (
        _repo_root()
        / "batch-runner"
        / "batch_runner"
        / "experiment"
        / "resources"
        / "sample.jsonl"
    ).read_text()
    assert packaged == committed


# --------------------------------------------------------------------------
# CLI command shape: verb separation + init/run wiring
# --------------------------------------------------------------------------
def test_cli_experiment_list_shows_twenty(capsys):
    from batch_runner.cli import main

    assert main(["experiment", "list"]) == 0
    out = capsys.readouterr().out
    assert "20 experiments" in out or out.count("exp") >= 20


def test_cli_has_no_experiment_run_verb():
    # `experiment` is a read-only catalog; the one-row real call is `sample run`.
    from batch_runner.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["experiment", "run"])
    assert exc.value.code != 0


def test_cli_sample_init_then_run_mock(tmp_path: Path, monkeypatch):
    _no_network(monkeypatch)
    from batch_runner.cli import main

    ws = tmp_path / "ws"
    assert main(["sample", "init", "--provider", "mock", "--out", str(ws)]) == 0
    assert (ws / "ledger.yaml").is_file()
    assert (ws / "sample.jsonl").is_file()
    assert (ws / ".env.example").is_file()

    out_dir = ws / "out"
    rc = main(["sample", "run", "--ledger", str(ws / "ledger.yaml")])
    assert rc == 0
    run_dir = _latest_run(ws)
    assert (run_dir / "run.json").is_file()
    assert (run_dir / "records.jsonl").is_file()
    assert (run_dir / "summary.md").is_file()
    assert (run_dir / "manifest.json").is_file()
    assert (run_dir / "artifacts.sha256").is_file()
    assert (out_dir / ".reasoning-payoff-experiment-owned").is_file()
    assert (out_dir / "latest.json").is_file()


def test_cli_sample_json_uses_workspace_relative_output_paths(
    tmp_path: Path, monkeypatch, capsys
):
    _no_network(monkeypatch)
    from batch_runner.cli import main

    ws = tmp_path / "private-workspace-name"
    assert main(["sample", "init", "--provider", "mock", "--out", str(ws)]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "sample",
                "run",
                "--ledger",
                str(ws / "ledger.yaml"),
                "--json",
            ]
        )
        == 0
    )
    payload = capsys.readouterr().out
    assert str(tmp_path) not in payload
    parsed = json.loads(payload)
    assert parsed["out_dir"] == f"out/runs/{parsed['run_id']}"
    assert parsed["run_json"] == f"{parsed['out_dir']}/run.json"


def test_cli_sample_init_matches_packaged_bytes(tmp_path: Path):
    from importlib import resources

    from batch_runner.cli import main

    ws = tmp_path / "ws"
    assert main(["sample", "init", "--provider", "ollama", "--out", str(ws)]) == 0
    pkg = resources.files(_RESOURCES)
    assert (ws / "sample.jsonl").read_text() == (pkg / "sample.jsonl").read_text()
    assert (ws / "ledger.yaml").read_text() == (pkg / "ledger.ollama.yaml").read_text()

def test_cli_top_level_help_is_truthful_umbrella(capsys):
    # The umbrella description must no longer claim "No live service calls."
    # now that `sample run` can make one, but must still credit the offline
    # analyze/report path.
    from batch_runner.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "No live service calls." not in out
    assert "Analyze recorded usage" in out
    assert "sample run" in out


def test_cli_sample_run_help_states_scope_and_cost(capsys):
    from batch_runner.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["sample", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out.lower()
    assert "sample" in out


# --------------------------------------------------------------------------
# Independent review blockers: regression coverage (HIGH1-6, MED7-15)
# --------------------------------------------------------------------------
class _CountingProvider:
    """Fake provider that counts construction and per-row calls."""

    name = "mock"
    built = 0

    def __init__(self) -> None:
        type(self).built += 1
        self.rows = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider="mock",
            billed=False,
            token_usage="synthetic",
            reasoning_tokens="not_supported",
            cached_tokens="not_supported",
        )

    def prepare(self) -> None:
        return None

    def run_row(self, row_id: str, repeat_index: int, prompt: str) -> OutputRecord:
        self.rows += 1
        return OutputRecord(
            row_id=row_id,
            repeat_index=repeat_index,
            provider="mock",
            model="mock-1",
            status="ok",
            latency_ms=1,
            response_text="ok",
        )


# --- HIGH1: hard ceiling is actually enforced pre-network -------------------
def test_high1_azure_preflight_refuses_when_estimate_exceeds_ceiling(tmp_path: Path):
    led_dict = _base_ledger("azure")
    # A $0.001 ceiling cannot cover 2 rows x 128 output tokens at the pinned
    # output rate, so the run must be refused before any provider call.
    led_dict["execution"]["cost"] = {
        "billed": True,
        "confirmed": True,
        "estimated_usd": 0.0,
        "hard_ceiling_usd": 0.001,
        "pricing_snapshot_id": "test-pricing-2026-03-08",
        "pricing_model": "gpt-5.2",
        "input_per_1m_usd": 2.0,
        "output_per_1m_usd": 20.0,
    }
    led = parse_ledger(led_dict)
    ws = _workspace(tmp_path)
    _CountingProvider.built = 0
    with pytest.raises(BudgetNotConfirmedError) as exc:
        run_ledger(
            led,
            base_dir=ws,
            environ={"AZURE_OPENAI_FOUNDRY_ENDPOINT": "https://h/openai/v1/"},
            confirm_cost=True,
            clock=_FIXED_CLOCK,
            provider_builder=lambda *_a, **_k: _CountingProvider(),
        )
    assert "ceiling" in str(exc.value).lower()
    assert _CountingProvider.built == 0  # refused BEFORE building a provider


def test_high1_within_ceiling_runs_and_records_preflight(tmp_path: Path):
    led = parse_ledger(_base_ledger("azure"))  # ceiling 1.0 -> ~0.03 est is fine
    ws = _workspace(tmp_path)
    plans: list[Any] = []
    run_ledger(
        led,
        base_dir=ws,
        environ={"AZURE_OPENAI_FOUNDRY_ENDPOINT": "https://h/openai/v1/"},
        confirm_cost=True,
        clock=_FIXED_CLOCK,
        provider_builder=lambda *_a, **_k: _CountingProvider(),
        preflight_sink=plans.append,
    )
    run_json = json.loads((_latest_run(tmp_path) / "run.json").read_text())
    pf = run_json["cost"]["preflight"]
    assert pf["within_ceiling"] is True
    assert pf["planned_requests"] == 2  # 2 rows x 1 repeat
    assert plans and plans[0].planned_requests == 2


def test_high1_preflight_uses_pinned_separate_rates_and_utf8_bound():
    from batch_runner.experiment.cost import estimate_azure_cost

    ledger = parse_ledger(_base_ledger("azure"))
    plan = estimate_azure_cost(ledger, ["é"])
    assert plan.estimated_input_tokens == 10  # two UTF-8 bytes + framing allowance
    assert plan.estimated_output_tokens == 128
    assert plan.input_rate_usd_per_1m_tokens == 2.0
    assert plan.output_rate_usd_per_1m_tokens == 20.0
    assert plan.estimated_usd == pytest.approx((10 * 2 + 128 * 20) / 1_000_000)


def test_high1_preflight_counts_each_repeat_once(tmp_path: Path):
    led_dict = _base_ledger("azure")
    led_dict["execution"]["repeats"] = 3
    led = parse_ledger(led_dict)
    plans: list[Any] = []
    result = run_ledger(
        led,
        base_dir=_workspace(tmp_path),
        environ={"AZURE_OPENAI_FOUNDRY_ENDPOINT": "https://h/openai/v1/"},
        confirm_cost=True,
        clock=_FIXED_CLOCK,
        random_hex=_FIXED_RANDOM,
        provider_builder=lambda *_a, **_k: _CountingProvider(),
        preflight_sink=plans.append,
    )
    assert result.ok_count == 6
    assert plans[0].planned_requests == 6


def test_high1_unpriced_azure_configuration_is_rejected():
    ledger = _base_ledger("azure")
    del ledger["execution"]["cost"]["output_per_1m_usd"]
    with pytest.raises(LedgerError):
        parse_ledger(ledger)


# --- HIGH2: Responses call is stateless (store=False) -----------------------
def test_high2_azure_sets_store_false(monkeypatch):
    prov, counters, _ = _azure_provider(monkeypatch)
    prov.prepare()
    prov.run_row("q1", 0, "hi")
    kwargs = counters["holder"]["create_kwargs"][0]
    assert kwargs["store"] is False


# --- HIGH3: endpoint URL hardening ------------------------------------------
def test_high3_azure_requires_https(monkeypatch):
    led = parse_ledger(_base_ledger("azure"))
    with pytest.raises(EndpointResolutionError):
        resolve_endpoint(
            led, environ={"AZURE_OPENAI_FOUNDRY_ENDPOINT": "http://h/openai/v1/"}
        )


def test_high3_endpoint_rejects_userinfo_query_fragment(monkeypatch):
    led = parse_ledger(_base_ledger("azure"))
    for bad in (
        "https://user:pw@h/openai/v1/",
        "https://h/openai/v1/?x=1",
        "https://h/openai/v1/#frag",
    ):
        with pytest.raises(EndpointResolutionError):
            resolve_endpoint(led, environ={"AZURE_OPENAI_FOUNDRY_ENDPOINT": bad})


def test_high3_openai_v1_base_is_idempotent():
    from batch_runner.experiment.providers.azure import _openai_v1_base

    assert _openai_v1_base("https://h") == "https://h/openai/v1/"
    assert _openai_v1_base("https://h/") == "https://h/openai/v1/"
    assert _openai_v1_base("https://h/openai/v1") == "https://h/openai/v1/"
    assert _openai_v1_base("https://h/openai/v1/") == "https://h/openai/v1/"


# --- HIGH4: owned output claimed before any provider/network call -----------
def test_high4_foreign_dir_fails_with_zero_provider_calls(tmp_path: Path):
    led = parse_ledger(_base_ledger("mock"))
    ws = _workspace(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("keep")
    _CountingProvider.built = 0
    with pytest.raises(ExperimentOutputConflict):
        run_ledger(
            led,
            base_dir=ws,
            out_dir=foreign,
            clock=_FIXED_CLOCK,
            provider_builder=lambda *_a, **_k: _CountingProvider(),
        )
    assert _CountingProvider.built == 0
    assert (foreign / "keep.txt").read_text() == "keep"


def test_high4_output_lock_is_held_until_the_run_finishes(tmp_path: Path):
    import threading

    led = parse_ledger(_base_ledger("mock"))
    ws = _workspace(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    class _BlockingProvider(_CountingProvider):
        def prepare(self) -> None:
            entered.set()
            assert release.wait(timeout=5)

    def first_run() -> None:
        try:
            run_ledger(
                led,
                base_dir=ws,
                clock=_FIXED_CLOCK,
                provider_builder=lambda *_a, **_k: _BlockingProvider(),
            )
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    thread = threading.Thread(target=first_run)
    thread.start()
    assert entered.wait(timeout=5)
    with pytest.raises(ExperimentOutputConflict, match="output lock"):
        run_ledger(
            led,
            base_dir=ws,
            clock=_FIXED_CLOCK,
            provider_builder=lambda *_a, **_k: _CountingProvider(),
        )
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not errors
    assert not (ws / ".reasoning-payoff-sample.lock").exists()


def test_high4_tampered_artifact_fails_before_provider_build(tmp_path: Path):
    led = parse_ledger(_base_ledger("mock"))
    ws = _workspace(tmp_path)
    run_ledger(led, base_dir=ws, clock=_FIXED_CLOCK)
    artifact = _latest_run(ws) / "run.json"
    artifact.unlink()
    artifact.mkdir()
    _CountingProvider.built = 0
    with pytest.raises(ExperimentOutputConflict, match="artifact path"):
        run_ledger(
            led,
            base_dir=ws,
            clock=_FIXED_CLOCK,
            provider_builder=lambda *_a, **_k: _CountingProvider(),
        )
    assert _CountingProvider.built == 0


def test_high4_symlinked_output_is_rejected_before_provider_build(tmp_path: Path):
    led = parse_ledger(_base_ledger("mock"))
    ws = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws / "out").symlink_to(outside, target_is_directory=True)
    _CountingProvider.built = 0
    with pytest.raises(ExperimentOutputConflict):
        run_ledger(
            led,
            base_dir=ws,
            clock=_FIXED_CLOCK,
            provider_builder=lambda *_a, **_k: _CountingProvider(),
        )
    assert _CountingProvider.built == 0
    assert not list(outside.iterdir())


# --- HIGH5: writes do not follow a planted symlink --------------------------
def test_high5_atomic_write_does_not_follow_symlink(tmp_path: Path):
    from batch_runner.experiment.runner import _atomic_write_text

    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    owned = tmp_path / "owned"
    owned.mkdir()
    link = owned / "run.json"
    link.symlink_to(outside)  # attacker plants a symlink at the target name
    _atomic_write_text(link, "safe")
    # The final path is replaced with a real file; the symlink target is intact.
    assert outside.read_text() == "original"
    assert not link.is_symlink()
    assert link.read_text() == "safe"


def test_high5_sequential_writes_do_not_collide(tmp_path: Path):
    from batch_runner.experiment.runner import _atomic_write_text

    target = tmp_path / "f.txt"
    _atomic_write_text(target, "one")
    _atomic_write_text(target, "two")
    assert target.read_text() == "two"
    # No leftover unpredictable temp files remain in the directory.
    assert [p.name for p in tmp_path.iterdir()] == ["f.txt"]


# --- HIGH6: SDK timeout + max_retries=0 reach the client --------------------
def test_high6_client_receives_timeout_and_no_retries(monkeypatch):
    prov, counters, _ = _azure_provider(monkeypatch)
    prov.prepare()
    ck = counters["holder"]["client_kwargs"]
    assert ck["timeout"] == 60.0
    assert ck["max_retries"] == 0


# --- MED7: only status=="completed" is a success ----------------------------
def _azure_provider_with_status(status: str | None):
    class _Resp:
        def __init__(self) -> None:
            self.output_text = "x"
            self.usage = _FakeUsage()
            self.status = status

    class _Responses:
        def create(self, **_k: Any) -> Any:
            return _Resp()

    def client_factory(*, base_url, api_key, **_k):  # noqa: ANN001
        c = type("C", (), {})()
        c.responses = _Responses()
        return c

    led = parse_ledger(_base_ledger("azure"))
    ep = ResolvedEndpoint(base_url="https://x/", source="env")
    return AzureFoundryProvider(
        ledger=led,
        endpoint=ep,
        capture_io=True,
        client_factory=client_factory,
        token_provider_factory=lambda: (lambda: "t"),
        environ={},
    )


def test_med7_failed_status_is_typed_failure():
    for status in (None, "", "failed", "cancelled", "incomplete", "in_progress"):
        prov = _azure_provider_with_status(status)
        prov.prepare()
        with pytest.raises(ResponseNotCompletedError):
            prov.run_row("q1", 0, "hi")


# --- MED8: Ollama redirect is blocked ---------------------------------------
def test_med8_ollama_redirect_is_blocked():
    led = parse_ledger(_base_ledger("ollama"))
    ep = ResolvedEndpoint(base_url="http://localhost:11434", source="endpoint.default")

    def transport(url: str, payload: bytes, timeout: float) -> tuple[int, bytes]:
        return 302, b""  # a 3xx that would bounce to another host

    prov = OllamaProvider(ledger=led, endpoint=ep, capture_io=True, transport=transport)
    with pytest.raises(ProviderUnavailableError):
        prov.run_row("q1", 0, "hi")


def test_med8_ollama_transport_disables_environment_proxies(monkeypatch):
    import urllib.request

    from batch_runner.experiment.providers.ollama import _urllib_transport

    handlers: list[Any] = []

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    class _Opener:
        def open(self, *_args: Any, **_kwargs: Any) -> _Response:
            return _Response()

    def fake_build_opener(*items: Any) -> _Opener:
        handlers.extend(items)
        return _Opener()

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
    assert _urllib_transport("http://localhost:11434/api/chat", b"{}", 1) == (
        200,
        b"{}",
    )
    proxy = next(item for item in handlers if isinstance(item, urllib.request.ProxyHandler))
    assert proxy.proxies == {}


# --- MED9: remote Ollama URL is never serialized ----------------------------
def test_med9_remote_ollama_url_is_redacted(tmp_path: Path):
    led = parse_ledger(_base_ledger("ollama"))
    ws = _workspace(tmp_path)
    env = {"OLLAMA_BASE_URL": "http://remote.example.com:11434"}
    run_ledger(
        led,
        base_dir=ws,
        environ=env,
        allow_remote_ollama=True,
        clock=_FIXED_CLOCK,
        provider_builder=lambda *_a, **_k: _CountingProvider(),
    )
    text = (_latest_run(tmp_path) / "run.json").read_text()
    assert "remote.example.com" not in text
    endpoint = json.loads(text)["endpoint"]
    assert "base_url" not in endpoint
    assert endpoint["locality"] == "remote"
    assert endpoint["remote_opt_in"] is True


# --- MED10: malformed ledger exits cleanly (no traceback/path) --------------
def test_med10_malformed_ledger_exits_three(tmp_path: Path, capsys):
    from batch_runner.cli import main

    bad = tmp_path / "ledger.yaml"
    bad.write_text("this: : : not valid : yaml\n\t- broken", encoding="utf-8")
    rc = main(["sample", "run", "--ledger", str(bad)])
    assert rc == 3
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert str(bad) not in err


# --- MED11: concurrency and artifacts are constrained -----------------------
def test_med11_rejects_concurrency_above_one():
    led = _base_ledger("mock")
    led["execution"]["concurrency"] = 2
    with pytest.raises(LedgerError):
        parse_ledger(led)


def test_med11_rejects_nonstandard_artifacts():
    led = _base_ledger("mock")
    led["output"]["artifacts"] = ["run.json", "records.jsonl"]  # missing summary
    with pytest.raises(LedgerError):
        parse_ledger(led)


# --- MED12: JSON Schema and runtime agree on a shared corpus ----------------
def _ledger_schema() -> dict[str, Any]:
    return json.loads(
        (_repo_root() / "schemas" / "experiment_ledger.v1.schema.json").read_text()
    )


def test_med12_schema_runtime_parity_valid_and_invalid():
    import jsonschema

    schema = _ledger_schema()
    validator = jsonschema.Draft7Validator(schema)
    # Valid docs: both accept.
    for provider in ("mock", "ollama", "azure"):
        doc = _base_ledger(provider)
        parse_ledger(doc)  # runtime accepts
        validator.validate(doc)  # schema accepts
    # Invalid: azure must use entra auth -> both reject.
    bad = _base_ledger("azure")
    bad["auth"] = {"mode": "none", "env_vars": []}
    with pytest.raises(LedgerError):
        parse_ledger(bad)
    assert list(validator.iter_errors(bad)), "schema must also reject azure+none auth"

    invalid_docs: list[dict[str, Any]] = []
    traversal = _base_ledger("mock")
    traversal["input"]["path"] = "../sample.jsonl"
    invalid_docs.append(traversal)
    custom_output = _base_ledger("mock")
    custom_output["output"]["dir"] = "../out"
    invalid_docs.append(custom_output)
    wrong_record_cap = _base_ledger("mock")
    wrong_record_cap["input"]["max_records"] = 49
    invalid_docs.append(wrong_record_cap)
    too_many_samples = _base_ledger("mock")
    too_many_samples["execution"]["max_samples"] = 51
    invalid_docs.append(too_many_samples)
    missing_id = _base_ledger("mock")
    del missing_id["input"]["row_shape"]["required_fields"]["id"]
    invalid_docs.append(missing_id)
    unpriced = _base_ledger("azure")
    del unpriced["execution"]["cost"]["input_per_1m_usd"]
    invalid_docs.append(unpriced)

    for doc in invalid_docs:
        with pytest.raises(LedgerError):
            parse_ledger(doc)
        assert list(validator.iter_errors(doc)), doc


# --- MED13: required non-empty, unique row ids ------------------------------
def _dataset_spec():
    return parse_ledger(_base_ledger("mock")).input


def test_med13_empty_input_rejected(tmp_path: Path):
    ws = _workspace(tmp_path, rows=[{"id": "a", "input": "   "}])
    with pytest.raises(DatasetError):
        load_dataset(ws / "sample.jsonl", _dataset_spec())


def test_med13_empty_id_rejected(tmp_path: Path):
    ws = _workspace(tmp_path, rows=[{"id": "", "input": "hi"}])
    with pytest.raises(DatasetError):
        load_dataset(ws / "sample.jsonl", _dataset_spec())


def test_med13_duplicate_id_rejected(tmp_path: Path):
    ws = _workspace(
        tmp_path, rows=[{"id": "a", "input": "hi"}, {"id": "a", "input": "yo"}]
    )
    with pytest.raises(DatasetError):
        load_dataset(ws / "sample.jsonl", _dataset_spec())


# --- MED14: workspace-local .gitignore covers out/ and .env -----------------
def test_med14_workspace_gitignore_present(tmp_path: Path):
    from batch_runner.cli import main

    ws = tmp_path / "custom-name-ws"
    assert main(["sample", "init", "--provider", "mock", "--out", str(ws)]) == 0
    gi = (ws / ".gitignore").read_text()
    assert "out/" in gi
    assert ".env" in gi


# --- MED15: help says a small run; run prints exact request count -----------
def test_med15_sample_help_says_small_run(capsys):
    from batch_runner.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out.lower()
    assert "small" in out


def test_med15_sample_run_prints_request_count(tmp_path: Path, monkeypatch, capsys):
    _no_network(monkeypatch)
    from batch_runner.cli import main

    ws = tmp_path / "ws"
    assert main(["sample", "init", "--provider", "mock", "--out", str(ws)]) == 0
    main(["sample", "run", "--ledger", str(ws / "ledger.yaml")])
    err = capsys.readouterr().err
    assert "request(s)" in err


# --------------------------------------------------------------------------
# Phase 2 provenance: immutable runs, complete manifest, and retry lineage
# --------------------------------------------------------------------------
def _tree_hashes(directory: Path) -> dict[str, str]:
    import hashlib

    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_sequential_runs_are_distinct_and_first_is_byte_immutable(tmp_path: Path):
    led = parse_ledger(_base_ledger("mock"))
    ws = _workspace(tmp_path)
    suffixes = iter(("00000001", "00000002"))
    first = run_ledger(
        led,
        base_dir=ws,
        clock=_FIXED_CLOCK,
        random_hex=lambda _n: next(suffixes),
    )
    first_hashes = _tree_hashes(first.out_dir)
    second = run_ledger(
        led,
        base_dir=ws,
        clock=_FIXED_CLOCK,
        random_hex=lambda _n: next(suffixes),
    )
    assert first.run_id != second.run_id
    assert _tree_hashes(first.out_dir) == first_hashes
    latest = json.loads((ws / "out" / "latest.json").read_text())
    assert latest["run_id"] == second.run_id
    assert not (ws / "out" / "latest.json").is_symlink()


def test_run_id_collision_refuses_before_provider_build(tmp_path: Path):
    led = parse_ledger(_base_ledger("mock"))
    ws = _workspace(tmp_path)
    run_ledger(
        led,
        base_dir=ws,
        clock=_FIXED_CLOCK,
        random_hex=_FIXED_RANDOM,
    )
    _CountingProvider.built = 0
    with pytest.raises(ExperimentOutputConflict, match="collision"):
        run_ledger(
            led,
            base_dir=ws,
            clock=_FIXED_CLOCK,
            random_hex=_FIXED_RANDOM,
            provider_builder=lambda *_a, **_k: _CountingProvider(),
        )
    assert _CountingProvider.built == 0


def test_latest_is_not_written_when_staged_publication_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import batch_runner.experiment.runner as runner

    led = parse_ledger(_base_ledger("mock"))
    ws = _workspace(tmp_path)
    original = runner._atomic_write_text

    def fail_manifest(path: Path, text: str) -> None:
        if path.name == "manifest.json":
            raise OSError("synthetic publication failure")
        original(path, text)

    monkeypatch.setattr(runner, "_atomic_write_text", fail_manifest)
    with pytest.raises(OSError):
        run_ledger(
            led,
            base_dir=ws,
            clock=_FIXED_CLOCK,
            random_hex=_FIXED_RANDOM,
        )
    assert not (ws / "out" / "latest.json").exists()
    assert {
        child.name
        for child in (ws / "out" / "runs").iterdir()
    } == {".reasoning-payoff-experiment-owned"}


def test_owned_stale_root_temp_files_are_recovered(tmp_path: Path):
    led = parse_ledger(_base_ledger("mock"))
    ws = _workspace(tmp_path)
    first = run_ledger(
        led,
        base_dir=ws,
        clock=_FIXED_CLOCK,
        random_hex=lambda _n: "00000001",
    )
    out = ws / "out"
    (out / ".latest.json.0123456789abcdef.tmp").write_text("partial")
    (out / ".reasoning-payoff-write-probe-fedcba9876543210").write_text("probe")
    second = run_ledger(
        led,
        base_dir=ws,
        clock=_FIXED_CLOCK,
        random_hex=lambda _n: "00000002",
    )
    assert second.run_id != first.run_id
    assert not (out / ".latest.json.0123456789abcdef.tmp").exists()
    assert not (out / ".reasoning-payoff-write-probe-fedcba9876543210").exists()


def test_latest_is_written_only_after_complete_run_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import batch_runner.experiment.runner as runner

    led = parse_ledger(_base_ledger("mock"))
    ws = _workspace(tmp_path)
    original = runner._atomic_write_text

    def observe_latest(path: Path, text: str) -> None:
        if path.name == "latest.json":
            run_id = json.loads(text)["run_id"]
            published = ws / "out" / "runs" / run_id
            assert {
                child.name for child in published.iterdir()
            } == {
                ".reasoning-payoff-experiment-owned",
                "run.json",
                "records.jsonl",
                "summary.md",
                "manifest.json",
                "artifacts.sha256",
            }
        original(path, text)

    monkeypatch.setattr(runner, "_atomic_write_text", observe_latest)
    run_ledger(
        led,
        base_dir=ws,
        clock=_FIXED_CLOCK,
        random_hex=_FIXED_RANDOM,
    )


def test_manifest_and_pointer_conform_and_checksums_match(tmp_path: Path):
    import hashlib

    import jsonschema

    led = parse_ledger(_base_ledger("mock"))
    result = run_ledger(
        led,
        base_dir=_workspace(tmp_path),
        clock=_FIXED_CLOCK,
        random_hex=_FIXED_RANDOM,
    )
    manifest = json.loads(result.manifest_path.read_text())
    pointer = json.loads(result.latest_path.read_text())
    manifest_schema = json.loads(
        (
            _repo_root() / "schemas" / "experiment_run_manifest.v1.schema.json"
        ).read_text()
    )
    pointer_schema = json.loads(
        (
            _repo_root() / "schemas" / "experiment_latest_pointer.v1.schema.json"
        ).read_text()
    )
    jsonschema.validate(manifest, manifest_schema)
    jsonschema.validate(pointer, pointer_schema)
    checksum_lines = result.artifacts_sha256_path.read_text().splitlines()
    checksums = dict(line.split("  ", 1)[::-1] for line in checksum_lines)
    assert set(checksums) == {
        "manifest.json",
        "records.jsonl",
        "run.json",
        "summary.md",
    }
    for name, expected in checksums.items():
        assert hashlib.sha256((result.out_dir / name).read_bytes()).hexdigest() == expected


def test_manifest_schema_rejects_missing_groups_and_privacy_field(tmp_path: Path):
    import copy

    import jsonschema

    led = parse_ledger(_base_ledger("mock"))
    result = run_ledger(
        led,
        base_dir=_workspace(tmp_path),
        clock=_FIXED_CLOCK,
        random_hex=_FIXED_RANDOM,
    )
    manifest = json.loads(result.manifest_path.read_text())
    schema = json.loads(
        (
            _repo_root() / "schemas" / "experiment_run_manifest.v1.schema.json"
        ).read_text()
    )
    for group in (
        "code",
        "runtime",
        "input",
        "provider",
        "pricing",
        "execution",
        "lineage",
        "artifacts",
    ):
        mutated = copy.deepcopy(manifest)
        del mutated[group]
        assert list(jsonschema.Draft7Validator(schema).iter_errors(mutated))
    mutated = copy.deepcopy(manifest)
    mutated["code"]["absolute_path"] = "/private/customer/repository"
    assert list(jsonschema.Draft7Validator(schema).iter_errors(mutated))


def test_manifest_git_unavailable_is_explicit_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import batch_runner.experiment.manifest as manifest_module

    def unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise OSError

    monkeypatch.setattr(manifest_module.subprocess, "run", unavailable)
    led = parse_ledger(_base_ledger("mock"))
    result = run_ledger(
        led,
        base_dir=_workspace(tmp_path),
        clock=_FIXED_CLOCK,
        random_hex=_FIXED_RANDOM,
    )
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["code"]["vcs_state"] == "unknown"
    assert manifest["code"]["commit_sha"] == "unknown"
    assert manifest["code"]["dirty"] == "unknown"
    assert manifest["runtime"]["dependency_lock"] == {
        "state": "unknown",
        "kind": "unknown",
        "sha256": "unknown",
    }


def test_manifest_verified_git_checkout_records_commit(
    monkeypatch: pytest.MonkeyPatch,
):
    import batch_runner.experiment.manifest as manifest_module

    monkeypatch.setattr(manifest_module, "_repository_root", _repo_root)
    identity = manifest_module._git_identity()
    lock = manifest_module._dependency_lock()
    assert identity["vcs_state"] == "available"
    assert len(identity["commit_sha"]) == 40
    assert isinstance(identity["dirty"], bool)
    assert lock["state"] == "available"
    assert len(lock["sha256"]) == 64


def test_manifest_privacy_boundary_omits_paths_hosts_and_row_ids(tmp_path: Path):
    led = parse_ledger(_base_ledger("azure"))
    private_root = tmp_path / "customer-alpha" / "users" / "local-name"
    ws = _workspace(
        private_root,
        rows=[{"id": "customer-alpha-request", "input": "private prompt"}],
    )
    result = run_ledger(
        led,
        base_dir=ws,
        environ={
            "AZURE_OPENAI_FOUNDRY_ENDPOINT": (
                "https://private-customer-host.example.com/openai/v1/"
            )
        },
        confirm_cost=True,
        clock=_FIXED_CLOCK,
        random_hex=_FIXED_RANDOM,
        provider_builder=lambda *_a, **_k: _CountingProvider(),
    )
    text = result.manifest_path.read_text()
    for forbidden in (
        str(tmp_path),
        "customer-alpha",
        "local-name",
        "private-customer-host.example.com",
        "customer-alpha-request",
        "private prompt",
    ):
        assert forbidden not in text


def test_retry_failed_creates_child_and_never_recalls_success(tmp_path: Path):
    led = parse_ledger(_base_ledger("mock"))
    ws = _workspace(tmp_path)
    parent = run_ledger(
        led,
        base_dir=ws,
        clock=_FIXED_CLOCK,
        random_hex=lambda _n: "00000001",
        provider_builder=lambda *_a, **_k: _FlakyProvider("q2"),
    )
    parent_hashes = _tree_hashes(parent.out_dir)

    class _TrackingProvider(_CountingProvider):
        def __init__(self) -> None:
            super().__init__()
            self.called: list[tuple[str, int]] = []

        def run_row(
            self, row_id: str, repeat_index: int, prompt: str
        ) -> OutputRecord:
            self.called.append((row_id, repeat_index))
            return super().run_row(row_id, repeat_index, prompt)

    provider = _TrackingProvider()
    child = retry_failed_run(
        led,
        base_dir=ws,
        parent_run_id=parent.run_id,
        clock=_FIXED_CLOCK,
        random_hex=lambda _n: "00000002",
        provider_builder=lambda *_a, **_k: provider,
    )
    assert provider.called == [("q2", 0)]
    assert _tree_hashes(parent.out_dir) == parent_hashes
    child_run = json.loads(child.run_json_path.read_text())
    child_manifest = json.loads(child.manifest_path.read_text())
    assert child_run["lineage"]["parent_run_id"] == parent.run_id
    assert child_manifest["lineage"] == {
        "kind": "retry_failed",
        "parent_run_id": parent.run_id,
        "retried_failed_count": 1,
    }


def test_cli_retry_failed_writes_only_failed_parent_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    _no_network(monkeypatch)
    from batch_runner.cli import main
    from batch_runner.experiment.ledger import load_ledger

    ws = tmp_path / "ws"
    assert main(["sample", "init", "--provider", "mock", "--out", str(ws)]) == 0
    led = load_ledger(ws / "ledger.yaml")
    parent = run_ledger(
        led,
        base_dir=ws,
        clock=_FIXED_CLOCK,
        random_hex=lambda _n: "00000001",
        provider_builder=lambda *_a, **_k: _FlakyProvider("q2"),
    )
    capsys.readouterr()
    assert (
        main(
            [
                "sample",
                "retry-failed",
                "--ledger",
                str(ws / "ledger.yaml"),
                "--parent-run-id",
                parent.run_id,
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    records = [
        json.loads(line)
        for line in (
            ws / payload["records"]
        ).read_text().splitlines()
    ]
    assert [(record["row_id"], record["repeat_index"]) for record in records] == [
        ("q2", 0)
    ]


def test_retry_refuses_tampered_parent_and_successful_parent(tmp_path: Path):
    led = parse_ledger(_base_ledger("mock"))
    ws = _workspace(tmp_path)
    successful = run_ledger(
        led,
        base_dir=ws,
        clock=_FIXED_CLOCK,
        random_hex=lambda _n: "00000001",
    )
    with pytest.raises(ExperimentOutputConflict, match="no failed rows"):
        retry_failed_run(led, base_dir=ws, parent_run_id=successful.run_id)

    partial = run_ledger(
        led,
        base_dir=ws,
        clock=_FIXED_CLOCK,
        random_hex=lambda _n: "00000002",
        provider_builder=lambda *_a, **_k: _FlakyProvider("q2"),
    )
    partial.run_json_path.write_text("{}")
    with pytest.raises(ExperimentOutputConflict, match="checksum mismatch"):
        retry_failed_run(led, base_dir=ws, parent_run_id=partial.run_id)


def test_retry_rejects_non_ascii_checksum_without_leaking_traceback(tmp_path: Path):
    led = parse_ledger(_base_ledger("mock"))
    ws = _workspace(tmp_path)
    parent = run_ledger(
        led,
        base_dir=ws,
        clock=_FIXED_CLOCK,
        random_hex=_FIXED_RANDOM,
        provider_builder=lambda *_a, **_k: _FlakyProvider("q2"),
    )
    parent.artifacts_sha256_path.write_bytes(b"\xff")
    with pytest.raises(ExperimentOutputConflict, match="checksum file is invalid"):
        retry_failed_run(led, base_dir=ws, parent_run_id=parent.run_id)
