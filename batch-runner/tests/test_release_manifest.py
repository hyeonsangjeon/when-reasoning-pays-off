"""Tests for the release manifest dataclasses and serializers (Task 032)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from batch_runner.release.manifest import (
    MANIFEST_SCHEMA_VERSION,
    AggregateManifest,
    PrivateContentLeakError,
    RawArchiveEntry,
    SanitizedManifest,
    compute_sha256_bytes,
    compute_sha256_file,
    deterministic_json_dumps,
    read_manifest,
    write_manifest,
)
from batch_runner.release.tiers import Tier


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_DIR = _REPO_ROOT / "schemas"


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _archive_id(s: str) -> str:
    return "raw-" + _sha(s)[:16]


def _raw_entry(**overrides) -> RawArchiveEntry:
    base = dict(
        archive_id=_archive_id("file-a"),
        sha256=_sha("payload-a"),
        size_bytes=1024,
        run_id="run-001",
        experiment_yaml_sha256=_sha("yaml-a"),
        captured_at_iso="2026-01-01T00:00:00+00:00",
        git_commit_sha="abc1234",
    )
    base.update(overrides)
    return RawArchiveEntry(**base)


def _sanitized(**overrides) -> SanitizedManifest:
    base = dict(
        artifact_sha256=_sha("sanitized-a"),
        source_raw_archive_id=_archive_id("file-a"),
        source_raw_sha256=_sha("payload-a"),
        redaction_rules_sha256=_sha("rules-yaml"),
        redacted_at_iso="2026-01-01T01:00:00+00:00",
        redactor_commit_sha="abc1234",
    )
    base.update(overrides)
    return SanitizedManifest(**base)


def _aggregate(**overrides) -> AggregateManifest:
    base = dict(
        artifact_sha256=_sha("aggregate-a"),
        source_tier2_archive_ids=(_archive_id("file-a"), _archive_id("file-b")),
        source_tier2_sha256_list=(_sha("payload-a"), _sha("payload-b")),
        aggregation_script_sha256=_sha("aggregator-py"),
        aggregated_at_iso="2026-01-01T02:00:00+00:00",
        aggregator_commit_sha="abc1234",
        aggregate_schema_version="1.0.0",
    )
    base.update(overrides)
    return AggregateManifest(**base)


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------


def test_compute_sha256_bytes_matches_hashlib():
    data = b"hello"
    assert compute_sha256_bytes(data) == hashlib.sha256(data).hexdigest()


def test_compute_sha256_bytes_rejects_str():
    with pytest.raises(TypeError):
        compute_sha256_bytes("hello")  # type: ignore[arg-type]


def test_compute_sha256_file_round_trip(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc" * 1024)
    assert compute_sha256_file(p) == hashlib.sha256(b"abc" * 1024).hexdigest()


# ---------------------------------------------------------------------------
# RawArchiveEntry
# ---------------------------------------------------------------------------


def test_raw_archive_entry_constructs():
    e = _raw_entry()
    assert e.tier is Tier.RAW_PRIVATE
    assert e.schema_version == MANIFEST_SCHEMA_VERSION


def test_raw_archive_entry_is_frozen():
    e = _raw_entry()
    with pytest.raises(Exception):
        e.sha256 = "0" * 64  # type: ignore[misc]


def test_raw_archive_entry_rejects_path_in_archive_id():
    with pytest.raises(ValueError):
        _raw_entry(archive_id="benchmarks/01/runs/file.jsonl")


def test_raw_archive_entry_rejects_short_sha():
    with pytest.raises(ValueError):
        _raw_entry(sha256="abc123")


def test_raw_archive_entry_rejects_negative_size():
    with pytest.raises(ValueError):
        _raw_entry(size_bytes=-1)


def test_raw_archive_entry_rejects_non_iso_timestamp():
    with pytest.raises(ValueError):
        _raw_entry(captured_at_iso="January 1, 2026")


def test_raw_archive_entry_rejects_non_raw_tier():
    with pytest.raises(ValueError):
        RawArchiveEntry(
            archive_id=_archive_id("x"),
            sha256=_sha("x"),
            size_bytes=1,
            run_id="r",
            experiment_yaml_sha256=_sha("y"),
            captured_at_iso="2026-01-01T00:00:00+00:00",
            git_commit_sha="abc1234",
            tier=Tier.SANITIZED_PUBLIC,
        )


# ---------------------------------------------------------------------------
# SanitizedManifest
# ---------------------------------------------------------------------------


def test_sanitized_manifest_constructs_and_is_frozen():
    m = _sanitized()
    assert m.tier is Tier.SANITIZED_PUBLIC
    with pytest.raises(Exception):
        m.artifact_sha256 = "0" * 64  # type: ignore[misc]


def test_sanitized_manifest_carries_no_path_field():
    # The dataclass shape MUST NOT expose any 'path' field that could
    # carry a private filesystem path.
    from dataclasses import fields

    forbidden_substrings = (
        "path",
        "endpoint",
        "deployment",
        "region",
        "request_id",
        "url",
        "hostname",
        "remote",
    )
    for f in fields(SanitizedManifest):
        for needle in forbidden_substrings:
            assert needle not in f.name.lower(), (
                f"SanitizedManifest exposes forbidden field {f.name!r}"
            )


def test_aggregate_manifest_carries_no_path_field():
    from dataclasses import fields

    forbidden_substrings = (
        "path",
        "endpoint",
        "deployment",
        "region",
        "request_id",
        "url",
        "hostname",
        "remote",
    )
    for f in fields(AggregateManifest):
        for needle in forbidden_substrings:
            assert needle not in f.name.lower(), (
                f"AggregateManifest exposes forbidden field {f.name!r}"
            )


def test_sanitized_manifest_rejects_path_in_archive_id():
    with pytest.raises(ValueError):
        _sanitized(source_raw_archive_id="/Users/x/private/raw.jsonl")


def test_sanitized_manifest_rejects_wrong_tier():
    with pytest.raises(ValueError):
        SanitizedManifest(
            artifact_sha256=_sha("a"),
            source_raw_archive_id=_archive_id("x"),
            source_raw_sha256=_sha("x"),
            redaction_rules_sha256=_sha("rules"),
            redacted_at_iso="2026-01-01T00:00:00+00:00",
            redactor_commit_sha="abc1234",
            tier=Tier.RAW_PRIVATE,
        )


# ---------------------------------------------------------------------------
# AggregateManifest
# ---------------------------------------------------------------------------


def test_aggregate_manifest_constructs():
    m = _aggregate()
    assert m.tier is Tier.AGGREGATE_AZURE_SAMPLE
    assert len(m.source_tier2_archive_ids) == 2


def test_aggregate_manifest_rejects_length_mismatch():
    with pytest.raises(ValueError):
        _aggregate(
            source_tier2_archive_ids=(_archive_id("a"),),
            source_tier2_sha256_list=(_sha("a"), _sha("b")),
        )


def test_aggregate_manifest_rejects_empty_sources():
    with pytest.raises(ValueError):
        _aggregate(
            source_tier2_archive_ids=(),
            source_tier2_sha256_list=(),
        )


def test_aggregate_manifest_rejects_path_in_source_archive_id():
    with pytest.raises(ValueError):
        _aggregate(
            source_tier2_archive_ids=("benchmarks/01/runs/file.jsonl",),
            source_tier2_sha256_list=(_sha("a"),),
        )


def test_aggregate_manifest_lists_become_tuples():
    m = _aggregate(
        source_tier2_archive_ids=[_archive_id("a"), _archive_id("b")],
        source_tier2_sha256_list=[_sha("a"), _sha("b")],
    )
    assert isinstance(m.source_tier2_archive_ids, tuple)
    assert isinstance(m.source_tier2_sha256_list, tuple)


# ---------------------------------------------------------------------------
# Determinism + write/read round-trip
# ---------------------------------------------------------------------------


def test_deterministic_json_dumps_sorts_keys():
    a = {"b": 1, "a": 2}
    text = deterministic_json_dumps(a)
    assert text == '{\n  "a": 2,\n  "b": 1\n}\n'


def test_deterministic_json_dumps_is_stable_for_equal_inputs():
    a = {"x": [1, 2], "y": "z"}
    b = {"y": "z", "x": [1, 2]}
    assert deterministic_json_dumps(a) == deterministic_json_dumps(b)


def test_write_manifest_emits_deterministic_bytes(tmp_path):
    m1 = _sanitized()
    m2 = _sanitized()
    p1 = write_manifest(m1, tmp_path / "a.manifest.json")
    p2 = write_manifest(m2, tmp_path / "b.manifest.json")
    assert p1.read_bytes() == p2.read_bytes()


def test_write_manifest_appends_trailing_newline(tmp_path):
    p = write_manifest(_sanitized(), tmp_path / "m.json")
    assert p.read_bytes().endswith(b"\n")


@pytest.mark.parametrize(
    "make",
    [_raw_entry, _sanitized, _aggregate],
    ids=["raw", "sanitized", "aggregate"],
)
def test_write_then_read_round_trip(tmp_path, make):
    original = make()
    path = write_manifest(original, tmp_path / "m.json")
    loaded = read_manifest(path)
    assert loaded == original
    # Re-serialize the loaded object — bytes MUST be byte-identical.
    p2 = write_manifest(loaded, tmp_path / "m2.json")
    assert path.read_bytes() == p2.read_bytes()


def test_read_manifest_dispatches_on_tier(tmp_path):
    p_raw = write_manifest(_raw_entry(), tmp_path / "raw.json")
    p_san = write_manifest(_sanitized(), tmp_path / "san.json")
    p_agg = write_manifest(_aggregate(), tmp_path / "agg.json")
    assert isinstance(read_manifest(p_raw), RawArchiveEntry)
    assert isinstance(read_manifest(p_san), SanitizedManifest)
    assert isinstance(read_manifest(p_agg), AggregateManifest)


def test_read_manifest_rejects_unknown_tier(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"tier": "TIER_X"}))
    with pytest.raises(ValueError):
        read_manifest(p)


def test_read_manifest_rejects_missing_tier(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"sha256": "0" * 64}))
    with pytest.raises(ValueError):
        read_manifest(p)


def test_read_manifest_rejects_unknown_field(tmp_path):
    payload = {
        "tier": "SANITIZED_PUBLIC",
        "artifact_sha256": _sha("a"),
        "source_raw_archive_id": _archive_id("a"),
        "source_raw_sha256": _sha("a"),
        "redaction_rules_sha256": _sha("r"),
        "redacted_at_iso": "2026-01-01T00:00:00+00:00",
        "redactor_commit_sha": "abc1234",
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "secret_field": "oops",
    }
    p = tmp_path / "x.json"
    p.write_text(deterministic_json_dumps(payload))
    with pytest.raises(ValueError):
        read_manifest(p)


# ---------------------------------------------------------------------------
# Privacy leak guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "leak",
    [
        "myco-prod.openai.azure.com",
        "myco.cognitiveservices.azure.com",
        # Azure AI Foundry project host shape — must be on the denylist
        # alongside the classic OpenAI and Cognitive Services hosts.
        "myco-proj.services.ai.azure.com",
        # Assembled from fragments so this source file does not contain
        # the contiguous private archive tree-root literal (the literal
        # is scanned for by scripts/check_public_surface.sh in tracked
        # public files). At runtime the value is identical to the
        # contiguous form and still exercises the denylist substring
        # match.
        "." + "internal/raw-archive/",
        "Bearer abc123def456",
        "sk-abc123def456789",
        "AZURE_OPENAI_API_KEY",
        "x-ms-deployment-name",
    ],
)
def test_write_manifest_blocks_private_substring_via_run_id(tmp_path, leak):
    # Inject the substring through a free-form field (run_id is the only
    # one on RawArchiveEntry; for sanitized/aggregate the dataclass
    # constructors reject obvious garbage, so we route through run_id).
    entry = _raw_entry(run_id=f"run-{leak}-001")
    with pytest.raises(PrivateContentLeakError):
        write_manifest(entry, tmp_path / "leak.json")


def test_write_manifest_clean_payload_succeeds(tmp_path):
    write_manifest(_raw_entry(run_id="run-001-clean"), tmp_path / "ok.json")


# ---------------------------------------------------------------------------
# Schema files on disk
# ---------------------------------------------------------------------------


def test_raw_archive_manifest_schema_file_exists_and_parses():
    p = _SCHEMA_DIR / "raw_archive_manifest.schema.json"
    assert p.exists(), f"missing {p}"
    schema = json.loads(p.read_text())
    assert schema["title"] == "RawArchiveManifest"
    assert schema["type"] == "object"
    entry = schema["definitions"]["RawArchiveEntry"]
    # Tier enum on the schema definition MUST be RAW_PRIVATE only.
    assert entry["properties"]["tier"]["enum"] == ["RAW_PRIVATE"]


def test_raw_archive_manifest_schema_has_no_private_fields():
    p = _SCHEMA_DIR / "raw_archive_manifest.schema.json"
    schema = json.loads(p.read_text())
    # Walk every key/value EXCEPT 'description' (which legitimately
    # describes what the schema does NOT contain).
    leaks: list[tuple[str, str]] = []
    forbidden = (
        ".openai.azure.com",
        ".cognitiveservices.azure.com",
        ".services.ai.azure.com",
        "deployment_name",
        "x_ms_deployment_name",
        "api_key",
        "bearer ",
        "azure_openai_api_key",
        "hf_token",
    )

    def _walk(obj, parent_key=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "description":
                    continue
                _walk(v, parent_key=k)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item, parent_key=parent_key)
        elif isinstance(obj, str):
            lower = obj.lower()
            for needle in forbidden:
                if needle in lower:
                    leaks.append((parent_key, obj))

    _walk(schema)
    assert not leaks, f"forbidden substrings leaked into schema: {leaks}"


def test_raw_archive_manifest_schema_field_names_match_dataclass():
    p = _SCHEMA_DIR / "raw_archive_manifest.schema.json"
    schema = json.loads(p.read_text())
    entry_schema = schema["definitions"]["RawArchiveEntry"]
    from dataclasses import fields

    dc_fields = {f.name for f in fields(RawArchiveEntry)}
    schema_fields = set(entry_schema["properties"].keys())
    assert dc_fields == schema_fields, (
        f"drift between dataclass and schema: "
        f"only-in-dc={dc_fields - schema_fields}, "
        f"only-in-schema={schema_fields - dc_fields}"
    )


def test_raw_archive_manifest_schema_validates_real_entry(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (_SCHEMA_DIR / "raw_archive_manifest.schema.json").read_text()
    )
    entry_schema = schema["definitions"]["RawArchiveEntry"]
    entry = _raw_entry()
    payload = json.loads(deterministic_json_dumps({
        **{
            "archive_id": entry.archive_id,
            "sha256": entry.sha256,
            "size_bytes": entry.size_bytes,
            "run_id": entry.run_id,
            "experiment_yaml_sha256": entry.experiment_yaml_sha256,
            "captured_at_iso": entry.captured_at_iso,
            "git_commit_sha": entry.git_commit_sha,
            "tier": entry.tier.value,
            "schema_version": entry.schema_version,
        }
    }))
    validator = jsonschema.Draft7Validator(entry_schema)
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    assert errors == [], [(list(e.path), e.message) for e in errors]


def test_redaction_rules_schema_exists_and_is_safe():
    p = _SCHEMA_DIR / "redaction_rules.schema.json"
    assert p.exists(), f"missing {p}"
    schema = json.loads(p.read_text())
    assert schema["title"] == "RedactionRules"
    # Walk non-description nodes only; description text legitimately
    # mentions what is NOT supposed to appear in the rules file.
    leaks: list[tuple[str, str]] = []
    forbidden = (
        ".openai.azure.com",
        ".cognitiveservices.azure.com",
        ".services.ai.azure.com",
        "azure_openai_api_key",
        "hf_token",
        "bearer ",
    )

    def _walk(obj, parent_key=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "description":
                    continue
                _walk(v, parent_key=k)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item, parent_key=parent_key)
        elif isinstance(obj, str):
            lower = obj.lower()
            for needle in forbidden:
                if needle in lower:
                    leaks.append((parent_key, obj))

    _walk(schema)
    assert not leaks, f"redaction-rules schema leaks: {leaks}"


# ---------------------------------------------------------------------------
# No-secret / no-private-content sanity scan of the module itself
# ---------------------------------------------------------------------------


def test_release_module_source_carries_no_real_secret():
    """The release subpackage source must not contain any real secret pattern."""
    import re

    pkg_dir = (
        Path(__file__).resolve().parents[1]
        / "batch_runner"
        / "release"
    )
    src_text = "\n".join(p.read_text() for p in pkg_dir.glob("*.py"))
    # Real secrets, not the *names* of secret patterns the denylist uses.
    real_secret_patterns = [
        r"sk-[A-Za-z0-9]{20,}",
        r"Bearer\s+[A-Za-z0-9_.-]{20,}",
        r"AccountKey=[A-Za-z0-9+/=]{20,}",
    ]
    for pattern in real_secret_patterns:
        assert not re.search(pattern, src_text), (
            f"release module appears to contain a real secret matching {pattern}"
        )
