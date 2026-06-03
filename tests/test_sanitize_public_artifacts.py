"""Tests for ``scripts/sanitize_public_artifacts.py``.

Covers the deterministic-replacement, idempotence, scope-exclusion,
test-fixture-map, manifest-write, and verify behaviors of the sweep
tool. No network calls. No real archive paths touched (tests use
``tmp_path``).

The workload-identifier literals (the resource short name, the project
name, and the throttled deployment alias) are reconstructed at
module-load time from string fragments so the test source bytes never
contain those literals directly. This is the same self-exclusion
pattern used by the sweep script itself, and is what lets
``sanitize_public_artifacts.py --verify`` pass on this test file:
a plain literal grep finds nothing, even though the constructed
runtime values are exactly what the sanitizer is designed to replace.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.sanitize_public_artifacts import (
    EXCLUDED_DIR_PREFIXES,
    FORBIDDEN_TOKENS,
    REPLACEMENTS,
    TEST_FIXTURE_REPLACEMENTS,
    FileScanResult,
    archive_original,
    is_in_scope,
    load_manifest,
    manifest_entry,
    scan_file,
    write_manifest,
)

# Fragment-constructed workload-identifier literals. Runtime values are
# identical to what the sanitizer looks for, but the source bytes of
# this test file do not contain the literals.
_RES = "wrpo" + "-meas"
_PRJ = "wrpo" + "-measurement"
_DEP = "gpt-5-" + "2-throttled"
_AZURE_SVC = ".services.ai.azure.com"
_HOST = _RES + _AZURE_SVC
_URL = f"https://{_HOST}/api/projects/{_PRJ}"


# ---------------------------------------------------------------------------
# Replacement maps
# ---------------------------------------------------------------------------


def test_canonical_map_replaces_url_compound_before_fragments() -> None:
    # The compound URL form must come first; the host form before the
    # short tokens; and — critically — the project name MUST be
    # replaced BEFORE the resource short name because the resource
    # short name is a substring of the project name. Reversing the
    # last two ordering rules corrupts every standalone occurrence of
    # the project name into ``<resource>urement``.
    keys = list(REPLACEMENTS.keys())
    url_idx = keys.index(_URL)
    host_idx = keys.index(_HOST)
    resource_idx = keys.index(_RES)
    project_idx = keys.index(_PRJ)
    assert url_idx < host_idx
    assert host_idx < project_idx
    assert project_idx < resource_idx, (
        "project name must precede resource short name in the map "
        "(substring trap; see REPLACEMENTS docstring)"
    )


def test_substring_trap_is_avoided_for_project_name(tmp_path: Path) -> None:
    # Standalone project-name occurrence on its own line — no URL
    # context. Verifies the project name is replaced as a unit and is
    # NOT split into ``<resource>urement`` by the shorter resource key.
    body = f"tenant: {_PRJ}\n"
    p = _write(tmp_path / "exp.yaml", body)
    res = scan_file(p, "experiments/exp.yaml")
    assert res is not None
    new = res.sanitized_bytes.decode()
    assert new == "tenant: <project>\n", repr(new)
    assert "urement" not in new


def test_substring_trap_is_avoided_for_project_name_in_test_map(tmp_path: Path) -> None:
    body = f'PROJ = "{_PRJ}"\n'
    p = _write(tmp_path / "t.py", body)
    res = scan_file(p, "tests/test_x.py")
    assert res is not None
    new = res.sanitized_bytes.decode()
    assert new == 'PROJ = "example-project"\n', repr(new)
    assert "urement" not in new


def test_canonical_map_substitutes_are_pseudonymous_only() -> None:
    # No substitute may itself contain a workload identifier.
    for key, sub in REPLACEMENTS.items():
        for forbidden in FORBIDDEN_TOKENS:
            assert forbidden not in sub, (
                f"replacement {key!r} → {sub!r} leaks forbidden token "
                f"{forbidden!r}"
            )


def test_test_fixture_map_substitutes_are_pseudonymous_only() -> None:
    for key, sub in TEST_FIXTURE_REPLACEMENTS.items():
        for forbidden in FORBIDDEN_TOKENS:
            assert forbidden not in sub


def test_test_fixture_map_substitutes_remain_pattern_valid_hosts() -> None:
    # The fixture map exists precisely so redaction-regex unit tests
    # still receive an Azure-shaped hostname. Verify the host substitute
    # ends with the Azure suffix and has only [a-z0-9-] in the prefix.
    host_sub = TEST_FIXTURE_REPLACEMENTS[_HOST]
    assert host_sub.endswith(_AZURE_SVC)
    prefix = host_sub[: -len(_AZURE_SVC)]
    assert prefix and all(c.isalnum() or c == "-" for c in prefix), prefix


# ---------------------------------------------------------------------------
# Scope filter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        ".internal/raw-archive/x.json",
        "docs/05-methodology.md",
        "docs/16-release-tiers-and-redaction-policy.md",
        ".git/HEAD",
        ".venv/bin/python",
        "__pycache__/x.cpython-311.pyc",
        "img.png",
        "diagram.pdf",
        ".env",
    ],
)
def test_is_in_scope_excludes_protected_paths(rel: str) -> None:
    assert not is_in_scope(rel)


@pytest.mark.parametrize(
    "rel",
    [
        ".env.example",
        "CHANGELOG.md",
        "scripts/sanitize_public_artifacts.py",
        "experiments/exp001_short-factual_baseline.yaml",
        "benchmarks/01-short-factual/runs/some.json",
        "tests/test_run_benchmark.py",
        "results/summary.md",
    ],
)
def test_is_in_scope_includes_public_paths(rel: str) -> None:
    assert is_in_scope(rel)


def test_excluded_dir_prefixes_contain_docs_and_internal() -> None:
    assert "docs/" in EXCLUDED_DIR_PREFIXES
    assert ".internal/" in EXCLUDED_DIR_PREFIXES


# ---------------------------------------------------------------------------
# scan_file behavior
# ---------------------------------------------------------------------------


def _write(p: Path, body: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_scan_file_returns_none_when_no_matches(tmp_path: Path) -> None:
    p = _write(tmp_path / "clean.yaml", "model: gpt-5.2\nendpoint: https://example.com\n")
    assert scan_file(p, "experiments/clean.yaml") is None


def test_scan_file_returns_none_on_binary_input(tmp_path: Path) -> None:
    p = tmp_path / "blob.bin"
    p.write_bytes(b"\xff\xfe\x00\x01" + _RES.encode() + b"\xff\xff")
    # Non-UTF8 — should be skipped silently.
    assert scan_file(p, "scripts/blob.bin") is None


def test_scan_file_applies_canonical_map_for_non_test_paths(tmp_path: Path) -> None:
    body = (
        f"endpoint: {_URL}\n"
        f"deployment_name: {_DEP}\n"
        f"tenant: {_PRJ}\n"
    )
    p = _write(tmp_path / "exp.yaml", body)
    res = scan_file(p, "experiments/exp.yaml")
    assert res is not None
    assert not res.used_test_fixture_map
    new = res.sanitized_bytes.decode()
    assert _RES not in new
    assert _PRJ not in new
    assert _DEP not in new
    assert "<resource>" + _AZURE_SVC in new
    assert "<project>" in new
    assert "ptu-deploy-throttled" in new


def test_scan_file_applies_fixture_map_for_tests_paths(tmp_path: Path) -> None:
    body = f'leaky = "{_HOST}"\n'
    p = _write(tmp_path / "t.py", body)
    res = scan_file(p, "tests/test_redact.py")
    assert res is not None
    assert res.used_test_fixture_map
    new = res.sanitized_bytes.decode()
    assert _RES not in new
    # Pattern-valid replacement — must end with .services.ai.azure.com so
    # the Azure-host regex under test still matches.
    assert "example-host" + _AZURE_SVC in new


def test_scan_file_match_counts_are_accurate(tmp_path: Path) -> None:
    body = (
        f"{_RES} {_RES} {_RES}\n"
        f"{_DEP} {_DEP}\n"
    )
    p = _write(tmp_path / "x.yaml", body)
    res = scan_file(p, "experiments/x.yaml")
    assert res is not None
    # The standalone resource short-name key matches all three occurrences.
    assert res.match_counts[_RES] == 3
    assert res.match_counts[_DEP] == 2


def test_sanitization_is_idempotent(tmp_path: Path) -> None:
    body = (
        f"{_URL}\n"
        f"{_DEP}\n"
    )
    p = _write(tmp_path / "y.yaml", body)
    first = scan_file(p, "experiments/y.yaml")
    assert first is not None
    p.write_bytes(first.sanitized_bytes)
    second = scan_file(p, "experiments/y.yaml")
    assert second is None, "second pass produced replacements; map is not idempotent"


def test_sanitization_preserves_unrelated_content(tmp_path: Path) -> None:
    body = (
        "# header\n"
        f"deployment_name: {_DEP}\n"
        "max_output_tokens: 4096\n"
        "latency_ms: 1234.5\n"
        "usage: {\"input_tokens\": 240, \"cached_tokens\": 0}\n"
    )
    p = _write(tmp_path / "z.yaml", body)
    res = scan_file(p, "experiments/z.yaml")
    assert res is not None
    new = res.sanitized_bytes.decode()
    # Numeric metrics and surrounding text are untouched.
    assert "max_output_tokens: 4096" in new
    assert "latency_ms: 1234.5" in new
    assert "cached_tokens" in new
    assert "input_tokens" in new
    assert "# header" in new


def test_sha256_matches_known_bytes() -> None:
    assert hashlib.sha256(b"abc").hexdigest() == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


# ---------------------------------------------------------------------------
# Manifest + archive helpers
# ---------------------------------------------------------------------------


def _mk_result(rel: str, body: bytes) -> FileScanResult:
    new_body = body  # for the test we don't care about content semantics
    return FileScanResult(
        relative_path=rel,
        original_sha256=hashlib.sha256(body).hexdigest(),
        original_size_bytes=len(body),
        match_counts={_RES: 1},
        sanitized_bytes=new_body,
        sanitized_sha256=hashlib.sha256(new_body).hexdigest(),
        used_test_fixture_map=False,
    )


def test_manifest_entry_contains_required_fields() -> None:
    res = _mk_result("experiments/x.yaml", b"hello")
    entry = manifest_entry(res, archived_at_iso="2026-06-03T07:00:00Z", git_commit_sha="abcdef0")
    for k in (
        "source_relative_path",
        "archive_relative_path",
        "original_sha256",
        "original_size_bytes",
        "sanitized_sha256",
        "match_counts",
        "used_test_fixture_map",
        "archived_at_iso",
        "git_commit_sha_at_capture",
        "sweep_id",
    ):
        assert k in entry, k
    # Archive path is inside the private archive tree.
    assert entry["archive_relative_path"].startswith(".internal/raw-archive/")
    # No private filesystem-absolute path is embedded.
    assert not entry["archive_relative_path"].startswith("/")


def test_write_manifest_sorts_entries_and_is_deterministic(tmp_path: Path) -> None:
    data = {
        "schema": "wrpo-public-readiness-sweep",
        "schema_version": "1.0.0",
        "archive_dir": "20260603-public-readiness",
        "entries": [
            manifest_entry(_mk_result("b.yaml", b"b"), "2026-06-03T07:00:00Z", "abcdef0"),
            manifest_entry(_mk_result("a.yaml", b"a"), "2026-06-03T07:00:00Z", "abcdef0"),
        ],
    }
    p = tmp_path / "manifest.json"
    write_manifest(p, data)
    first = p.read_bytes()
    write_manifest(p, data)
    second = p.read_bytes()
    assert first == second
    text = first.decode()
    assert text.endswith("\n")
    parsed = json.loads(text)
    assert [e["source_relative_path"] for e in parsed["entries"]] == ["a.yaml", "b.yaml"]


def test_load_manifest_handles_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "doesnt-exist.json"
    data = load_manifest(p)
    assert data["entries"] == []
    assert data["schema"] == "wrpo-public-readiness-sweep"


def test_archive_original_copies_bytes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    archive = tmp_path / "archive"
    src_rel = "experiments/x.yaml"
    src_abs = repo / src_rel
    src_abs.parent.mkdir(parents=True)
    src_abs.write_bytes(b"the original bytes")
    dst = archive_original(repo, archive, src_rel)
    assert dst.exists()
    assert dst.read_bytes() == b"the original bytes"
    # Source is untouched.
    assert src_abs.read_bytes() == b"the original bytes"


def test_archive_original_is_append_only_for_identical_bytes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    archive = tmp_path / "archive"
    src_rel = "experiments/y.yaml"
    src_abs = repo / src_rel
    src_abs.parent.mkdir(parents=True)
    src_abs.write_bytes(b"same")
    first = archive_original(repo, archive, src_rel)
    second = archive_original(repo, archive, src_rel)
    # Same destination, no .dup1 created.
    assert first == second
    assert not (archive / "experiments" / "y.yaml.dup1").exists()


def test_archive_original_preserves_divergent_history(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    archive = tmp_path / "archive"
    src_rel = "experiments/z.yaml"
    src_abs = repo / src_rel
    src_abs.parent.mkdir(parents=True)
    src_abs.write_bytes(b"v1")
    archive_original(repo, archive, src_rel)
    # Source rewritten between sweeps — archive must NOT overwrite.
    src_abs.write_bytes(b"v2")
    dup = archive_original(repo, archive, src_rel)
    assert dup.name.endswith(".dup1")
    assert (archive / src_rel).read_bytes() == b"v1"
    assert dup.read_bytes() == b"v2"


# ---------------------------------------------------------------------------
# CLI-shaped end-to-end check against a tiny fake repo
# ---------------------------------------------------------------------------


def test_iter_candidates_against_synthetic_repo(tmp_path: Path) -> None:
    # Build a minimal git repo so list_tracked_files returns deterministic content.
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "x@x"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "x"], check=True)

    (repo / "experiments").mkdir()
    (repo / "docs").mkdir()
    (repo / ".internal").mkdir()
    _write(repo / "experiments/exp001.yaml", f"deployment: {_DEP}\n")
    _write(repo / "docs/05-methodology.md", f"Mentions {_RES} in narrative.\n")
    _write(repo / ".internal/notes.md", f"Private {_PRJ} note.\n")
    _write(repo / "README.md", "Clean readme.\n")

    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)

    from scripts.sanitize_public_artifacts import iter_candidates

    results = list(iter_candidates(repo))
    paths = {r.relative_path for r in results}
    # Only the experiments yaml is in scope (docs/ excluded; .internal/ excluded;
    # README.md has no matches).
    assert paths == {"experiments/exp001.yaml"}
    res = next(r for r in results if r.relative_path == "experiments/exp001.yaml")
    assert res.match_counts == {_DEP: 1}


def test_verify_clean_on_synthetic_repo_after_sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.sanitize_public_artifacts as mod

    repo = tmp_path / "r2"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "x@x"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "x"], check=True)

    _write(repo / "experiments/exp001.yaml", f"deployment: {_DEP}\n")
    _write(repo / "scripts/x.py", f"U = '{_URL}'\n")
    _write(repo / "tests/test_x.py", f'LEAK = "{_HOST}"\n')
    _write(repo / "docs/05-methodology.md", f"{_RES} appears in docs and is allowed.\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)

    # Monkey-patch module-level paths to point at the synthetic repo.
    monkeypatch.setattr(mod, "REPO_ROOT", repo)
    monkeypatch.setattr(mod, "ARCHIVE_ROOT", repo / ".internal" / "raw-archive" / mod.ARCHIVE_DIR_NAME)
    monkeypatch.setattr(mod, "MANIFEST_PATH", repo / ".internal" / "release" / "raw_archive_manifest.json")
    monkeypatch.setattr(mod, "PUBLIC_MANIFEST_PATH", repo / "release" / "public_sanitized_manifest.json")

    summary = mod.apply_sweep(repo, dry_run=False)
    assert summary["candidate_count"] == 3

    # Verify pass: clean, despite docs/ still containing the resource short-name.
    code, matches = mod.verify(repo)
    assert code == 0, matches
    assert matches == []

    # Manifest was created.
    manifest = json.loads((repo / ".internal" / "release" / "raw_archive_manifest.json").read_text())
    assert len(manifest["entries"]) == 3

    # Archive originals exist with the original bytes preserved.
    arch = repo / ".internal" / "raw-archive" / mod.ARCHIVE_DIR_NAME
    assert (arch / "experiments/exp001.yaml").read_text() == f"deployment: {_DEP}\n"
    assert (arch / "tests/test_x.py").read_text() == f'LEAK = "{_HOST}"\n'

    # Second sweep is a no-op.
    summary2 = mod.apply_sweep(repo, dry_run=False)
    assert summary2["candidate_count"] == 0
    assert summary2.get("written_entries", 0) == 0


def test_verify_flags_forbidden_token_on_unsanitized_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.sanitize_public_artifacts as mod

    repo = tmp_path / "r3"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "x@x"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "x"], check=True)

    _write(repo / "scripts/x.py", f"X = '{_RES}'\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)

    monkeypatch.setattr(mod, "REPO_ROOT", repo)
    code, matches = mod.verify(repo)
    assert code == 1
    assert ("scripts/x.py", _RES) in matches


# ---------------------------------------------------------------------------
# Public-safe provenance: redaction_rules_sha256 / archive_id / classes
# ---------------------------------------------------------------------------


def test_redaction_rules_sha256_is_deterministic() -> None:
    import scripts.sanitize_public_artifacts as mod

    a = mod._compute_redaction_rules_sha256()
    b = mod._compute_redaction_rules_sha256()
    assert a == b
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)


def test_redaction_rules_sha256_does_not_embed_private_tokens() -> None:
    # The hash MUST NOT be computed by hashing the workload tokens
    # themselves (those are low-entropy private values; a deterministic
    # hash of them would be a confirmation oracle). We test this by
    # confirming the hash is identical to one computed from ONLY the
    # public-safe class descriptor; if a future maintainer accidentally
    # routes `REPLACEMENTS` keys into the hash this test will trip.
    import hashlib
    import json

    import scripts.sanitize_public_artifacts as mod

    expected_payload = {
        "classes": [
            {
                "class": c["class"],
                "canonical_placeholder": c["canonical_placeholder"],
                "fixture_placeholder": c["fixture_placeholder"],
            }
            for c in mod.PUBLIC_REPLACEMENT_CLASSES
        ],
        "forbidden_classes": list(mod.PUBLIC_FORBIDDEN_CLASSES),
        "order_rule": mod.PUBLIC_REPLACEMENT_ORDER_RULE,
    }
    expected = hashlib.sha256(
        json.dumps(expected_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert mod._compute_redaction_rules_sha256() == expected
    # And the public payload must not literally contain any private token.
    for tok in mod.FORBIDDEN_TOKENS:
        assert tok not in json.dumps(expected_payload)


def test_public_replacement_classes_have_no_private_token_in_placeholders() -> None:
    import scripts.sanitize_public_artifacts as mod

    for c in mod.PUBLIC_REPLACEMENT_CLASSES:
        for placeholder in (c["canonical_placeholder"], c["fixture_placeholder"]):
            for tok in mod.FORBIDDEN_TOKENS:
                assert tok not in placeholder, (
                    f"placeholder {placeholder!r} leaks forbidden token {tok!r}"
                )


def test_make_source_archive_id_format_and_stability() -> None:
    import scripts.sanitize_public_artifacts as mod

    sha = "a" * 64
    aid = mod._make_source_archive_id(sha, "experiments/exp001.yaml")
    assert aid.startswith("raw-")
    assert len(aid) == len("raw-") + 32
    # Stable.
    assert aid == mod._make_source_archive_id(sha, "experiments/exp001.yaml")
    # Path-sensitive.
    assert aid != mod._make_source_archive_id(sha, "experiments/exp002.yaml")
    # Sha-sensitive.
    assert aid != mod._make_source_archive_id("b" * 64, "experiments/exp001.yaml")


def test_is_clean_artifact_path_rejects_unsafe_paths() -> None:
    import scripts.sanitize_public_artifacts as mod

    for bad in [
        "",
        "/absolute/path.json",
        "a\\b.json",
        "../outside.json",
        ".internal/secret.json",
        "foo/.internal/secret.json",
    ]:
        assert mod._is_clean_artifact_path(bad) is False, bad
    for good in [
        "experiments/exp001.yaml",
        "benchmarks/01/runs/x.json",
        ".env.example",
        "scripts/sanitize_public_artifacts.py",
    ]:
        assert mod._is_clean_artifact_path(good) is True, good


def test_public_manifest_entry_excludes_internal_path() -> None:
    import scripts.sanitize_public_artifacts as mod

    e = mod.public_manifest_entry(
        artifact_path="experiments/exp001.yaml",
        sanitized_sha256="b" * 64,
        source_raw_sha256="a" * 64,
        redaction_rules_sha256="c" * 64,
        redacted_at_iso="2026-06-03T07:00:00Z",
        redactor_commit_sha="abc1234",
        redactor_script_sha256="d" * 64,
        sweep_id=mod.ARCHIVE_DIR_NAME,
    )
    text = repr(e)
    assert ".internal" not in text
    assert "archive_relative_path" not in e
    # Required fields are present.
    for k in (
        "artifact_path",
        "tier",
        "sanitized_sha256",
        "source_raw_sha256",
        "source_archive_id",
        "redaction_rules_sha256",
        "redacted_at_iso",
        "redactor_commit_sha",
        "redactor_script_sha256",
        "sweep_id",
    ):
        assert k in e, k
    assert e["tier"] == "SANITIZED_PUBLIC"
    # archive_id is derived deterministically.
    assert e["source_archive_id"] == mod._make_source_archive_id("a" * 64, "experiments/exp001.yaml")


def test_public_manifest_entry_rejects_unsafe_artifact_path() -> None:
    import scripts.sanitize_public_artifacts as mod

    with pytest.raises(ValueError):
        mod.public_manifest_entry(
            artifact_path=".internal/leak.json",
            sanitized_sha256="b" * 64,
            source_raw_sha256="a" * 64,
            redaction_rules_sha256="c" * 64,
            redacted_at_iso="2026-06-03T07:00:00Z",
            redactor_commit_sha="abc1234",
            redactor_script_sha256="d" * 64,
            sweep_id=mod.ARCHIVE_DIR_NAME,
        )


def test_public_manifest_entry_omits_concrete_private_values() -> None:
    import scripts.sanitize_public_artifacts as mod

    e = mod.public_manifest_entry(
        artifact_path="experiments/exp001.yaml",
        sanitized_sha256="b" * 64,
        source_raw_sha256="a" * 64,
        redaction_rules_sha256="c" * 64,
        redacted_at_iso="2026-06-03T07:00:00Z",
        redactor_commit_sha="abc1234",
        redactor_script_sha256="d" * 64,
        sweep_id=mod.ARCHIVE_DIR_NAME,
    )
    s = repr(e)
    for forbidden in mod.FORBIDDEN_TOKENS:
        assert forbidden not in s


# ---------------------------------------------------------------------------
# Public manifest write/verify
# ---------------------------------------------------------------------------


def _setup_synthetic_repo_and_sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build a tiny git repo and run apply_sweep against it. Returns (mod, repo, summary)."""
    import scripts.sanitize_public_artifacts as mod

    repo = tmp_path / "pmrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "x@x"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "x"], check=True)
    _write(repo / "experiments/exp001.yaml", f"deployment: {_DEP}\n")
    _write(repo / "scripts/y.py", f"U = '{_URL}'\n")
    _write(repo / "tests/test_y.py", f'HOST = "{_HOST}"\n')
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    monkeypatch.setattr(mod, "REPO_ROOT", repo)
    monkeypatch.setattr(mod, "ARCHIVE_ROOT", repo / ".internal" / "raw-archive" / mod.ARCHIVE_DIR_NAME)
    monkeypatch.setattr(mod, "MANIFEST_PATH", repo / ".internal" / "release" / "raw_archive_manifest.json")
    monkeypatch.setattr(mod, "PUBLIC_MANIFEST_PATH", repo / "release" / "public_sanitized_manifest.json")
    summary = mod.apply_sweep(repo, dry_run=False)
    return mod, repo, summary


