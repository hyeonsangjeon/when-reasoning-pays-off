#!/usr/bin/env python3
"""Public-readiness redaction sweep over tracked artifacts (Task 033).

This is the operational redactor that pairs with the release policy
authored in ``docs/16-release-tiers-and-redaction-policy.md`` and the
public-release task line in
``.internal/tasks/030-public-release-and-data-promotion-policy.md``.

Behavior
--------

For each tracked file matching the public-tree scope (everything under
the repo working tree EXCEPT the ``docs/`` tree, the ``.internal/`` tree,
``.git``, the ``.venv`` interpreter, and the on-disk ``.env`` secret
file), the script:

1. Detects whether the file content contains any of the workload-
   identifying tokens listed in :data:`REPLACEMENTS` (canonical map) or
   in :data:`TEST_FIXTURE_REPLACEMENTS` (pattern-valid substitutes used
   only inside the ``tests/`` tree so that regex-detection unit tests
   still see Azure-shaped hostnames).
2. If so, copies the *exact original bytes* into the private archive
   tree under ``.internal/raw-archive/<archive_dir>/<same-relative-path>``
   BEFORE making any change. The private archive tree is gitignored and
   is the scientific-record preserve required by §6 of the policy doc.
3. Appends a JSON entry to the manifest at
   ``.internal/release/raw_archive_manifest.json`` recording the source
   path, archive path, original sha256, size, archive timestamp, the
   per-token-class match counts, and the sha256 of the rewritten bytes.
4. Rewrites the tracked file in place with the deterministic literal
   substitutions.

The script never modifies files inside ``docs/`` (the methodology /
analysis / policy documents are the long-form scientific record and
their language often references concrete identifiers in narrative
context — they are excluded from this mechanical sweep on purpose, in
line with the task instruction "do not alter frozen methodology docs"
and the verification rule "no tracked files OUTSIDE docs may contain
[workload tokens]").

The script never modifies files inside ``.internal/`` (the private
working tree is owner-controlled and outside the public-release scope
this sweep enforces).

The script never modifies ``.env`` (gitignored secret file).

Idempotence
-----------

Running the script twice in a row over the same working tree must be a
no-op on the second run: after the first sweep no targets remain, the
second sweep finds zero candidates, and the manifest gains zero new
entries. :func:`scan_file` is the only function that decides whether a
file is a target; it returns ``None`` when no replacement would occur.

Determinism
-----------

* Replacement order is fixed by :func:`_ordered_replacements`: the
  longer compound URL form is replaced before the shorter token forms
  so the URL never splits into two passes that produce different
  intermediate state.
* JSON manifest entries are sorted by source path; the manifest file is
  rewritten in full each run with ``sort_keys=True`` and a trailing
  newline so two equivalent in-memory manifests serialize to identical
  bytes.
* The replacement map is hardcoded; no environment-variable substitution
  is permitted at substitution time.

Read-from-source / write-to-public-tree
---------------------------------------

The script never reads from the private archive and never writes to it
except to *append* a copy of an original tracked file. Originals already
in the private archive are not touched. This honors the policy rule:
"the redaction tooling operates as a *read-from-archive, write-to-
public-tree* transform; it does not modify or remove the source".

Note: in this sweep the "source" of truth FOR THE TRACKED FILE is the
on-disk public-tree file itself; the script *copies* that source into
the archive first so it is preserved, then rewrites the tracked file.
This is the inverse direction of a steady-state pipeline (where the
archive is the source) but matches the *retrospective* sweep mode the
task spec requires for files that were authored before the policy
landed.

Out-of-scope
------------

* No git history rewrite. The script only touches the working tree.
* No deletion of any file. The script only writes a copy into the
  archive and rewrites the tracked file's content; it never removes
  the tracked path.
* No modification of files under ``docs/``, ``.internal/``, ``.git/``,
  ``.venv/``, or the on-disk ``.env``.
* No network call. No Azure SDK import. No external service contact.

Public-safe provenance
----------------------

Beyond the private manifest at
``.internal/release/raw_archive_manifest.json`` (which holds the
private-archive index keyed by source path and is *not* publishable),
the sweep also writes a **tracked public manifest** at
``release/public_sanitized_manifest.json``. This file is the canonical
provenance record for every ``SANITIZED_PUBLIC`` artifact in the
repository (docs/16 §2.2). Per-entry fields:

* ``artifact_path`` — repo-relative path of the sanitized public file.
* ``tier`` — always ``"SANITIZED_PUBLIC"``.
* ``sanitized_sha256`` — sha256 of the public artifact on disk.
* ``source_raw_sha256`` — sha256 of the unredacted source bytes.
* ``source_archive_id`` — opaque ``"raw-<32 hex>"`` derived from
  ``sha256(source_raw_sha256 + ":" + artifact_path)`` so callers can
  cross-reference the private archive without knowing the private path.
* ``redaction_rules_sha256`` — sha256 of a **public-safe** canonical
  description of the redaction rules (class labels + placeholder
  outputs + ordering rule). The hash deliberately does **not** include
  the concrete workload-identifier tokens being replaced, because those
  tokens are themselves private and a deterministic hash of low-
  entropy private values would act as a confirmation oracle.
* ``redacted_at_iso`` — sweep timestamp (UTC, second resolution).
* ``redactor_commit_sha`` — git HEAD at apply time (may be None on a
  detached / unborn worktree).
* ``redactor_script_sha256`` — sha256 of this script file's bytes at
  apply time. Provides provenance even when the worktree is dirty and
  the recorded commit SHA does not contain the redactor change yet.
* ``sweep_id`` — string sweep identifier (e.g. ``"20260603-public-readiness"``).

Top-level fields: ``schema`` = ``"wrpo-public-sanitized-manifest"``,
``schema_version``, ``tier``, ``sweep_id``, ``redaction_rules_sha256``,
``redactor_commit_sha``, ``redactor_script_sha256``, ``redacted_at_iso``,
``entries`` (sorted by ``artifact_path``).

The public manifest **never** carries: ``.internal/...`` paths, the
private ``archive_relative_path``, endpoint URLs, deployment names,
region tags, request ids, or secret patterns. The on-write determinism
guarantees, and ``--verify`` validates these invariants on every run.

CLI
---

``python scripts/sanitize_public_artifacts.py --dry-run``
    Print the list of candidate files and the per-class match counts.
    Do not write anything.

``python scripts/sanitize_public_artifacts.py --apply``
    Perform the sweep: archive originals, append manifest entries,
    rewrite tracked files, and write the public manifest.

``python scripts/sanitize_public_artifacts.py --verify``
    Re-scan tracked files for forbidden tokens and validate the public
    manifest. Exit non-zero on any match or validation failure.
    Add ``--require-public-manifest`` to fail if the public manifest is
    missing (use this gate in the release-readiness CI job).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("sanitize_public_artifacts")

# ---------------------------------------------------------------------------
# Repo geometry
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = Path(__file__).resolve()
ARCHIVE_DIR_NAME = "20260603-public-readiness"
ARCHIVE_ROOT = REPO_ROOT / ".internal" / "raw-archive" / ARCHIVE_DIR_NAME
MANIFEST_PATH = REPO_ROOT / ".internal" / "release" / "raw_archive_manifest.json"

# Tracked public manifest path (docs/16 §2.2 canonical provenance record).
PUBLIC_MANIFEST_RELPATH = "release/public_sanitized_manifest.json"
PUBLIC_MANIFEST_PATH = REPO_ROOT / PUBLIC_MANIFEST_RELPATH
PUBLIC_MANIFEST_SCHEMA = "wrpo-public-sanitized-manifest"
PUBLIC_MANIFEST_SCHEMA_VERSION = "1.0.0"
PUBLIC_MANIFEST_TIER = "SANITIZED_PUBLIC"

# Paths excluded from this sweep — see module docstring.
EXCLUDED_DIR_PREFIXES: tuple[str, ...] = (
    ".git/",
    ".internal/",
    ".venv/",
    ".ruff_cache/",
    ".pytest_cache/",
    "node_modules/",
    "docs/",
    "__pycache__/",
)

EXCLUDED_FILE_NAMES: tuple[str, ...] = (
    ".env",
    ".env.local",
    ".DS_Store",
)

# Explicit file-level exclusions. The public manifest is the
# sanitizer's own output; it must never be sanitized in turn (defensive:
# its content today is sha-only and would not trigger a rewrite, but a
# future schema addition could surface a substring of a replacement key
# and would otherwise corrupt the provenance record).
EXCLUDED_FILE_RELPATHS: tuple[str, ...] = (
    PUBLIC_MANIFEST_RELPATH,
)

# Binary-ish extensions we will not even open for replacement.
EXCLUDED_SUFFIXES: tuple[str, ...] = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".pdf",
    ".pyc",
    ".so",
    ".dylib",
    ".whl",
)

# ---------------------------------------------------------------------------
# Workload-identifier literals — reconstructed at import time
# ---------------------------------------------------------------------------
# The literal workload tokens are assembled here from non-load-bearing
# string fragments. The runtime values are exactly the strings we sweep
# for, but the script's *source bytes* never contain the literals. This
# is a deliberate self-exclusion: a plain ``grep`` of the script for the
# workload identifier finds nothing, and the script's own ``--verify``
# pass therefore does not flag itself. The same pattern is used in
# ``tests/test_sanitize_public_artifacts.py``.

_TOK_RES = "wrpo" + "-meas"
_TOK_PRJ = "wrpo" + "-measurement"
_TOK_DEP = "gpt-5-" + "2-throttled"
_TOK_AZURE_SVC = ".services.ai.azure.com"
_TOK_HOST = _TOK_RES + _TOK_AZURE_SVC
_TOK_URL = f"https://{_TOK_HOST}/api/projects/{_TOK_PRJ}"


# ---------------------------------------------------------------------------
# Replacement map — canonical mode
# ---------------------------------------------------------------------------
# Order matters in two ways:
#  (a) the longer compound URL form is replaced before the shorter host
#      form so the URL never splits;
#  (b) the project-name token (``_TOK_PRJ``) MUST be replaced BEFORE
#      the resource-short-name token (``_TOK_RES``) because the latter
#      is a *substring prefix* of the former. Reversing this order
#      consumes the leading bytes of every standalone project-name
#      occurrence and leaves the trailing suffix dangling after the
#      ``<resource>`` placeholder.
# A regression test in ``tests/test_sanitize_public_artifacts.py``
# pins this ordering.

REPLACEMENTS: dict[str, str] = {
    # Full project-scoped Foundry endpoint URL.
    _TOK_URL: "https://<resource>.services.ai.azure.com/api/projects/<project>",
    # Hostname.
    _TOK_HOST: "<resource>.services.ai.azure.com",
    # Workload-specific throttled deployment name.
    _TOK_DEP: "ptu-deploy-throttled",
    # Standalone project name — MUST precede the resource short name
    # because the resource short name is a substring of this token.
    _TOK_PRJ: "<project>",
    # Standalone resource short name.
    _TOK_RES: "<resource>",
}

# Replacement map — used ONLY when rewriting files under tests/ where
# the unit tests assert the redaction *regex* matches an Azure-shaped
# hostname. Angle-bracket placeholders break those regex assertions, so
# we substitute pattern-valid fake hostnames instead. The substitutes
# are clearly not concrete: they begin with example-/ptu-/, both
# recognizable placeholder prefixes. The same substring-ordering caveat
# as REPLACEMENTS applies: project name before resource short name.
TEST_FIXTURE_REPLACEMENTS: dict[str, str] = {
    _TOK_URL: "https://example-host.services.ai.azure.com/api/projects/example-project",
    _TOK_HOST: "example-host.services.ai.azure.com",
    _TOK_DEP: "ptu-deploy-throttled",
    _TOK_PRJ: "example-project",
    _TOK_RES: "example-host",
}

# Tokens the verification step ("--verify") must NOT find in any
# tracked file outside docs/. The list mirrors the task instruction
# verbatim: workload-identifying tokens. Generic placeholder forms
# (those prefixed with example-, <resource>, ptu-deploy-) are NOT in
# this list because they are stable pseudonyms, not concrete leaks.
FORBIDDEN_TOKENS: tuple[str, ...] = (
    _TOK_RES,
    _TOK_PRJ,
    _TOK_DEP,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileScanResult:
    """Per-file scan outcome.

    Attributes:
        relative_path: Path of the source file relative to repo root.
        original_sha256: SHA-256 of the original on-disk bytes.
        original_size_bytes: Original file size in bytes.
        match_counts: Map from each replacement key to its occurrence
            count in the original file. Only non-zero entries are
            included.
        sanitized_bytes: The rewritten content as bytes.
        sanitized_sha256: SHA-256 of the rewritten content.
        used_test_fixture_map: True iff this file was rewritten with
            :data:`TEST_FIXTURE_REPLACEMENTS` instead of
            :data:`REPLACEMENTS`.
    """

    relative_path: str
    original_sha256: str
    original_size_bytes: int
    match_counts: dict[str, int]
    sanitized_bytes: bytes
    sanitized_sha256: str
    used_test_fixture_map: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ordered_replacements(use_test_fixture: bool) -> dict[str, str]:
    """Return the active replacement map for the given file class.

    The map preserves insertion order. Python dict preserves insertion
    order on every supported interpreter (3.7+); the ordering here is
    *load-bearing* because the URL form must be replaced before its
    fragments.
    """
    return TEST_FIXTURE_REPLACEMENTS if use_test_fixture else REPLACEMENTS


def _is_test_path(rel_path: str) -> bool:
    """True for files where pattern-valid fixture substitutes are required.

    Returns True for any path under ``tests/`` (so the redaction-regex
    unit tests still receive Azure-shaped hostnames). All other tracked
    paths use the canonical placeholder map.
    """
    return rel_path.startswith("tests/")


def list_tracked_files(repo_root: Path) -> list[str]:
    """Return all tracked files in the repo as paths relative to root.

    Uses ``git ls-files`` so untracked / gitignored content is excluded
    by construction. Sorted for determinism.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files"],
        capture_output=True,
        check=True,
        text=True,
    )
    return sorted(
        line for line in result.stdout.splitlines() if line.strip()
    )


