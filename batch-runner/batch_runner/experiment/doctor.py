"""Read-only sample workspace diagnosis with explicit, fail-closed recovery."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import platform
import re
from importlib import metadata
from pathlib import Path
from typing import Any

from batch_runner import __version__
from batch_runner.experiment.ledger import RunLedger, load_ledger
from batch_runner.experiment.locking import (
    OWNED_MARKER_NAME,
    LockSafetyError,
    clean_owned_staging_directories,
    diagnose_lock,
    exclusive_run_lock,
    find_owned_staging_directories,
    record_repair_event,
    remove_proven_stale_lock,
    valid_owned_marker,
)
from batch_runner.experiment.manifest import _repository_root
from batch_runner.experiment.providers.base import (
    EndpointResolutionError,
    resolve_endpoint,
)
from batch_runner.experiment.providers.ollama import OllamaProvider, Transport
from batch_runner.experiment.record import ProviderError
from batch_runner.experiment.record import ModelUnavailableError

_RUN_ID_RE = re.compile(
    r"^\d{8}T\d{6}Z_[0-9a-f]{8}_[0-9a-f]{8}_[0-9a-f]{8}$"
)
_RUN_ARTIFACTS = {
    OWNED_MARKER_NAME,
    "run.json",
    "records.jsonl",
    "summary.md",
    "manifest.json",
    "artifacts.sha256",
}


@dataclasses.dataclass(frozen=True)
class DoctorResult:
    payload: dict[str, Any]
    exit_code: int

    def render(self) -> str:
        package = self.payload["package"]
        workspace = self.payload["workspace"]
        lock = self.payload["lock"]
        output = workspace["output"]
        lines = [
            f"doctor: {self.payload['status']}",
            (
                f"package: {package['version']} ({package['installation']}); "
                f"python {package['python']} on {package['os']}/{package['architecture']}"
            ),
            (
                f"workspace: {workspace['state']}; output={output['state']}; "
                f"completed_runs={output['completed_runs']}; "
                f"owned_staging={output['owned_staging']}"
            ),
            f"lock: {lock['state']} - {lock['guidance']}",
        ]
        ollama = self.payload["ollama"]
        if ollama is not None:
            lines.append(
                "ollama: "
                f"{ollama['reachability']}; locality={ollama['endpoint_locality']}; "
                f"version={ollama['runtime_version'] or 'not-reported'}; "
                f"tag={ollama['requested_tag']}; "
                f"digest={ollama['digest'] or 'not-reported'}"
            )
            lines.append(
                "ollama model: "
                f"format={ollama['format'] or 'not-reported'}; "
                f"family={ollama['family'] or 'not-reported'}; "
                f"parameters={ollama['parameter_size'] or 'not-reported'}; "
                f"quantization={ollama['quantization'] or 'not-reported'}"
            )
            warm = ollama["warm_prerequisites"]
            lines.append(
                "warm timing prerequisites: "
                f"{'ready' if warm['ready'] else 'not ready'}; "
                "Ollama install and model pull are excluded; "
                "doctor sent no prompt"
            )
        repair = self.payload["repair"]
        if repair["requested"]:
            lines.append(
                "repair: "
                f"performed={str(repair['performed']).lower()}; "
                f"lock_removed={str(repair['lock_removed']).lower()}; "
                f"staging_removed={repair['staging_removed']}; "
                f"event_recorded={str(repair['event_recorded']).lower()}"
            )
            if repair["error"] is not None:
                lines.append(
                    f"repair guidance: stopped safely ({repair['error']}); "
                    "inspect the workspace and retry doctor before manual changes"
                )
        return "\n".join(lines)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _output_diagnosis(base_dir: Path) -> dict[str, Any]:
    out_dir = base_dir / "out"
    result: dict[str, Any] = {
        "state": "absent",
        "completed_runs": 0,
        "owned_staging": 0,
        "foreign_paths": 0,
        "latest": "absent",
    }
    if not out_dir.exists():
        return result
    if out_dir.is_symlink() or not out_dir.is_dir() or not valid_owned_marker(out_dir):
        result["state"] = "unsafe"
        return result
    runs_dir = out_dir / "runs"
    if runs_dir.is_symlink() or not runs_dir.is_dir() or not valid_owned_marker(runs_dir):
        result["state"] = "unsafe"
        return result
    result["state"] = "owned"
    owned_staging = find_owned_staging_directories(base_dir)
    result["owned_staging"] = len(owned_staging)
    for child in runs_dir.iterdir():
        if child.name == OWNED_MARKER_NAME:
            continue
        if child.name.startswith("."):
            if child not in owned_staging:
                result["foreign_paths"] += 1
            continue
        if (
            child.is_symlink()
            or not child.is_dir()
            or not _RUN_ID_RE.fullmatch(child.name)
            or not valid_owned_marker(child)
            or {entry.name for entry in child.iterdir()} != _RUN_ARTIFACTS
        ):
            result["foreign_paths"] += 1
            continue
        result["completed_runs"] += 1
    latest_path = out_dir / "latest.json"
    if latest_path.exists():
        try:
            if latest_path.is_symlink() or not latest_path.is_file():
                raise ValueError
            pointer = json.loads(latest_path.read_text(encoding="utf-8"))
            run_id = pointer["run_id"]
            manifest = runs_dir / run_id / "manifest.json"
            if (
                set(pointer)
                != {
                    "schema_version",
                    "run_id",
                    "run_path",
                    "status",
                    "manifest_sha256",
                }
                or pointer["schema_version"] != "1.0.0"
                or not isinstance(run_id, str)
                or not _RUN_ID_RE.fullmatch(run_id)
                or pointer["run_path"] != f"runs/{run_id}"
                or pointer["status"] not in {"ok", "partial", "failed"}
                or manifest.is_symlink()
                or not manifest.is_file()
                or _sha256(manifest) != pointer["manifest_sha256"]
            ):
                raise ValueError
            result["latest"] = "valid"
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError):
            result["latest"] = "invalid"
            result["state"] = "unsafe"
    if result["foreign_paths"]:
        result["state"] = "unsafe"
    return result


def _package_diagnosis() -> dict[str, str]:
    try:
        installed_version = metadata.version("when-reasoning-pays-off")
    except metadata.PackageNotFoundError:
        installed_version = __version__
    return {
        "name": "when-reasoning-pays-off",
        "version": installed_version,
        "installation": (
            "verified-source-checkout"
            if _repository_root() is not None
            else "installed-wheel-or-unverified-source"
        ),
        "python": platform.python_version(),
        "os": platform.system() or "unknown",
        "architecture": platform.machine() or "unknown",
    }


def _ollama_diagnosis(
    ledger: RunLedger,
    *,
    environ: dict[str, str] | None,
    allow_remote: bool,
    transport: Transport | None,
) -> tuple[dict[str, Any], int]:
    base: dict[str, Any] = {
        "endpoint_source": None,
        "endpoint_locality": "unknown",
        "remote_opt_in": allow_remote,
        "contacted": False,
        "proxy_bypass": True,
        "redirects_blocked": True,
        "reachability": "blocked",
        "runtime_version": None,
        "requested_tag": ledger.model,
        "digest": None,
        "format": None,
        "family": None,
        "parameter_size": None,
        "quantization": None,
        "template_sha256": None,
        "model_info_sha256": None,
        "error_type": None,
        "warm_prerequisites": {
            "ready": False,
            "service_reachable": False,
            "model_installed": None,
            "expected_digest": (
                "not-configured"
                if ledger.expected_model_digest is None
                else "not-verified"
            ),
            "install_excluded": True,
            "model_pull_excluded": True,
            "prompt_sent": False,
        },
    }
    try:
        endpoint = resolve_endpoint(
            ledger, environ=environ, allow_remote=True
        )
    except EndpointResolutionError:
        base["error_type"] = "endpoint_unresolved"
        return base, 7
    base["endpoint_source"] = endpoint.source
    base["endpoint_locality"] = "local" if endpoint.is_local else "remote"
    if not endpoint.is_local and not allow_remote:
        base["error_type"] = "remote_opt_in_required"
        return base, 7
    provider = OllamaProvider(
        ledger=ledger,
        endpoint=endpoint,
        capture_io=False,
        transport=transport,
    )
    base["contacted"] = True
    try:
        provider.prepare()
    except ProviderError as exc:
        base["reachability"] = "unavailable"
        base["error_type"] = exc.error_type
        if isinstance(exc, ModelUnavailableError):
            base["warm_prerequisites"]["service_reachable"] = True
        return base, 7
    fingerprint = provider.fingerprint()
    if fingerprint is None:
        base["error_type"] = "fingerprint_unavailable"
        return base, 7
    requested_tag = fingerprint.pop("tag", ledger.model)
    base.update(fingerprint)
    base["requested_tag"] = requested_tag
    base["reachability"] = "reachable"
    base["warm_prerequisites"].update(
        {
            "ready": True,
            "service_reachable": True,
            "model_installed": True,
            "expected_digest": (
                "not-configured"
                if ledger.expected_model_digest is None
                else "matched"
            ),
        }
    )
    return base, 0


def diagnose_workspace(
    ledger_path: Path,
    *,
    repair_stale_lock: bool = False,
    allow_remote_ollama: bool = False,
    environ: dict[str, str] | None = None,
    ollama_transport: Transport | None = None,
) -> DoctorResult:
    """Diagnose one sample workspace and optionally perform guarded repair."""
    ledger = load_ledger(ledger_path)
    base_dir = Path(os.path.abspath(ledger_path.parent))
    workspace_state = (
        "unsafe"
        if base_dir.is_symlink() or not base_dir.is_dir()
        else "real-directory"
    )
    initial_lock = diagnose_lock(base_dir)
    repair = {
        "requested": repair_stale_lock,
        "performed": False,
        "lock_removed": False,
        "staging_removed": 0,
        "event_recorded": False,
        "error": None,
    }
    if repair_stale_lock:
        if initial_lock.state == "stale":
            try:
                remove_proven_stale_lock(base_dir, initial_lock)
                repair["lock_removed"] = True
                record_repair_event(
                    base_dir,
                    prior_lock_sha256=initial_lock.content_sha256,
                    lock_removed=True,
                    staging_removed=0,
                )
                repair["event_recorded"] = True
            except LockSafetyError:
                repair["error"] = "lock_changed"
            except OSError:
                repair["error"] = "repair_event_write_failed"
        if initial_lock.state in {"absent", "stale"} and repair["error"] is None:
            try:
                with exclusive_run_lock(
                    base_dir,
                    operation="doctor-repair",
                    ledger_sha256=ledger.sha256(),
                ):
                    repair["staging_removed"] = clean_owned_staging_directories(
                        base_dir
                    )
                    if repair["staging_removed"]:
                        try:
                            record_repair_event(
                                base_dir,
                                prior_lock_sha256=None,
                                lock_removed=False,
                                staging_removed=int(repair["staging_removed"]),
                            )
                            repair["event_recorded"] = True
                        except (LockSafetyError, OSError):
                            repair["error"] = "repair_event_write_failed"
            except (LockSafetyError, FileExistsError):
                repair["error"] = "exclusive_repair_lock_unavailable"
            except OSError:
                repair["error"] = "staging_cleanup_failed"
        elif initial_lock.state not in {"absent", "stale"}:
            repair["error"] = "lock_not_proven_stale"
    repair["performed"] = bool(
        repair["lock_removed"] or repair["staging_removed"]
    )
    final_lock = diagnose_lock(base_dir)
    output = _output_diagnosis(base_dir)
    ollama = None
    provider_exit = 0
    if ledger.provider == "ollama":
        ollama, provider_exit = _ollama_diagnosis(
            ledger,
            environ=environ,
            allow_remote=allow_remote_ollama,
            transport=ollama_transport,
        )
    lock_blocked = final_lock.state not in {"absent"}
    structure_blocked = workspace_state == "unsafe" or output["state"] == "unsafe"
    repair_blocked = repair["error"] is not None
    exit_code = provider_exit or (5 if lock_blocked or structure_blocked or repair_blocked else 0)
    if repair["performed"] and exit_code == 0:
        status = "repaired"
    elif exit_code:
        status = "blocked"
    else:
        status = "ok"
    return DoctorResult(
        payload={
            "schema_version": "1.1.0",
            "status": status,
            "package": _package_diagnosis(),
            "workspace": {
                "state": workspace_state,
                "ledger": "valid",
                "output": output,
            },
            "lock": final_lock.public_json(),
            "ollama": ollama,
            "repair": repair,
        },
        exit_code=exit_code,
    )


__all__ = ["DoctorResult", "diagnose_workspace"]