def test_apply_writes_public_manifest_with_required_top_level_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod, repo, summary = _setup_synthetic_repo_and_sweep(tmp_path, monkeypatch)
    pm_path = repo / "release" / "public_sanitized_manifest.json"
    assert pm_path.exists(), summary
    data = json.loads(pm_path.read_text())
    assert data["schema"] == "wrpo-public-sanitized-manifest"
    assert data["schema_version"] == "1.0.0"
    assert data["tier"] == "SANITIZED_PUBLIC"
    assert data["sweep_id"] == mod.ARCHIVE_DIR_NAME
    assert data["redaction_rules_sha256"] == mod._compute_redaction_rules_sha256()
    assert data["redactor_script_sha256"] == mod._compute_redactor_script_sha256()
    # Three candidates were sanitized, all should appear in the public manifest.
    assert len(data["entries"]) == 3
    paths = [e["artifact_path"] for e in data["entries"]]
    assert paths == sorted(paths)
    assert set(paths) == {"experiments/exp001.yaml", "scripts/y.py", "tests/test_y.py"}


def test_apply_public_manifest_has_no_internal_substring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, repo, _ = _setup_synthetic_repo_and_sweep(tmp_path, monkeypatch)
    pm_path = repo / "release" / "public_sanitized_manifest.json"
    raw = pm_path.read_text(encoding="utf-8")
    assert ".internal/" not in raw
    assert "archive_relative_path" not in raw


