"""Validate schema documents and their committed artifact instances separately."""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Literal

import yaml
from jsonschema import Draft7Validator, FormatChecker
from jsonschema.exceptions import SchemaError

REPO_ROOT = Path(__file__).resolve().parents[1]
DocumentMode = Literal["json", "json-array-items", "jsonl", "yaml"]


@dataclass(frozen=True)
class InstanceGroup:
    schema: str
    patterns: tuple[str, ...]
    mode: DocumentMode


INSTANCE_GROUPS = (
    InstanceGroup(
        "schemas/azure_pricing_snapshot.v1.schema.json",
        (
            "pricing/azure-openai-payg-sample-2026-08.yaml",
            "pricing/azure-openai-payg-sample-2026-05.yaml",
            "batch-runner/batch_runner/data/azure_sample_pricing.yaml",
            "tests/fixtures/pricing/azure-openai-payg-2026-05.yaml",
        ),
        "yaml",
    ),
    InstanceGroup(
        "schemas/experiment_ledger.v1.schema.json",
        (
            "batch-runner/batch_runner/experiment/resources/ledger.azure.yaml",
            "batch-runner/batch_runner/experiment/resources/ledger.mock.yaml",
            "batch-runner/batch_runner/experiment/resources/ledger.ollama.yaml",
        ),
        "yaml",
    ),
    InstanceGroup(
        "schemas/experiment_sample_row.v1.schema.json",
        (
            "batch-runner/batch_runner/experiment/resources/sample.json",
        ),
        "json-array-items",
    ),
    InstanceGroup(
        "schemas/experiment_sample_row.v1.schema.json",
        (
            "batch-runner/batch_runner/experiment/resources/sample.jsonl",
        ),
        "jsonl",
    ),
    InstanceGroup(
        "schemas/usage_envelope.v1.schema.json",
        (
            "batch-runner/batch_runner/data/sample_usage.jsonl",
            "examples/five-minute/usage.jsonl",
        ),
        "jsonl",
    ),
    InstanceGroup(
        "schemas/workload_spec.v1.schema.json",
        (
            "batch-runner/batch_runner/data/sample_workload.yaml",
            "examples/five-minute/workload.yaml",
        ),
        "yaml",
    ),
    InstanceGroup(
        "schemas/public_claim_contract.v1.schema.json",
        ("batch-runner/batch_runner/data/public_claims.v1.json",),
        "json",
    ),
    InstanceGroup(
        "schemas/public_chart_candidates.schema.json",
        ("release/public_chart_candidates.json",),
        "json",
    ),
)

GOVERNED_ARTIFACT_PATTERNS = (
    "pricing/azure-openai-payg-sample-2026-08.yaml",
    "pricing/azure-openai-payg-sample-2026-05.yaml",
    "batch-runner/batch_runner/data/azure_sample_pricing.yaml",
    "batch-runner/batch_runner/experiment/resources/ledger.*.yaml",
    "batch-runner/batch_runner/experiment/resources/sample*.json",
    "batch-runner/batch_runner/experiment/resources/sample*.jsonl",
    "batch-runner/batch_runner/data/sample_usage*.jsonl",
    "batch-runner/batch_runner/data/sample_workload*.yaml",
    "batch-runner/batch_runner/data/public_claims*.json",
    "examples/five-minute/usage*.jsonl",
    "examples/five-minute/workload*.yaml",
    "release/public_*.json",
    "**/*manifest*.json",
)

# These schemas govern private or future artifacts that are intentionally absent
# from the public tree. Keeping the exemptions beside the mappings makes adding a
# schema without an instance policy a failing change.
SCHEMA_EXEMPTIONS = {
    "schemas/ptu_request_record.schema.json": (
        "contract-only schema; historical benchmark rows use older experiment-specific "
        "shapes"
    ),
    "schemas/ptu_cell_summary.schema.json": (
        "contract-only schema; no canonical committed cell summary exists yet"
    ),
    "schemas/raw_archive_manifest.schema.json": (
        "the governed manifest is RAW_PRIVATE and intentionally gitignored"
    ),
    "schemas/redaction_rules.schema.json": (
        "the governed rules file is private and intentionally gitignored"
    ),
    "schemas/experiment_run_manifest.v1.schema.json": (
        "runtime-generated immutable sample manifests are intentionally gitignored"
    ),
    "schemas/experiment_run.v2.schema.json": (
        "runtime-generated immutable sample run metadata is intentionally gitignored"
    ),
    "schemas/experiment_latest_pointer.v1.schema.json": (
        "runtime-generated latest pointers are intentionally gitignored"
    ),
    "schemas/sample_doctor.v1.schema.json": (
        "runtime-generated doctor results are emitted to stdout and not committed"
    ),
    "schemas/cold_mock_timing.v1.schema.json": (
        "runtime-generated timing reports are uploaded by CI and not committed"
    ),
    "schemas/campaign_pricing_policy.v1.schema.json": (
        "runtime-generated campaign summaries carry this embedded provenance"
    ),
    "schemas/protected_azure_smoke_health.v1.schema.json": (
        "runtime-generated protected health is uploaded briefly and never committed"
    ),
}

# These committed manifests have code-level validators but no JSON Schema. They
# remain in the governed artifact inventory so adding another manifest cannot
# silently bypass this gate.
ARTIFACT_EXEMPTIONS = {
    "release/public_sanitized_manifest.json": (
        "validated by sanitize_public_artifacts.py; no JSON Schema exists"
    ),
    "docs/blog/data/chart-data/snapshot_manifest.json": (
        "validated by docs/validate.sh chart synchronization; no JSON Schema exists"
    ),
}


