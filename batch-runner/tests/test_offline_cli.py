"""End-to-end contract tests for the offline-first provenance CLI."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import socket
import time
from importlib import resources
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import batch_runner.reporting as reporting
from batch_runner.cli import main
from batch_runner.contracts import (
    MAX_TOKEN_COUNT,
    MAX_USAGE_FILE_BYTES,
    InputValidationError,
    UsageEnvelope,
    WorkloadSpec,
    load_usage_jsonl,
)
from batch_runner.reporting import (
    GENERATED_ARTIFACTS,
    OutputConflictError,
    ReportValidationError,
    _h,
    analyze_files,
    validate_report,
    write_report_bundle,
)
from batch_runner.report_contracts import SourceRows
from scripts.cost_calculator import TokenUsage, load_payg_pricing, payg_cost_per_call

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_DIR = REPO_ROOT / "examples" / "five-minute"


def _sample_report() -> dict:
    return analyze_files(
        EXAMPLE_DIR / "usage.jsonl",
        EXAMPLE_DIR / "workload.yaml",
    )


def _copy_sample_inputs(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name in (
        "usage.jsonl",
        "workload.yaml",
        "pricing.yaml",
        "ptu-pricing.yaml",
        "density.yaml",
    ):
        shutil.copyfile(EXAMPLE_DIR / name, target / name)


def test_packaged_init_resources_match_documented_examples_byte_for_byte():
    data_root = resources.files("batch_runner.data")
    pairs = {
        "sample_usage.jsonl": "usage.jsonl",
        "sample_workload.yaml": "workload.yaml",
        "sample_pricing.yaml": "pricing.yaml",
        "sample_ptu_pricing.yaml": "ptu-pricing.yaml",
        "sample_density.yaml": "density.yaml",
    }
    for resource_name, example_name in pairs.items():
        assert data_root.joinpath(resource_name).read_bytes() == (
            EXAMPLE_DIR / example_name
        ).read_bytes()


def test_checked_in_schemas_validate_examples():
    jsonschema = pytest.importorskip("jsonschema")
    usage_schema = json.loads(
        (REPO_ROOT / "schemas/usage_envelope.v1.schema.json").read_text()
    )
    workload_schema = json.loads(
        (REPO_ROOT / "schemas/workload_spec.v1.schema.json").read_text()
    )
    jsonschema.Draft7Validator.check_schema(usage_schema)
    jsonschema.Draft7Validator.check_schema(workload_schema)
    for line in (EXAMPLE_DIR / "usage.jsonl").read_text().splitlines():
        jsonschema.validate(json.loads(line), usage_schema)
    jsonschema.validate(
        yaml.safe_load((EXAMPLE_DIR / "workload.yaml").read_text()),
        workload_schema,
    )
    unsafe_usage = json.loads(
        (EXAMPLE_DIR / "usage.jsonl").read_text().splitlines()[0]
    )
    unsafe_usage["model"] = "a" * 64
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(unsafe_usage, usage_schema)
    for prohibited in (
        "123e4567-e89b-12d3-a456-426614174000",
        "01890f3c-7b89-7cc8-98c4-dc0c0c07398f",
        "example.com",
        "10.23.45.67",
        "model-10.23.45.67",
        "2001:db8::1",
        "10.23.45.67",
        "model-10.23.45.67",
        "2001:db8::1",
    ):
        unsafe_usage["model"] = prohibited
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(unsafe_usage, usage_schema)
    unsafe_workload = yaml.safe_load((EXAMPLE_DIR / "workload.yaml").read_text())
    unsafe_workload["name"] = "customer@example.com"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(unsafe_workload, workload_schema)
    for prohibited in (
        "123e4567-e89b-12d3-a456-426614174000",
        "01890f3c-7b89-7cc8-98c4-dc0c0c07398f",
        "example.com",
        "10.23.45.67",
        "workload-10.23.45.67",
        "2001:db8::1",
    ):
        unsafe_workload["name"] = prohibited
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(unsafe_workload, workload_schema)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_tokens", "1200"),
        ("cached_input_tokens", "0"),
        ("output_tokens", "180"),
        ("reasoning_tokens", "0"),
        ("latency_ms", "1450"),
        ("status_code", "200"),
    ],
)
def test_usage_contract_rejects_numeric_strings(field: str, value: str):
    payload = json.loads((EXAMPLE_DIR / "usage.jsonl").read_text().splitlines()[0])
    payload[field] = value
    with pytest.raises(ValidationError):
        UsageEnvelope.model_validate(payload)


def test_workload_contract_rejects_numeric_strings():
    payload = yaml.safe_load((EXAMPLE_DIR / "workload.yaml").read_text())
    payload["ptu_sizing"]["expected_rpm"] = "120"
    with pytest.raises(ValidationError):
        WorkloadSpec.model_validate(payload)


def test_usage_loader_rejects_oversized_file_before_reading(tmp_path: Path):
    path = tmp_path / "oversized.jsonl"
    with path.open("wb") as handle:
        handle.seek(MAX_USAGE_FILE_BYTES)
        handle.write(b"\n")
    with pytest.raises(InputValidationError, match="byte safety limit"):
        load_usage_jsonl(path)


def test_extreme_token_count_fails_safely_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    payload = json.loads((EXAMPLE_DIR / "usage.jsonl").read_text().splitlines()[0])
    payload["input_tokens"] = MAX_TOKEN_COUNT + 1
    usage = tmp_path / "usage.jsonl"
    usage.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    code = main(
        [
            "analyze",
            str(usage),
            "--workload",
            str(EXAMPLE_DIR / "workload.yaml"),
            "--out",
            str(tmp_path / "bundle"),
        ]
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "input_tokens" in captured.err
    assert "Traceback" not in captured.err
    assert not (tmp_path / "bundle").exists()


def test_extreme_json_numbers_fail_with_documented_input_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    payload = json.loads((EXAMPLE_DIR / "usage.jsonl").read_text().splitlines()[0])
    payload["latency_ms"] = 10**400
    usage = tmp_path / "usage.jsonl"
    usage.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    code = main(
        [
            "analyze",
            str(usage),
            "--workload",
            str(EXAMPLE_DIR / "workload.yaml"),
            "--out",
            str(tmp_path / "bundle"),
        ]
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "Traceback" not in captured.err
    assert not (tmp_path / "bundle").exists()


def test_huge_json_integer_fails_with_documented_input_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    line = (EXAMPLE_DIR / "usage.jsonl").read_text().splitlines()[0]
    line = line.replace('"input_tokens":1200', '"input_tokens":' + ("9" * 5000))
    usage = tmp_path / "usage.jsonl"
    usage.write_text(line + "\n", encoding="utf-8")
    code = main(
        [
            "analyze",
            str(usage),
            "--workload",
            str(EXAMPLE_DIR / "workload.yaml"),
            "--out",
            str(tmp_path / "bundle"),
        ]
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "Traceback" not in captured.err
    assert not (tmp_path / "bundle").exists()


def test_huge_yaml_integer_fails_with_documented_input_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    workload = (EXAMPLE_DIR / "workload.yaml").read_text()
    workload = workload.replace(
        "expected_rpm: 120.0",
        "expected_rpm: " + ("9" * 5000),
    )
    workload_path = tmp_path / "workload.yaml"
    workload_path.write_text(workload, encoding="utf-8")
    for name in ("pricing.yaml", "ptu-pricing.yaml", "density.yaml"):
        (tmp_path / name).write_bytes((EXAMPLE_DIR / name).read_bytes())
    code = main(
        [
            "analyze",
            str(EXAMPLE_DIR / "usage.jsonl"),
            "--workload",
            str(workload_path),
            "--out",
            str(tmp_path / "bundle"),
        ]
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "Traceback" not in captured.err
    assert not (tmp_path / "bundle").exists()


def test_snapshot_path_with_control_character_fails_safely(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    workload = yaml.safe_load((EXAMPLE_DIR / "workload.yaml").read_text())
    workload["pricing"]["snapshot_file"] = "pricing\x00.yaml"
    path = tmp_path / "workload.yaml"
    path.write_text(yaml.safe_dump(workload, sort_keys=False), encoding="utf-8")
    code = main(
        [
            "analyze",
            str(EXAMPLE_DIR / "usage.jsonl"),
            "--workload",
            str(path),
            "--out",
            str(tmp_path / "bundle"),
        ]
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "Traceback" not in captured.err
    assert not (tmp_path / "bundle").exists()


def test_init_analyze_report_work_outside_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    sample = tmp_path / ".reasoning-payoff"
    assert {path.name for path in sample.iterdir()} == {
        "usage.jsonl",
        "workload.yaml",
        "pricing.yaml",
        "ptu-pricing.yaml",
        "density.yaml",
    }
    assert (
        main(
            [
                "analyze",
                str(sample / "usage.jsonl"),
                "--workload",
                str(sample / "workload.yaml"),
                "--out",
                "bundle",
            ]
        )
        == 0
    )
    bundle = tmp_path / "bundle"
    assert {path.name for path in bundle.iterdir()} == set(GENERATED_ARTIFACTS)
    before = {name: (bundle / name).read_bytes() for name in GENERATED_ARTIFACTS}
    assert main(["report", str(bundle / "report.json")]) == 0
    after = {name: (bundle / name).read_bytes() for name in GENERATED_ARTIFACTS}
    assert before == after


def test_sample_analyze_uses_no_network(monkeypatch: pytest.MonkeyPatch):
    def blocked_connect(*_args, **_kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)
    report = _sample_report()
    assert report["boundaries"] == {
        "usage_metrics": "MEASURED",
        "payg_cost": "MODELED",
        "quality": "NOT_MEASURED",
        "ptu_sizing": "MODELED",
    }


def test_sample_completes_well_under_one_minute():
    started = time.monotonic()
    report = _sample_report()
    assert report["input"]["row_count"] == 8
    assert time.monotonic() - started < 60


def test_reasoning_tokens_are_not_double_billed():
    report = _sample_report()
    high = next(
        group
        for group in report["groups"]
        if group["reasoning_effort"] == "high"
    )
    assert high["output_tokens"] == 990
    assert high["reasoning_tokens"] == 560
    pricing = load_payg_pricing(EXAMPLE_DIR / "pricing.yaml")
    rows, _ = load_usage_jsonl(EXAMPLE_DIR / "usage.jsonl")
    high_rows = [row for row in rows if row.reasoning_effort == "high"]
    expected = sum(
        payg_cost_per_call(
            TokenUsage(
                input_tokens=row.input_tokens,
                cached_tokens=row.cached_input_tokens,
                output_tokens=row.output_tokens,
                reasoning_tokens=row.reasoning_tokens,
            ),
            pricing,
            row.model,
        ).usd_per_request
        for row in high_rows
    ) / len(high_rows)
    assert high["mean_modeled_usd_per_request"] == pytest.approx(expected)
    hypothetical_double_bill = expected + (560 * 14.0 / 1_000_000) / 3
    assert high["mean_modeled_usd_per_request"] < hypothetical_double_bill


def test_missing_ptu_block_is_explicitly_not_modeled(tmp_path: Path):
    workload = yaml.safe_load((EXAMPLE_DIR / "workload.yaml").read_text())
    workload.pop("ptu_sizing")
    workload_path = tmp_path / "workload.yaml"
    workload_path.write_text(yaml.safe_dump(workload, sort_keys=False))
    (tmp_path / "pricing.yaml").write_bytes(
        (EXAMPLE_DIR / "pricing.yaml").read_bytes()
    )
    report = analyze_files(EXAMPLE_DIR / "usage.jsonl", workload_path)
    assert report["boundaries"]["ptu_sizing"] == "NOT_MODELED"
    assert report["ptu_sizing"]["missing_inputs"] == [
        "expected_rpm",
        "mean_max_output_tokens",
    ]


def test_unsupported_ptu_model_is_explicitly_not_modeled(
    tmp_path: Path,
):
    _copy_sample_inputs(tmp_path)
    rows = [
        json.loads(line)
        for line in (tmp_path / "usage.jsonl").read_text().splitlines()
    ]
    for row in rows:
        row["model"] = "gpt-4o"
        row["reasoning_tokens"] = 0
    (tmp_path / "usage.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    report = analyze_files(tmp_path / "usage.jsonl", tmp_path / "workload.yaml")
    assert report["boundaries"]["ptu_sizing"] == "NOT_MODELED"
    assert report["ptu_sizing"]["reason"] == (
        "pinned PTU snapshots do not support the analyzed model"
    )


def test_ptu_max_output_cap_must_cover_visible_and_reasoning_output(
    tmp_path: Path,
):
    _copy_sample_inputs(tmp_path)
    workload = yaml.safe_load((tmp_path / "workload.yaml").read_text())
    workload["ptu_sizing"]["mean_max_output_tokens"] = 300
    (tmp_path / "workload.yaml").write_text(
        yaml.safe_dump(workload, sort_keys=False),
        encoding="utf-8",
    )
    with pytest.raises(
        InputValidationError,
        match="ptu_sizing.mean_max_output_tokens",
    ):
        analyze_files(tmp_path / "usage.jsonl", tmp_path / "workload.yaml")


def test_analyze_refuses_nonempty_output_without_touching_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    out = tmp_path / "bundle"
    out.mkdir()
    sentinel = out / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    code = main(
        [
            "analyze",
            str(EXAMPLE_DIR / "usage.jsonl"),
            "--workload",
            str(EXAMPLE_DIR / "workload.yaml"),
            "--out",
            str(out),
        ]
    )
    assert code == 5
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert {path.name for path in out.iterdir()} == {"keep.txt"}
    assert "must be empty" in capsys.readouterr().err


def test_rerender_refuses_unowned_four_file_directory(tmp_path: Path):
    out = tmp_path / "bundle"
    out.mkdir()
    original = {
        name: f"unrelated {name}".encode()
        for name in GENERATED_ARTIFACTS
    }
    for name, data in original.items():
        (out / name).write_bytes(data)
    with pytest.raises(OutputConflictError, match="not owned"):
        write_report_bundle(
            _sample_report(),
            out,
            allow_existing_generated=True,
        )
    assert {
        name: (out / name).read_bytes()
        for name in GENERATED_ARTIFACTS
    } == original
    assert (tmp_path / ".bundle.reasoning-payoff.lock").read_bytes() == (
        reporting._LOCK_MARKER_BYTES
    )


def test_bundle_marker_cannot_authorize_mismatched_files(tmp_path: Path):
    out = tmp_path / "bundle"
    out.mkdir()
    for name in GENERATED_ARTIFACTS:
        (out / name).write_text(f"unrelated {name}", encoding="utf-8")
    (tmp_path / ".bundle.reasoning-payoff.lock").write_bytes(
        reporting._LOCK_MARKER_BYTES
    )
    with pytest.raises(OutputConflictError, match="does not match"):
        write_report_bundle(
            _sample_report(),
            out,
            allow_existing_generated=True,
        )
    assert (out / "report.json").read_text(encoding="utf-8") == (
        "unrelated report.json"
    )


def test_staged_rerender_restores_original_bundle_on_swap_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    report = _sample_report()
    out = tmp_path / "bundle"
    write_report_bundle(report, out, allow_existing_generated=False)
    original = {name: (out / name).read_bytes() for name in GENERATED_ARTIFACTS}
    real_replace = os.replace
    failed = False

    def fail_stage_swap(source, destination):
        nonlocal failed
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            not failed
            and destination_path == out
            and source_path.name == ".bundle.staging"
        ):
            failed = True
            raise OSError("injected swap failure")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_stage_swap)
    with pytest.raises(OSError, match="injected"):
        write_report_bundle(report, out, allow_existing_generated=True)
    assert {name: (out / name).read_bytes() for name in GENERATED_ARTIFACTS} == original


def test_rerender_recovers_abandoned_backup_and_stage(tmp_path: Path):
    report = _sample_report()
    out = tmp_path / "bundle"
    write_report_bundle(report, out, allow_existing_generated=False)
    expected = {name: (out / name).read_bytes() for name in GENERATED_ARTIFACTS}
    backup = tmp_path / ".bundle.backup"
    stage = tmp_path / ".bundle.staging"
    os.replace(out, backup)
    (backup / reporting._RECOVERY_MARKER_NAME).write_bytes(
        reporting._RECOVERY_MARKER_BYTES
    )
    stage.mkdir()
    (stage / reporting._RECOVERY_MARKER_NAME).write_bytes(
        reporting._RECOVERY_MARKER_BYTES
    )
    (stage / "report.json").write_text("partial", encoding="utf-8")
    write_report_bundle(report, out, allow_existing_generated=True)
    assert {name: (out / name).read_bytes() for name in GENERATED_ARTIFACTS} == expected
    assert not backup.exists()
    assert not stage.exists()


@pytest.mark.parametrize("recovery_name", [".bundle.staging", ".bundle.backup"])
def test_unowned_recovery_directory_is_never_deleted(
    recovery_name: str,
    tmp_path: Path,
):
    recovery = tmp_path / recovery_name
    recovery.mkdir()
    sentinel = recovery / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(OutputConflictError, match="not owned"):
        write_report_bundle(
            _sample_report(),
            tmp_path / "bundle",
            allow_existing_generated=True,
        )
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert {entry.name for entry in recovery.iterdir()} == {"keep.txt"}


@pytest.mark.parametrize("recovery_name", [".bundle.staging", ".bundle.backup"])
def test_invalid_marked_recovery_directory_is_never_deleted(
    recovery_name: str,
    tmp_path: Path,
):
    recovery = tmp_path / recovery_name
    recovery.mkdir()
    (recovery / reporting._RECOVERY_MARKER_NAME).write_bytes(
        reporting._RECOVERY_MARKER_BYTES
    )
    sentinel = recovery / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(OutputConflictError, match="unexpected content"):
        write_report_bundle(
            _sample_report(),
            tmp_path / "bundle",
            allow_existing_generated=True,
        )
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert {entry.name for entry in recovery.iterdir()} == {
        reporting._RECOVERY_MARKER_NAME,
        "keep.txt",
    }


def test_concurrent_bundle_writer_is_rejected(tmp_path: Path):
    out = tmp_path / "bundle"
    with reporting._bundle_lock(out):
        with pytest.raises(OutputConflictError, match="another process"):
            write_report_bundle(
                _sample_report(),
                out,
                allow_existing_generated=False,
            )


def test_unowned_bundle_lock_is_never_modified(tmp_path: Path):
    out = tmp_path / "bundle"
    lock = tmp_path / ".bundle.reasoning-payoff.lock"
    lock.write_text("user content", encoding="utf-8")
    with pytest.raises(OutputConflictError, match="lock is not owned"):
        write_report_bundle(
            _sample_report(),
            out,
            allow_existing_generated=False,
        )
    assert lock.read_text(encoding="utf-8") == "user content"


def test_stale_owned_lock_does_not_authorize_unrelated_bundle(tmp_path: Path):
    out = tmp_path / "bundle"
    report = _sample_report()
    write_report_bundle(report, out, allow_existing_generated=False)
    shutil.rmtree(out)
    out.mkdir()
    for name in GENERATED_ARTIFACTS:
        (out / name).write_text("unrelated user content", encoding="utf-8")
    with pytest.raises(OutputConflictError, match="does not match"):
        write_report_bundle(report, out, allow_existing_generated=True)
    assert {
        name: (out / name).read_text(encoding="utf-8")
        for name in GENERATED_ARTIFACTS
    } == {name: "unrelated user content" for name in GENERATED_ARTIFACTS}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda report: report["boundaries"].__setitem__("quality", "MEASURED"),
        lambda report: report["policy"].__setitem__("auto_apply", True),
        lambda report: report["policy"]["candidates"][0]["evidence"][0].__setitem__(
            "value", "MEASURED"
        ),
        lambda report: report["aggregate"].__setitem__("status_429_rate", 2.0),
        lambda report: report["aggregate"].__setitem__("status_429_rate", 0.0),
        lambda report: report["groups"][0].__setitem__(
            "request_count", report["groups"][0]["request_count"] + 1
        ),
        lambda report: report["privacy"].__setitem__(
            "unknown_input_fields_accepted", True
        ),
        lambda report: report["conclusions"][0]["provenance"].__setitem__(
            "claim_registry_sha256", "0" * 64
        ),
        lambda report: report["workload"].__setitem__("unknown", "value"),
        lambda report: report["conclusions"][0].__setitem__(
            "finding", "NOT_MEASURED, but quality is preserved."
        ),
        lambda report: report["conclusions"][0].__setitem__(
            "assumptions", ["Quality is preserved."]
        ),
    ],
)
def test_pinned_report_nested_mutations_fail_closed(mutate):
    report = copy.deepcopy(_sample_report())
    mutate(report)
    with pytest.raises(ReportValidationError, match="nested contract"):
        validate_report(report)


def test_pinned_utc_fields_require_fixed_zulu_representation():
    report = copy.deepcopy(_sample_report())
    report["input"]["window_start_utc"] = "2026-08-20T09:00:00+09:00"
    with pytest.raises(ReportValidationError, match="nested contract"):
        validate_report(report)


def test_pinned_row_count_is_bounded_before_range_allocation():
    report = copy.deepcopy(_sample_report())
    report["input"]["row_count"] = 10**100
    with pytest.raises(ReportValidationError, match="nested contract"):
        validate_report(report)


def test_source_ranges_reject_oversized_endpoints_without_expansion():
    with pytest.raises(ValidationError):
        SourceRows.model_validate(
            {"ranges": ["1-1000000000"], "count": 100_000}
        )


def test_pinned_report_reader_rejects_oversized_file_before_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(reporting, "MAX_REPORT_FILE_BYTES", 128)
    path = tmp_path / "report.json"
    path.write_bytes(b"{" + (b" " * 128))
    with pytest.raises(ReportValidationError, match="file-size limit"):
        reporting.load_report(path)


def test_generated_report_cannot_exceed_rerender_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(reporting, "MAX_REPORT_FILE_BYTES", 128)
    with pytest.raises(InputValidationError, match="file-size limit"):
        _sample_report()


def test_huge_integer_in_pinned_report_fails_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    report_path = tmp_path / "report.json"
    report_path.write_text(
        '{"schema_version":' + ("9" * 5000) + "}",
        encoding="utf-8",
    )
    code = main(["report", str(report_path)])
    captured = capsys.readouterr()
    assert code == 5
    assert "Traceback" not in captured.err
    assert str(tmp_path) not in captured.err


def test_duplicate_conclusion_identity_fails_closed():
    report = copy.deepcopy(_sample_report())
    duplicate = copy.deepcopy(report["conclusions"][0])
    duplicate["boundary"] = "MEASURED"
    duplicate["finding"] = "Quality is preserved."
    report["conclusions"].append(duplicate)
    with pytest.raises(ReportValidationError, match="nested contract"):
        validate_report(report)


def test_conclusion_cannot_contradict_aggregate_or_cite_out_of_range_rows():
    report = copy.deepcopy(_sample_report())
    reasoning = next(
        item
        for item in report["conclusions"]
        if item["id"] == "reasoning-token-share"
    )
    reasoning["finding"] = "Reasoning tokens are 0.0% of measured output tokens."
    reasoning["evidence"][0]["value"] = 0.0
    reasoning["source_rows"] = {"ranges": ["999-1006"], "count": 8}
    with pytest.raises(ReportValidationError, match="nested contract"):
        validate_report(report)


def test_group_source_rows_must_match_pinned_row_dimensions():
    report = copy.deepcopy(_sample_report())
    low = next(
        group for group in report["groups"] if group["reasoning_effort"] == "low"
    )
    medium = next(
        group for group in report["groups"] if group["reasoning_effort"] == "medium"
    )
    low["source_rows"], medium["source_rows"] = (
        medium["source_rows"],
        low["source_rows"],
    )
    with pytest.raises(ReportValidationError, match="nested contract"):
        validate_report(report)


def test_status_counts_must_match_owned_row_dimensions():
    report = copy.deepcopy(_sample_report())
    row = next(
        item for item in report["input"]["row_dimensions"]
        if item["status_code"] == 429
    )
    row["status_code"] = 500
    with pytest.raises(ReportValidationError, match="nested contract"):
        validate_report(report)


def test_report_visible_identifiers_reuse_input_privacy_rules():
    report = copy.deepcopy(_sample_report())
    report["workload"]["name"] = "a" * 32
    with pytest.raises(ReportValidationError, match="nested contract"):
        validate_report(report)


def test_unrelated_conclusion_cannot_claim_snapshot_usage():
    report = copy.deepcopy(_sample_report())
    quality = report["conclusions"][0]
    quality["provenance"]["pricing_snapshot"] = {
        "status": "USED",
        "snapshot_id": report["pricing_snapshot"]["snapshot_id"],
        "sha256": report["pricing_snapshot"]["sha256"],
    }
    with pytest.raises(ReportValidationError, match="nested contract"):
        validate_report(report)


def test_all_claim_registry_hashes_must_match_bundled_contract():
    report = copy.deepcopy(_sample_report())
    report["claim_registry"]["sha256"] = "0" * 64
    report["policy"]["claim_registry_sha256"] = "0" * 64
    for conclusion in report["conclusions"]:
        conclusion["provenance"]["claim_registry_sha256"] = "0" * 64
    with pytest.raises(ReportValidationError, match="nested contract"):
        validate_report(report)


def test_ptu_not_modeled_reason_cannot_be_fabricated():
    report = copy.deepcopy(_sample_report())
    report["ptu_sizing"]["status"] = "NOT_MODELED"
    report["ptu_sizing"]["reason"] = "fabricated applicability reason"
    report["ptu_sizing"]["pricing_snapshot"] = None
    report["ptu_sizing"]["density_snapshot"] = None
    report["ptu_sizing"]["result"] = None
    ptu = next(
        item for item in report["conclusions"] if item["id"] == "ptu-applicability"
    )
    ptu["boundary"] = "NOT_MODELED"
    ptu["finding"] = "PTU sizing is NOT_MODELED: fabricated applicability reason."
    with pytest.raises(ReportValidationError, match="nested contract"):
        validate_report(report)


def test_ptu_modeled_result_model_must_match_report_rows():
    report = copy.deepcopy(_sample_report())
    report["ptu_sizing"]["result"]["inputs_snapshot"]["model_id"] = "gpt-4o"
    with pytest.raises(ReportValidationError, match="nested contract"):
        validate_report(report)


def test_ptu_not_modeled_source_rows_must_stay_inside_input():
    report = copy.deepcopy(_sample_report())
    report["ptu_sizing"].update(
        {
            "status": "NOT_MODELED",
            "reason": "pinned PTU snapshots do not support the analyzed model",
            "missing_inputs": [],
            "source_rows": {"ranges": ["999"], "count": 1},
            "pricing_snapshot": None,
            "density_snapshot": None,
            "result": None,
        }
    )
    report["boundaries"]["ptu_sizing"] = "NOT_MODELED"
    ptu = next(
        item for item in report["conclusions"] if item["id"] == "ptu-applicability"
    )
    ptu.update(
        {
            "boundary": "NOT_MODELED",
            "finding": (
                "PTU sizing is NOT_MODELED: pinned PTU snapshots do not support "
                "the analyzed model."
            ),
            "evidence": [
                {
                    "metric": "missing_capacity_inputs",
                    "value": [],
                    "unit": "field-names",
                }
            ],
            "source_rows": {"ranges": ["999"], "count": 1},
        }
    )
    with pytest.raises(ReportValidationError, match="nested contract"):
        validate_report(report)


def test_unmodeled_ptu_policy_cannot_cite_snapshots():
    report = copy.deepcopy(_sample_report())
    report["ptu_sizing"].update(
        {
            "status": "NOT_MODELED",
            "reason": "required capacity inputs were not supplied",
            "missing_inputs": ["expected_rpm", "mean_max_output_tokens"],
            "source_rows": report["aggregate"]["source_rows"],
            "pricing_snapshot": None,
            "density_snapshot": None,
            "result": None,
        }
    )
    report["boundaries"]["ptu_sizing"] = "NOT_MODELED"
    report["policy"]["ptu_sizing_snapshots"] = {
        "status": "USED",
        "pricing_snapshot": {
            "snapshot_id": "fake-ptu",
            "sha256": "1" * 64,
        },
        "density_snapshot": {
            "snapshot_id": "fake-density",
            "sha256": "2" * 64,
        },
    }
    with pytest.raises(ReportValidationError, match="nested contract"):
        validate_report(report)


def test_unknown_secret_field_fails_without_echoing_value_or_input_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    secret = "Bearer" + " " + "synthetic_token_value_123456"
    payload = json.loads((EXAMPLE_DIR / "usage.jsonl").read_text().splitlines()[0])
    payload["prompt"] = secret
    usage = tmp_path / "customer-secret-name.jsonl"
    usage.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    out = tmp_path / "bundle"
    code = main(
        [
            "analyze",
            str(usage),
            "--workload",
            str(EXAMPLE_DIR / "workload.yaml"),
            "--out",
            str(out),
        ]
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "<unknown-field>" in captured.err
    assert "prompt" not in captured.err
    assert secret not in captured.err
    assert usage.name not in captured.err
    assert not out.exists()


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "John Doe",
        "customer@example.com",
        "<img src=x onerror=alert(1)>",
        "10.23.45.67",
        "workload-10.23.45.67",
    ],
)
def test_workload_name_rejects_free_text_pii_and_html(unsafe_name: str):
    payload = yaml.safe_load((EXAMPLE_DIR / "workload.yaml").read_text())
    payload["name"] = unsafe_name
    with pytest.raises(ValidationError):
        WorkloadSpec.model_validate(payload)


@pytest.mark.parametrize(
    "secret",
    [
        "a" * 32,
        "b" * 64,
        "01890f3c-7b89-7cc8-98c4-dc0c0c07398f",
        "example.com",
    ],
)
def test_credential_shaped_allowed_identifiers_fail_without_echo(
    secret: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    usage_payload = json.loads(
        (EXAMPLE_DIR / "usage.jsonl").read_text().splitlines()[0]
    )
    usage_payload["model"] = secret
    usage = tmp_path / "usage.jsonl"
    usage.write_text(json.dumps(usage_payload) + "\n", encoding="utf-8")
    out = tmp_path / "usage-out"
    assert (
        main(
            [
                "analyze",
                str(usage),
                "--workload",
                str(EXAMPLE_DIR / "workload.yaml"),
                "--out",
                str(out),
            ]
        )
        == 4
    )
    assert secret not in capsys.readouterr().err
    assert not out.exists()

    workload = yaml.safe_load((EXAMPLE_DIR / "workload.yaml").read_text())
    workload["name"] = secret
    workload["pricing"]["snapshot_id"] = secret
    workload_path = tmp_path / "workload.yaml"
    workload_path.write_text(yaml.safe_dump(workload, sort_keys=False))
    assert (
        main(
            [
                "analyze",
                str(EXAMPLE_DIR / "usage.jsonl"),
                "--workload",
                str(workload_path),
                "--out",
                str(tmp_path / "workload-out"),
            ]
        )
        == 4
    )
    assert secret not in capsys.readouterr().err
    assert not (tmp_path / "workload-out").exists()


def test_long_model_identifier_with_separators_is_allowed():
    payload = json.loads((EXAMPLE_DIR / "usage.jsonl").read_text().splitlines()[0])
    payload["model"] = "provider.model-family-version-2026-08-20-long-identifier"
    assert UsageEnvelope.model_validate(payload).model == payload["model"]


def test_secret_unknown_workload_field_name_is_masked(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    secret_key = "customer@example.com"
    workload = yaml.safe_load((EXAMPLE_DIR / "workload.yaml").read_text())
    workload[secret_key] = "value"
    workload_path = tmp_path / "workload.yaml"
    workload_path.write_text(yaml.safe_dump(workload, sort_keys=False))
    code = main(
        [
            "analyze",
            str(EXAMPLE_DIR / "usage.jsonl"),
            "--workload",
            str(workload_path),
            "--out",
            str(tmp_path / "bundle"),
        ]
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "<unknown-field>" in captured.err
    assert secret_key not in captured.err
    assert not (tmp_path / "bundle").exists()


def test_unused_pricing_model_key_cannot_leak(tmp_path: Path):
    pricing = yaml.safe_load((EXAMPLE_DIR / "pricing.yaml").read_text())
    secret_key = "c" * 64
    pricing["models"][secret_key] = copy.deepcopy(pricing["models"]["gpt-5.2"])
    (tmp_path / "pricing.yaml").write_text(yaml.safe_dump(pricing, sort_keys=False))
    workload = yaml.safe_load((EXAMPLE_DIR / "workload.yaml").read_text())
    workload.pop("ptu_sizing")
    (tmp_path / "workload.yaml").write_text(
        yaml.safe_dump(workload, sort_keys=False)
    )
    with pytest.raises(InputValidationError, match="pricing snapshot is invalid"):
        analyze_files(EXAMPLE_DIR / "usage.jsonl", tmp_path / "workload.yaml")


def test_unpriced_model_is_not_reported_as_zero_cost(tmp_path: Path):
    _copy_sample_inputs(tmp_path)
    rows = [
        json.loads(line)
        for line in (tmp_path / "usage.jsonl").read_text().splitlines()
    ]
    for row in rows:
        row["model"] = "gpt-5.3"
    (tmp_path / "usage.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    workload = yaml.safe_load((tmp_path / "workload.yaml").read_text())
    workload.pop("ptu_sizing")
    (tmp_path / "workload.yaml").write_text(
        yaml.safe_dump(workload, sort_keys=False),
        encoding="utf-8",
    )
    report = analyze_files(tmp_path / "usage.jsonl", tmp_path / "workload.yaml")
    assert report["boundaries"]["payg_cost"] == "NOT_MODELED"
    assert report["aggregate"]["mean_modeled_usd_per_request"] is None
    cost = next(
        item for item in report["conclusions"] if item["id"] == "payg-cost-model"
    )
    assert cost["finding"].startswith("PAYG cost is NOT_MODELED")
    assert cost["recommendation"].startswith("Add a pinned pricing entry")
    assert "$0.000000000" not in reporting.render_markdown(report)
    assert "$0.000000000" not in reporting.render_html(report)
    assert ">NOT_MODELED<" in reporting.render_html(report)


def test_all_failed_requests_preserve_exact_ptu_not_modeled_reason(
    tmp_path: Path,
):
    _copy_sample_inputs(tmp_path)
    rows = [
        json.loads(line)
        for line in (tmp_path / "usage.jsonl").read_text().splitlines()
    ]
    for row in rows:
        row["status_code"] = 500
    (tmp_path / "usage.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    report = analyze_files(tmp_path / "usage.jsonl", tmp_path / "workload.yaml")
    ptu = next(
        item for item in report["conclusions"] if item["id"] == "ptu-applicability"
    )
    assert ptu["finding"] == (
        "PTU sizing is NOT_MODELED: PTU sizing requires at least one "
        "successful usage row."
    )


def test_fractional_timestamps_are_ordered_chronologically(tmp_path: Path):
    _copy_sample_inputs(tmp_path)
    rows = [
        json.loads(line)
        for line in (tmp_path / "usage.jsonl").read_text().splitlines()
    ]
    rows[0]["timestamp"] = "2026-08-20T00:00:00Z"
    rows[1]["timestamp"] = "2026-08-20T00:00:00.500Z"
    (tmp_path / "usage.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    report = analyze_files(tmp_path / "usage.jsonl", tmp_path / "workload.yaml")
    assert report["input"]["window_start_utc"] == "2026-08-20T00:00:00.000000Z"
    assert report["input"]["window_end_utc"] == "2026-08-20T00:01:10.000000Z"


def test_offset_timestamps_are_labeled_as_canonical_utc(tmp_path: Path):
    _copy_sample_inputs(tmp_path)
    rows = [
        json.loads(line)
        for line in (tmp_path / "usage.jsonl").read_text().splitlines()
    ]
    rows[0]["timestamp"] = "2026-08-20T02:00:00+02:00"
    (tmp_path / "usage.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    report = analyze_files(tmp_path / "usage.jsonl", tmp_path / "workload.yaml")
    assert report["input"]["window_start_utc"] == "2026-08-20T00:00:00.000000Z"


@pytest.mark.parametrize(
    "snapshot_name",
    ["pricing.yaml", "ptu-pricing.yaml", "density.yaml"],
)
def test_malformed_snapshot_returns_safe_input_exit(
    snapshot_name: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    _copy_sample_inputs(tmp_path)
    (tmp_path / snapshot_name).write_bytes(b"\xff\xfe:not-yaml")
    code = main(
        [
            "analyze",
            str(tmp_path / "usage.jsonl"),
            "--workload",
            str(tmp_path / "workload.yaml"),
            "--out",
            str(tmp_path / "bundle"),
        ]
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "Traceback" not in captured.err
    assert str(tmp_path) not in captured.err
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize(
    ("snapshot_name", "old", "new"),
    [
        ("pricing.yaml", "input_per_1m_usd: 1.75", "input_per_1m_usd: .inf"),
        ("pricing.yaml", "input_per_1m_usd: 1.75", "input_per_1m_usd: 1.0e+308"),
        ("density.yaml", "gpt-5.2: 8", "gpt-5.2: .nan"),
        ("density.yaml", "gpt-5.2: 8", "gpt-5.2: 1.0e+308"),
        (
            "ptu-pricing.yaml",
            "ptu_hourly_rate_usd: 2.0",
            "ptu_hourly_rate_usd: 1.0e+308",
        ),
        (
            "ptu-pricing.yaml",
            "ptu_hourly_rate_usd: 2.0",
            "ptu_hourly_rate_usd: " + ("9" * 5000),
        ),
    ],
)
def test_nonfinite_or_extreme_snapshot_values_fail_safely(
    snapshot_name: str,
    old: str,
    new: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    _copy_sample_inputs(tmp_path)
    path = tmp_path / snapshot_name
    source = path.read_text(encoding="utf-8")
    assert old in source
    path.write_text(source.replace(old, new), encoding="utf-8")
    code = main(
        [
            "analyze",
            str(tmp_path / "usage.jsonl"),
            "--workload",
            str(tmp_path / "workload.yaml"),
            "--out",
            str(tmp_path / "bundle"),
        ]
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "Traceback" not in captured.err
    assert str(tmp_path) not in captured.err
    assert not (tmp_path / "bundle").exists()


def test_snapshot_hash_and_calculation_use_the_same_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _copy_sample_inputs(tmp_path)
    workload = yaml.safe_load((tmp_path / "workload.yaml").read_text())
    workload.pop("ptu_sizing")
    (tmp_path / "workload.yaml").write_text(
        yaml.safe_dump(workload, sort_keys=False),
        encoding="utf-8",
    )
    pricing_path = tmp_path / "pricing.yaml"
    original_bytes = pricing_path.read_bytes()
    original_reader = reporting._read_snapshot_bytes

    def mutate_after_read(path: Path, *, label: str) -> bytes:
        data = original_reader(path, label=label)
        if label == "pricing snapshot":
            path.write_text("malformed: [", encoding="utf-8")
        return data

    monkeypatch.setattr(reporting, "_read_snapshot_bytes", mutate_after_read)
    report = analyze_files(tmp_path / "usage.jsonl", tmp_path / "workload.yaml")
    assert report["pricing_snapshot"]["sha256"] == hashlib.sha256(
        original_bytes
    ).hexdigest()
    assert report["boundaries"]["payg_cost"] == "MODELED"


def test_html_escape_uses_quote_true():
    assert _h('"><script>alert(1)</script>') == (
        "&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"
    )


def test_generated_artifacts_contain_no_input_paths_or_private_values(
    tmp_path: Path,
):
    report = _sample_report()
    out = tmp_path / "bundle"
    write_report_bundle(report, out, allow_existing_generated=False)
    combined = b"\n".join((out / name).read_bytes() for name in GENERATED_ARTIFACTS)
    assert str(EXAMPLE_DIR).encode() not in combined
    assert b"customer@example.com" not in combined
    assert ("Bearer" + " " + "synthetic_token_value_123456").encode() not in combined
    assert b".internal/" not in combined
    assert b"Task 024" not in combined
    assert b"Guide" not in combined
    assert b"https://" not in combined
    html_text = (out / "report.html").read_text(encoding="utf-8")
    assert "<script" not in html_text.lower()
    assert 'rel="icon" href="data:image/svg+xml;base64,' in html_text
    assert "<caption>Usage metrics by model and reasoning effort</caption>" in html_text


def test_policy_candidates_have_auditable_provenance():
    policy = _sample_report()["policy"]
    assert policy["input_usage_sha256"]
    assert policy["input_workload_sha256"]
    assert policy["pricing_snapshot"]["sha256"]
    assert policy["claim_registry_sha256"]
    assert policy["ptu_sizing_snapshots"]["status"] == "USED"
    for candidate in policy["candidates"]:
        assert candidate["conclusion_refs"]
        assert candidate["confidence"] in {"HIGH", "MEDIUM", "LOW"}
        assert candidate["evidence"]
        assert candidate["assumptions"]
        assert candidate["source_rows"]["count"] == 8
        assert candidate["selector"]


def test_modeled_ptu_conclusion_cites_successful_rows_and_payg_snapshot():
    report = _sample_report()
    conclusion = next(
        item
        for item in report["conclusions"]
        if item["id"] == "ptu-applicability"
    )
    assert conclusion["source_rows"] == {
        "ranges": ["1-6", "8"],
        "count": 7,
    }
    assert conclusion["provenance"]["pricing_snapshot"] == {
        "status": "USED",
        "snapshot_id": report["pricing_snapshot"]["snapshot_id"],
        "sha256": report["pricing_snapshot"]["sha256"],
    }


def test_reasoning_threshold_candidate_only_appears_above_threshold(
    tmp_path: Path,
):
    baseline = _sample_report()["policy"]["candidates"]
    assert "investigate-reasoning-share" not in {
        candidate["id"] for candidate in baseline
    }

    workload = yaml.safe_load((EXAMPLE_DIR / "workload.yaml").read_text())
    workload["thresholds"]["max_reasoning_output_ratio"] = 0.40
    workload_path = tmp_path / "workload.yaml"
    workload_path.write_text(yaml.safe_dump(workload, sort_keys=False))
    for name in ("pricing.yaml", "ptu-pricing.yaml", "density.yaml"):
        (tmp_path / name).write_bytes((EXAMPLE_DIR / name).read_bytes())
    candidates = analyze_files(
        EXAMPLE_DIR / "usage.jsonl",
        workload_path,
    )["policy"]["candidates"]
    assert "investigate-reasoning-share" in {
        candidate["id"] for candidate in candidates
    }


def test_policy_thresholds_compare_unrounded_metrics(tmp_path: Path):
    _copy_sample_inputs(tmp_path)
    rows = [
        json.loads(line)
        for line in (tmp_path / "usage.jsonl").read_text().splitlines()[:3]
    ]
    rows[0].update(
        {
            "status_code": 429,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "retry_after_ms": 1000,
        }
    )
    (tmp_path / "usage.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    workload = yaml.safe_load((tmp_path / "workload.yaml").read_text())
    workload.pop("ptu_sizing")
    workload["thresholds"]["max_429_rate"] = 0.3333332
    (tmp_path / "workload.yaml").write_text(
        yaml.safe_dump(workload, sort_keys=False),
        encoding="utf-8",
    )
    report = analyze_files(tmp_path / "usage.jsonl", tmp_path / "workload.yaml")
    assert report["aggregate"]["status_429_rate"] == pytest.approx(1 / 3)
    assert "investigate-429-pressure" in {
        candidate["id"] for candidate in report["policy"]["candidates"]
    }


def test_retry_after_mean_excludes_non_429_rows(tmp_path: Path):
    payloads = [
        json.loads(line)
        for line in (EXAMPLE_DIR / "usage.jsonl").read_text().splitlines()
    ]
    payloads[0]["retry_after_ms"] = 9000
    usage = tmp_path / "usage.jsonl"
    usage.write_text(
        "".join(json.dumps(payload) + "\n" for payload in payloads),
        encoding="utf-8",
    )
    report = analyze_files(usage, EXAMPLE_DIR / "workload.yaml")
    assert report["aggregate"]["mean_retry_after_ms_on_429"] == 1000


def test_invalid_cli_arguments_exit_two():
    with pytest.raises(SystemExit) as exc:
        main(["analyze"])
    assert exc.value.code == 2


def test_invalid_cli_arguments_do_not_echo_secret(
    capsys: pytest.CaptureFixture[str],
):
    secret = "sk-synthetic-secret-1234567890"
    with pytest.raises(SystemExit) as exc:
        main(["analyze", f"--api-key={secret}"])
    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert secret not in captured.err
    assert "invalid command arguments" in captured.err
