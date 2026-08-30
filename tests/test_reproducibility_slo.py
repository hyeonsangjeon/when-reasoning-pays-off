from __future__ import annotations

import argparse
import json
from pathlib import Path

import jsonschema
import pytest

from scripts import measure_cold_mock

ROOT = Path(__file__).resolve().parents[1]


def test_cold_mock_reference_threshold_and_report_schema() -> None:
    assert measure_cold_mock.DEFAULT_THRESHOLD_SECONDS == 300.0
    report = {
        "schema_version": "1.0.0",
        "contract_id": "cold-mock",
        "reference_label": "test",
        "commit": "a" * 40,
        "source_state": "clean",
        "environment": {
            "os": "Linux",
            "os_release": "test",
            "python": "3.13.7",
            "implementation": "CPython",
            "architecture": "x86_64",
        },
        "cache_policy": {
            "pip_no_cache_dir": True,
            "preexisting_venv_reused": False,
        },
        "start_point": "tracked files available in the source checkout",
        "end_point": "Mock run schemas, checksums, and immutable artifacts inspected",
        "started_at": "2026-08-30T00:00:00Z",
        "ended_at": "2026-08-30T00:01:00Z",
        "steps_seconds": {
            "checkout": 1.0,
            "venv": 2.0,
            "build": 3.0,
            "install": 4.0,
            "help": 0.1,
            "sample_init": 0.1,
            "sample_run": 0.2,
            "artifact_inspection": 0.1,
        },
        "total_seconds": 10.5,
        "threshold_seconds": 300.0,
        "passed": True,
        "error_step": None,
        "error_type": None,
    }
    schema = json.loads(
        (ROOT / "schemas/cold_mock_timing.v1.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(report, schema)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_cold_mock_threshold_rejects_nonpositive_or_nonfinite(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        measure_cold_mock._positive_finite_seconds(value)


@pytest.mark.parametrize(
    "value",
    ["", "/tmp/report", "user@example.com", "x" * 81, "contains space"],
)
def test_cold_mock_reference_label_rejects_sensitive_shapes(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        measure_cold_mock._safe_reference_label(value)


def test_python_support_is_bounded() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.11,<3.14"' in pyproject
    capabilities = json.loads(
        (
            ROOT / "batch-runner/batch_runner/data/cli_capabilities.v1.json"
        ).read_text(encoding="utf-8")
    )
    assert capabilities["python_support"]["ci_endpoints"] == ["3.11", "3.13"]
    assert capabilities["platform_support"]["full_research_campaign"]["windows"].startswith(
        "unsupported"
    )
