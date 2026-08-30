"""Secret-safe provenance manifest construction for immutable sample runs."""

from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from batch_runner import __version__
from batch_runner.experiment.dataset import LoadedDataset
from batch_runner.experiment.ledger import RunLedger
from batch_runner.experiment.providers.base import ResolvedEndpoint
from batch_runner.experiment.record import ProviderCapabilities

MANIFEST_SCHEMA_VERSION = "1.0.0"
REPOSITORY_IDENTITY = "hyeonsangjeon/when-reasoning-pays-off"
_SHA256_RE = r"^[0-9a-f]{64}$"
_COMMIT_RE = r"^(unknown|[0-9a-f]{40})$"
_SELECTED_PACKAGES = (
    "azure-identity",
    "numpy",
    "openai",
    "pandas",
    "pydantic",
    "pyyaml",
    "python-dotenv",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CodeIdentity(_StrictModel):
    repository: Literal["hyeonsangjeon/when-reasoning-pays-off"]
    vcs_state: Literal["available", "unknown"]
    commit_sha: str = Field(pattern=_COMMIT_RE)
    dirty: bool | Literal["unknown"]
    package_version: str = Field(min_length=1, max_length=64)


class DependencyLockIdentity(_StrictModel):
    state: Literal["available", "unknown"]
    kind: Literal["requirements.txt", "unknown"]
    sha256: str = Field(pattern=r"^(unknown|[0-9a-f]{64})$")


class RuntimeIdentity(_StrictModel):
    python: str = Field(min_length=1, max_length=64)
    os: str = Field(min_length=1, max_length=64)
    architecture: str = Field(min_length=1, max_length=64)
    dependency_lock: DependencyLockIdentity
    selected_packages: dict[str, str]


class InputIdentity(_StrictModel):
    format: Literal["json", "jsonl"]
    sha256: str = Field(pattern=_SHA256_RE)
    total_count: int = Field(ge=1)
    selected_count: int = Field(ge=1)
    selected_ids_sha256: str = Field(pattern=_SHA256_RE)


class ProviderFingerprint(_StrictModel):
    provider: Literal["azure", "ollama", "mock"]
    model: str = Field(min_length=1, max_length=120)
    endpoint_source: str = Field(min_length=1, max_length=80)
    endpoint_locality: Literal["local", "remote", "environment", "none"]
    auth_mode: Literal["none", "entra"]
    capabilities: dict[str, str | bool]
    fingerprint_sha256: str = Field(pattern=_SHA256_RE)


class PricingProvenance(_StrictModel):
    state: Literal["available", "not_applicable"]
    snapshot_id: str | None = Field(default=None, max_length=120)
    snapshot_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    pricing_model: str | None = Field(default=None, max_length=120)
    input_per_1m_usd: float | None = Field(default=None, gt=0)
    output_per_1m_usd: float | None = Field(default=None, gt=0)


class ExecutionKnobs(_StrictModel):
    max_samples: int = Field(ge=1)
    concurrency: Literal[1]
    timeout_seconds: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    repeats: int = Field(ge=1)
    reasoning_effort: str | None
    capture_io: bool
    cost_confirmed_in_ledger: bool
    cost_confirmed_by_cli: bool
    remote_ollama_opt_in: bool


class RunLineage(_StrictModel):
    kind: Literal["initial", "retry_failed"]
    parent_run_id: str | None
    retried_failed_count: int = Field(ge=0)

    @field_validator("parent_run_id")
    @classmethod
    def _safe_parent_run_id(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(
            r"\d{8}T\d{6}Z_[0-9a-f]{8}_[0-9a-f]{8}_[0-9a-f]{8}", value
        ):
            raise ValueError("parent_run_id is invalid")
        return value


class ArtifactIdentity(_StrictModel):
    sha256: str = Field(pattern=_SHA256_RE)
    bytes: int = Field(ge=0)


class RunManifest(_StrictModel):
    schema_version: Literal["1.0.0"]
    run_id: str
    ledger_sha256: str = Field(pattern=_SHA256_RE)
    code: CodeIdentity
    runtime: RuntimeIdentity
    input: InputIdentity
    provider: ProviderFingerprint
    pricing: PricingProvenance
    execution: ExecutionKnobs
    status: Literal["ok", "partial", "failed"]
    lineage: RunLineage
    artifacts: dict[str, ArtifactIdentity]

    @field_validator("run_id")
    @classmethod
    def _safe_run_id(cls, value: str) -> str:
        if not re.fullmatch(
            r"\d{8}T\d{6}Z_[0-9a-f]{8}_[0-9a-f]{8}_[0-9a-f]{8}", value
        ):
            raise ValueError("run_id is invalid")
        return value

    @field_validator("artifacts")
    @classmethod
    def _fixed_artifact_hashes(
        cls, value: dict[str, ArtifactIdentity]
    ) -> dict[str, ArtifactIdentity]:
        expected = {"records.jsonl", "run.json", "summary.md"}
        if set(value) != expected:
            raise ValueError("manifest artifacts must hash the three payload artifacts")
        return value


def canonical_json(value: Any) -> bytes:
    """Return stable UTF-8 JSON bytes suitable for hashing."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _repository_root() -> Path | None:
    """Return the verified source checkout that owns this exact module."""
    package_dir = Path(__file__).resolve().parent
    try:
        root_text = subprocess.run(
            ["git", "-C", str(package_dir), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        root = Path(root_text).resolve()
        module = Path(__file__).resolve()
        relative_module = module.relative_to(root).as_posix()
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--error-unmatch",
                "--",
                relative_module,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        remote = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        if remote not in {
            "https://github.com/hyeonsangjeon/when-reasoning-pays-off",
            "https://github.com/hyeonsangjeon/when-reasoning-pays-off.git",
            "git@github.com:hyeonsangjeon/when-reasoning-pays-off.git",
            "ssh://git@github.com/hyeonsangjeon/when-reasoning-pays-off.git",
        }:
            return None
        return root
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _git_identity() -> dict[str, Any]:
    """Inspect this package's verified source checkout without exposing its path."""
    root = _repository_root()
    try:
        if root is None:
            raise OSError
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        dirty_result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain",
                "--untracked-files=normal",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit):
            raise ValueError("unexpected commit identity")
        return {
            "repository": REPOSITORY_IDENTITY,
            "vcs_state": "available",
            "commit_sha": commit,
            "dirty": bool(dirty_result.stdout),
            "package_version": __version__,
        }
    except (OSError, subprocess.SubprocessError, ValueError):
        return {
            "repository": REPOSITORY_IDENTITY,
            "vcs_state": "unknown",
            "commit_sha": "unknown",
            "dirty": "unknown",
            "package_version": __version__,
        }


def _dependency_lock() -> dict[str, str]:
    root = _repository_root()
    try:
        if root is None:
            raise OSError
        lock = Path(root) / "requirements.txt"
        if not lock.is_file() or lock.is_symlink():
            raise OSError
        return {
            "state": "available",
            "kind": "requirements.txt",
            "sha256": sha256_bytes(lock.read_bytes()),
        }
    except (OSError, subprocess.SubprocessError):
        return {"state": "unknown", "kind": "unknown", "sha256": "unknown"}


def _package_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for package in _SELECTED_PACKAGES:
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "unknown"
    return versions


def _provider_fingerprint(
    ledger: RunLedger,
    endpoint: ResolvedEndpoint,
    capabilities: ProviderCapabilities,
) -> dict[str, Any]:
    if ledger.provider == "mock":
        locality = "none"
    elif endpoint.is_local:
        locality = "local"
    elif ledger.provider == "azure":
        locality = "environment"
    else:
        locality = "remote"
    safe_capabilities = {
        "billed": capabilities.billed,
        "token_usage": capabilities.token_usage,
        "reasoning_tokens": capabilities.reasoning_tokens,
        "cached_tokens": capabilities.cached_tokens,
    }
    identity = {
        "provider": ledger.provider,
        "model": ledger.model,
        "endpoint_source": endpoint.source,
        "endpoint_locality": locality,
        "auth_mode": ledger.auth.mode,
        "capabilities": safe_capabilities,
    }
    return {**identity, "fingerprint_sha256": sha256_bytes(canonical_json(identity))}


def _pricing(ledger: RunLedger) -> dict[str, Any]:
    cost = ledger.execution.cost
    if not cost.billed:
        return {
            "state": "not_applicable",
            "snapshot_id": None,
            "snapshot_sha256": None,
            "pricing_model": None,
            "input_per_1m_usd": None,
            "output_per_1m_usd": None,
        }
    snapshot = {
        "snapshot_id": cost.pricing_snapshot_id,
        "pricing_model": cost.pricing_model,
        "input_per_1m_usd": cost.input_per_1m_usd,
        "output_per_1m_usd": cost.output_per_1m_usd,
    }
    return {
        "state": "available",
        **snapshot,
        "snapshot_sha256": sha256_bytes(canonical_json(snapshot)),
    }


def build_manifest(
    *,
    run_id: str,
    ledger: RunLedger,
    dataset: LoadedDataset,
    selected_ids: list[str],
    endpoint: ResolvedEndpoint,
    capabilities: ProviderCapabilities,
    status: str,
    parent_run_id: str | None,
    retried_failed_count: int,
    artifact_bytes: dict[str, bytes],
    cost_confirmed_by_cli: bool,
    remote_ollama_opt_in: bool,
) -> RunManifest:
    """Build and validate one complete, path-free run manifest."""
    artifacts = {
        name: {"sha256": sha256_bytes(content), "bytes": len(content)}
        for name, content in artifact_bytes.items()
    }
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "ledger_sha256": ledger.sha256(),
        "code": _git_identity(),
        "runtime": {
            "python": platform.python_version(),
            "os": platform.system() or "unknown",
            "architecture": platform.machine() or "unknown",
            "dependency_lock": _dependency_lock(),
            "selected_packages": _package_versions(),
        },
        "input": {
            "format": dataset.format,
            "sha256": dataset.sha256,
            "total_count": dataset.total_records,
            "selected_count": len(selected_ids),
            "selected_ids_sha256": sha256_bytes(canonical_json(selected_ids)),
        },
        "provider": _provider_fingerprint(ledger, endpoint, capabilities),
        "pricing": _pricing(ledger),
        "execution": {
            "max_samples": ledger.execution.max_samples,
            "concurrency": ledger.execution.concurrency,
            "timeout_seconds": ledger.execution.timeout_seconds,
            "max_output_tokens": ledger.execution.max_output_tokens,
            "repeats": ledger.execution.repeats,
            "reasoning_effort": ledger.execution.reasoning_effort,
            "capture_io": ledger.execution.capture_io,
            "cost_confirmed_in_ledger": ledger.execution.cost.confirmed,
            "cost_confirmed_by_cli": cost_confirmed_by_cli,
            "remote_ollama_opt_in": remote_ollama_opt_in,
        },
        "status": status,
        "lineage": {
            "kind": "retry_failed" if parent_run_id else "initial",
            "parent_run_id": parent_run_id,
            "retried_failed_count": retried_failed_count,
        },
        "artifacts": artifacts,
    }
    return RunManifest.model_validate(payload)


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "RunManifest",
    "build_manifest",
    "canonical_json",
    "sha256_bytes",
]