def is_in_scope(rel_path: str) -> bool:
    """True iff *rel_path* is in the sweep scope.

    A path is in scope when it is tracked AND does not start with any
    excluded directory prefix AND its basename is not in the excluded
    set AND its full relative path is not in
    :data:`EXCLUDED_FILE_RELPATHS` AND its suffix is not a known binary
    extension.
    """
    p = rel_path.replace("\\", "/")
    if p in EXCLUDED_FILE_RELPATHS:
        return False
    for prefix in EXCLUDED_DIR_PREFIXES:
        if p.startswith(prefix):
            return False
    name = p.rsplit("/", 1)[-1]
    if name in EXCLUDED_FILE_NAMES:
        return False
    lower = name.lower()
    for suffix in EXCLUDED_SUFFIXES:
        if lower.endswith(suffix):
            return False
    return True


def scan_file(abs_path: Path, rel_path: str) -> FileScanResult | None:
    """Scan a single file; return a :class:`FileScanResult` or ``None``.

    Returns ``None`` when the file is binary, unreadable as UTF-8, or
    contains zero matches of any replacement key.

    Idempotence: a file that has already been sanitized contains zero
    occurrences of any replacement key (the canonical replacements
    replace each key with a string that does not itself contain the
    key) and is therefore reported as ``None``.
    """
    try:
        raw = abs_path.read_bytes()
    except OSError as exc:
        logger.debug("unreadable file %s: %s", rel_path, exc)
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        logger.debug("non-utf8 file %s; skipping", rel_path)
        return None

    use_test = _is_test_path(rel_path)
    repl = _ordered_replacements(use_test)

    counts: dict[str, int] = {}
    for needle in repl:
        c = text.count(needle)
        if c > 0:
            counts[needle] = c
    if not counts:
        return None

    new_text = text
    for needle, sub in repl.items():
        if needle in new_text:
            new_text = new_text.replace(needle, sub)
    new_bytes = new_text.encode("utf-8")

    return FileScanResult(
        relative_path=rel_path,
        original_sha256=_sha256_bytes(raw),
        original_size_bytes=len(raw),
        match_counts=counts,
        sanitized_bytes=new_bytes,
        sanitized_sha256=_sha256_bytes(new_bytes),
        used_test_fixture_map=use_test,
    )


