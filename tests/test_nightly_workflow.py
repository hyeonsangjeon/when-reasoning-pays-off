from __future__ import annotations

from pathlib import Path

import yaml

from scripts.sanitize_nightly_test_log import sanitize


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/nightly-full.yml"
RUNNER = ROOT / "scripts/run_nightly_offline_tests.sh"
CAMPAIGNS = (
    "tests/test_measure_max_output_tokens_sweep.py",
    "tests/test_measure_cache_key_bucketing.py",
    "tests/test_measure_dual_spillover.py",
)


def test_nightly_workflow_is_scheduled_and_dispatchable() -> None:
    data = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    triggers = data["on"]
    assert triggers["schedule"][0]["cron"]
    assert "workflow_dispatch" in triggers
    graph = data["jobs"]["offline-full"]["strategy"]["matrix"]["graph"]
    assert graph == ["locked", "current"]


def test_nightly_runner_collects_and_runs_every_campaign_without_ignore() -> None:
    workflow_text = WORKFLOW.read_text(encoding="utf-8")
    runner_text = RUNNER.read_text(encoding="utf-8")
    assert "scripts/run_nightly_offline_tests.sh" in workflow_text
    assert "--collect-only" in runner_text
    assert "batch-runner/tests" in runner_text
    assert "find tests -type f" in runner_text
    assert "--ignore" not in runner_text
    for campaign in CAMPAIGNS:
        assert campaign in runner_text


def test_nightly_execution_enables_socket_and_credential_guards() -> None:
    workflow_text = WORKFLOW.read_text(encoding="utf-8")
    runner_text = RUNNER.read_text(encoding="utf-8")
    guard_text = (
        ROOT / ".github/offline-python/sitecustomize.py"
    ).read_text(encoding="utf-8")
    assert ".github/offline-python" in runner_text
    assert "socket.socket.connect = _deny_network" in guard_text
    assert "unshare --net" in workflow_text
    assert "--require-hashes -r \"$test_lock\"" in workflow_text
    provider_prefix = "OPENAI_API_"
    empty_credential = "KEY=\"\""
    assert "AZURE_" + provider_prefix + empty_credential in runner_text
    assert provider_prefix + empty_credential in runner_text
    assert "Upload secret-safe nightly diagnostics" in workflow_text


def test_failure_log_sanitizer_excludes_runtime_content(tmp_path: Path) -> None:
    raw = tmp_path / "pytest.raw"
    safe = tmp_path / "pytest.log"
    raw.write_text(
        "FAILED tests/test_example.py::test_case[secret-alias] - AssertionError\n"
        "E endpoint=https://private.invalid prompt=private request_id=abc123\n"
        "1 failed in 0.10s\n",
        encoding="utf-8",
    )
    sanitize(raw, safe)
    content = safe.read_text(encoding="utf-8")
    assert "FAILED tests/test_example.py::test_case[PARAMETERS_REDACTED]" in content
    assert "1 failed in 0.10s" in content
    for forbidden in ("private.invalid", "secret-alias", "prompt=", "request_id="):
        assert forbidden not in content
