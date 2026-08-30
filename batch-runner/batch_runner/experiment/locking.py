"""Secret-safe workspace lock ownership, diagnosis, and guarded recovery."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Literal

from batch_runner import __version__

LOCK_NAME = ".reasoning-payoff-sample.lock"
LOCK_SCHEMA_VERSION = "1.0.0"
REPAIR_LOG_NAME = ".reasoning-payoff-sample-repairs.jsonl"
OWNED_MARKER_NAME = ".reasoning-payoff-experiment-owned"
OWNED_MARKER_BYTES = b"reasoning-payoff experiment output\n"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = (
    r"\d{8}T\d{6}Z_[0-9a-f]{8}_[0-9a-f]{8}_[0-9a-f]{8}"
)
_STAGING_RE = re.compile(
    rf"^\.(?P<run_id>{_RUN_ID_RE})\.staging-[0-9a-f]{{16}}$"
)

LockState = Literal[
    "absent",
    "live",
    "stale",
    "cross_host",
    "malformed",
    "symlink",
    "pid_reuse",
    "unknown",
]
PidLiveness = Literal["alive", "missing", "unknown"]


class LockSafetyError(RuntimeError):
    """A lock or recovery target cannot be handled without unsafe assumptions."""


@dataclass(frozen=True)
class LockDiagnosis:
    state: LockState
    repairable: bool
    guidance: str
    metadata: dict[str, object] | None = None
    inode: tuple[int, int] | None = None
    content_sha256: str | None = None

    def public_json(self) -> dict[str, object]:
        owner = None
        if self.metadata is not None:
            owner = {
                "pid": self.metadata["pid"],
                "created_at": self.metadata["created_at"],
                "process_start_token_available": (
                    self.metadata["process_start_token"] is not None
                ),
                "operation": self.metadata["run"]["operation"],
                "tool_version": self.metadata["tool"]["version"],
            }
        return {
            "state": self.state,
            "repairable": self.repairable,
            "owner": owner,
            "guidance": self.guidance,
        }


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _machine_identity_material() -> bytes:
    system = platform.system()
    if system == "Linux":
        try:
            value = Path("/etc/machine-id").read_text(encoding="ascii").strip()
            if value:
                return f"linux:{value}".encode("ascii")
        except (OSError, UnicodeError):
            pass
    if system == "Darwin":
        try:
            value = subprocess.run(
                ["sysctl", "-n", "kern.uuid"],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
            if value:
                return f"darwin:{value}".encode("ascii")
        except (OSError, subprocess.SubprocessError, UnicodeError):
            pass
    return f"{system}:{uuid.getnode():012x}".encode("ascii")


def host_fingerprint() -> str:
    """Return a stable one-way host identity without storing a hostname."""
    return _sha256(b"reasoning-payoff-host-v1\0" + _machine_identity_material())


def process_start_token(pid: int) -> str | None:
    """Return a one-way process-start token when the platform exposes one."""
    if pid <= 0:
        return None
    if platform.system() == "Windows":
        return _windows_process_identity(pid)[1]
    if platform.system() == "Linux":
        try:
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()
            raw = fields[21]
            return _sha256(f"linux:{raw}".encode("ascii"))
        except (OSError, UnicodeError, IndexError):
            return None
    try:
        raw = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    return _sha256(f"{platform.system()}:{raw}".encode("utf-8")) if raw else None


def _pid_liveness(pid: int) -> PidLiveness:
    if platform.system() == "Windows":
        return _windows_process_identity(pid)[0]
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "missing"
    except PermissionError:
        return "unknown"
    except OSError:
        return "unknown"
    return "alive"


def _windows_process_identity(pid: int) -> tuple[PidLiveness, str | None]:
    """Read Windows process liveness and creation time without sending a signal."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, ImportError, OSError):
        return "unknown", None

    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        process_query_limited_information | synchronize,
        False,
        wintypes.DWORD(pid),
    )
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no process with this PID.
            return "missing", None
        return "unknown", None
    try:
        wait_result = kernel32.WaitForSingleObject(handle, 0)
        if wait_result == wait_object_0:
            return "missing", None
        if wait_result != wait_timeout:
            return "unknown", None
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return "alive", None
        raw = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return "alive", _sha256(f"windows:{raw}".encode("ascii"))
    finally:
        kernel32.CloseHandle(handle)


def _utc_iso(epoch: float | None = None) -> str:
    value = time.time() if epoch is None else epoch
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def _lock_metadata(*, operation: str, ledger_sha256: str | None) -> dict[str, object]:
    pid = os.getpid()
    return {
        "schema_version": LOCK_SCHEMA_VERSION,
        "pid": pid,
        "host_fingerprint": host_fingerprint(),
        "created_at": _utc_iso(),
        "process_start_token": process_start_token(pid),
        "run": {
            "operation": operation,
            "ledger_sha256": ledger_sha256,
        },
        "tool": {
            "name": "reasoning-payoff",
            "version": __version__,
        },
    }


