#!/usr/bin/env python3
"""Build and verify the minimal core wheel outside the source checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(
    command: list[str],
    *,
    cwd: Path,
    expected: int = 0,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        env={
            **os.environ,
            "AZURE_OPENAI_API_KEY": "",
            "OPENAI_API_KEY": "",
            "HF_TOKEN": "",
            "CI": "true",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    if completed.returncode != expected:
        raise RuntimeError(
            f"command returned {completed.returncode}; expected {expected}: "
            f"{completed.stderr or completed.stdout or '<no output>'}"
        )
    return completed


def _venv_paths(directory: Path) -> tuple[Path, Path]:
    scripts = directory / ("Scripts" if os.name == "nt" else "bin")
    return (
        scripts / ("python.exe" if os.name == "nt" else "python"),
        scripts / ("reasoning-payoff.exe" if os.name == "nt" else "reasoning-payoff"),
    )


def _json_command(command: list[str], *, cwd: Path) -> dict[str, object]:
    return json.loads(_run(command, cwd=cwd, capture=True).stdout)


def _verify_checksum_file(run_dir: Path) -> None:
    for line in (run_dir / "artifacts.sha256").read_text(encoding="ascii").splitlines():
        expected, name = line.split("  ", 1)
        actual = hashlib.sha256((run_dir / name).read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError("immutable artifact checksum mismatch")


def verify(source: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="core-wheel-") as temporary:
        root = Path(temporary)
        wheel_dir = root / "wheel"
        runtime = root / "runtime"
        workspace = root / "mock"
        _run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                ".",
                "--no-deps",
                "--no-cache-dir",
                "--wheel-dir",
                str(wheel_dir),
            ],
            cwd=source,
        )
        venv.EnvBuilder(with_pip=True).create(runtime)
        python, cli = _venv_paths(runtime)
        wheel = next(wheel_dir.glob("*.whl"))
        _run(
            [str(python), "-m", "pip", "install", "--no-cache-dir", str(wheel)],
            cwd=root,
        )
        _run([str(cli), "--help"], cwd=root, capture=True)
        installed = _run(
            [
                str(python),
                "-c",
                (
                    "import importlib.metadata as m,json;"
                    "names=('azure-identity','matplotlib','numpy','openai','pandas');"
                    "print(json.dumps([n for n in names "
                    "if n in {d.metadata['Name'].lower() for d in m.distributions()}]))"
                ),
            ],
            cwd=root,
            capture=True,
        )
        if json.loads(installed.stdout):
            raise RuntimeError("optional extra leaked into the minimal core wheel")

        _run(
            [str(cli), "sample", "init", "--provider", "mock", "--out", str(workspace)],
            cwd=root,
            capture=True,
        )
        ledger = workspace / "ledger.yaml"
        doctor_before = _json_command(
            [str(cli), "sample", "doctor", "--ledger", str(ledger), "--json"],
            cwd=root,
        )
        if doctor_before["status"] != "ok":
            raise RuntimeError("fresh Mock workspace failed doctor")
        first = _json_command(
            [str(cli), "sample", "run", "--ledger", str(ledger), "--json"],
            cwd=root,
        )
        first_dir = workspace / "out" / "runs" / str(first["run_id"])
        _verify_checksum_file(first_dir)
        first_manifest_hash = hashlib.sha256(
            (first_dir / "manifest.json").read_bytes()
        ).hexdigest()
        retry = _run(
            [
                str(cli),
                "sample",
                "retry-failed",
                "--ledger",
                str(ledger),
                "--parent-run-id",
                str(first["run_id"]),
            ],
            cwd=root,
            expected=5,
            capture=True,
        )
        if "no failed rows to retry" not in retry.stderr:
            raise RuntimeError("retry-failed did not fail closed for a successful parent")
        second = _json_command(
            [str(cli), "sample", "run", "--ledger", str(ledger), "--json"],
            cwd=root,
        )
        if first["run_id"] == second["run_id"]:
            raise RuntimeError("second Mock run reused an immutable run directory")
        if (
            hashlib.sha256((first_dir / "manifest.json").read_bytes()).hexdigest()
            != first_manifest_hash
        ):
            raise RuntimeError("later Mock run mutated the first manifest")
        _verify_checksum_file(workspace / "out" / "runs" / str(second["run_id"]))
        doctor_after = _json_command(
            [str(cli), "sample", "doctor", "--ledger", str(ledger), "--json"],
            cwd=root,
        )
        if (
            doctor_after["workspace"]["output"]["completed_runs"] != 2
            or doctor_after["workspace"]["output"]["latest"] != "valid"
        ):
            raise RuntimeError("doctor did not verify immutable Mock runs")

        catalog = _json_command([str(cli), "experiment", "list", "--json"], cwd=root)
        described = _json_command(
            [
                str(cli),
                "experiment",
                "describe",
                "exp001_short-factual_baseline",
                "--json",
            ],
            cwd=root,
        )
        if (
            catalog["experiment_count"] != 20
            or described["experiment_id"] != "exp001_short-factual_baseline"
        ):
            raise RuntimeError("packaged experiment catalog is incomplete")
        analysis = _run(
            [
                str(cli),
                "analyze",
                "missing.jsonl",
                "--workload",
                "missing.yaml",
                "--out",
                "report",
            ],
            cwd=root,
            expected=8,
            capture=True,
        )
        if 'pip install "when-reasoning-pays-off[analysis]"' not in analysis.stderr:
            raise RuntimeError("analysis extra failure was not actionable")
        azure = root / "azure"
        _run(
            [str(cli), "sample", "init", "--provider", "azure", "--out", str(azure)],
            cwd=root,
            capture=True,
        )
        azure_run = _run(
            [
                str(cli),
                "sample",
                "run",
                "--ledger",
                str(azure / "ledger.yaml"),
                "--confirm-cost",
            ],
            cwd=root,
            expected=8,
            capture=True,
        )
        if 'pip install "when-reasoning-pays-off[azure]"' not in azure_run.stderr:
            raise RuntimeError("Azure extra failure was not actionable")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT)
    args = parser.parse_args()
    verify(args.source.resolve())
    print("minimal core wheel smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
