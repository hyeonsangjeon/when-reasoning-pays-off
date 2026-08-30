#!/usr/bin/env python3
"""Generate the cache-disabled Cold Mock reference timing report."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import venv
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Callable, TypeVar

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_THRESHOLD_SECONDS = 300.0
_REFERENCE_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
T = TypeVar("T")


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _positive_finite_seconds(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


def _safe_reference_label(value: str) -> str:
    if not _REFERENCE_LABEL_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "must be 1-80 ASCII letters, digits, dots, underscores, or hyphens"
        )
    return value


def _run(command: list[str], *, cwd: Path, capture: bool = False) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        env={
            **os.environ,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    return completed.stdout if capture else ""


def _timed(
    name: str, steps: dict[str, float], operation: Callable[[], T]
) -> T:
    started = time.perf_counter()
    try:
        return operation()
    finally:
        steps[name] = round(time.perf_counter() - started, 6)


def _copy_tracked_checkout(source: Path, destination: Path) -> None:
    raw = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=source,
        check=True,
        capture_output=True,
    ).stdout
    destination.mkdir(parents=True)
    for encoded in raw.split(b"\0"):
        if not encoded:
            continue
        relative = PurePosixPath(os.fsdecode(encoded))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("git returned an unsafe tracked path")
        source_path = source.joinpath(*relative.parts)
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source_path.is_symlink():
            target.symlink_to(os.readlink(source_path))
        elif source_path.is_file():
            shutil.copy2(source_path, target, follow_symlinks=False)
        else:
            raise RuntimeError("tracked path is missing or unsupported")


def _venv_paths(directory: Path) -> tuple[Path, Path]:
    scripts = directory / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    cli = scripts / ("reasoning-payoff.exe" if os.name == "nt" else "reasoning-payoff")
    return python, cli


def _inspect_artifacts(workspace: Path, run_payload: dict[str, object], runtime: Path) -> None:
    run_id = str(run_payload["run_id"])
    run_dir = workspace / "out" / "runs" / run_id
    expected = {
        ".reasoning-payoff-experiment-owned",
        "artifacts.sha256",
        "manifest.json",
        "records.jsonl",
        "run.json",
        "summary.md",
    }
    if {entry.name for entry in run_dir.iterdir()} != expected:
        raise RuntimeError("Mock run artifact set is incomplete")
    checksums = (run_dir / "artifacts.sha256").read_text(encoding="ascii").splitlines()
    for line in checksums:
        digest, name = line.split("  ", 1)
        actual = hashlib.sha256((run_dir / name).read_bytes()).hexdigest()
        if digest != actual:
            raise RuntimeError("Mock run artifact checksum mismatch")
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    latest = json.loads((workspace / "out" / "latest.json").read_text(encoding="utf-8"))
    if (
        run["status"] != "ok"
        or manifest["run_id"] != run_id
        or latest["run_id"] != run_id
        or len((run_dir / "records.jsonl").read_text(encoding="utf-8").splitlines()) != 3
    ):
        raise RuntimeError("Mock run structured artifacts are inconsistent")
    schema_dir = runtime / "share" / "when-reasoning-pays-off" / "schemas"
    for name in (
        "experiment_run.v2.schema.json",
        "experiment_run_manifest.v1.schema.json",
        "experiment_latest_pointer.v1.schema.json",
        "cold_mock_timing.v1.schema.json",
    ):
        if not (schema_dir / name).is_file():
            raise RuntimeError("installed wheel is missing a required schema")


def measure(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    source = args.source.resolve()
    commit = _run(["git", "rev-parse", "HEAD"], cwd=source, capture=True).strip()
    dirty = bool(_run(["git", "status", "--porcelain"], cwd=source, capture=True).strip())
    steps: dict[str, float] = {}
    started_at = _utc_now()
    total_started = time.perf_counter()
    error_step: str | None = None
    error_type: str | None = None
    with tempfile.TemporaryDirectory(prefix="cold-mock-") as temporary:
        root = Path(temporary)
        checkout = root / "checkout"
        runtime = root / "runtime"
        dist = root / "dist"
        workspace = root / "sample-workspace"
        try:
            error_step = "checkout"
            _timed("checkout", steps, lambda: _copy_tracked_checkout(source, checkout))
            error_step = "venv"
            _timed("venv", steps, lambda: venv.EnvBuilder(with_pip=True).create(runtime))
            python, cli = _venv_paths(runtime)
            error_step = "build"
            _timed(
                "build",
                steps,
                lambda: _run(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "wheel",
                        ".",
                        "--no-deps",
                        "--no-cache-dir",
                        "--wheel-dir",
                        str(dist),
                    ],
                    cwd=checkout,
                ),
            )
            wheel = next(dist.glob("*.whl"))
            error_step = "install"
            _timed(
                "install",
                steps,
                lambda: _run(
                    [str(python), "-m", "pip", "install", "--no-cache-dir", str(wheel)],
                    cwd=root,
                ),
            )
            error_step = "help"
            _timed("help", steps, lambda: _run([str(cli), "--help"], cwd=root, capture=True))
            error_step = "sample_init"
            _timed(
                "sample_init",
                steps,
                lambda: _run(
                    [str(cli), "sample", "init", "--provider", "mock", "--out", str(workspace)],
                    cwd=root,
                    capture=True,
                ),
            )
            error_step = "sample_run"
            run_text = _timed(
                "sample_run",
                steps,
                lambda: _run(
                    [
                        str(cli),
                        "sample",
                        "run",
                        "--ledger",
                        str(workspace / "ledger.yaml"),
                        "--json",
                    ],
                    cwd=root,
                    capture=True,
                ),
            )
            run_payload = json.loads(run_text)
            error_step = "artifact_inspection"
            _timed(
                "artifact_inspection",
                steps,
                lambda: _inspect_artifacts(workspace, run_payload, runtime),
            )
            error_step = None
        except (OSError, RuntimeError, ValueError, StopIteration, subprocess.SubprocessError) as exc:
            error_type = type(exc).__name__
        total = round(time.perf_counter() - total_started, 6)
        ended_at = _utc_now()

    passed = error_type is None and total <= args.threshold_seconds
    report: dict[str, object] = {
        "schema_version": "1.0.0",
        "contract_id": "cold-mock",
        "reference_label": args.reference_label,
        "commit": commit,
        "source_state": "dirty" if dirty else "clean",
        "environment": {
            "os": platform.system() or "unknown",
            "os_release": platform.release() or "unknown",
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "architecture": platform.machine() or "unknown",
        },
        "cache_policy": {
            "pip_no_cache_dir": True,
            "preexisting_venv_reused": False,
        },
        "start_point": "tracked files available in the source checkout",
        "end_point": "Mock run schemas, checksums, and immutable artifacts inspected",
        "started_at": started_at,
        "ended_at": ended_at,
        "steps_seconds": steps,
        "total_seconds": total,
        "threshold_seconds": args.threshold_seconds,
        "passed": passed,
        "error_step": error_step,
        "error_type": error_type,
    }
    return report, 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=Path("cold-mock-timing.json"))
    parser.add_argument(
        "--threshold-seconds",
        type=_positive_finite_seconds,
        default=DEFAULT_THRESHOLD_SECONDS,
    )
    parser.add_argument(
        "--reference-label", type=_safe_reference_label, default="local"
    )
    args = parser.parse_args()
    report, exit_code = measure(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"Cold Mock: {report['total_seconds']:.3f}s / "
        f"{report['threshold_seconds']:.1f}s "
        f"({'PASS' if report['passed'] else 'FAIL'})"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