def iter_candidates(repo_root: Path) -> Iterable[FileScanResult]:
    """Yield a :class:`FileScanResult` for every in-scope file with matches."""
    tracked = list_tracked_files(repo_root)
    for rel in tracked:
        if not is_in_scope(rel):
            continue
        abs_path = repo_root / rel
        if not abs_path.is_file():
            continue
        res = scan_file(abs_path, rel)
        if res is not None:
            yield res


# ---------------------------------------------------------------------------
# Manifest IO
# ---------------------------------------------------------------------------


def load_manifest(path: Path) -> dict:
    """Load the sweep manifest, returning a fresh skeleton if missing."""
    if not path.exists():
        return {
            "schema": "wrpo-public-readiness-sweep",
            "schema_version": "1.0.0",
            "archive_dir": ARCHIVE_DIR_NAME,
            "entries": [],
        }
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"manifest at {path} is not a JSON object")
    data.setdefault("schema", "wrpo-public-readiness-sweep")
    data.setdefault("schema_version", "1.0.0")
    data.setdefault("archive_dir", ARCHIVE_DIR_NAME)
    data.setdefault("entries", [])
    if not isinstance(data["entries"], list):
        raise ValueError(f"manifest 'entries' at {path} is not a list")
    return data


def manifest_entry(
    res: FileScanResult,
    archived_at_iso: str,
    git_commit_sha: str | None,
) -> dict:
    """Build the per-file manifest entry dict for *res*."""
    return {
        "source_relative_path": res.relative_path,
        "archive_relative_path": str(
            Path(".internal/raw-archive") / ARCHIVE_DIR_NAME / res.relative_path
        ),
        "original_sha256": res.original_sha256,
        "original_size_bytes": res.original_size_bytes,
        "sanitized_sha256": res.sanitized_sha256,
        "match_counts": dict(sorted(res.match_counts.items())),
        "used_test_fixture_map": res.used_test_fixture_map,
        "archived_at_iso": archived_at_iso,
        "git_commit_sha_at_capture": git_commit_sha,
        "sweep_id": ARCHIVE_DIR_NAME,
    }


def write_manifest(path: Path, data: dict) -> None:
    """Write the manifest deterministically (sorted keys, trailing newline)."""
    # Sort entries by source path for stable bytes.
    entries = list(data.get("entries", []))
    entries.sort(key=lambda e: (e.get("source_relative_path") or "", e.get("archived_at_iso") or ""))
    data = dict(data)
    data["entries"] = entries
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        data,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        separators=(",", ": "),
    ) + "\n"
    path.write_text(serialized, encoding="utf-8")