def _valid_metadata(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "pid",
        "host_fingerprint",
        "created_at",
        "process_start_token",
        "run",
        "tool",
    }:
        return None
    run = value.get("run")
    tool = value.get("tool")
    if (
        value.get("schema_version") != LOCK_SCHEMA_VERSION
        or not isinstance(value.get("pid"), int)
        or isinstance(value.get("pid"), bool)
        or int(value["pid"]) <= 0
        or not isinstance(value.get("host_fingerprint"), str)
        or not _SHA256_RE.fullmatch(str(value["host_fingerprint"]))
        or not isinstance(value.get("created_at"), str)
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", str(value["created_at"]))
        or (
            value.get("process_start_token") is not None
            and (
                not isinstance(value.get("process_start_token"), str)
                or not _SHA256_RE.fullmatch(str(value["process_start_token"]))
            )
        )
        or not isinstance(run, dict)
        or set(run) != {"operation", "ledger_sha256"}
        or run.get("operation") not in {"sample-run", "sample-retry", "doctor-repair"}
        or (
            run.get("ledger_sha256") is not None
            and (
                not isinstance(run.get("ledger_sha256"), str)
                or not _SHA256_RE.fullmatch(str(run["ledger_sha256"]))
            )
        )
        or not isinstance(tool, dict)
        or set(tool) != {"name", "version"}
        or tool.get("name") != "reasoning-payoff"
        or not isinstance(tool.get("version"), str)
        or not tool["version"]
    ):
        return None
    return value