def test_apply_public_manifest_has_no_forbidden_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod, repo, _ = _setup_synthetic_repo_and_sweep(tmp_path, monkeypatch)
    pm_path = repo / "release" / "public_sanitized_manifest.json"
    raw = pm_path.read_text(encoding="utf-8")
    for tok in mod.FORBIDDEN_TOKENS:
        assert tok not in raw, tok
    for needle in mod._PUBLIC_MANIFEST_DENY_SUBSTRINGS:
        assert needle not in raw.lower(), needle


def test_apply_public_manifest_is_deterministic_on_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod, repo, _ = _setup_synthetic_repo_and_sweep(tmp_path, monkeypatch)
    pm_path = repo / "release" / "public_sanitized_manifest.json"
    bytes_before = pm_path.read_bytes()
    # Re-run apply. No new candidates (idempotent), but public manifest is
    # rebuilt anyway.
    summary2 = mod.apply_sweep(repo, dry_run=False)
    assert summary2["candidate_count"] == 0
    bytes_after = pm_path.read_bytes()
    assert bytes_before == bytes_after, "public manifest is not deterministic on rebuild"


def test_apply_public_manifest_entry_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod, repo, _ = _setup_synthetic_repo_and_sweep(tmp_path, monkeypatch)
    pm_path = repo / "release" / "public_sanitized_manifest.json"
    data = json.loads(pm_path.read_text())
    for entry in data["entries"]:
        for k in (
            "artifact_path",
            "tier",
            "sanitized_sha256",
            "source_raw_sha256",
            "source_archive_id",
            "redaction_rules_sha256",
            "redacted_at_iso",
            "redactor_commit_sha",
            "redactor_script_sha256",
            "sweep_id",
        ):
            assert k in entry, (k, entry)
        # source_archive_id is opaque hex prefix.
        assert entry["source_archive_id"].startswith("raw-")
        assert len(entry["source_archive_id"]) == len("raw-") + 32
        # Per-entry rules sha matches top-level.
        assert entry["redaction_rules_sha256"] == data["redaction_rules_sha256"]
        # tier consistent.
        assert entry["tier"] == "SANITIZED_PUBLIC"
        # No private filesystem path embedded.
        assert ".internal" not in json.dumps(entry)
        # On-disk file's sha matches recorded sanitized sha.
        f = repo / entry["artifact_path"]
        import hashlib

        assert entry["sanitized_sha256"] == hashlib.sha256(f.read_bytes()).hexdigest()