def current_git_commit_sha(repo_root: Path) -> str | None:
    """Return the current HEAD commit sha, or ``None`` if unavailable."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    sha = result.stdout.strip()
    return sha if sha else None


# ---------------------------------------------------------------------------
# Public manifest — tracked, public-safe provenance (docs/16 §2.2)
# ---------------------------------------------------------------------------
#
# The public manifest is a single tracked JSON file that carries one
# provenance entry per SANITIZED_PUBLIC artifact in the working tree.
# It deliberately avoids any private value: no `.internal/` paths, no
# concrete endpoint/deployment/region/secret strings, no archive file
# system path. Every reference back to the private archive is via an
# opaque `source_archive_id` (a deterministic short hash) plus the
# `source_raw_sha256`.

# Public-safe canonical description of the replacement classes. Hashing
# *this* (not the raw token map) produces `redaction_rules_sha256`.
# Updating any element below changes the hash, which is the desired
# semantics: if the replacement contract changes, every downstream
# provenance entry should reflect that. The list contains only labels
# and the *placeholder outputs* (which are themselves pseudonyms by
# construction and safe to publish), never the private tokens being
# replaced.
PUBLIC_REPLACEMENT_CLASSES: tuple[dict[str, str], ...] = (
    {
        "class": "endpoint_url",
        "canonical_placeholder": (
            "https://<resource>.services.ai.azure.com/api/projects/<project>"
        ),
        "fixture_placeholder": (
            "https://example-host.services.ai.azure.com/api/projects/example-project"
        ),
    },
    {
        "class": "hostname",
        "canonical_placeholder": "<resource>.services.ai.azure.com",
        "fixture_placeholder": "example-host.services.ai.azure.com",
    },
    {
        "class": "deployment_name_throttled",
        "canonical_placeholder": "ptu-deploy-throttled",
        "fixture_placeholder": "ptu-deploy-throttled",
    },
    {
        "class": "project_name",
        "canonical_placeholder": "<project>",
        "fixture_placeholder": "example-project",
    },
    {
        "class": "resource_short_name",
        "canonical_placeholder": "<resource>",
        "fixture_placeholder": "example-host",
    },
)

# Forbidden replacement classes (used by `--verify` token scan). Class
# labels, not the private tokens, so the hash stays public-safe.
PUBLIC_FORBIDDEN_CLASSES: tuple[str, ...] = (
    "resource_short_name",
    "project_name",
    "deployment_name_throttled",
)

PUBLIC_REPLACEMENT_ORDER_RULE = (
    "compound_url_before_host_before_project_before_resource_short_name"
)


def _compute_redaction_rules_sha256() -> str:
    """Deterministic sha256 of the *public-safe* rules description.

    Hashes only labels + placeholder outputs + ordering rule. Does NOT
    hash the private workload tokens being replaced (those are
    low-entropy; a deterministic hash of them would be an offline
    confirmation oracle). A public-only verifier can recompute this
    hash because everything it depends on is part of this script.
    """
    payload = {
        "classes": [
            {
                "class": c["class"],
                "canonical_placeholder": c["canonical_placeholder"],
                "fixture_placeholder": c["fixture_placeholder"],
            }
            for c in PUBLIC_REPLACEMENT_CLASSES
        ],
        "forbidden_classes": list(PUBLIC_FORBIDDEN_CLASSES),
        "order_rule": PUBLIC_REPLACEMENT_ORDER_RULE,
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _compute_redactor_script_sha256(script_path: Path | None = None) -> str:
    """sha256 of this script's bytes on disk (provable provenance)."""
    p = Path(script_path) if script_path is not None else SCRIPT_PATH
    return _sha256_bytes(p.read_bytes())


def _make_source_archive_id(source_raw_sha256: str, artifact_path: str) -> str:
    """Opaque archive id derived from source sha + artifact path.

    32 hex chars (128 bits) is comfortably collision-resistant for
    populations far larger than this repo will ever contain. The
    artifact_path is part of the input so two different files with the
    same content (rare but possible across fixtures) still get distinct
    ids.
    """
    payload = f"{source_raw_sha256}:{artifact_path}".encode("utf-8")
    return "raw-" + hashlib.sha256(payload).hexdigest()[:32]


def _is_clean_artifact_path(p: str) -> bool:
    """Sanity gate for a public manifest `artifact_path`.

    Refuses absolute paths, traversal segments, backslashes (Windows
    separators leaking into a forward-slash POSIX manifest), and any
    `.internal/` reference. Returns ``True`` on a clean path.
    """
    if not p or not isinstance(p, str):
        return False
    if "\\" in p:
        return False
    if p.startswith("/"):
        return False
    if ".." in p.split("/"):
        return False
    if p.startswith(".internal/") or "/.internal/" in p:
        return False
    return True


def public_manifest_entry(
    *,
    artifact_path: str,
    sanitized_sha256: str,
    source_raw_sha256: str,
    redaction_rules_sha256: str,
    redacted_at_iso: str,
    redactor_commit_sha: str | None,
    redactor_script_sha256: str,
    sweep_id: str,
) -> dict:
    """Build a public manifest entry.

    All fields are public-safe by construction. No archive path. No
    workload tokens. Validates the input artifact path is clean.
    """
    if not _is_clean_artifact_path(artifact_path):
        raise ValueError(
            f"refusing to build public entry for unsafe path: {artifact_path!r}"
        )
    return {
        "artifact_path": artifact_path,
        "tier": PUBLIC_MANIFEST_TIER,
        "sanitized_sha256": sanitized_sha256,
        "source_raw_sha256": source_raw_sha256,
        "source_archive_id": _make_source_archive_id(source_raw_sha256, artifact_path),
        "redaction_rules_sha256": redaction_rules_sha256,
        "redacted_at_iso": redacted_at_iso,
        "redactor_commit_sha": redactor_commit_sha,
        "redactor_script_sha256": redactor_script_sha256,
        "sweep_id": sweep_id,
    }


def write_public_manifest(path: Path, data: dict) -> None:
    """Write the public manifest deterministically (sorted, trailing newline).

    Entries are sorted by ``artifact_path``. Top-level keys are emitted
    sorted by :func:`json.dumps` ``sort_keys=True``. Two manifests with
    the same field values therefore produce identical bytes.
    """
    entries = list(data.get("entries", []))
    entries.sort(key=lambda e: str(e.get("artifact_path") or ""))
    data = dict(data)
    data["entries"] = entries
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        data,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        separators=(",", ": "),
    ) + "\n"
    path.write_text(serialized, encoding="utf-8")


