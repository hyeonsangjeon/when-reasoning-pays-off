#!/usr/bin/env python3
"""Sync governed public chart-data into the GitHub Pages surface.

This helper is intentionally static and deterministic: it copies only the
candidate-listed public chart-data files, emits a byte-stable snapshot manifest,
and checks the mirrored chart-data surface without calling any network service.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

MIRROR_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LAB_ROOT = MIRROR_ROOT.parent / "when-reasoning-pays-off"
CHART_DATA_DIR = MIRROR_ROOT / "docs" / "blog" / "data" / "chart-data"
MIRROR_MANIFEST = CHART_DATA_DIR / "public_chart_candidates.json"
SNAPSHOT_MANIFEST = CHART_DATA_DIR / "snapshot_manifest.json"
SHA_REPORT = CHART_DATA_DIR / "SHA_SYNC_REPORT.md"
MIRROR_SCHEMA = MIRROR_ROOT / "schemas" / "public_chart_candidates.schema.json"
SOURCE_PREFIX = "results/public/chart-data/"
EXPECTED_FAMILIES = {"cost-curves-effort", "token-composition", "ptu-payg-crossover"}
EXPECTED_COUNT = 18
TIER = "SANITIZED_PUBLIC"
STABLE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
HEX_RE = re.compile(r"^[0-9a-f]{64}$")
FORBIDDEN_KEYS = {
    "title",
    "label",
    "labels",
    "legend",
    "description",
    "descriptions",
    "pricing_source_url",
    "pricing_accessed_date",
    "pricing_snapshot_path",
    "endpoint",
    "deployment",
    "deployment_name",
    "request_id",
    "run_id",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def candidate_digest(candidate: dict[str, Any]) -> str:
    blob = json.dumps(candidate, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
    return sha256_bytes(blob)


def repo_rel(path: Path) -> str:
    return path.relative_to(MIRROR_ROOT).as_posix()


def chart_dest(source_chart_data_path: str) -> Path:
    if not source_chart_data_path.startswith(SOURCE_PREFIX):
        raise ValueError(f"candidate path outside public chart-data: {source_chart_data_path}")
    suffix = source_chart_data_path[len(SOURCE_PREFIX):]
    if suffix.startswith("/") or ".." in Path(suffix).parts:
        raise ValueError(f"unsafe candidate suffix: {source_chart_data_path}")
    return CHART_DATA_DIR / suffix


def assert_stable_key(value: str, context: str) -> None:
    if not STABLE_KEY_RE.match(value):
        raise ValueError(f"{context}: non-stable key {value!r}")


def walk_keys(value: Any, path: str = "$"):
    if isinstance(value, dict):
        for key, child in value.items():
            yield path, key, child
            yield from walk_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from walk_keys(child, f"{path}[{i}]")


def validate_candidate_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if manifest.get("schema") != "wrpo-public-chart-candidates":
        raise ValueError("candidate manifest schema mismatch")
    if manifest.get("tier") != TIER:
        raise ValueError("candidate manifest tier mismatch")
    candidates = manifest.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("candidate manifest candidates must be a list")
    if len(candidates) != EXPECTED_COUNT:
        raise ValueError(f"expected {EXPECTED_COUNT} candidates, got {len(candidates)}")
    families = {c.get("family_key") for c in candidates}
    if families != EXPECTED_FAMILIES:
        raise ValueError(f"family set mismatch: {sorted(families)}")
    seen = set()
    for candidate in candidates:
        for key in ("family_key", "chart_data_path", "dimension_keys", "series_keys", "units_key", "tier", "target_topic_slug", "source_sanitized_sha256", "schema_semver"):
            if key not in candidate:
                raise ValueError(f"candidate missing {key}: {candidate}")
        if candidate["tier"] != TIER:
            raise ValueError(f"candidate tier mismatch: {candidate['chart_data_path']}")
        path = candidate["chart_data_path"]
        if path in seen:
            raise ValueError(f"duplicate candidate path: {path}")
        seen.add(path)
        if not path.startswith(SOURCE_PREFIX) or ".." in Path(path).parts or path.startswith("/"):
            raise ValueError(f"unsafe candidate path: {path}")
        assert_stable_key(candidate["family_key"], path)
        assert_stable_key(candidate["units_key"], path)
        assert_stable_key(candidate["target_topic_slug"], path)
        for dim in candidate["dimension_keys"]:
            assert_stable_key(dim, f"{path}.dimension_keys")
        for series in candidate["series_keys"]:
            assert_stable_key(series, f"{path}.series_keys")
        for digest in candidate["source_sanitized_sha256"]:
            if not HEX_RE.match(digest):
                raise ValueError(f"bad source digest in {path}: {digest}")
    return candidates


def validate_chart_payload(payload: dict[str, Any], candidate: dict[str, Any], path: str) -> None:
    for parent, key, child in walk_keys(payload):
        if key in FORBIDDEN_KEYS:
            raise ValueError(f"{path}: forbidden display/provenance key {parent}.{key}")
        if isinstance(child, str):
            if "/Users/" in child or "/home/" in child or "http://" in child or "https://" in child:
                raise ValueError(f"{path}: forbidden public string at {parent}.{key}")
    if payload.get("schema") != "wrpo.chart_data":
        raise ValueError(f"{path}: chart schema mismatch")
    if payload.get("tier") != TIER:
        raise ValueError(f"{path}: chart tier mismatch")
    for key in ("family_key", "schema_semver", "dimension_keys", "series_keys", "units_key"):
        if payload.get(key) != candidate.get(key):
            raise ValueError(f"{path}: payload/candidate mismatch for {key}")
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{path}: rows must be a non-empty list")
    dims = set(payload["dimension_keys"])
    series = set(payload["series_keys"])
    allowed_stable_string_keys = dims | {"metric_key", "benchmark_key", "family_key", "framing_key", "schema", "schema_semver", "tier", "units_key", "quality_family_key", "quality_metric_key", "quality_chart_data_path", "quality_benchmark_key"}
    for row_i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{path}: row {row_i} is not an object")
        for series_key in series:
            value = row.get(series_key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{path}: row {row_i}.{series_key} must be numeric")
        for dim in dims:
            value = row.get(dim)
            if not isinstance(value, str):
                raise ValueError(f"{path}: row {row_i}.{dim} must be a stable string")
            if dim == "effort":
                assert_stable_key(value, f"{path}: row {row_i}.{dim}")
    for parent, key, child in walk_keys(payload):
        if isinstance(child, str) and key not in allowed_stable_string_keys and key not in dims:
            if not HEX_RE.match(child) and not STABLE_KEY_RE.match(child) and not child.startswith(SOURCE_PREFIX):
                raise ValueError(f"{path}: non-stable string at {parent}.{key}")
    if payload.get("family_key") == "ptu-payg-crossover":
        if payload.get("framing_key") != "throughput_gain_hypothesis":
            raise ValueError(f"{path}: missing PTU modeled-hypothesis framing key")
        qp = payload.get("quality_pairing")
        if not isinstance(qp, dict) or not str(qp.get("quality_chart_data_path", "")).startswith(SOURCE_PREFIX):
            raise ValueError(f"{path}: missing quality pairing")


def load_source_manifest(lab_root: Path) -> dict[str, Any]:
    return load_json(lab_root / "release" / "public_chart_candidates.json")


def build_snapshot(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    entries = []
    for candidate in sorted(candidates, key=lambda c: c["chart_data_path"]):
        dest = chart_dest(candidate["chart_data_path"])
        data = dest.read_bytes()
        entries.append({
            "bytes": len(data),
            "candidate_digest": candidate_digest(candidate),
            "chart_data_path": repo_rel(dest),
            "family_key": candidate["family_key"],
            "schema_semver": candidate["schema_semver"],
            "sha256": sha256_bytes(data),
            "source_chart_data_path": candidate["chart_data_path"],
            "tier": candidate["tier"],
        })
    return {
        "candidates": entries,
        "generated_count": len(entries),
        "schema": "wrpo-pages-chart-data-snapshot",
        "schema_semver": "0.1.0",
        "tier": TIER,
    }


def write_sha_report(snapshot: dict[str, Any]) -> None:
    lines = [
        "# Public chart-data snapshot SHA report",
        "",
        "This report lists copied static chart-data files and their mirrored file SHA-256 values. Source lineage digests remain in the chart payloads and candidate manifest; they are not expected to equal the mirrored file SHA.",
        "",
        "| Family | Mirror path | Bytes | SHA-256 | Status |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for entry in snapshot["candidates"]:
        lines.append(f"| `{entry['family_key']}` | `{entry['chart_data_path']}` | {entry['bytes']} | `{entry['sha256']}` | PASS |")
    SHA_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def sync(lab_root: Path) -> None:
    manifest = load_source_manifest(lab_root)
    candidates = validate_candidate_manifest(manifest)
    CHART_DATA_DIR.mkdir(parents=True, exist_ok=True)
    dump_json(MIRROR_MANIFEST, manifest)
    MIRROR_SCHEMA.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(lab_root / "schemas" / "public_chart_candidates.schema.json", MIRROR_SCHEMA)
    copied = []
    for candidate in candidates:
        src = lab_root / candidate["chart_data_path"]
        if not src.is_file():
            raise FileNotFoundError(candidate["chart_data_path"])
        payload = load_json(src)
        validate_chart_payload(payload, candidate, candidate["chart_data_path"])
        dest = chart_dest(candidate["chart_data_path"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied.append(dest)
    snapshot = build_snapshot(candidates)
    dump_json(SNAPSHOT_MANIFEST, snapshot)
    write_sha_report(snapshot)
    print(f"synced {len(copied)} chart-data files across {len(EXPECTED_FAMILIES)} families")


def check() -> None:
    manifest = load_json(MIRROR_MANIFEST)
    candidates = validate_candidate_manifest(manifest)
    if not MIRROR_SCHEMA.is_file():
        raise FileNotFoundError(repo_rel(MIRROR_SCHEMA))
    snapshot = load_json(SNAPSHOT_MANIFEST)
    if snapshot.get("schema") != "wrpo-pages-chart-data-snapshot" or snapshot.get("tier") != TIER:
        raise ValueError("snapshot manifest header mismatch")
    entries = {entry["source_chart_data_path"]: entry for entry in snapshot.get("candidates", [])}
    if len(entries) != EXPECTED_COUNT:
        raise ValueError("snapshot entry count mismatch")
    for candidate in candidates:
        source_path = candidate["chart_data_path"]
        dest = chart_dest(source_path)
        mirror_rel = repo_rel(dest)
        if not dest.is_file():
            raise FileNotFoundError(mirror_rel)
        payload = load_json(dest)
        validate_chart_payload(payload, candidate, mirror_rel)
        entry = entries.get(source_path)
        if not entry:
            raise ValueError(f"missing snapshot entry for {source_path}")
        expected = {
            "chart_data_path": mirror_rel,
            "source_chart_data_path": source_path,
            "family_key": candidate["family_key"],
            "schema_semver": candidate["schema_semver"],
            "bytes": len(dest.read_bytes()),
            "sha256": sha256_file(dest),
            "candidate_digest": candidate_digest(candidate),
            "tier": TIER,
        }
        for key, value in expected.items():
            if entry.get(key) != value:
                raise ValueError(f"snapshot mismatch for {source_path}.{key}: {entry.get(key)!r} != {value!r}")
    rebuilt = build_snapshot(candidates)
    if snapshot != rebuilt:
        raise ValueError("snapshot_manifest.json is not deterministic with current files")
    report = SHA_REPORT.read_text(encoding="utf-8")
    if "Source lineage digests" not in report or "PASS" not in report:
        raise ValueError("SHA report missing lineage caveat or PASS rows")
    print(f"check passed: {len(candidates)} candidates, {len(EXPECTED_FAMILIES)} families")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--lab-root", default=os.environ.get("WRPO_LAB_REPO", str(DEFAULT_LAB_ROOT)))
    args = parser.parse_args()
    if args.check:
        check()
    else:
        sync(Path(args.lab_root).resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