def test_verify_public_manifest_clean_after_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod, repo, _ = _setup_synthetic_repo_and_sweep(tmp_path, monkeypatch)
    errors = mod.verify_public_manifest(
        repo,
        repo / "release" / "public_sanitized_manifest.json",
        private_manifest_path=repo / ".internal" / "release" / "raw_archive_manifest.json",
    )
    assert errors == [], errors


def test_verify_public_manifest_missing_passes_without_strict(tmp_path: Path) -> None:
    import scripts.sanitize_public_artifacts as mod

    repo = tmp_path / "empty"
    repo.mkdir()
    pm = repo / "release" / "public_sanitized_manifest.json"
    errors = mod.verify_public_manifest(repo, pm, require_present=False)
    assert errors == []


def test_verify_public_manifest_missing_fails_strict(tmp_path: Path) -> None:
    import scripts.sanitize_public_artifacts as mod

    repo = tmp_path / "empty2"
    repo.mkdir()
    pm = repo / "release" / "public_sanitized_manifest.json"
    errors = mod.verify_public_manifest(repo, pm, require_present=True)
    assert errors and any("missing" in e for e in errors)


def test_verify_public_manifest_detects_tampered_sanitized_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod, repo, _ = _setup_synthetic_repo_and_sweep(tmp_path, monkeypatch)
    pm = repo / "release" / "public_sanitized_manifest.json"
    data = json.loads(pm.read_text())
    # Flip one entry's sanitized_sha256 to a wrong value.
    data["entries"][0]["sanitized_sha256"] = "0" * 64
    pm.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    errors = mod.verify_public_manifest(repo, pm)
    assert any("sanitized_sha256" in e and "does not match" in e for e in errors), errors