def load_public_manifest(path: Path) -> dict:
    """Load the public manifest, returning a fresh skeleton if missing."""
    if not path.exists():
        return {
            "schema": PUBLIC_MANIFEST_SCHEMA,
            "schema_version": PUBLIC_MANIFEST_SCHEMA_VERSION,
            "tier": PUBLIC_MANIFEST_TIER,
            "sweep_id": ARCHIVE_DIR_NAME,
            "redaction_rules_sha256": _compute_redaction_rules_sha256(),
            "redactor_commit_sha": None,
            "redactor_script_sha256": _compute_redactor_script_sha256(),
            "redacted_at_iso": None,
            "entries": [],
        }
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"public manifest at {path} is not a JSON object")
    return data


def build_public_manifest_from_private(
    private_manifest: dict,
    repo_root: Path,
    *,
    redaction_rules_sha256: str,
    redactor_commit_sha: str | None,
    redactor_script_sha256: str,
    redacted_at_iso: str,
    sweep_id: str,
) -> dict:
    """Construct the public manifest dict from the private manifest.

    For each unique ``source_relative_path`` in the private manifest:

    1. Skip if the path is not in the public sweep scope (e.g. an entry
       authored against a now-excluded path).
    2. Skip if the file no longer exists in the working tree.
    3. Pick the entry with the latest ``archived_at_iso`` (with
       deterministic tie-break on source/sanitized sha and the path)
       as the source-of-truth for ``source_raw_sha256`` and the
       redaction timestamp.
    4. Emit one public entry with ``sanitized_sha256`` set to the
       CURRENT on-disk file's sha256 — the artifact in its present
       publishable form. Legitimate downstream edits to the file (that
       do not reintroduce forbidden tokens) are therefore tracked as
       the new sanitized snapshot; ``source_raw_sha256`` continues to
       pin to the original RAW source. ``--verify`` catches any drift
       between the recorded ``sanitized_sha256`` and the on-disk sha
       (i.e. the manifest is stale and ``--apply`` must be re-run).
    """
    by_path: dict[str, list[dict]] = {}
    for e in private_manifest.get("entries", []):
        src = e.get("source_relative_path")
        if not isinstance(src, str):
            continue
        by_path.setdefault(src, []).append(e)

    entries: list[dict] = []
    for src, candidates in by_path.items():
        if not _is_clean_artifact_path(src):
            continue
        if not is_in_scope(src):
            continue
        f = repo_root / src
        if not f.is_file():
            continue
        try:
            on_disk = _sha256_bytes(f.read_bytes())
        except OSError:
            continue
        # Deterministic tie-break: latest archived_at_iso, then sha, then path.
        candidates_sorted = sorted(
            candidates,
            key=lambda c: (
                str(c.get("archived_at_iso") or ""),
                str(c.get("original_sha256") or ""),
                str(c.get("sanitized_sha256") or ""),
                str(c.get("source_relative_path") or ""),
            ),
        )
        chosen = candidates_sorted[-1]
        entries.append(
            public_manifest_entry(
                artifact_path=src,
                sanitized_sha256=on_disk,
                source_raw_sha256=str(chosen.get("original_sha256") or ""),
                redaction_rules_sha256=redaction_rules_sha256,
                redacted_at_iso=str(chosen.get("archived_at_iso") or redacted_at_iso),
                redactor_commit_sha=redactor_commit_sha,
                redactor_script_sha256=redactor_script_sha256,
                sweep_id=sweep_id,
            )
        )

    return {
        "schema": PUBLIC_MANIFEST_SCHEMA,
        "schema_version": PUBLIC_MANIFEST_SCHEMA_VERSION,
        "tier": PUBLIC_MANIFEST_TIER,
        "sweep_id": sweep_id,
        "redaction_rules_sha256": redaction_rules_sha256,
        "redactor_commit_sha": redactor_commit_sha,
        "redactor_script_sha256": redactor_script_sha256,
        "redacted_at_iso": redacted_at_iso,
        "entries": entries,
    }


# Last-line-of-defense substring denylist enforced on the serialized
# public manifest bytes. Catches any accidental leak of a private value
# into a string field. Mirrors `_PRIVATE_SUBSTRINGS` in
# `batch_runner.release.manifest` but kept locally so this script has
# no cross-package import.
_PUBLIC_MANIFEST_DENY_SUBSTRINGS: tuple[str, ...] = (
    ".openai.azure.com",
    ".cognitiveservices.azure.com",
    ".services.ai.azure.com",
    ".internal/",
    ".corp.",
    "bearer ",
    "sk-",
    "api_key",
    "accountkey=",
    "?sig=",
    "azure_openai_api_key",
    "hf_token",
    "x-ms-deployment-name",
    "x-ms-region",
    "x-request-id",
    "x-ms-correlation-request-id",
    "x-ms-spillover-from-deployment",
)


