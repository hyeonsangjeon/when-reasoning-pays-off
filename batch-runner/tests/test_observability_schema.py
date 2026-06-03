"""Tests for the canonical PTU observability schema (Task 028)."""

from __future__ import annotations

import json
import re
from dataclasses import fields
from pathlib import Path

import pytest

from batch_runner.observability.schema import (
    PTUCellSummary,
    PTURequestRecord,
    build_cell_summary_schema,
    build_request_record_schema,
    hash_cache_key,
    write_schema_files,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_DIR = _REPO_ROOT / "schemas"

_FORBIDDEN_FIELDS = {
    "api_key",
    "authorization",
    "messages",
    "system_prompt",
    "request_body",
    "prompt",
    "content",
}

# Verbatim Appendix A / B header names that MUST appear as header_name
# annotations on the corresponding fields.
_EXPECTED_HEADERS = {
    "retry_after_ms": "retry-after-ms",
    "retry_after_seconds": "retry-after",
    "x_ms_region": "x-ms-region",
    "x_request_id": "x-request-id",
    "x_ms_deployment_name": "x-ms-deployment-name",
    "x_ms_spillover_from_deployment": "x-ms-spillover-from-deployment",
    "x_ms_spillover_error": "x-ms-spillover-error",
    "x_ratelimit_remaining_requests": "x-ratelimit-remaining-requests",
}


def _sample_record(**overrides) -> PTURequestRecord:
    base = dict(
        request_idx=0,
        wallclock_timestamp_iso="2026-01-01T00:00:00+00:00",
        deployment_name_requested="ptu-A",
        response_status_code=200,
        retry_after_ms=None,
        retry_after_seconds=None,
        x_ms_region="eastus",
        x_request_id="req-1",
        x_ms_deployment_name="ptu-A",
        x_ms_spillover_from_deployment=None,
        x_ms_spillover_error=None,
        x_ratelimit_remaining_requests=None,
        prompt_tokens=100,
        completion_tokens=10,
        cached_tokens=0,
        reasoning_tokens=0,
        total_tokens=110,
        max_output_tokens_sent=512,
        prompt_cache_key_used=hash_cache_key("acme:flow:answer"),
        prompt_cache_retention_sent="in_memory",
        reasoning_effort_sent="low",
        model_id="gpt-5-mini",
        first_token_latency_ms=120.0,
        total_latency_ms=400.0,
    )
    base.update(overrides)
    return PTURequestRecord(**base)


def test_request_record_constructs_with_all_fields():
    rec = _sample_record()
    assert rec.deployment_name_requested == "ptu-A"
    assert rec.response_status_code == 200


def test_request_record_is_frozen():
    rec = _sample_record()
    with pytest.raises(Exception):
        rec.request_idx = 99  # type: ignore[misc]


def test_cell_summary_constructs():
    summary = PTUCellSummary(
        cell_id="cell-1",
        cell_label="ptu-A / low-effort",
        window_start_iso="2026-01-01T00:00:00+00:00",
        window_end_iso="2026-01-01T00:05:00+00:00",
        deployment_name="ptu-A",
        request_count=10,
        real_429_count=1,
        mean_cached_fraction=0.42,
        p50_ttft_ms=120.0,
        p95_ttft_ms=400.0,
        p99_ttft_ms=900.0,
        mean_retry_after_ms_on_429=250.0,
        azure_monitor_metrics_to_query=("AzureOpenAIRequests",),
    )
    assert summary.request_count == 10


def test_hash_cache_key_is_16_hex():
    h = hash_cache_key("acme:flow:answer")
    assert re.fullmatch(r"[0-9a-f]{16}", h), h
    # Stable: same input → same hash.
    assert h == hash_cache_key("acme:flow:answer")
    # Different input → different hash.
    assert h != hash_cache_key("other:flow:answer")
    # The raw string is NOT a substring of the hash.
    assert "acme" not in h
    assert "flow" not in h


def test_hash_cache_key_rejects_non_string():
    with pytest.raises(TypeError):
        hash_cache_key(123)  # type: ignore[arg-type]


def test_no_forbidden_request_fields():
    names = {f.name.lower() for f in fields(PTURequestRecord)}
    for forbidden in _FORBIDDEN_FIELDS:
        assert forbidden not in names, f"forbidden field present: {forbidden}"


def test_no_forbidden_cell_fields():
    names = {f.name.lower() for f in fields(PTUCellSummary)}
    for forbidden in _FORBIDDEN_FIELDS:
        assert forbidden not in names


def test_prompt_cache_key_field_documents_hash_requirement():
    schema = build_request_record_schema()
    prop = schema["properties"]["prompt_cache_key_used"]
    assert "hash" in prop["description"].lower()
    assert "never raw" in prop["description"].lower()


def test_prompt_cache_key_raw_value_is_normalized_to_hash():
    rec = _sample_record(prompt_cache_key_used="acme:flow:answer")
    assert rec.prompt_cache_key_used != "acme:flow:answer"
    assert rec.prompt_cache_key_used == hash_cache_key("acme:flow:answer")
    assert re.fullmatch(r"[0-9a-f]{16}", rec.prompt_cache_key_used)


def test_prompt_cache_key_already_valid_digest_preserved():
    digest = hash_cache_key("acme:flow:answer")
    rec = _sample_record(prompt_cache_key_used=digest)
    assert rec.prompt_cache_key_used == digest


def test_prompt_cache_key_none_preserved():
    rec = _sample_record(prompt_cache_key_used=None)
    assert rec.prompt_cache_key_used is None


def test_prompt_cache_key_uppercase_or_wrong_length_is_hashed():
    # 16 uppercase hex chars do not match the lowercase invariant and
    # must be normalized via hash_cache_key.
    upper = "ABCDEF0123456789"
    rec = _sample_record(prompt_cache_key_used=upper)
    assert rec.prompt_cache_key_used == hash_cache_key(upper)
    # Wrong length (15 hex chars) also gets hashed.
    short = "abcdef012345678"
    rec2 = _sample_record(prompt_cache_key_used=short)
    assert rec2.prompt_cache_key_used == hash_cache_key(short)


def test_prompt_cache_key_schema_has_digest_constraints():
    schema = build_request_record_schema()
    prop = schema["properties"]["prompt_cache_key_used"]
    assert prop["pattern"] == "^[0-9a-f]{16}$"
    assert prop["minLength"] == 16
    assert prop["maxLength"] == 16
    # Still nullable.
    assert "null" in prop["type"]


def test_request_schema_header_names_verbatim():
    schema = build_request_record_schema()
    props = schema["properties"]
    for field_name, header in _EXPECTED_HEADERS.items():
        assert field_name in props, f"missing field {field_name}"
        assert props[field_name].get("header_name") == header, (
            f"{field_name} header_name drift: {props[field_name].get('header_name')!r}"
        )


def test_x_ratelimit_marked_optional_on_ptu():
    schema = build_request_record_schema()
    prop = schema["properties"]["x_ratelimit_remaining_requests"]
    assert prop.get("optional_on_ptu") is True
    # And it must allow null in its type.
    t = prop.get("type")
    assert (isinstance(t, list) and "null" in t) or t == "null"


def test_retry_after_ms_and_seconds_coexist():
    schema = build_request_record_schema()
    props = schema["properties"]
    assert "retry_after_ms" in props
    assert "retry_after_seconds" in props
    assert props["retry_after_ms"]["header_name"] == "retry-after-ms"
    assert props["retry_after_seconds"]["header_name"] == "retry-after"


def test_every_property_has_category_tag():
    for schema in (build_request_record_schema(), build_cell_summary_schema()):
        for name, prop in schema["properties"].items():
            assert "category" in prop, f"field {name} missing category tag"
            assert prop["category"] in {
                "official_spec",
                "operational_inference",
            }


def test_schema_files_round_trip(tmp_path):
    req_path, cell_path = write_schema_files(tmp_path)
    req = json.loads(req_path.read_text())
    cell = json.loads(cell_path.read_text())
    assert req["title"] == "PTURequestRecord"
    assert cell["title"] == "PTUCellSummary"


def test_committed_schema_files_exist_and_parse():
    req_path = _SCHEMA_DIR / "ptu_request_record.schema.json"
    cell_path = _SCHEMA_DIR / "ptu_cell_summary.schema.json"
    assert req_path.exists(), f"missing {req_path}"
    assert cell_path.exists(), f"missing {cell_path}"
    req = json.loads(req_path.read_text())
    cell = json.loads(cell_path.read_text())
    assert req["type"] == "object"
    assert cell["type"] == "object"
    # Sanity: committed file matches generator output.
    assert req == build_request_record_schema()
    assert cell == build_cell_summary_schema()


def test_optional_literal_fields_accept_null_in_committed_schema():
    """Optional[Literal[...]] fields must validate as null per the Python
    contract and docs (`| null`). The emitted JSON Schema must include
    JSON null in the enum so Draft 7 validators accept None.
    """
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (_SCHEMA_DIR / "ptu_request_record.schema.json").read_text()
    )
    # Schema-shape assertion that is independent of any validator dep:
    # the enum lists for both nullable literal fields must contain JSON null.
    retention = schema["properties"]["prompt_cache_retention_sent"]
    effort = schema["properties"]["reasoning_effort_sent"]
    assert "null" in retention["type"]
    assert None in retention["enum"]
    assert "null" in effort["type"]
    assert None in effort["enum"]

    # End-to-end Draft 7 validation against a canonical record dict with
    # both nullable optional literals set to None.
    rec = _sample_record(
        prompt_cache_retention_sent=None,
        reasoning_effort_sent=None,
    )
    payload = {f.name: getattr(rec, f.name) for f in fields(rec)}
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda e: e.path)
    assert errors == [], [
        (list(e.path), e.message) for e in errors
    ]


def test_no_secret_or_pii_substrings_in_schema():
    schema_text = json.dumps(build_request_record_schema()) + json.dumps(
        build_cell_summary_schema()
    )
    lower = schema_text.lower()
    for needle in ("api_key", "authorization", "system_prompt", "messages"):
        assert needle not in lower, f"forbidden substring {needle} in schema"