def test_verify_public_manifest_detects_internal_substring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod, repo, _ = _setup_synthetic_repo_and_sweep(tmp_path, monkeypatch)
    pm = repo / "release" / "public_sanitized_manifest.json"
    data = json.loads(pm.read_text())
    # Inject `.internal/...` into a field value (sweep_id, which is a string).
    data["sweep_id"] = ".internal/raw-archive/leak"
    pm.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    errors = mod.verify_public_manifest(repo, pm)
    assert any(".internal/" in e for e in errors), errors


def test_verify_public_manifest_detects_duplicate_artifact_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod, repo, _ = _setup_synthetic_repo_and_sweep(tmp_path, monkeypatch)
    pm = repo / "release" / "public_sanitized_manifest.json"
    data = json.loads(pm.read_text())
    # Duplicate the first entry.
    data["entries"].append(dict(data["entries"][0]))
    data["entries"].sort(key=lambda e: e["artifact_path"])
    pm.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    errors = mod.verify_public_manifest(repo, pm)
    assert any("duplicate artifact_path" in e for e in errors), errors


def test_verify_public_manifest_detects_unsorted_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod, repo, _ = _setup_synthetic_repo_and_sweep(tmp_path, monkeypatch)
    pm = repo / "release" / "public_sanitized_manifest.json"
    data = json.loads(pm.read_text())
    # Reverse entries to violate the sorted-by-artifact_path invariant.
    data["entries"] = list(reversed(data["entries"]))
    pm.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    errors = mod.verify_public_manifest(repo, pm)
    assert any("not sorted" in e for e in errors), errors


