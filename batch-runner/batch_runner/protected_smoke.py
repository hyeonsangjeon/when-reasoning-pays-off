"""Protected Azure live smoke with an offline, exact-orchestration fake mode."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from batch_runner import __version__
from batch_runner.experiment.ledger import parse_ledger
from batch_runner.experiment.manifest import canonical_json
from batch_runner.experiment.providers.azure import (
    FOUNDRY_AUDIENCE,
    AzureFoundryProvider,
)
from batch_runner.experiment.providers.base import ResolvedEndpoint
from batch_runner.experiment.record import BudgetNotConfirmedError, ProviderError
from batch_runner.experiment.runner import RunResult, run_ledger
from batch_runner.optional_dependencies import OptionalDependencyError, require_extra
from scripts._azure_pricing import (
    CANONICAL_PAYG_SNAPSHOT_ID,
    CANONICAL_PAYG_SNAPSHOT_PATH,
    CANONICAL_PAYG_SNAPSHOT_SHA256,
    LIVE_MEASUREMENT,
    PricingPolicyError,
    PricingSelectionError,
    verify_campaign_pricing,
)

HEALTH_SCHEMA_VERSION = "1.0.0"
PROTECTED_ENVIRONMENT = "protected-azure-smoke"
PROTECTED_WORKFLOW = (
    "hyeonsangjeon/when-reasoning-pays-off/"
    ".github/workflows/protected-azure-smoke.yml@refs/heads/main"
)
REPOSITORY = "hyeonsangjeon/when-reasoning-pays-off"
ENDPOINT_ENV_NAME = "AZURE_OPENAI_FOUNDRY_ENDPOINT"
DEPLOYMENT_ENV_NAME = "PROTECTED_AZURE_SMOKE_DEPLOYMENT"
RUNNER_CLASS = "azure-managed-identity"
FIXED_PROMPT = "Reply with exactly OK."
TIMEOUT_SECONDS = 30
MAX_OUTPUT_TOKENS = 32
HARD_CEILING_USD = 0.001
PRICING_SNAPSHOT_DATE = dt.date(2026, 8, 30)

FailureClass = Literal[
    "protected_context",
    "managed_identity",
    "pricing",
    "cost_guard",
    "authentication",
    "quota",
    "deployment",
    "api_compatibility",
    "timeout",
    "provider",
    "response",
    "runtime_dependency",
    "internal",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, protected_namespaces=())


class CodeHealth(_Strict):
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    package_version: str = Field(pattern=r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")
    package_fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuntimeHealth(_Strict):
    python: str = Field(pattern=r"^CPython-\d+\.\d+\.\d+$")
    os: Literal["Linux", "macOS", "Windows"]
    architecture: Literal["x86_64", "aarch64"]
    fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProviderHealth(_Strict):
    model_family: Literal["gpt-5.2"]
    model_version: Literal["2025-12-11"]
    deployment_type: Literal["Global Standard"]
    region_class: Literal["global"]


class PricingHealth(_Strict):
    snapshot_id: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,119}$")
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MetricsHealth(_Strict):
    latency_ms: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class ExecutionHealth(_Strict):
    mode: Literal["live", "offline_fake"]
    requests_planned: Literal[1]
    requests_attempted: Literal[0, 1]
    capture_io: Literal[False]
    store: Literal[False]
    max_retries: Literal[0]
    timeout_seconds: Literal[30]
    max_output_tokens: Literal[32]
    cost_confirmed_in_contract: Literal[True]
    cost_confirmed_by_entrypoint: Literal[True]
    hard_ceiling_usd: Literal[0.001]


class ProtectedSmokeHealth(_Strict):
    schema_version: Literal["1.0.0"]
    status: Literal["ok", "failed"]
    observed_at_utc: str = Field(
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
    )
    code: CodeHealth
    runtime: RuntimeHealth
    provider: ProviderHealth
    pricing: PricingHealth
    metrics: MetricsHealth
    execution: ExecutionHealth
    failure_class: FailureClass | None

    @model_validator(mode="after")
    def _status_matches_failure(self) -> ProtectedSmokeHealth:
        if (self.status == "ok") != (self.failure_class is None):
            raise ValueError("status and failure_class disagree")
        if self.status == "ok" and self.execution.requests_attempted != 1:
            raise ValueError("successful smoke must attempt exactly one request")
        return self

    @field_validator("observed_at_utc")
    @classmethod
    def _valid_utc(cls, value: str) -> str:
        dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        return value


class ProtectedContextError(RuntimeError):
    """A value-free protected-runtime refusal."""


class ManagedIdentityError(RuntimeError):
    """Managed identity could not be proven without exposing its details."""


@dataclass(frozen=True)
class ProtectedContext:
    output_dir: Path
    commit_sha: str
    managed_identity_client_id: str


class _FakeResponse:
    status = "completed"
    output_text = "OK"
    usage = {
        "input_tokens": 7,
        "output_tokens": 1,
        "total_tokens": 8,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 0},
    }


class _OfflineHarness:
    def __init__(self) -> None:
        self.client_builds = 0
        self.token_factories = 0
        self.calls: list[dict[str, Any]] = []
        self.client_options: dict[str, Any] = {}

    def token_provider_factory(self) -> Callable[[], str]:
        self.token_factories += 1
        return lambda: "offline-token-not-exported"

    def client_factory(self, **kwargs: Any) -> Any:
        self.client_builds += 1
        self.client_options = {
            "timeout": kwargs["timeout"],
            "max_retries": kwargs["max_retries"],
        }
        harness = self

        class Responses:
            @staticmethod
            def create(**request: Any) -> _FakeResponse:
                harness.calls.append(request)
                return _FakeResponse()

        return type("OfflineClient", (), {"responses": Responses()})()

    def assert_contract(self) -> None:
        if (
            self.client_builds != 1
            or self.token_factories != 1
            or len(self.calls) != 1
            or self.client_options
            != {"timeout": float(TIMEOUT_SECONDS), "max_retries": 0}
            or self.calls[0].get("store") is not False
            or self.calls[0].get("max_output_tokens") != MAX_OUTPUT_TOKENS
            or self.calls[0].get("input") != FIXED_PROMPT
        ):
            raise RuntimeError("offline fake orchestration contract drifted")


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _safe_runtime() -> RuntimeHealth:
    machine = platform.machine().lower()
    architecture = {
        "amd64": "x86_64",
        "arm64": "aarch64",
    }.get(machine, machine)
    if architecture not in {"x86_64", "aarch64"}:
        raise ProtectedContextError("unsupported protected runner architecture")
    system = platform.system()
    safe_system = "macOS" if system == "Darwin" else system
    if safe_system not in {"Linux", "macOS", "Windows"}:
        raise ProtectedContextError("unsupported protected runner operating system")
    identity = {
        "python": f"CPython-{platform.python_version()}",
        "os": safe_system,
        "architecture": architecture,
    }
    return RuntimeHealth(**identity, fingerprint_sha256=_sha256(identity))


def _safe_code(commit_sha: str) -> CodeHealth:
    versions: dict[str, str] = {}
    for package in ("azure-identity", "openai", "pydantic", "pyyaml"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "absent"
    identity = {"package_version": __version__, "dependencies": versions}
    return CodeHealth(
        commit_sha=commit_sha,
        package_version=__version__,
        package_fingerprint_sha256=_sha256(identity),
    )


def _require_exact(env: dict[str, str], name: str, expected: str) -> None:
    if env.get(name) != expected:
        raise ProtectedContextError("protected runtime marker was not satisfied")


def _approved_endpoint(value: str) -> bool:
    try:
        parts = urlsplit(value)
        hostname = (parts.hostname or "").lower()
        return (
            parts.scheme == "https"
            and not parts.username
            and not parts.password
            and parts.port is None
            and not parts.query
            and not parts.fragment
            and hostname.endswith((".openai.azure.com", ".services.ai.azure.com"))
            and parts.path.rstrip("/") in {"", "/openai/v1"}
        )
    except ValueError:
        return False


def validate_live_context(
    env: dict[str, str], *, repo_root: Path
) -> ProtectedContext:
    """Prove the main-only protected GitHub/Azure execution boundary."""

    _require_exact(env, "GITHUB_ACTIONS", "true")
    if env.get("GITHUB_EVENT_NAME") not in {"schedule", "workflow_dispatch"}:
        raise ProtectedContextError("protected smoke trigger was not approved")
    _require_exact(env, "GITHUB_REF", "refs/heads/main")
    _require_exact(env, "GITHUB_REF_TYPE", "branch")
    _require_exact(env, "GITHUB_REPOSITORY", REPOSITORY)
    _require_exact(env, "GITHUB_WORKFLOW_REF", PROTECTED_WORKFLOW)
    _require_exact(env, "RUNNER_ENVIRONMENT", "self-hosted")
    _require_exact(
        env, "PROTECTED_AZURE_SMOKE_ENVIRONMENT", PROTECTED_ENVIRONMENT
    )
    _require_exact(env, "PROTECTED_AZURE_SMOKE_RUNNER_CLASS", RUNNER_CLASS)
    _require_exact(
        env, "PROTECTED_AZURE_SMOKE_ENDPOINT_ENV", ENDPOINT_ENV_NAME
    )
    sha = env.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ProtectedContextError("protected smoke commit marker was invalid")
    try:
        head = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise ProtectedContextError("protected smoke checkout could not be verified") from None
    if head != sha:
        raise ProtectedContextError("protected smoke checkout did not match the event")

    for name in (
        "AZURE_OPENAI_API_KEY",
        "OPENAI_API_KEY",
        "AZURE_CLIENT_SECRET",
        "AZURE_CLIENT_CERTIFICATE_PATH",
        "AZURE_USERNAME",
        "AZURE_PASSWORD",
    ):
        if env.get(name):
            raise ProtectedContextError("credential-shaped authentication input was present")
    endpoint = env.get(ENDPOINT_ENV_NAME, "")
    deployment = env.get(DEPLOYMENT_ENV_NAME, "")
    client_id = env.get("AZURE_CLIENT_ID", "")
    if not endpoint or not deployment or not client_id:
        raise ProtectedContextError("protected smoke configuration was incomplete")
    if not _approved_endpoint(endpoint):
        raise ProtectedContextError("protected smoke endpoint was not approved")
    if not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        client_id,
    ):
        raise ProtectedContextError("managed identity configuration was invalid")

    runner_temp = Path(env.get("RUNNER_TEMP", ""))
    output = Path(env.get("PROTECTED_AZURE_SMOKE_OUTPUT_DIR", ""))
    if (
        not runner_temp.is_absolute()
        or runner_temp.is_symlink()
        or not runner_temp.is_dir()
        or output != runner_temp / "protected-azure-smoke"
        or output.is_symlink()
        or repo_root.resolve() == output.resolve()
        or repo_root.resolve() in output.resolve().parents
    ):
        raise ProtectedContextError("protected smoke output directory was not approved")
    return ProtectedContext(output, sha, client_id)


def _managed_identity_credential(client_id: str) -> Any:
    require_extra("azure")
    from azure.identity import DefaultAzureCredential  # noqa: PLC0415

    credential = DefaultAzureCredential(
        managed_identity_client_id=client_id,
        exclude_environment_credential=True,
        exclude_workload_identity_credential=True,
        exclude_shared_token_cache_credential=True,
        exclude_visual_studio_code_credential=True,
        exclude_cli_credential=True,
        exclude_powershell_credential=True,
        exclude_developer_cli_credential=True,
        exclude_interactive_browser_credential=True,
        exclude_broker_credential=True,
    )
    chain = getattr(credential, "credentials", ())
    if [type(item).__name__ for item in chain] != ["ManagedIdentityCredential"]:
        close = getattr(credential, "close", None)
        if callable(close):
            close()
        raise ManagedIdentityError("managed identity credential chain was not exclusive")
    return credential


def probe_managed_identity(client_id: str) -> None:
    credential = _managed_identity_credential(client_id)
    try:
        credential.get_token(FOUNDRY_AUDIENCE)
    except Exception:
        raise ManagedIdentityError("managed identity was unavailable") from None
    finally:
        close = getattr(credential, "close", None)
        if callable(close):
            close()


def _managed_identity_token_provider_factory(
    client_id: str,
) -> Callable[[], Callable[[], str]]:
    def factory() -> Callable[[], str]:
        from azure.identity import get_bearer_token_provider  # noqa: PLC0415

        return get_bearer_token_provider(
            _managed_identity_credential(client_id), FOUNDRY_AUDIENCE
        )

    return factory


def _ledger(deployment: str) -> Any:
    return parse_ledger(
        {
            "schema_version": "1.1.0",
            "experiment": {
                "id": "protected-azure-live-smoke",
                "purpose": "One bounded provider health request outside public CI.",
            },
            "provider": "azure",
            "model": deployment,
            "endpoint": {"env_var": ENDPOINT_ENV_NAME},
            "auth": {"mode": "entra", "env_vars": ["AZURE_CLIENT_ID"]},
            "input": {
                "path": "sample.jsonl",
                "format": "jsonl",
                "row_shape": {
                    "required_fields": {"id": "string", "input": "string"},
                    "optional_fields": {},
                },
                "max_records": 50,
                "sample_selector": "first",
            },
            "execution": {
                "max_samples": 1,
                "concurrency": 1,
                "timeout_seconds": TIMEOUT_SECONDS,
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "repeats": 1,
                "reasoning_effort": "none",
                "capture_io": False,
                "cost": {
                    "billed": True,
                    "confirmed": True,
                    "estimated_usd": 0.0,
                    "hard_ceiling_usd": HARD_CEILING_USD,
                    "pricing": {
                        "snapshot_id": CANONICAL_PAYG_SNAPSHOT_ID,
                        "snapshot_path": CANONICAL_PAYG_SNAPSHOT_PATH,
                        "snapshot_sha256": CANONICAL_PAYG_SNAPSHOT_SHA256,
                        "price_key": (
                            "azure-openai:gpt-5.2:2025-12-11:"
                            "global:global-standard"
                        ),
                        "model_family": "gpt-5.2",
                        "model_version": "2025-12-11",
                        "geography": "global",
                        "region": "global",
                        "deployment_type": "Global Standard",
                        "currency": "USD",
                    },
                },
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
            "provenance": {
                "method_id": "protected-azure-smoke",
                "method_version": "1.0.0",
            },
        }
    )


def _record(result: RunResult) -> dict[str, Any]:
    lines = result.records_path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1:
        raise RuntimeError("protected smoke did not produce exactly one record")
    value = json.loads(lines[0])
    if not isinstance(value, dict):
        raise RuntimeError("protected smoke record was invalid")
    return value


def _failure_class(error_type: str | None) -> FailureClass:
    return {
        "authentication_failed": "authentication",
        "quota_exceeded": "quota",
        "model_unavailable": "deployment",
        "api_compatibility": "api_compatibility",
        "timeout": "timeout",
        "bad_response": "response",
        "response_not_completed": "response",
    }.get(error_type or "", "provider")  # type: ignore[return-value]


def validate_health_privacy(payload: dict[str, Any]) -> None:
    """Reject fields and values outside the health-only disclosure contract."""

    forbidden_keys = {
        "prompt",
        "request",
        "response",
        "request_id",
        "trace_id",
        "endpoint",
        "endpoint_hostname",
        "deployment_alias",
        "access_token",
        "subscription_id",
        "tenant_id",
        "resource_id",
        "username",
        "absolute_path",
        "environment",
        "environment_contents",
    }
    secret_patterns = (
        re.compile(r"https?://", re.IGNORECASE),
        re.compile(r"\bBearer\s+\S+", re.IGNORECASE),
        re.compile(r"\b(?:api[_-]?key|client[_-]?secret)\s*[:=]", re.IGNORECASE),
        re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
        re.compile(r"^[A-Za-z]:[\\/]|^/"),
        re.compile(r"\.openai\.azure\.com\b", re.IGNORECASE),
    )

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in forbidden_keys:
                    raise ValueError("health payload contains a forbidden field")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str) and any(pattern.search(value) for pattern in secret_patterns):
            raise ValueError("health payload contains a forbidden value shape")

    walk(payload)


def _write_health(output_dir: Path, health: ProtectedSmokeHealth) -> Path:
    payload_dict = health.model_dump(mode="json")
    validate_health_privacy(payload_dict)
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    path = output_dir / "health.json"
    payload = json.dumps(payload_dict, indent=2, sort_keys=True) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def _health(
    *,
    mode: Literal["live", "offline_fake"],
    commit_sha: str,
    now: float,
    attempted: int,
    record: dict[str, Any] | None,
    failure_class: FailureClass | None,
) -> ProtectedSmokeHealth:
    success = failure_class is None
    metrics = MetricsHealth(
        **{
            key: record.get(key) if record else None
            for key in (
                "latency_ms",
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cached_tokens",
                "total_tokens",
            )
        }
    )
    return ProtectedSmokeHealth(
        schema_version=HEALTH_SCHEMA_VERSION,
        status="ok" if success else "failed",
        observed_at_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        code=_safe_code(commit_sha),
        runtime=_safe_runtime(),
        provider=ProviderHealth(
            model_family="gpt-5.2",
            model_version="2025-12-11",
            deployment_type="Global Standard",
            region_class="global",
        ),
        pricing=PricingHealth(
            snapshot_id=CANONICAL_PAYG_SNAPSHOT_ID,
            snapshot_sha256=CANONICAL_PAYG_SNAPSHOT_SHA256,
        ),
        metrics=metrics,
        execution=ExecutionHealth(
            mode=mode,
            requests_planned=1,
            requests_attempted=attempted,
            capture_io=False,
            store=False,
            max_retries=0,
            timeout_seconds=TIMEOUT_SECONDS,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            cost_confirmed_in_contract=True,
            cost_confirmed_by_entrypoint=True,
            hard_ceiling_usd=HARD_CEILING_USD,
        ),
        failure_class=failure_class,
    )


def _execute(
    *,
    mode: Literal["live", "offline_fake"],
    output_dir: Path,
    commit_sha: str,
    deployment: str,
    env: dict[str, str],
    today: dt.date,
    identity_client_id: str | None,
    identity_probe: Callable[[str], None] | None,
    clock: Callable[[], float],
) -> tuple[Path, ProtectedSmokeHealth]:
    verify_campaign_pricing(
        snapshot_path=CANONICAL_PAYG_SNAPSHOT_PATH,
        model_family="gpt-5.2",
        model_version="2025-12-11",
        policy_mode=LIVE_MEASUREMENT,
        today=today,
    )
    if identity_client_id is not None:
        (identity_probe or probe_managed_identity)(identity_client_id)

    work = output_dir.parent / ".protected-azure-smoke-work"
    if output_dir.exists() or output_dir.is_symlink() or work.exists() or work.is_symlink():
        raise ProtectedContextError("protected smoke output already exists")
    ledger = _ledger(deployment)
    harness: _OfflineHarness | None = None

    if mode == "offline_fake":
        harness = _OfflineHarness()

        def build(
            selected_ledger: Any, endpoint: ResolvedEndpoint, capture_io: bool
        ) -> AzureFoundryProvider:
            return AzureFoundryProvider(
                ledger=selected_ledger,
                endpoint=endpoint,
                capture_io=capture_io,
                client_factory=harness.client_factory,
                token_provider_factory=harness.token_provider_factory,
                environ={},
            )

    else:
        if identity_client_id is None:
            raise ProtectedContextError("managed identity context was missing")

        provider_env = dict(env)
        provider_env.pop("CI", None)

        def build(
            selected_ledger: Any, endpoint: ResolvedEndpoint, capture_io: bool
        ) -> AzureFoundryProvider:
            return AzureFoundryProvider(
                ledger=selected_ledger,
                endpoint=endpoint,
                capture_io=capture_io,
                token_provider_factory=_managed_identity_token_provider_factory(
                    identity_client_id
                ),
                environ=provider_env,
            )

    workspace = work / "workspace"
    work.mkdir(mode=0o700, exist_ok=False)
    workspace.mkdir(mode=0o700)
    (workspace / "sample.jsonl").write_text(
        json.dumps({"id": "health", "input": FIXED_PROMPT}) + "\n",
        encoding="utf-8",
    )
    attempted = 0
    record: dict[str, Any] | None = None
    failure: FailureClass | None = None
    try:
        result = run_ledger(
            ledger,
            base_dir=workspace,
            environ=env,
            confirm_cost=True,
            provider_builder=build,
        )
        attempted = 1
        record = _record(result)
        if harness is not None:
            harness.assert_contract()
        if result.status != "ok":
            failure = _failure_class(record.get("error_type"))
    finally:
        if work.exists() and not work.is_symlink():
            shutil.rmtree(work)

    health = _health(
        mode=mode,
        commit_sha=commit_sha,
        now=clock(),
        attempted=attempted,
        record=record,
        failure_class=failure,
    )
    return _write_health(output_dir, health), health


def run_offline_fake(
    output_dir: Path,
    *,
    repo_root: Path,
    today: dt.date | None = None,
    clock: Callable[[], float] = time.time,
) -> tuple[Path, ProtectedSmokeHealth]:
    commit = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    ).stdout.strip()
    return _execute(
        mode="offline_fake",
        output_dir=output_dir,
        commit_sha=commit,
        deployment="offline-fake-deployment",
        env={ENDPOINT_ENV_NAME: "https://offline.invalid"},
        today=today or PRICING_SNAPSHOT_DATE,
        identity_client_id=None,
        identity_probe=None,
        clock=clock,
    )


def run_live(
    *,
    repo_root: Path,
    environ: dict[str, str] | None = None,
    today: dt.date | None = None,
    identity_probe: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.time,
) -> tuple[Path, ProtectedSmokeHealth]:
    env = dict(os.environ if environ is None else environ)
    context = validate_live_context(env, repo_root=repo_root)
    try:
        return _execute(
            mode="live",
            output_dir=context.output_dir,
            commit_sha=context.commit_sha,
            deployment=env[DEPLOYMENT_ENV_NAME],
            env=env,
            today=today or dt.date.today(),
            identity_client_id=context.managed_identity_client_id,
            identity_probe=identity_probe,
            clock=clock,
        )
    except (
        ProtectedContextError,
        ManagedIdentityError,
        PricingPolicyError,
        PricingSelectionError,
        BudgetNotConfirmedError,
        OptionalDependencyError,
        ProviderError,
    ) as exc:
        if isinstance(exc, ProtectedContextError):
            if context.output_dir.exists() or context.output_dir.is_symlink():
                raise
            failure: FailureClass = "protected_context"
        elif isinstance(exc, ManagedIdentityError):
            failure: FailureClass = "managed_identity"
        elif isinstance(exc, (PricingPolicyError, PricingSelectionError)):
            failure = "pricing"
        elif isinstance(exc, BudgetNotConfirmedError):
            failure = "cost_guard"
        elif isinstance(exc, OptionalDependencyError):
            failure = "runtime_dependency"
        else:
            failure = "provider"
        health = _health(
            mode="live",
            commit_sha=context.commit_sha,
            now=clock(),
            attempted=0,
            record=None,
            failure_class=failure,
        )
        return _write_health(context.output_dir, health), health


__all__ = [
    "ENDPOINT_ENV_NAME",
    "FIXED_PROMPT",
    "HARD_CEILING_USD",
    "MAX_OUTPUT_TOKENS",
    "ProtectedContextError",
    "ProtectedSmokeHealth",
    "TIMEOUT_SECONDS",
    "run_live",
    "run_offline_fake",
    "validate_health_privacy",
    "validate_live_context",
]
