from __future__ import annotations

import datetime as dt
import json
import socket
import subprocess
import sys
from types import ModuleType
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft7Validator
from pydantic import ValidationError

from batch_runner.experiment.providers import azure
from batch_runner.experiment.record import ApiCompatibilityError, QuotaExceededError
from batch_runner.protected_smoke import (
    ENDPOINT_ENV_NAME,
    FIXED_PROMPT,
    PROTECTED_ENVIRONMENT,
    PROTECTED_WORKFLOW,
    REPOSITORY,
    RUNNER_CLASS,
    ProtectedContextError,
    ProtectedSmokeHealth,
    run_live,
    run_offline_fake,
    validate_health_privacy,
    validate_live_context,
)
from batch_runner import protected_smoke

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/protected-azure-smoke.yml"
SCHEMA = ROOT / "schemas/protected_azure_smoke_health.v1.schema.json"


def _head() -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _live_env(tmp_path: Path) -> dict[str, str]:
    return {
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "schedule",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REF_TYPE": "branch",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_WORKFLOW_REF": PROTECTED_WORKFLOW,
        "GITHUB_SHA": _head(),
        "RUNNER_ENVIRONMENT": "self-hosted",
        "RUNNER_TEMP": str(tmp_path),
        "PROTECTED_AZURE_SMOKE_ENVIRONMENT": PROTECTED_ENVIRONMENT,
        "PROTECTED_AZURE_SMOKE_RUNNER_CLASS": RUNNER_CLASS,
        "PROTECTED_AZURE_SMOKE_ENDPOINT_ENV": ENDPOINT_ENV_NAME,
        "PROTECTED_AZURE_SMOKE_OUTPUT_DIR": str(
            tmp_path / "protected-azure-smoke"
        ),
        ENDPOINT_ENV_NAME: (
            "https://unit-test-resource.services.ai.azure.com/openai/v1/"
        ),
        "PROTECTED_AZURE_SMOKE_DEPLOYMENT": "private-deployment",
        "AZURE_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
    }


def test_offline_fake_uses_exact_one_call_contract_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access was attempted")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    output = tmp_path / "health"
    path, health = run_offline_fake(
        output,
        repo_root=ROOT,
        today=dt.date(2026, 8, 30),
        clock=lambda: 1_788_067_200,
    )

    assert health.status == "ok"
    assert health.execution.model_dump() == {
        "mode": "offline_fake",
        "requests_planned": 1,
        "requests_attempted": 1,
        "capture_io": False,
        "store": False,
        "max_retries": 0,
        "timeout_seconds": 30,
        "max_output_tokens": 32,
        "cost_confirmed_in_contract": True,
        "cost_confirmed_by_entrypoint": True,
        "hard_ceiling_usd": 0.001,
    }
    assert health.metrics.input_tokens == 7
    assert health.metrics.output_tokens == 1
    assert path == output / "health.json"
    assert [item.name for item in output.iterdir()] == ["health.json"]
    assert not (tmp_path / ".protected-azure-smoke-work").exists()
    assert FIXED_PROMPT not in path.read_text(encoding="utf-8")


def test_offline_fake_is_hermetic_under_far_future_wall_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FarFutureDate(dt.date):
        @classmethod
        def today(cls) -> FarFutureDate:
            return cls(2035, 1, 1)

    monkeypatch.setattr(protected_smoke.dt, "date", FarFutureDate)
    _, health = run_offline_fake(tmp_path / "health", repo_root=ROOT)
    assert health.status == "ok"


def test_health_conforms_to_json_schema_and_privacy_contract(tmp_path: Path) -> None:
    path, health = run_offline_fake(
        tmp_path / "health",
        repo_root=ROOT,
        today=dt.date(2026, 8, 30),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    Draft7Validator(json.loads(SCHEMA.read_text(encoding="utf-8"))).validate(payload)
    assert payload == health.model_dump(mode="json")
    serialized = json.dumps(payload)
    for forbidden in (
        "private-deployment",
        "offline.invalid",
        "request_id",
        "trace_id",
        "access_token",
        "AZURE_CLIENT_ID",
        str(tmp_path),
        FIXED_PROMPT,
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("prompt",), "do not export"),
        (("endpoint",), "https://host.example"),
        (("absolute_path",), "/private/work"),
        (("access_token",), "Bearer secret-value"),
    ],
)
def test_health_rejects_forbidden_field_mutations(
    tmp_path: Path, path: tuple[str], value: str
) -> None:
    _, health = run_offline_fake(
        tmp_path / "health",
        repo_root=ROOT,
        today=dt.date(2026, 8, 30),
    )
    payload = health.model_dump(mode="json")
    payload[path[0]] = value
    with pytest.raises(ValidationError):
        ProtectedSmokeHealth.model_validate(payload)
    with pytest.raises(ValueError, match="forbidden"):
        validate_health_privacy(payload)