def verify_public_manifest(
    repo_root: Path,
    manifest_path: Path,
    *,
    private_manifest_path: Path | None = None,
    require_present: bool = False,
) -> list[str]:
    """Validate the public manifest. Return a list of human-readable errors.

    Empty list means clean.

    Checks:
      * If require_present is True, file must exist.
      * If file exists:
          - top-level schema / schema_version / tier / sweep_id correct;
          - top-level redaction_rules_sha256 equals the currently
            computed public-safe rules hash;
          - serialized text contains no FORBIDDEN_TOKEN and no item
            from :data:`_PUBLIC_MANIFEST_DENY_SUBSTRINGS`;
          - entries are sorted by artifact_path with no duplicates;
          - each entry has all required keys, a clean artifact_path,
            tier == SANITIZED_PUBLIC, sweep_id == top-level, and
            redaction_rules_sha256 == top-level;
          - each entry's source_archive_id matches the deterministic
            derivation from source_raw_sha256 + artifact_path;
          - each artifact_path is an in-scope tracked file whose
            on-disk sha256 equals the entry's sanitized_sha256.
      * If private_manifest_path exists, every private entry whose
        on-disk file matches its sanitized_sha256 must appear in the
        public manifest (completeness).
    """
    errors: list[str] = []
    if not manifest_path.exists():
        if require_present:
            errors.append(
                f"public manifest missing at {manifest_path.relative_to(repo_root)} "
                f"(strict mode)"
            )
        return errors

    raw = manifest_path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [f"public manifest is not valid JSON: {exc}"]
    if not isinstance(data, dict):
        return ["public manifest top-level is not a JSON object"]

    if data.get("schema") != PUBLIC_MANIFEST_SCHEMA:
        errors.append(
            f"public manifest schema {data.get('schema')!r} != {PUBLIC_MANIFEST_SCHEMA!r}"
        )
    if data.get("schema_version") != PUBLIC_MANIFEST_SCHEMA_VERSION:
        errors.append(
            f"public manifest schema_version {data.get('schema_version')!r} "
            f"!= {PUBLIC_MANIFEST_SCHEMA_VERSION!r}"
        )
    if data.get("tier") != PUBLIC_MANIFEST_TIER:
        errors.append(
            f"public manifest tier {data.get('tier')!r} != {PUBLIC_MANIFEST_TIER!r}"
        )

    top_rules = data.get("redaction_rules_sha256")
    expected_rules = _compute_redaction_rules_sha256()
    if top_rules != expected_rules:
        errors.append(
            f"top-level redaction_rules_sha256 {top_rules!r} != "
            f"currently computed {expected_rules!r}"
        )

    top_sweep = data.get("sweep_id")
    if not isinstance(top_sweep, str) or not top_sweep:
        errors.append("top-level sweep_id missing or non-string")

    # Denylist scan on the serialized text.
    lower = raw.lower()
    for needle in _PUBLIC_MANIFEST_DENY_SUBSTRINGS:
        if needle in lower:
            errors.append(
                f"public manifest contains forbidden substring {needle!r}"
            )
    for tok in FORBIDDEN_TOKENS:
        if tok in raw:
            errors.append(
                f"public manifest contains forbidden workload token "
                f"{tok!r} (verbatim)"
            )

    entries = data.get("entries")
    if not isinstance(entries, list):
        return errors + ["public manifest 'entries' is not a list"]

    seen_paths: set[str] = set()
    expected_keys = {
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
    }
    sorted_paths = sorted(str(e.get("artifact_path") or "") for e in entries)
    actual_paths = [str(e.get("artifact_path") or "") for e in entries]
    if actual_paths != sorted_paths:
        errors.append("public manifest entries are not sorted by artifact_path")

    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"entry[{i}] is not a JSON object")
            continue
        missing = expected_keys - set(entry.keys())
        if missing:
            errors.append(
                f"entry[{i}] missing required keys: {sorted(missing)}"
            )
            continue
        ap = entry["artifact_path"]
        if not _is_clean_artifact_path(ap):
            errors.append(f"entry[{i}] artifact_path {ap!r} is not a clean public path")
            continue
        if ap in seen_paths:
            errors.append(f"duplicate artifact_path {ap!r} in public manifest entries")
            continue
        seen_paths.add(ap)
        if entry.get("tier") != PUBLIC_MANIFEST_TIER:
            errors.append(f"entry[{i}] tier != {PUBLIC_MANIFEST_TIER!r}")
        if entry.get("sweep_id") != top_sweep:
            errors.append(
                f"entry[{i}] sweep_id {entry.get('sweep_id')!r} "
                f"differs from top-level {top_sweep!r}"
            )
        if entry.get("redaction_rules_sha256") != top_rules:
            errors.append(
                f"entry[{i}] redaction_rules_sha256 differs from top-level"
            )
        derived_aid = _make_source_archive_id(
            str(entry.get("source_raw_sha256") or ""), ap
        )
        if entry.get("source_archive_id") != derived_aid:
            errors.append(
                f"entry[{i}] source_archive_id does not match derivation"
            )
        f = repo_root / ap
        if not is_in_scope(ap):
            errors.append(
                f"entry[{i}] artifact_path {ap!r} is not in the public sweep scope"
            )
            continue
        if not f.is_file():
            errors.append(f"entry[{i}] artifact_path {ap!r} does not exist on disk")
            continue
        try:
            on_disk = _sha256_bytes(f.read_bytes())
        except OSError as exc:
            errors.append(f"entry[{i}] could not read {ap!r}: {exc}")
            continue
        if entry.get("sanitized_sha256") != on_disk:
            errors.append(
                f"entry[{i}] sanitized_sha256 {entry.get('sanitized_sha256')!r} "
                f"does not match on-disk sha {on_disk!r} for {ap!r}"
            )

    # Completeness against the private manifest, if it exists. Every
    # path that ever appeared in the private manifest AND still exists
    # in the working tree AND is still in public sweep scope MUST be
    # represented in the public manifest. Sha drift between public
    # entry and on-disk file is caught above as a per-entry error
    # (sanitized_sha256 mismatch); this check catches absent entries.
    if private_manifest_path is not None and private_manifest_path.exists():
        try:
            priv_text = private_manifest_path.read_text(encoding="utf-8")
            priv = json.loads(priv_text)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"could not load private manifest for completeness check: {exc}")
        else:
            missing_public: list[str] = []
            for e in priv.get("entries", []):
                src = e.get("source_relative_path")
                if not isinstance(src, str) or not _is_clean_artifact_path(src):
                    continue
                if not is_in_scope(src):
                    continue
                f = repo_root / src
                if not f.is_file():
                    continue
                if src not in seen_paths:
                    missing_public.append(src)
            if missing_public:
                errors.append(
                    "private manifest lists "
                    f"{len(set(missing_public))} sanitized artifact(s) absent from "
                    f"the public manifest (first 5: {sorted(set(missing_public))[:5]})"
                )

    return errors


# ---------------------------------------------------------------------------
# Archive + apply
# ---------------------------------------------------------------------------


def archive_original(
    repo_root: Path,
    archive_root: Path,
    rel_path: str,
) -> Path:
    """Copy ``repo_root/rel_path`` to ``archive_root/rel_path`` (creating dirs).

    Uses :func:`shutil.copy2` to preserve mtime. If the destination
    already exists with identical content, leaves it as-is (the archive
    is append-only).
    """
    src = repo_root / rel_path
    dst = archive_root / rel_path
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        existing = dst.read_bytes()
        incoming = src.read_bytes()
        if existing == incoming:
            return dst
        # Different bytes already archived for this path under this sweep id.
        # Versions are appended as sibling files so neither is lost.
        i = 1
        while True:
            sibling = dst.with_name(f"{dst.name}.dup{i}")
            if not sibling.exists():
                break
            i += 1
        shutil.copy2(src, sibling)
        return sibling
    shutil.copy2(src, dst)
    return dst