def test_verify_public_manifest_detects_rules_sha_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod, repo, _ = _setup_synthetic_repo_and_sweep(tmp_path, monkeypatch)
    pm = repo / "release" / "public_sanitized_manifest.json"
    data = json.loads(pm.read_text())
    data["redaction_rules_sha256"] = "f" * 64
    pm.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    errors = mod.verify_public_manifest(repo, pm)
    assert any("redaction_rules_sha256" in e for e in errors), errors


def test_verify_public_manifest_completeness_against_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod, repo, _ = _setup_synthetic_repo_and_sweep(tmp_path, monkeypatch)
    pm = repo / "release" / "public_sanitized_manifest.json"
    private_path = repo / ".internal" / "release" / "raw_archive_manifest.json"
    # Drop one entry from the public manifest while the private retains it.
    data = json.loads(pm.read_text())
    dropped = data["entries"].pop(0)
    pm.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    errors = mod.verify_public_manifest(
        repo, pm, private_manifest_path=private_path
    )
    assert any(
        dropped["artifact_path"] in e or "absent from the public manifest" in e
        for e in errors
    ), errors


def test_public_manifest_refreshes_sanitized_sha_when_file_edited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When a previously-sanitized file is legitimately edited (the edit
    # does not reintroduce any forbidden token), the public manifest
    # MUST update the entry's sanitized_sha256 to the new on-disk sha,
    # NOT drop the entry. The artifact remains SANITIZED_PUBLIC (the
    # source_raw_sha256 / source_archive_id pin to the original RAW
    # source). Stale-manifest detection lives in --verify, not here.
    mod, repo, _ = _setup_synthetic_repo_and_sweep(tmp_path, monkeypatch)
    target = repo / "experiments/exp001.yaml"
    pm = repo / "release" / "public_sanitized_manifest.json"

    # Original entry exists and sha matches.
    data0 = json.loads(pm.read_text())
    e0 = next(e for e in data0["entries"] if e["artifact_path"] == "experiments/exp001.yaml")
    original_source_raw = e0["source_raw_sha256"]
    original_archive_id = e0["source_archive_id"]

    # Legitimate edit — append a clean line.
    target.write_text("deployment: ptu-deploy-throttled\nadded_line: 1\n")
    import hashlib

    expected_new_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    summary2 = mod.apply_sweep(repo, dry_run=False)
    assert summary2["candidate_count"] == 0  # no forbidden token re-introduced

    data1 = json.loads(pm.read_text())
    e1 = next((e for e in data1["entries"] if e["artifact_path"] == "experiments/exp001.yaml"), None)
    assert e1 is not None, "entry was dropped; should be refreshed"
    assert e1["sanitized_sha256"] == expected_new_sha
    # The provenance pointers back to the original RAW source remain.
    assert e1["source_raw_sha256"] == original_source_raw
    assert e1["source_archive_id"] == original_archive_id


def test_verify_detects_stale_public_manifest_when_file_edited_without_reapply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If the maintainer edits a sanitized file but does NOT re-run
    # --apply, the public manifest's sanitized_sha256 for that entry
    # is stale. --verify must catch this drift.
    mod, repo, _ = _setup_synthetic_repo_and_sweep(tmp_path, monkeypatch)
    target = repo / "experiments/exp001.yaml"
    target.write_text("deployment: ptu-deploy-throttled\nadded_line: 1\n")
    pm = repo / "release" / "public_sanitized_manifest.json"
    private = repo / ".internal" / "release" / "raw_archive_manifest.json"
    errors = mod.verify_public_manifest(repo, pm, private_manifest_path=private)
    assert any(
        "sanitized_sha256" in e and "does not match" in e
        for e in errors
    ), errors


def test_apply_excludes_public_manifest_from_candidate_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even if the public manifest is tracked and contains text, it must
    # never appear as a sanitization candidate (defensive exclusion).
    mod, repo, _ = _setup_synthetic_repo_and_sweep(tmp_path, monkeypatch)
    subprocess.run(["git", "-C", str(repo), "add", "release/public_sanitized_manifest.json"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "track public manifest"], check=True)
    cand_paths = {c.relative_path for c in mod.iter_candidates(repo)}
    assert "release/public_sanitized_manifest.json" not in cand_paths


def test_is_in_scope_excludes_public_manifest_path() -> None:
    import scripts.sanitize_public_artifacts as mod

    assert mod.is_in_scope("release/public_sanitized_manifest.json") is False


# ---------------------------------------------------------------------------
# Substring-corruption regression — re-asserted with public manifest live
# ---------------------------------------------------------------------------


def test_project_name_substring_trap_still_avoided_with_public_manifest_active(tmp_path: Path) -> None:
    # Re-pin the substring-trap fix: project-name must be replaced as a
    # whole, never split by the resource-short-name prefix. This is the
    # regression that the original sanitizer tests pinned; we re-assert
    # it here after adding the public-manifest layer.
    body = f"tenant: {_PRJ}\nresource: {_RES}\nproject_again: {_PRJ}\n"
    p = _write(tmp_path / "exp2.yaml", body)
    from scripts.sanitize_public_artifacts import scan_file

    res = scan_file(p, "experiments/exp2.yaml")
    assert res is not None
    new = res.sanitized_bytes.decode()
    assert "urement" not in new
    assert "<project>" in new
    assert "<resource>" in new
