from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from importlib import metadata
from pathlib import Path

import pytest

from batch_runner import __version__
from batch_runner.optional_dependencies import (
    EXTRA_REQUIREMENTS,
    OptionalDependencyError,
    require_extra,
)

ROOT = Path(__file__).resolve().parents[2]


def _project() -> dict[str, object]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]


def _requirement_map(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        name, minimum = value.split(">=", 1)
        result[name] = minimum
    return result


def test_dependency_metadata_and_runtime_checks_stay_in_parity() -> None:
    project = _project()
    optional = project["optional-dependencies"]
    expected = {
        extra: {
            requirement.distribution: requirement.minimum
            for requirement in requirements
        }
        for extra, requirements in EXTRA_REQUIREMENTS.items()
    }
    assert _requirement_map(optional["analysis"]) == expected["analysis"]
    assert _requirement_map(optional["azure"]) == expected["azure"]
    assert set(optional["all"]) == set(optional["analysis"]) | set(optional["azure"])
    assert project["version"] == __version__
    try:
        installed_version = metadata.version("when-reasoning-pays-off")
    except metadata.PackageNotFoundError:
        installed_version = __version__
    assert installed_version == __version__

    minimum_lines = (
        ROOT
        / "batch-runner/batch_runner/data/dependencies/minimum-direct.txt"
    ).read_text(encoding="utf-8").splitlines()
    minimum = dict(
        line.split("==", 1)
        for line in minimum_lines
        if line and not line.startswith("#")
    )
    assert minimum == {
        **_requirement_map(project["dependencies"]),
        **expected["analysis"],
        **expected["azure"],
    }


def test_cli_import_does_not_load_optional_stacks() -> None:
    code = (
        "import sys; import batch_runner.cli; "
        "blocked=('azure','matplotlib','numpy','openai','pandas'); "
        "loaded=sorted(n for n in sys.modules if n.split('.')[0] in blocked); "
        "assert not loaded, loaded"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT / 'batch-runner'}:{ROOT}"
    subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        env=env,
    )


def test_missing_extra_has_one_actionable_install_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_name: str) -> str:
        raise metadata.PackageNotFoundError

    monkeypatch.setattr(metadata, "version", missing)
    with pytest.raises(
        OptionalDependencyError,
        match=r'pip install "when-reasoning-pays-off\[analysis\]"',
    ):
        require_extra("analysis")


def test_unsupported_extra_reports_installed_and_required_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metadata, "version", lambda _name: "1.0.0")
    with pytest.raises(OptionalDependencyError) as caught:
        require_extra("azure")
    message = str(caught.value)
    assert "azure-identity 1.0.0 (requires >= 1.19.0)" in message
    assert "openai 1.0.0 (requires >= 2.37.0)" in message