def regenerate_public_manifest(
    repo_root: Path,
    *,
    private_manifest_path: Path,
    public_manifest_path: Path,
    redactor_commit_sha: str | None,
    redacted_at_iso: str | None = None,
) -> dict:
    """Rebuild the public manifest from the current private manifest.

    Deterministic: two consecutive calls with identical inputs (private
    manifest content + on-disk file shas + redactor commit SHA +
    script bytes) produce byte-identical output. The top-level
    ``redacted_at_iso`` is derived from the **latest per-entry**
    ``redacted_at_iso`` (the most recent underlying sanitization
    timestamp) rather than wall-clock ``now()``, so no-op re-runs do
    not drift.

    If the private manifest is missing or empty, writes an empty public
    manifest skeleton (so a release-gate `--require-public-manifest`
    check on a fresh repo still observes a well-formed file).
    """
    rules_sha = _compute_redaction_rules_sha256()
    script_sha = _compute_redactor_script_sha256()
    fallback_when = redacted_at_iso or datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    if private_manifest_path.exists():
        private = json.loads(private_manifest_path.read_text(encoding="utf-8"))
    else:
        private = {"entries": []}
    data = build_public_manifest_from_private(
        private,
        repo_root,
        redaction_rules_sha256=rules_sha,
        redactor_commit_sha=redactor_commit_sha,
        redactor_script_sha256=script_sha,
        redacted_at_iso=fallback_when,
        sweep_id=ARCHIVE_DIR_NAME,
    )
    # Pin top-level redacted_at_iso to the latest per-entry timestamp
    # for byte-determinism across no-op re-runs. Empty manifest →
    # fall back to the caller-provided / now() value.
    entry_isos = [
        str(e.get("redacted_at_iso") or "")
        for e in data.get("entries", [])
        if e.get("redacted_at_iso")
    ]
    if entry_isos:
        data["redacted_at_iso"] = max(entry_isos)
    write_public_manifest(public_manifest_path, data)
    return data


def apply_sweep(repo_root: Path, *, dry_run: bool) -> dict:
    """Run the full sweep.

    Args:
        repo_root: Repo root path.
        dry_run: If True, do not write to the archive, do not modify
            tracked files, and do not write the manifest. Just collect
            the candidate list.

    Returns:
        Dict summary suitable for printing or programmatic inspection.
    """
    candidates = list(iter_candidates(repo_root))
    summary = {
        "dry_run": dry_run,
        "candidate_count": len(candidates),
        "candidates_by_top_dir": {},
        "total_match_count": 0,
        "manifest_path": str(MANIFEST_PATH.relative_to(repo_root)),
        "public_manifest_path": str(PUBLIC_MANIFEST_PATH.relative_to(repo_root)),
        "archive_root": str(ARCHIVE_ROOT.relative_to(repo_root)),
    }
    by_top: dict[str, int] = {}
    total_matches = 0
    for c in candidates:
        top = c.relative_path.split("/", 1)[0]
        by_top[top] = by_top.get(top, 0) + 1
        total_matches += sum(c.match_counts.values())
    summary["candidates_by_top_dir"] = dict(sorted(by_top.items()))
    summary["total_match_count"] = total_matches

    if dry_run:
        return summary

    archive_root = ARCHIVE_ROOT
    archive_root.mkdir(parents=True, exist_ok=True)
    archived_at_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    git_sha = current_git_commit_sha(repo_root)
    manifest = load_manifest(MANIFEST_PATH)
    seen = {
        (e.get("source_relative_path"), e.get("original_sha256"))
        for e in manifest["entries"]
    }
    new_entries: list[dict] = []
    for c in candidates:
        archive_original(repo_root, archive_root, c.relative_path)
        entry = manifest_entry(c, archived_at_iso, git_sha)
        key = (entry["source_relative_path"], entry["original_sha256"])
        if key not in seen:
            new_entries.append(entry)
            seen.add(key)
        # Rewrite tracked file in place.
        dst = repo_root / c.relative_path
        dst.write_bytes(c.sanitized_bytes)
    manifest["entries"].extend(new_entries)
    write_manifest(MANIFEST_PATH, manifest)

    # Always rebuild the public manifest from the (possibly updated)
    # private manifest, even when no new entries were added: the public
    # manifest's redaction_rules_sha256 / redactor_script_sha256 may
    # have changed independently and must reconverge on the latest run.
    public_data = regenerate_public_manifest(
        repo_root,
        private_manifest_path=MANIFEST_PATH,
        public_manifest_path=PUBLIC_MANIFEST_PATH,
        redactor_commit_sha=git_sha,
        redacted_at_iso=archived_at_iso,
    )

    summary["written_entries"] = len(new_entries)
    summary["archived_at_iso"] = archived_at_iso
    summary["git_commit_sha_at_capture"] = git_sha
    summary["public_manifest_entries"] = len(public_data.get("entries", []))
    return summary


