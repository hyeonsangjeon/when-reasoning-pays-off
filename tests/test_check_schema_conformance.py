from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml

from scripts.check_schema_conformance import (
    InstanceGroup,
    check_artifact_conformance,
    check_schema_meta,
    validate_instance,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_schema_meta_validation_passes() -> None:
    assert check_schema_meta(REPO_ROOT) == []


def test_committed_artifact_instances_conform() -> None:
    assert check_artifact_conformance(REPO_ROOT) == []


def test_pricing_schema_rejects_model_incompatible_reasoning_rates(
    tmp_path: Path,
) -> None:
    schema = json.loads(
        (REPO_ROOT / "schemas/azure_pricing_snapshot.v1.schema.json").read_text()
    )
    source = yaml.safe_load(
        (
            REPO_ROOT / "pricing/azure-openai-payg-sample-2026-05.yaml"
        ).read_text()
    )
    gpt4o = "azure-openai:gpt-4o:2024-11-20:global:global-standard"
    gpt52 = "azure-openai:gpt-5.2:2025-12-11:global:global-standard"

    source["records"][gpt4o]["rates"]["reasoning_per_1m_usd"] = 10.0
    invalid_gpt4o = tmp_path / "gpt4o-reasoning.yaml"
    invalid_gpt4o.write_text(yaml.safe_dump(source, sort_keys=False))
    assert validate_instance(invalid_gpt4o, schema, "yaml")

    del source["records"][gpt4o]["rates"]["reasoning_per_1m_usd"]
    del source["records"][gpt52]["rates"]["reasoning_per_1m_usd"]
    invalid_gpt52 = tmp_path / "gpt52-missing-reasoning.yaml"
    invalid_gpt52.write_text(yaml.safe_dump(source, sort_keys=False))
    assert validate_instance(invalid_gpt52, schema, "yaml")


def test_mutated_sample_row_is_rejected(tmp_path: Path) -> None:
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    schema_name = "experiment_sample_row.v1.schema.json"
    shutil.copy2(
        REPO_ROOT / "schemas" / schema_name,
        schemas / schema_name,
    )
    mutated = tmp_path / "sample.jsonl"
    mutated.write_text('{"id": "missing-input"}\n', encoding="utf-8")
    groups = (
        InstanceGroup(
            f"schemas/{schema_name}",
            ("sample.jsonl",),
            "jsonl",
        ),
    )

    failures = check_artifact_conformance(
        tmp_path,
        groups=groups,
        exemptions={},
        discovery_patterns=("sample*.jsonl",),
        artifact_exemptions={},
        tracked_files={"sample.jsonl"},
    )

    assert any("'input' is a required property" in failure for failure in failures)


def test_validate_instance_reports_mutated_public_manifest(tmp_path: Path) -> None:
    schema = json.loads(
        (REPO_ROOT / "schemas/public_chart_candidates.schema.json").read_text()
    )
    mutated = tmp_path / "public_chart_candidates.json"
    mutated.write_text(
        json.dumps(
            {
                "schema": "wrpo-public-chart-candidates",
                "schema_semver": "0.1.0",
                "tier": "RAW_PRIVATE",
                "candidates": [],
            }
        ),
        encoding="utf-8",
    )

    failures = validate_instance(mutated, schema, "json")

    assert any("'SANITIZED_PUBLIC' was expected" in failure for failure in failures)


def test_empty_sample_array_is_rejected(tmp_path: Path) -> None:
    schema = json.loads(
        (REPO_ROOT / "schemas/experiment_sample_row.v1.schema.json").read_text()
    )
    empty = tmp_path / "sample.json"
    empty.write_text("[]", encoding="utf-8")

    failures = validate_instance(empty, schema, "json-array-items")

    assert any("expected at least one JSON array item" in failure for failure in failures)


def test_unaccounted_schema_fails_instance_inventory(tmp_path: Path) -> None:
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    schema = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"}
    (schemas / "mapped.schema.json").write_text(json.dumps(schema), encoding="utf-8")
    (schemas / "unmapped.schema.json").write_text(json.dumps(schema), encoding="utf-8")
    instance = tmp_path / "fixture.json"
    instance.write_text("{}", encoding="utf-8")
    groups = (
        InstanceGroup(
            "schemas/mapped.schema.json",
            ("fixture.json",),
            "json",
        ),
    )

    failures = check_artifact_conformance(
        tmp_path,
        groups=groups,
        exemptions={},
        discovery_patterns=("fixture.json",),
        artifact_exemptions={},
        tracked_files={"fixture.json"},
    )

    assert failures == [
        "schemas/unmapped.schema.json: no committed-instance mapping or exemption"
    ]


def test_unmapped_governed_artifact_fails_inventory(tmp_path: Path) -> None:
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    schema_name = "fixture.schema.json"
    schema = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"}
    (schemas / schema_name).write_text(json.dumps(schema), encoding="utf-8")
    (tmp_path / "mapped.json").write_text("{}", encoding="utf-8")
    (tmp_path / "unmapped.json").write_text("{}", encoding="utf-8")
    groups = (
        InstanceGroup(
            f"schemas/{schema_name}",
            ("mapped.json",),
            "json",
        ),
    )

    failures = check_artifact_conformance(
        tmp_path,
        groups=groups,
        exemptions={},
        discovery_patterns=("*.json",),
        artifact_exemptions={},
        tracked_files={"mapped.json", "unmapped.json"},
    )

    assert failures == ["unmapped.json: no schema mapping or artifact exemption"]
