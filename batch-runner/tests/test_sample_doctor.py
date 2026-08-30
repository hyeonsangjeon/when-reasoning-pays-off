"""Offline lock-doctor and Ollama fingerprint acceptance tests."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from batch_runner.cli import main
from batch_runner.experiment.doctor import diagnose_workspace
from batch_runner.experiment.ledger import LedgerError, parse_ledger
from batch_runner.experiment.locking import (
    LOCK_NAME,
    OWNED_MARKER_BYTES,
    OWNED_MARKER_NAME,
    REPAIR_LOG_NAME,
    LockSafetyError,
    diagnose_lock,
    exclusive_run_lock,
    host_fingerprint,
    process_start_token,
    record_repair_event,
)
from batch_runner.experiment import locking
from batch_runner.experiment.manifest import canonical_json, sha256_bytes
from batch_runner.experiment.providers.base import ResolvedEndpoint
from batch_runner.experiment.providers.ollama import OllamaProvider
from batch_runner.experiment.record import (
    ModelUnavailableError,
    ProviderUnavailableError,
)
from batch_runner.experiment.runner import run_ledger

_DIGEST = "sha256:" + ("a" * 64)
_OTHER_DIGEST = "sha256:" + ("b" * 64)
_FIXED_CLOCK = lambda: 1_700_000_000.0  # noqa: E731
_FIXED_RANDOM = lambda _n: "0123abcd"  # noqa: E731


def _ledger(provider: str = "mock") -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": "1.1.0",
        "experiment": {"id": "doctor-test", "purpose": "offline acceptance test"},
        "provider": provider,
        "model": "mock-preview" if provider == "mock" else "qwen2.5:0.5b",
        "endpoint": {
            "env_var": "TEST_ENDPOINT",
            "default": "http://localhost:11434",
        },
        "auth": {"mode": "none", "env_vars": []},
        "input": {
            "path": "sample.jsonl",
            "format": "jsonl",
            "row_shape": {
                "required_fields": {"id": "string", "input": "string"}
            },
            "max_records": 50,
            "sample_selector": "first",
        },
        "execution": {
            "max_samples": 1,
            "concurrency": 1,
            "timeout_seconds": 5,
            "max_output_tokens": 16,
            "repeats": 1,
            "capture_io": False,
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
        "provenance": {
            "method_id": "experiment-runner",
            "method_version": "1.0.0",
        },
    }
    if provider == "ollama":
        value["endpoint"]["env_var"] = "OLLAMA_BASE_URL"
    return value


def _workspace(tmp_path: Path, provider: str = "mock") -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    ledger_path = tmp_path / "ledger.yaml"
    ledger_path.write_text(yaml.safe_dump(_ledger(provider)), encoding="utf-8")
    (tmp_path / "sample.jsonl").write_text(
        '{"id":"q1","input":"What is 2+2?"}\n', encoding="utf-8"
    )
    return tmp_path, ledger_path


def _lock_metadata(
    *,
    pid: int,
    host: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "pid": pid,
        "host_fingerprint": host or host_fingerprint(),
        "created_at": "2026-08-30T09:00:00Z",
        "process_start_token": token,
        "run": {"operation": "sample-run", "ledger_sha256": "c" * 64},
        "tool": {"name": "reasoning-payoff", "version": "0.2.0"},
    }


def _write_lock(workspace: Path, value: object) -> Path:
    path = workspace / LOCK_NAME
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")
    return path


def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)


def _ollama_transport(
    calls: list[tuple[str, bytes | None]],
    *,
    digest: str = _DIGEST,
    include_optional: bool = True,
):
    def transport(
        url: str, payload: bytes | None, _timeout: float
    ) -> tuple[int, bytes]:
        calls.append((url, payload))
        if url.endswith("/api/version"):
            return 200, b'{"version":"0.11.7"}'
        if url.endswith("/api/tags"):
            details = (
                {
                    "format": "gguf",
                    "family": "qwen2",
                    "parameter_size": "494.03M",
                    "quantization_level": "Q4_K_M",
                }
                if include_optional
                else {}
            )
            return 200, json.dumps(
                {
                    "models": [
                        {
                            "name": "qwen2.5:0.5b",
                            "digest": digest,
                            "details": details,
                        }
                    ]
                }
            ).encode()
        if url.endswith("/api/show"):
            assert json.loads(payload or b"{}") == {
                "model": "qwen2.5:0.5b",
                "verbose": False,
            }
            body: dict[str, Any] = {}
            if include_optional:
                body = {
                    "template": "{{ .System }}\n{{ .Prompt }}",
                    "model_info": {"general.architecture": "qwen2", "size": 494},
                }
            return 200, json.dumps(body).encode()
        if url.endswith("/api/chat"):
            return 200, json.dumps(
                {
                    "message": {"content": "4"},
                    "done": True,
                    "prompt_eval_count": 4,
                    "eval_count": 1,
                }
            ).encode()
        raise AssertionError("unexpected Ollama endpoint")

    return transport


def test_lock_metadata_is_versioned_secret_safe_and_full_lifetime(
    tmp_path: Path,
) -> None:
    workspace, _ = _workspace(tmp_path)
    with exclusive_run_lock(
        workspace, operation="sample-run", ledger_sha256="d" * 64
    ):
        value = json.loads((workspace / LOCK_NAME).read_text())
        assert set(value) == {
            "schema_version",
            "pid",
            "host_fingerprint",
            "created_at",
            "process_start_token",
            "run",
            "tool",
        }
        assert value["schema_version"] == "1.0.0"
        assert value["pid"] == os.getpid()
        assert len(value["host_fingerprint"]) == 64
        assert value["host_fingerprint"] != socket.gethostname()
        assert (workspace / LOCK_NAME).exists()
    assert not (workspace / LOCK_NAME).exists()


def test_lock_diagnoses_live_stale_cross_host_malformed_and_pid_reuse(
    tmp_path: Path,
) -> None:
    workspace, _ = _workspace(tmp_path)
    live = _write_lock(
        workspace,
        _lock_metadata(
            pid=os.getpid(), token=process_start_token(os.getpid())
        ),
    )
    assert diagnose_lock(workspace).state == "live"
    live.unlink()

    _write_lock(workspace, _lock_metadata(pid=99_999_999))
    stale = diagnose_lock(workspace, liveness_probe=lambda _pid: "missing")
    assert stale.state == "stale" and stale.repairable
    live.unlink()

    _write_lock(workspace, _lock_metadata(pid=1, host="0" * 64))
    assert diagnose_lock(workspace).state == "cross_host"
    live.unlink()

    live.write_text("41731\n", encoding="ascii")
    assert diagnose_lock(workspace).state == "malformed"
    live.unlink()

    _write_lock(
        workspace,
        _lock_metadata(pid=os.getpid(), token="0" * 64),
    )
    assert diagnose_lock(workspace).state == "pid_reuse"
    assert live.exists()


def test_lock_symlink_and_unknown_liveness_fail_closed(tmp_path: Path) -> None:
    workspace, _ = _workspace(tmp_path)
    target = workspace / "foreign"
    target.write_text("keep", encoding="utf-8")
    lock = workspace / LOCK_NAME
    lock.symlink_to(target)
    assert diagnose_lock(workspace).state == "symlink"
    assert target.read_text() == "keep"
    lock.unlink()
    _write_lock(workspace, _lock_metadata(pid=os.getpid()))
    diagnosis = diagnose_lock(
        workspace, liveness_probe=lambda _pid: "unknown"
    )
    assert diagnosis.state == "unknown"
    assert not diagnosis.repairable


def test_windows_liveness_uses_query_without_signaling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(locking.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        locking,
        "_windows_process_identity",
        lambda _pid: ("alive", "a" * 64),
    )
    assert locking._pid_liveness(123) == "alive"
    assert process_start_token(123) == "a" * 64


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process-handle semantics")
def test_windows_exit_code_259_is_not_treated_as_alive() -> None:
    process = subprocess.Popen([sys.executable, "-c", "raise SystemExit(259)"])
    process.wait(timeout=10)
    assert locking._windows_process_identity(process.pid)[0] == "missing"


def test_repair_event_refuses_symlink_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "foreign"
    target.write_text("keep\n", encoding="utf-8")
    (tmp_path / REPAIR_LOG_NAME).symlink_to(target)
    with pytest.raises(LockSafetyError):
        record_repair_event(
            tmp_path,
            prior_lock_sha256=None,
            lock_removed=False,
            staging_removed=1,
        )
    assert target.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.parametrize("state", ["cross_host", "malformed", "pid_reuse"])
def test_doctor_repair_fails_closed_for_unprovable_lock(
    tmp_path: Path,
    state: str,
) -> None:
    workspace, ledger_path = _workspace(tmp_path)
    if state == "cross_host":
        lock = _write_lock(
            workspace, _lock_metadata(pid=os.getpid(), host="0" * 64)
        )
    elif state == "pid_reuse":
        lock = _write_lock(
            workspace,
            _lock_metadata(pid=os.getpid(), token="0" * 64),
        )
    else:
        lock = workspace / LOCK_NAME
        lock.write_text("not-json\n", encoding="utf-8")
    before = lock.read_bytes()
    result = diagnose_workspace(ledger_path, repair_stale_lock=True)
    assert result.exit_code == 5
    assert result.payload["lock"]["state"] == state
    assert result.payload["repair"]["performed"] is False
    assert result.payload["repair"]["error"] == "lock_not_proven_stale"
    assert lock.read_bytes() == before


def test_doctor_repairs_sigkill_lock_and_only_owned_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_network(monkeypatch)
    workspace, ledger_path = _workspace(tmp_path)
    ledger = parse_ledger(_ledger())
    completed = run_ledger(
        ledger,
        base_dir=workspace,
        clock=_FIXED_CLOCK,
        random_hex=_FIXED_RANDOM,
    ).out_dir
    runs = workspace / "out" / "runs"
    stage = runs / ".20260830T090000Z_aaaaaaaa_bbbbbbbb_cccccccc.staging-0123456789abcdef"
    stage.mkdir()
    (stage / OWNED_MARKER_NAME).write_bytes(OWNED_MARKER_BYTES)
    foreign = runs / ".foreign.staging-0123456789abcdef"
    foreign.mkdir()
    (foreign / "keep").write_text("keep", encoding="utf-8")
    _write_lock(workspace, _lock_metadata(pid=99_999_999))

    result = diagnose_workspace(ledger_path, repair_stale_lock=True)

    assert result.exit_code == 5
    assert result.payload["repair"] == {
        "requested": True,
        "performed": True,
        "lock_removed": True,
        "staging_removed": 1,
        "event_recorded": True,
        "error": None,
    }
    assert not (workspace / LOCK_NAME).exists()
    assert not stage.exists()
    assert foreign.is_dir() and (foreign / "keep").read_text() == "keep"
    assert completed.is_dir()
    event_text = (workspace / REPAIR_LOG_NAME).read_text()
    for forbidden in (str(tmp_path), os.getenv("USER", ""), "localhost"):
        if forbidden:
            assert forbidden not in event_text


def test_doctor_never_deletes_live_lock(tmp_path: Path) -> None:
    workspace, ledger_path = _workspace(tmp_path)
    lock = _write_lock(
        workspace,
        _lock_metadata(
            pid=os.getpid(), token=process_start_token(os.getpid())
        ),
    )
    before = lock.read_bytes()
    result = diagnose_workspace(ledger_path, repair_stale_lock=True)
    assert result.exit_code == 5
    assert result.payload["lock"]["state"] == "live"
    assert result.payload["repair"]["performed"] is False
    assert lock.read_bytes() == before


def test_ollama_fingerprint_calls_official_endpoints_and_hashes_canonically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_network(monkeypatch)
    ledger = parse_ledger(_ledger("ollama"))
    endpoint = ResolvedEndpoint(
        base_url="http://localhost:11434", source="endpoint.default"
    )
    calls: list[tuple[str, bytes | None]] = []
    provider = OllamaProvider(
        ledger=ledger,
        endpoint=endpoint,
        capture_io=False,
        transport=_ollama_transport(calls),
    )
    provider.prepare()
    fingerprint = provider.fingerprint()
    assert fingerprint == {
        "runtime_version": "0.11.7",
        "tag": "qwen2.5:0.5b",
        "digest": _DIGEST,
        "format": "gguf",
        "family": "qwen2",
        "parameter_size": "494.03M",
        "quantization": "Q4_K_M",
        "template_sha256": sha256_bytes(
            canonical_json("{{ .System }}\n{{ .Prompt }}")
        ),
        "model_info_sha256": sha256_bytes(
            canonical_json(
                {"general.architecture": "qwen2", "size": 494}
            )
        ),
    }
    assert [url.rsplit("/", 2)[-1] for url, _ in calls] == [
        "version",
        "tags",
        "show",
    ]


def test_ollama_missing_tag_and_digest_mismatch_stop_before_prompt() -> None:
    ledger_data = _ledger("ollama")
    ledger_data["expected_model_digest"] = _OTHER_DIGEST
    ledger = parse_ledger(ledger_data)
    endpoint = ResolvedEndpoint(
        base_url="http://localhost:11434", source="endpoint.default"
    )
    calls: list[tuple[str, bytes | None]] = []
    provider = OllamaProvider(
        ledger=ledger,
        endpoint=endpoint,
        capture_io=False,
        transport=_ollama_transport(calls),
    )
    with pytest.raises(ModelUnavailableError, match="before prompt"):
        provider.prepare()
    assert not any(url.endswith("/api/chat") for url, _ in calls)
    assert not any(url.endswith("/api/show") for url, _ in calls)

    def missing(
        url: str, payload: bytes | None, timeout: float
    ) -> tuple[int, bytes]:
        if url.endswith("/api/tags"):
            return 200, b'{"models":[]}'
        return _ollama_transport([])(url, payload, timeout)

    missing_provider = OllamaProvider(
        ledger=parse_ledger(_ledger("ollama")),
        endpoint=endpoint,
        capture_io=False,
        transport=missing,
    )
    with pytest.raises(ModelUnavailableError, match="ollama pull"):
        missing_provider.prepare()


def test_ollama_fingerprint_missing_optional_metadata_is_explicit_null() -> None:
    calls: list[tuple[str, bytes | None]] = []
    provider = OllamaProvider(
        ledger=parse_ledger(_ledger("ollama")),
        endpoint=ResolvedEndpoint(
            base_url="http://localhost:11434", source="endpoint.default"
        ),
        capture_io=False,
        transport=_ollama_transport(calls, include_optional=False),
    )
    provider.prepare()
    fingerprint = provider.fingerprint()
    assert fingerprint is not None
    for field in (
        "format",
        "family",
        "parameter_size",
        "quantization",
        "template_sha256",
        "model_info_sha256",
    ):
        assert fingerprint[field] is None


def test_ollama_fingerprint_redirect_is_blocked() -> None:
    provider = OllamaProvider(
        ledger=parse_ledger(_ledger("ollama")),
        endpoint=ResolvedEndpoint(
            base_url="http://localhost:11434", source="endpoint.default"
        ),
        capture_io=False,
        transport=lambda *_args: (302, b""),
    )
    with pytest.raises(ProviderUnavailableError, match="redirect"):
        provider.prepare()


def test_ollama_manifest_contains_fingerprint_and_no_raw_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_network(monkeypatch)
    workspace, _ = _workspace(tmp_path, "ollama")
    ledger = parse_ledger(_ledger("ollama"))
    calls: list[tuple[str, bytes | None]] = []

    def build(current, endpoint, capture):
        return OllamaProvider(
            ledger=current,
            endpoint=endpoint,
            capture_io=capture,
            transport=_ollama_transport(calls),
        )

    result = run_ledger(
        ledger,
        base_dir=workspace,
        clock=_FIXED_CLOCK,
        random_hex=_FIXED_RANDOM,
        provider_builder=build,
    )
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["schema_version"] == "1.3.0"
    assert manifest["provider"]["ollama"]["digest"] == _DIGEST
    text = result.manifest_path.read_text()
    assert "{{ .System }}" not in text
    assert "general.architecture" not in text
    assert not any(
        url.endswith("/api/chat")
        for url, _ in calls[:3]
    )


def test_doctor_ollama_json_and_remote_opt_in_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _block_network(monkeypatch)
    workspace, ledger_path = _workspace(tmp_path, "ollama")
    calls: list[tuple[str, bytes | None]] = []
    local = diagnose_workspace(
        ledger_path,
        ollama_transport=_ollama_transport(calls),
    )
    assert local.exit_code == 0
    assert local.payload["ollama"]["reachability"] == "reachable"
    assert local.payload["ollama"]["proxy_bypass"] is True
    assert local.payload["ollama"]["digest"] == _DIGEST
    assert local.payload["ollama"]["warm_prerequisites"] == {
        "ready": True,
        "service_reachable": True,
        "model_installed": True,
        "expected_digest": "not-configured",
        "install_excluded": True,
        "model_pull_excluded": True,
        "prompt_sent": False,
    }

    contacted = False

    def forbidden_transport(*_args: Any) -> tuple[int, bytes]:
        nonlocal contacted
        contacted = True
        raise AssertionError("remote endpoint contacted without opt-in")

    remote = diagnose_workspace(
        ledger_path,
        environ={"OLLAMA_BASE_URL": "http://remote.example.com:11434"},
        ollama_transport=forbidden_transport,
    )
    assert remote.exit_code == 7
    assert remote.payload["ollama"]["contacted"] is False
    assert remote.payload["ollama"]["endpoint_locality"] == "remote"
    assert remote.payload["ollama"]["error_type"] == "remote_opt_in_required"
    assert remote.payload["ollama"]["warm_prerequisites"]["ready"] is False
    assert contacted is False
    assert "remote.example.com" not in json.dumps(remote.payload)

    mock_workspace, mock_ledger = _workspace(tmp_path / "mock")
    assert main(["sample", "doctor", "--ledger", str(mock_ledger), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "1.1.0"
    assert payload["package"]["installation"]
    assert payload["workspace"]["state"] == "real-directory"
    assert mock_workspace.is_dir()
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "schemas"
            / "sample_doctor.v1.schema.json"
        ).read_text()
    )
    __import__("jsonschema").validate(payload, schema)


def test_expected_digest_is_strict_and_ollama_only() -> None:
    invalid = _ledger("ollama")
    invalid["expected_model_digest"] = "sha256:ABC"
    with pytest.raises(LedgerError):
        parse_ledger(invalid)
    wrong_provider = _ledger("mock")
    wrong_provider["expected_model_digest"] = _DIGEST
    with pytest.raises(LedgerError):
        parse_ledger(wrong_provider)