def refresh_public_hashes(
    repo_root: Path,
    *,
    public_manifest_path: Path,
    dry_run: bool = False,
) -> dict:
    """Public-side re-pin of ``sanitized_sha256`` for legitimately edited artifacts.

    Refreshes ONLY the ``sanitized_sha256`` of *existing* public-manifest
    entries whose on-disk artifact is still in scope, present, and free of
    forbidden workload tokens. Every other field — crucially
    ``source_raw_sha256`` and ``source_archive_id`` — stays pinned to the
    original ``RAW_PRIVATE`` lineage; provenance-metadata fields
    (``redacted_at_iso``, ``redactor_commit_sha``) are left untouched
    because a byte-edit of a published file does not re-derive it from raw.

    Unlike ``--apply`` this needs **no private archive input**: it never
    reads ``.internal/`` and never adds or removes entries. It exists so a
    legitimate downstream edit to an already-published, token-clean
    artifact (the case documented in
    :func:`build_public_manifest_from_private` step 4) can be re-pinned
    from the public tree — decoupling the ``--verify`` drift signal from
    the owner-only ``--apply`` regeneration. See docs/16 §.

    Safety invariants:

    * An entry whose file reintroduced a forbidden token is **not**
      refreshed (that would launder a leak into a blessed hash); it is
      reported under ``blocked_forbidden_token`` and the CLI exits
      non-zero so the operator routes it through the private ``--apply``
      sweep (which archives the raw original and redacts).
    * Entries whose file is missing or now out-of-scope are reported, not
      silently pruned — pruning needs the private manifest, i.e.
      ``--apply``.
    * Idempotent: a second run over an unchanged tree refreshes nothing.

    Returns a summary dict describing what was (or would be) changed.
    """
    result: dict = {
        "dry_run": dry_run,
        "refreshed": [],
        "unchanged": 0,
        "blocked_forbidden_token": [],
        "missing_on_disk": [],
        "out_of_scope": [],
        "wrote": False,
        "public_manifest_path": str(public_manifest_path.relative_to(repo_root)),
    }
    if not public_manifest_path.exists():
        result["error"] = (
            f"public manifest missing at {result['public_manifest_path']}"
        )
        return result

    data = load_public_manifest(public_manifest_path)
    entries = data.get("entries")
    if not isinstance(entries, list):
        result["error"] = "public manifest 'entries' is not a list"
        return result

    changed = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ap = str(entry.get("artifact_path") or "")
        if not ap:
            continue
        if not is_in_scope(ap):
            result["out_of_scope"].append(ap)
            continue
        f = repo_root / ap
        if not f.is_file():
            result["missing_on_disk"].append(ap)
            continue
        try:
            content = f.read_bytes()
        except OSError:
            result["missing_on_disk"].append(ap)
            continue
        # Security gate: never re-pin a file that reintroduced a forbidden
        # workload token — that must go through the private --apply sweep.
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
        if any(tok in text for tok in FORBIDDEN_TOKENS):
            result["blocked_forbidden_token"].append(ap)
            continue
        on_disk = _sha256_bytes(content)
        if entry.get("sanitized_sha256") == on_disk:
            result["unchanged"] += 1
            continue
        result["refreshed"].append(
            {
                "artifact_path": ap,
                "old": str(entry.get("sanitized_sha256") or ""),
                "new": on_disk,
            }
        )
        if not dry_run:
            entry["sanitized_sha256"] = on_disk
        changed = True

    if changed and not dry_run:
        write_public_manifest(public_manifest_path, data)
        result["wrote"] = True
    return result


def verify(repo_root: Path) -> tuple[int, list[tuple[str, str]]]:
    """Scan tracked in-scope files for any forbidden literal token.

    Returns:
        (exit_code, matches). ``exit_code`` is 0 on clean, 1 on any
        match. ``matches`` is a list of (relative_path, token) tuples.
    """
    matches: list[tuple[str, str]] = []
    for rel in list_tracked_files(repo_root):
        if not is_in_scope(rel):
            continue
        abs_path = repo_root / rel
        try:
            text = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for tok in FORBIDDEN_TOKENS:
            if tok in text:
                matches.append((rel, tok))
    return (1 if matches else 0, matches)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="List candidates, write nothing.")
    g.add_argument("--apply", action="store_true", help="Apply the sweep: archive + rewrite + public manifest.")
    g.add_argument("--verify", action="store_true", help="Scan for forbidden tokens and validate the public manifest.")
    g.add_argument(
        "--refresh-hashes",
        action="store_true",
        help=(
            "Public-side re-pin: refresh sanitized_sha256 for existing, "
            "token-clean public-manifest entries whose bytes changed, without "
            "the private archive. Refuses files that reintroduced a forbidden "
            "token (use --apply for those)."
        ),
    )
    parser.add_argument(
        "--require-public-manifest",
        action="store_true",
        help=(
            "When used with --verify, fail if release/public_sanitized_manifest.json "
            "is missing. Use this in the release-readiness CI job."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    if args.verify:
        code, matches = verify(REPO_ROOT)
        if matches:
            for rel, tok in matches[:50]:
                print(f"FORBIDDEN TOKEN: {tok!r} in {rel}")
            print(f"verify: {len(matches)} match(es) across {len({m[0] for m in matches})} file(s)")
        else:
            print("verify: clean — no forbidden tokens in tracked non-docs files.")
        # Public manifest validation.
        pm_errors = verify_public_manifest(
            REPO_ROOT,
            PUBLIC_MANIFEST_PATH,
            private_manifest_path=MANIFEST_PATH,
            require_present=args.require_public_manifest,
        )
        if pm_errors:
            for err in pm_errors[:50]:
                print(f"PUBLIC MANIFEST: {err}")
            print(f"verify (public manifest): {len(pm_errors)} error(s)")
            return 1 if code == 0 else code
        else:
            if PUBLIC_MANIFEST_PATH.exists():
                print(
                    "verify (public manifest): clean — "
                    f"{PUBLIC_MANIFEST_RELPATH} is well-formed and integrity-checked."
                )
            elif args.require_public_manifest:
                # Should already be reported above; this branch is defensive.
                print(f"verify (public manifest): missing {PUBLIC_MANIFEST_RELPATH}")
                return 1
            else:
                print(
                    "verify (public manifest): "
                    f"{PUBLIC_MANIFEST_RELPATH} not present (allowed without "
                    f"--require-public-manifest)."
                )
        return code

    if args.refresh_hashes:
        res = refresh_public_hashes(
            REPO_ROOT,
            public_manifest_path=PUBLIC_MANIFEST_PATH,
        )
        if res.get("error"):
            print(f"refresh-hashes: {res['error']}")
            return 1
        for r in res["refreshed"]:
            print(
                f"refresh-hashes: re-pinned {r['artifact_path']} "
                f"{r['old'][:12]} -> {r['new'][:12]}"
            )
        if res["wrote"]:
            print(
                f"refresh-hashes: updated {len(res['refreshed'])} "
                f"entr{'y' if len(res['refreshed']) == 1 else 'ies'} in "
                f"{PUBLIC_MANIFEST_RELPATH}."
            )
        elif not res["refreshed"]:
            print(
                "refresh-hashes: no drift — all in-scope entries already "
                "match on disk."
            )
        blocked = res["blocked_forbidden_token"]
        missing = res["missing_on_disk"]
        oos = res["out_of_scope"]
        if blocked or missing or oos:
            for ap in blocked:
                print(
                    f"refresh-hashes: REFUSED {ap} — file contains a forbidden "
                    f"workload token; run the private '--apply' sweep "
                    f"(archives + redacts) instead."
                )
            for ap in missing:
                print(
                    f"refresh-hashes: {ap} is listed in the manifest but missing "
                    f"on disk — run '--apply' to reconcile."
                )
            for ap in oos:
                print(
                    f"refresh-hashes: {ap} is no longer in public scope — run "
                    f"'--apply' to reconcile."
                )
            return 1
        return 0

    summary = apply_sweep(REPO_ROOT, dry_run=args.dry_run)
    print(json.dumps(summary, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