@contextmanager
def exclusive_run_lock(
    base_dir: Path,
    *,
    operation: str,
    ledger_sha256: str | None,
) -> Iterator[None]:
    """Hold the O_EXCL/no-follow workspace lock for an entire operation."""
    lock_path = base_dir / LOCK_NAME
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except FileExistsError:
        raise LockSafetyError(
            "another sample operation holds the workspace output lock; run "
            "`reasoning-payoff sample doctor` for a safe diagnosis"
        ) from None
    locked = os.fstat(fd)
    try:
        payload = (
            json.dumps(
                _lock_metadata(operation=operation, ledger_sha256=ledger_sha256),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        os.write(fd, payload)
        os.fsync(fd)
        yield
    finally:
        os.close(fd)
        try:
            current = lock_path.lstat()
        except FileNotFoundError:
            return
        if (current.st_dev, current.st_ino) == (locked.st_dev, locked.st_ino):
            lock_path.unlink()


def diagnose_lock(
    base_dir: Path,
    *,
    current_host: str | None = None,
    liveness_probe: Callable[[int], PidLiveness] | None = None,
    token_reader: Callable[[int], str | None] | None = None,
) -> LockDiagnosis:
    """Classify the workspace lock without following or mutating it."""
    lock_path = base_dir / LOCK_NAME
    try:
        info = lock_path.lstat()
    except FileNotFoundError:
        return LockDiagnosis(
            "absent", False, "No workspace lock is present."
        )
    if stat.S_ISLNK(info.st_mode):
        return LockDiagnosis(
            "symlink",
            False,
            "The lock is a symlink. Do not repair automatically; inspect the "
            "workspace and remove it manually only after proving no run is active.",
        )
    if not stat.S_ISREG(info.st_mode) or info.st_size > 16_384:
        return LockDiagnosis(
            "malformed",
            False,
            "The lock is not a bounded regular metadata file. Inspect it manually "
            "and prove no run is active before removal.",
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags)
        try:
            opened = os.fstat(fd)
            content = os.read(fd, 16_385)
        finally:
            os.close(fd)
    except OSError:
        return LockDiagnosis(
            "unknown",
            False,
            "The lock changed or could not be read safely. Retry diagnosis; do "
            "not remove it automatically.",
        )
    inode = (opened.st_dev, opened.st_ino)
    if inode != (info.st_dev, info.st_ino) or len(content) > 16_384:
        return LockDiagnosis(
            "unknown",
            False,
            "The lock changed during diagnosis. Retry; do not remove it automatically.",
        )
    try:
        parsed = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None
    metadata = _valid_metadata(parsed)
    if metadata is None:
        return LockDiagnosis(
            "malformed",
            False,
            "Lock ownership metadata is malformed. Prove no run is active and "
            "remove it manually; automatic repair is disabled.",
            inode=inode,
            content_sha256=_sha256(content),
        )
    local_host = host_fingerprint() if current_host is None else current_host
    if metadata["host_fingerprint"] != local_host:
        return LockDiagnosis(
            "cross_host",
            False,
            "The lock belongs to another host fingerprint. Check that host or "
            "coordinate with the workspace owner; automatic repair is disabled.",
            metadata=metadata,
            inode=inode,
            content_sha256=_sha256(content),
        )
    pid = int(metadata["pid"])
    liveness = (liveness_probe or _pid_liveness)(pid)
    if liveness == "missing":
        return LockDiagnosis(
            "stale",
            True,
            "The owning PID no longer exists on this host. Use "
            "`sample doctor --repair-stale-lock` to repair it.",
            metadata=metadata,
            inode=inode,
            content_sha256=_sha256(content),
        )
    if liveness == "unknown":
        return LockDiagnosis(
            "unknown",
            False,
            "Process liveness could not be proven. Check the PID locally; "
            "automatic repair is disabled.",
            metadata=metadata,
            inode=inode,
            content_sha256=_sha256(content),
        )
    saved_token = metadata["process_start_token"]
    current_token = (token_reader or process_start_token)(pid)
    if (
        isinstance(saved_token, str)
        and current_token is not None
        and saved_token != current_token
    ):
        return LockDiagnosis(
            "pid_reuse",
            False,
            "The PID is live but its start token differs. Treat this as PID reuse; "
            "inspect manually and do not remove the lock automatically.",
            metadata=metadata,
            inode=inode,
            content_sha256=_sha256(content),
        )
    return LockDiagnosis(
        "live",
        False,
        "A process with the recorded PID is live. Wait for it to finish; a live "
        "lock is never removed.",
        metadata=metadata,
        inode=inode,
        content_sha256=_sha256(content),
    )


def remove_proven_stale_lock(base_dir: Path, diagnosis: LockDiagnosis) -> None:
    """Remove only the exact inode previously proven stale on this host."""
    if diagnosis.state != "stale" or not diagnosis.repairable or diagnosis.inode is None:
        raise LockSafetyError("lock removal requires a proven same-host stale diagnosis")
    lock_path = base_dir / LOCK_NAME
    try:
        current = lock_path.lstat()
    except FileNotFoundError:
        raise LockSafetyError("the diagnosed lock disappeared; retry diagnosis") from None
    if stat.S_ISLNK(current.st_mode) or (
        current.st_dev,
        current.st_ino,
    ) != diagnosis.inode:
        raise LockSafetyError("the diagnosed lock changed; retry diagnosis")
    lock_path.unlink()


def valid_owned_marker(directory: Path) -> bool:
    marker = directory / OWNED_MARKER_NAME
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        return marker.read_bytes() == OWNED_MARKER_BYTES
    except OSError:
        return False


def find_owned_staging_directories(base_dir: Path) -> list[Path]:
    """Return only marker-validated tool staging directories."""
    out_dir = base_dir / "out"
    runs_dir = out_dir / "runs"
    if not out_dir.exists():
        return []
    if (
        out_dir.is_symlink()
        or not out_dir.is_dir()
        or not valid_owned_marker(out_dir)
        or runs_dir.is_symlink()
        or not runs_dir.is_dir()
        or not valid_owned_marker(runs_dir)
    ):
        return []
    owned: list[Path] = []
    for child in runs_dir.iterdir():
        if (
            _STAGING_RE.fullmatch(child.name)
            and not child.is_symlink()
            and child.is_dir()
            and valid_owned_marker(child)
        ):
            owned.append(child)
    return sorted(owned, key=lambda path: path.name)


def clean_owned_staging_directories(base_dir: Path) -> int:
    """Delete only marker-validated hidden run staging directories."""
    owned = find_owned_staging_directories(base_dir)
    for path in owned:
        shutil.rmtree(path)
    return len(owned)


def record_repair_event(
    base_dir: Path,
    *,
    prior_lock_sha256: str | None,
    lock_removed: bool,
    staging_removed: int,
) -> None:
    """Append a bounded, secret-free recovery event."""
    path = base_dir / REPAIR_LOG_NAME
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and (
        stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode)
    ):
        raise LockSafetyError("repair event log is not an owned regular file")
    flags = os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    if before is None:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (
            before is not None
            and (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise LockSafetyError("repair event log is not a regular file")
        event = {
            "schema_version": "1.0.0",
            "event": "sample_workspace_repair",
            "created_at": _utc_iso(),
            "same_host_stale_lock_proven": lock_removed,
            "prior_lock_sha256": prior_lock_sha256,
            "staging_removed": staging_removed,
            "tool_version": __version__,
        }
        os.write(
            fd,
            (
                json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8"),
        )
        os.fsync(fd)
    finally:
        os.close(fd)


__all__ = [
    "LOCK_NAME",
    "OWNED_MARKER_BYTES",
    "OWNED_MARKER_NAME",
    "REPAIR_LOG_NAME",
    "LockDiagnosis",
    "LockSafetyError",
    "clean_owned_staging_directories",
    "diagnose_lock",
    "exclusive_run_lock",
    "find_owned_staging_directories",
    "host_fingerprint",
    "process_start_token",
    "record_repair_event",
    "remove_proven_stale_lock",
    "valid_owned_marker",
]
