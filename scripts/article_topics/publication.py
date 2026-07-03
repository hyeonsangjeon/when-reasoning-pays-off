"""Public article-data publication helpers.

Generator modules should own domain work: parsing benchmark CSVs, joining
chart rows, or applying a modeled formula. This module owns the repeated public
publication contract around those outputs: deterministic JSON, the shared chart
candidate manifest, and the public sanitized-manifest ledger.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts import sanitize_public_artifacts as spa

REPO_ROOT = Path(__file__).resolve().parents[2]

CHART_DATA_ROOT = REPO_ROOT / "results" / "public" / "chart-data"
CANDIDATE_MANIFEST_RELPATH = "release/public_chart_candidates.json"
CANDIDATE_MANIFEST_PATH = REPO_ROOT / CANDIDATE_MANIFEST_RELPATH
CHANGELOG_RELPATH = "CHANGELOG.md"

SCHEMA_SEMVER = "0.1.0"
CHART_DATA_SCHEMA = "wrpo.chart_data"
CANDIDATES_SCHEMA = "wrpo-public-chart-candidates"
TIER = "SANITIZED_PUBLIC"

# Fixed publication marker (UTC, hour-rounded) so re-runs are byte-stable. This
# records when the public artifact was promoted, not when measurements ran.
PROMOTION_TS_ISO = "2026-06-04T00:00:00Z"


@dataclass(frozen=True)
class PublishedArtifact:
    """One generated public artifact plus its publication metadata."""

    relpath: str
    payload: dict[str, Any]
    source_raw_sha: str
    candidate: dict[str, Any]


def serialize_json(data: dict[str, Any]) -> bytes:
    """Serialize JSON exactly as public chart-data generators write it."""
    return (
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return spa._sha256_bytes(data)


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def load_existing_candidates(path: Path = CANDIDATE_MANIFEST_PATH) -> list[dict[str, Any]]:
    """Load the current shared candidate list, returning empty on absence/drift."""
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        return []
    return [candidate for candidate in candidates if isinstance(candidate, dict)]


def merged_candidate_manifest(
    emitted: list[PublishedArtifact],
    *,
    owned_family_keys: frozenset[str],
    existing_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Merge emitted candidates into the shared append-aware manifest.

    Each generator owns one or more chart families. Candidates for those
    families are regenerated from source; candidates for other families are
    preserved from the on-disk manifest so the generators can run independently.
    """
    candidates = existing_candidates
    if candidates is None:
        candidates = load_existing_candidates()
    others = [
        candidate
        for candidate in candidates
        if candidate.get("family_key") not in owned_family_keys
    ]
    merged = others + [artifact.candidate for artifact in emitted]
    merged.sort(key=lambda candidate: candidate["chart_data_path"])
    return {
        "schema": CANDIDATES_SCHEMA,
        "schema_semver": SCHEMA_SEMVER,
        "tier": TIER,
        "candidates": merged,
    }


def write_or_check_artifacts(
    emitted: list[PublishedArtifact],
    *,
    check: bool,
    repo_root: Path = REPO_ROOT,
) -> list[str]:
    """Write generated artifacts or return relpaths whose bytes would drift."""
    drift: list[str] = []
    for artifact in emitted:
        out_path = repo_root / artifact.relpath
        new_bytes = serialize_json(artifact.payload)
        if check:
            if not out_path.is_file() or out_path.read_bytes() != new_bytes:
                drift.append(artifact.relpath)
            continue
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(new_bytes)
    return drift


def write_or_check_candidate_manifest(
    candidate_manifest: dict[str, Any],
    *,
    check: bool,
    path: Path = CANDIDATE_MANIFEST_PATH,
) -> tuple[str, bool]:
    """Write/check the shared candidate manifest.

    Returns ``(candidate_sha, drifted)``. In check mode, ``drifted`` means the
    on-disk manifest is absent or byte-different from the regenerated one.
    """
    candidate_bytes = serialize_json(candidate_manifest)
    candidate_sha = sha256_bytes(candidate_bytes)
    if check:
        return candidate_sha, not path.is_file() or path.read_bytes() != candidate_bytes
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(candidate_bytes)
    return candidate_sha, False


def update_public_manifest(
    emitted: list[PublishedArtifact],
    *,
    candidate_sha: str,
    repo_root: Path = REPO_ROOT,
) -> None:
    """Upsert emitted artifacts into ``release/public_sanitized_manifest.json``."""
    manifest = spa.load_public_manifest(spa.PUBLIC_MANIFEST_PATH)
    rules_sha = manifest.get("redaction_rules_sha256") or spa._compute_redaction_rules_sha256()
    sweep_id = manifest.get("sweep_id")
    redactor_commit = head_commit_sha(repo_root)
    redactor_script_sha = (
        manifest.get("redactor_script_sha256") or spa._compute_redactor_script_sha256()
    )

    by_path: dict[str, dict[str, Any]] = {
        str(entry.get("artifact_path")): entry for entry in manifest.get("entries", [])
    }

    def upsert(relpath: str, sanitized_sha: str, source_raw_sha: str) -> None:
        by_path[relpath] = spa.public_manifest_entry(
            artifact_path=relpath,
            sanitized_sha256=sanitized_sha,
            source_raw_sha256=source_raw_sha,
            redaction_rules_sha256=rules_sha,
            redacted_at_iso=PROMOTION_TS_ISO,
            redactor_commit_sha=redactor_commit,
            redactor_script_sha256=redactor_script_sha,
            sweep_id=sweep_id,
        )

    for artifact in emitted:
        on_disk = sha256_file(repo_root / artifact.relpath)
        upsert(artifact.relpath, on_disk, artifact.source_raw_sha)

    # Candidate manifest is a born-clean authored artifact, so it self-pins.
    upsert(CANDIDATE_MANIFEST_RELPATH, candidate_sha, candidate_sha)
    refresh_changelog_entry(by_path, repo_root=repo_root)

    manifest["entries"] = list(by_path.values())
    spa.write_public_manifest(spa.PUBLIC_MANIFEST_PATH, manifest)


def refresh_changelog_entry(
    by_path: dict[str, dict[str, Any]],
    *,
    repo_root: Path = REPO_ROOT,
) -> None:
    """Refresh the changelog manifest sha when the changelog changed."""
    changelog_path = repo_root / CHANGELOG_RELPATH
    if not changelog_path.is_file() or CHANGELOG_RELPATH not in by_path:
        return
    existing = by_path[CHANGELOG_RELPATH]
    on_disk = sha256_file(changelog_path)
    if existing.get("sanitized_sha256") == on_disk:
        return
    refreshed = dict(existing)
    refreshed["sanitized_sha256"] = on_disk
    refreshed["source_raw_sha256"] = on_disk
    refreshed["source_archive_id"] = spa._make_source_archive_id(on_disk, CHANGELOG_RELPATH)
    by_path[CHANGELOG_RELPATH] = refreshed


def head_commit_sha(repo_root: Path = REPO_ROOT) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
        )
        return out.stdout.strip() or None
    except (subprocess.CalledProcessError, OSError):
        return None


def report_drift(drift: list[str]) -> None:
    import sys

    print("DRIFT: regenerated output differs from on-disk:", file=sys.stderr)
    for relpath in drift:
        print(f"  - {relpath}", file=sys.stderr)