def check_schema_meta(repo_root: Path = REPO_ROOT) -> list[str]:
    """Return failures from parsing and Draft 7 meta-validating every schema."""
    failures: list[str] = []
    schema_paths = sorted((repo_root / "schemas").glob("*.schema.json"))
    if not schema_paths:
        return ["no schema documents found under schemas/"]
    for schema_path in schema_paths:
        relative = schema_path.relative_to(repo_root)
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            Draft7Validator.check_schema(schema)
        except (OSError, json.JSONDecodeError, SchemaError) as exc:
            failures.append(f"{relative}: {exc}")
    return failures


def _load_documents(path: Path, mode: DocumentMode) -> list[tuple[str, Any]]:
    if mode == "yaml":
        return [("", yaml.safe_load(path.read_text(encoding="utf-8")))]
    if mode == "json":
        return [("", json.loads(path.read_text(encoding="utf-8")))]
    if mode == "json-array-items":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("expected a top-level JSON array")
        if not payload:
            raise ValueError("expected at least one JSON array item")
        return [(f"[{index}]", item) for index, item in enumerate(payload)]
    documents: list[tuple[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if line.strip():
            documents.append((f":{line_number}", json.loads(line)))
    if not documents:
        raise ValueError("expected at least one JSONL record")
    return documents


def validate_instance(
    path: Path, schema: dict[str, Any], mode: DocumentMode
) -> list[str]:
    """Return validation failures for one mapped artifact."""
    failures: list[str] = []
    try:
        documents = _load_documents(path, mode)
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        return [f"{path}: {exc}"]

    validator = Draft7Validator(schema, format_checker=FormatChecker())
    for suffix, document in documents:
        errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
        for error in errors:
            location = "/".join(str(part) for part in error.path) or "<root>"
            failures.append(f"{path}{suffix} at {location}: {error.message}")
    return failures


def check_artifact_conformance(
    repo_root: Path = REPO_ROOT,
    groups: tuple[InstanceGroup, ...] = INSTANCE_GROUPS,
    exemptions: dict[str, str] = SCHEMA_EXEMPTIONS,
    discovery_patterns: tuple[str, ...] = GOVERNED_ARTIFACT_PATTERNS,
    artifact_exemptions: dict[str, str] = ARTIFACT_EXEMPTIONS,
    tracked_files: set[str] | None = None,
) -> list[str]:
    """Return mapping, discovery, parsing, and instance-conformance failures."""
    failures: list[str] = []
    schema_paths = {
        path.relative_to(repo_root).as_posix()
        for path in (repo_root / "schemas").glob("*.schema.json")
    }
    mapped_schemas = {group.schema for group in groups}
    accounted_schemas = mapped_schemas | set(exemptions)
    for schema in sorted(schema_paths - accounted_schemas):
        failures.append(f"{schema}: no committed-instance mapping or exemption")
    for schema in sorted(accounted_schemas - schema_paths):
        failures.append(f"{schema}: mapping or exemption references a missing schema")

    seen_instances: dict[str, str] = {}
    loaded_schemas: dict[str, dict[str, Any]] = {}
    for group in groups:
        schema_path = repo_root / group.schema
        try:
            schema = loaded_schemas.setdefault(
                group.schema,
                json.loads(schema_path.read_text(encoding="utf-8")),
            )
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"{group.schema}: {exc}")
            continue
        for pattern in group.patterns:
            if any(character in pattern for character in "*?["):
                failures.append(f"{pattern}: instance mappings must be explicit paths")
                continue
            matches = sorted(path for path in repo_root.glob(pattern) if path.is_file())
            if not matches:
                failures.append(f"{pattern}: mapped pattern matched no committed artifact")
                continue
            for instance_path in matches:
                relative = instance_path.relative_to(repo_root).as_posix()
                prior_schema = seen_instances.get(relative)
                if prior_schema is not None:
                    failures.append(
                        f"{relative}: mapped more than once ({prior_schema}, {group.schema})"
                    )
                    continue
                seen_instances[relative] = group.schema
                failures.extend(validate_instance(instance_path, schema, group.mode))

    if tracked_files is None:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_root), "ls-files", "-z"],
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            failures.append(f"could not inventory committed artifacts: {exc}")
            return failures
        tracked_files = {
            path
            for path in result.stdout.decode("utf-8").split("\0")
            if path
        }
    discovered_instances = {
        relative
        for relative in tracked_files
        if not relative.endswith(".schema.json")
        and any(
            PurePosixPath(relative).match(pattern) for pattern in discovery_patterns
        )
    }
    accounted_instances = set(seen_instances) | set(artifact_exemptions)
    for instance in sorted(discovered_instances - accounted_instances):
        failures.append(f"{instance}: no schema mapping or artifact exemption")
    for instance in sorted(set(artifact_exemptions) - discovered_instances):
        failures.append(f"{instance}: artifact exemption does not match a governed artifact")
    return failures


def _report(label: str, failures: list[str]) -> int:
    if failures:
        for failure in failures:
            print(f"FAIL {failure}")
        return 1
    print(f"OK   {label}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "gate",
        choices=("schema-meta", "artifact-conformance"),
        help="Run schema document validation or committed instance validation.",
    )
    args = parser.parse_args(argv)
    if args.gate == "schema-meta":
        return _report("all schema documents are valid Draft 7 schemas", check_schema_meta())
    return _report(
        "all mapped committed artifacts conform to their schemas",
        check_artifact_conformance(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