@pytest.mark.parametrize(
    "value",
    [
        "https://private-name.example",
        "Bearer secret-value",
        "client_secret=secret-value",
        "sk-secretvalue",
        "/private/absolute/path",
    ],
)
def test_health_rejects_secret_endpoint_and_path_value_shapes(
    tmp_path: Path, value: str
) -> None:
    _, health = run_offline_fake(
        tmp_path / "health",
        repo_root=ROOT,
        today=dt.date(2026, 8, 30),
    )
    payload = health.model_dump(mode="json")
    payload["pricing"]["snapshot_id"] = value
    with pytest.raises(ValueError, match="forbidden"):
        validate_health_privacy(payload)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_REF", "refs/pull/123/merge"),
        ("GITHUB_REPOSITORY", "fork/when-reasoning-pays-off"),
        ("GITHUB_WORKFLOW_REF", "unapproved/workflow@refs/heads/main"),
        ("RUNNER_ENVIRONMENT", "github-hosted"),
        ("PROTECTED_AZURE_SMOKE_ENVIRONMENT", "ordinary-ci"),
        ("PROTECTED_AZURE_SMOKE_RUNNER_CLASS", "ordinary-runner"),
        ("PROTECTED_AZURE_SMOKE_ENDPOINT_ENV", "OTHER_ENDPOINT"),
        ("AZURE_CLIENT_SECRET", "secret"),
    ],
)
def test_live_context_mutations_fail_closed(
    tmp_path: Path, name: str, value: str
) -> None:
    env = _live_env(tmp_path)
    env[name] = value
    with pytest.raises(ProtectedContextError):
        validate_live_context(env, repo_root=ROOT)


def test_live_context_requires_exact_checkout_and_output_boundary(
    tmp_path: Path,
) -> None:
    env = _live_env(tmp_path)
    env["GITHUB_SHA"] = "a" * 40
    with pytest.raises(ProtectedContextError, match="checkout"):
        validate_live_context(env, repo_root=ROOT)

    env = _live_env(tmp_path)
    env["PROTECTED_AZURE_SMOKE_OUTPUT_DIR"] = str(ROOT / "health")
    with pytest.raises(ProtectedContextError, match="output"):
        validate_live_context(env, repo_root=ROOT)


def test_live_guard_precedes_identity_and_provider_work(tmp_path: Path) -> None:
    calls: list[str] = []
    env = _live_env(tmp_path)
    env["GITHUB_EVENT_NAME"] = "pull_request"
    with pytest.raises(ProtectedContextError):
        run_live(
            repo_root=ROOT,
            environ=env,
            identity_probe=lambda _client_id: calls.append("identity"),
        )
    assert calls == []
    assert not (tmp_path / "protected-azure-smoke").exists()


def test_managed_identity_failure_exports_typed_health_only(tmp_path: Path) -> None:
    def unavailable(_client_id: str) -> None:
        from batch_runner.protected_smoke import ManagedIdentityError

        raise ManagedIdentityError("not exported")

    path, health = run_live(
        repo_root=ROOT,
        environ=_live_env(tmp_path),
        today=dt.date(2026, 8, 30),
        identity_probe=unavailable,
    )
    assert health.status == "failed"
    assert health.failure_class == "managed_identity"
    assert health.execution.requests_attempted == 0
    assert [item.name for item in path.parent.iterdir()] == ["health.json"]
    assert "not exported" not in path.read_text(encoding="utf-8")


def test_post_guard_context_failure_exports_typed_health(tmp_path: Path) -> None:
    (tmp_path / ".protected-azure-smoke-work").mkdir()
    path, health = run_live(
        repo_root=ROOT,
        environ=_live_env(tmp_path),
        today=dt.date(2026, 8, 30),
        identity_probe=lambda _client_id: None,
    )
    assert health.status == "failed"
    assert health.failure_class == "protected_context"
    assert health.execution.requests_attempted == 0
    assert path.name == "health.json"


def test_default_credential_chain_is_managed_identity_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class ManagedIdentityCredential:
        pass

    class DefaultAzureCredential:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.credentials = (ManagedIdentityCredential(),)

    identity = ModuleType("azure.identity")
    identity.DefaultAzureCredential = DefaultAzureCredential  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    monkeypatch.setattr(protected_smoke, "require_extra", lambda _extra: None)

    credential = protected_smoke._managed_identity_credential(
        "00000000-0000-0000-0000-000000000000"
    )
    assert isinstance(credential, DefaultAzureCredential)
    assert captured["managed_identity_client_id"] == (
        "00000000-0000-0000-0000-000000000000"
    )
    assert all(
        captured[name] is True
        for name in (
            "exclude_environment_credential",
            "exclude_workload_identity_credential",
            "exclude_shared_token_cache_credential",
            "exclude_visual_studio_code_credential",
            "exclude_cli_credential",
            "exclude_powershell_credential",
            "exclude_developer_cli_credential",
            "exclude_interactive_browser_credential",
            "exclude_broker_credential",
        )
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, QuotaExceededError), (400, ApiCompatibilityError), (422, ApiCompatibilityError)],
)
def test_azure_failure_taxonomy(status: int, expected: type[Exception]) -> None:
    error = type("SdkError", (Exception,), {"status_code": status})("sensitive")
    classified = azure._classify_error(error)
    assert isinstance(classified, expected)
    assert "sensitive" not in str(classified)


def test_protected_workflow_is_separate_least_privilege_contract() -> None:
    data = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert set(data["on"]) == {"schedule", "workflow_dispatch"}
    assert data["permissions"] == {"contents": "read"}
    assert data["concurrency"]["cancel-in-progress"] == "false"
    job = data["jobs"]["live-smoke"]
    assert job["environment"] == PROTECTED_ENVIRONMENT
    assert job["runs-on"] == [
        "self-hosted",
        "linux",
        "x64",
        "azure",
        "managed-identity",
        "protected-smoke",
    ]
    assert job["timeout-minutes"] == "10"
    assert "refs/heads/main" in job["if"]
    assert "github.repository" in job["if"]
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "pull_request" not in text
    assert "id-token: write" not in text
    assert "retention-days: 3" in text
    assert "protected-azure-smoke/health.json" in text
    assert "run_protected_azure_smoke --live" in text


def test_public_ci_invokes_only_offline_fake() -> None:
    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "run_protected_azure_smoke" in text
    assert "--offline-fake" in text
    protected_line = "python -m scripts.run_protected_azure_smoke --live"
    assert protected_line not in text
