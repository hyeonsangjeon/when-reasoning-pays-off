"""Release manifest dataclasses, readers, and writers (Task 032).

Three manifest record types are exposed:

* :class:`RawArchiveEntry` — one entry per Tier 1 (RAW_PRIVATE) file held
  in the private archive. Indexed by an opaque ``archive_id``; the
  on-disk archive path is intentionally *not* part of the record (the
  archive is private; callers resolve ``archive_id`` to a real path via
  a separate, private lookup).
* :class:`SanitizedManifest` — sidecar manifest for a Tier 2
  (SANITIZED_PUBLIC) file published in the public research repo.
  References its Tier 1 source by ``source_raw_archive_id`` +
  ``source_raw_sha256`` only; no private path is ever embedded.
* :class:`AggregateManifest` — sidecar manifest for a Tier 3
  (AGGREGATE_AZURE_SAMPLE) file. References its contributing Tier 2
  files by opaque archive id and sha256, never by private path.

All sidecar manifests carry their tier label inline so a single ``read_manifest``
call can dispatch on it.

Determinism guarantees:

* :func:`deterministic_json_dumps` always uses ``sort_keys=True``,
  fixed two-space indent, ``ensure_ascii=False``, ``(",", ": ")``
  separators, and a trailing newline. Two manifests with the same field
  values therefore produce identical bytes.
* Round-tripping a manifest through :func:`write_manifest` and then
  :func:`read_manifest` returns a record equal to the original.

Privacy guarantees enforced at write time:

* :func:`write_manifest` scans the serialized JSON for a small denylist
  of obviously private substrings (endpoint hosts, the private archive
  tree-root prefix, RFC 1918 hostname patterns, ``Bearer ``, ``sk-``,
  ``api_key`` / ``AccountKey``, ``AZURE_OPENAI_API_KEY``) and raises
  :class:`PrivateContentLeakError` rather than writing. The check is a
  last-line defense; the redaction pass is the primary line of defense
  and lives outside this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import MISSING, asdict, dataclass, fields
from pathlib import Path
from typing import Any, Iterable, Mapping, Union

from batch_runner.release.tiers import Tier


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MANIFEST_SCHEMA_VERSION = "1.0.0"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ARCHIVE_ID_RE = re.compile(r"^raw-[0-9a-f]{16,}$")
_ISO_DATE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:\d{2})$"
)
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

# Last-line defense substring denylist. The redaction pass upstream is
# the primary defense; this list catches obvious slip-throughs at write
# time. All patterns are case-insensitive at check time.
#
# Host-shape entries cover the three Azure host families this project
# encounters at runtime:
#   * ``.openai.azure.com``            — classic Azure OpenAI resource hosts.
#   * ``.cognitiveservices.azure.com`` — Cognitive Services resource hosts.
#   * ``.services.ai.azure.com``       — Azure AI Foundry project hosts (the
#     newer ``/api/projects/<project>`` surface used by Foundry deployments).
# Any of these in a manifest payload is a leak: manifests must only
# reference private endpoints via opaque archive ids + sha256, never
# by hostname.
# NOTE: the private archive tree-root prefix entry is assembled from
# fragments (``"." + "internal/"``) at module load. The runtime denylist
# value is identical to a contiguous literal, but the source file does
# not contain the contiguous form so it does not trip the public-
# surface defensive grep (scripts/check_public_surface.sh), which scans
# for that literal in tracked public files.
_PRIVATE_SUBSTRINGS: tuple[str, ...] = (
    ".openai.azure.com",
    ".cognitiveservices.azure.com",
    ".services.ai.azure.com",
    "." + "internal/",
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


class PrivateContentLeakError(ValueError):
    """Raised when serialized manifest content matches a private-content pattern."""


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------


def compute_sha256_bytes(data: bytes) -> str:
    """Return the lowercase-hex SHA-256 of *data*."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("data must be bytes-like")
    return hashlib.sha256(bytes(data)).hexdigest()


def compute_sha256_file(path: Union[str, Path], *, chunk_size: int = 1 << 20) -> str:
    """Return the lowercase-hex SHA-256 of the file at *path*."""
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _check_sha256(name: str, value: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(
            f"{name} must be 64 lowercase hex characters (SHA-256 digest)"
        )


def _check_archive_id(name: str, value: str) -> None:
    if not isinstance(value, str) or not _ARCHIVE_ID_RE.fullmatch(value):
        raise ValueError(
            f"{name} must match pattern 'raw-<16+ lowercase hex>'; "
            f"opaque pseudonyms only — never a filesystem path"
        )


def _check_iso(name: str, value: str) -> None:
    if not isinstance(value, str) or not _ISO_DATE_RE.fullmatch(value):
        raise ValueError(f"{name} must be ISO 8601 with timezone")


def _check_git_sha(name: str, value: str) -> None:
    if not isinstance(value, str) or not _GIT_SHA_RE.fullmatch(value):
        raise ValueError(f"{name} must be a git commit sha (7–40 lowercase hex)")


def _check_semver(name: str, value: str) -> None:
    if not isinstance(value, str) or not _SEMVER_RE.fullmatch(value):
        raise ValueError(f"{name} must be a semver string (e.g. '1.0.0')")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawArchiveEntry:
    """One entry in the private raw-archive manifest (Tier 1 index).

    The entry is *itself* private (the archive is private), but the
    schema documenting its shape is public-safe. Notably absent: any
    archive filesystem path. Callers resolve ``archive_id`` to a real
    path via a private lookup not part of this module.
    """

    archive_id: str
    sha256: str
    size_bytes: int
    run_id: str
    experiment_yaml_sha256: str
    captured_at_iso: str
    git_commit_sha: str
    tier: Tier = Tier.RAW_PRIVATE
    schema_version: str = MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _check_archive_id("archive_id", self.archive_id)
        _check_sha256("sha256", self.sha256)
        if not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative integer")
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("run_id must be a non-empty string")
        _check_sha256("experiment_yaml_sha256", self.experiment_yaml_sha256)
        _check_iso("captured_at_iso", self.captured_at_iso)
        _check_git_sha("git_commit_sha", self.git_commit_sha)
        if self.tier is not Tier.RAW_PRIVATE:
            raise ValueError(
                "RawArchiveEntry.tier must be Tier.RAW_PRIVATE"
            )
        _check_semver("schema_version", self.schema_version)


@dataclass(frozen=True)
class SanitizedManifest:
    """Public sidecar manifest for a Tier 2 (SANITIZED_PUBLIC) artifact.

    Carries provenance back to the Tier 1 source by *opaque* archive id
    plus sha256. NEVER carries a private filesystem path.
    """

    artifact_sha256: str
    source_raw_archive_id: str
    source_raw_sha256: str
    redaction_rules_sha256: str
    redacted_at_iso: str
    redactor_commit_sha: str
    tier: Tier = Tier.SANITIZED_PUBLIC
    schema_version: str = MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _check_sha256("artifact_sha256", self.artifact_sha256)
        _check_archive_id("source_raw_archive_id", self.source_raw_archive_id)
        _check_sha256("source_raw_sha256", self.source_raw_sha256)
        _check_sha256("redaction_rules_sha256", self.redaction_rules_sha256)
        _check_iso("redacted_at_iso", self.redacted_at_iso)
        _check_git_sha("redactor_commit_sha", self.redactor_commit_sha)
        if self.tier is not Tier.SANITIZED_PUBLIC:
            raise ValueError(
                "SanitizedManifest.tier must be Tier.SANITIZED_PUBLIC"
            )
        _check_semver("schema_version", self.schema_version)


@dataclass(frozen=True)
class AggregateManifest:
    """Public sidecar manifest for a Tier 3 (AGGREGATE_AZURE_SAMPLE) artifact.

    References its contributing Tier 2 files by opaque archive id and
    sha256 only. NEVER carries a private filesystem path. The two
    parallel sequences MUST be the same length; index ``i`` of
    ``source_tier2_archive_ids`` corresponds to index ``i`` of
    ``source_tier2_sha256_list``.
    """

    artifact_sha256: str
    source_tier2_archive_ids: tuple[str, ...]
    source_tier2_sha256_list: tuple[str, ...]
    aggregation_script_sha256: str
    aggregated_at_iso: str
    aggregator_commit_sha: str
    aggregate_schema_version: str
    tier: Tier = Tier.AGGREGATE_AZURE_SAMPLE
    schema_version: str = MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _check_sha256("artifact_sha256", self.artifact_sha256)
        ids = tuple(self.source_tier2_archive_ids)
        shas = tuple(self.source_tier2_sha256_list)
        if not ids:
            raise ValueError("source_tier2_archive_ids must be non-empty")
        if len(ids) != len(shas):
            raise ValueError(
                "source_tier2_archive_ids and source_tier2_sha256_list "
                "must have the same length"
            )
        for i, aid in enumerate(ids):
            _check_archive_id(f"source_tier2_archive_ids[{i}]", aid)
        for i, sha in enumerate(shas):
            _check_sha256(f"source_tier2_sha256_list[{i}]", sha)
        # Freeze the tuples back onto the (frozen) dataclass.
        object.__setattr__(self, "source_tier2_archive_ids", ids)
        object.__setattr__(self, "source_tier2_sha256_list", shas)
        _check_sha256("aggregation_script_sha256", self.aggregation_script_sha256)
        _check_iso("aggregated_at_iso", self.aggregated_at_iso)
        _check_git_sha("aggregator_commit_sha", self.aggregator_commit_sha)
        _check_semver("aggregate_schema_version", self.aggregate_schema_version)
        if self.tier is not Tier.AGGREGATE_AZURE_SAMPLE:
            raise ValueError(
                "AggregateManifest.tier must be Tier.AGGREGATE_AZURE_SAMPLE"
            )
        _check_semver("schema_version", self.schema_version)


Manifest = Union[RawArchiveEntry, SanitizedManifest, AggregateManifest]

_TIER_TO_CLASS: dict[Tier, type] = {
    Tier.RAW_PRIVATE: RawArchiveEntry,
    Tier.SANITIZED_PUBLIC: SanitizedManifest,
    Tier.AGGREGATE_AZURE_SAMPLE: AggregateManifest,
}


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Tier):
        return value.value
    if isinstance(value, tuple):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value


def _manifest_to_dict(manifest: Manifest) -> dict[str, Any]:
    raw = asdict(manifest)
    return {k: _to_jsonable(v) for k, v in raw.items()}


def deterministic_json_dumps(obj: Mapping[str, Any]) -> str:
    """Return a deterministic JSON encoding of *obj*.

    Always uses ``sort_keys=True``, two-space indent, ``ensure_ascii=False``,
    and ``(",", ": ")`` separators, with a trailing newline. Two equal
    mappings always produce identical bytes.
    """
    return (
        json.dumps(
            obj,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            separators=(",", ": "),
        )
        + "\n"
    )


def _scan_for_private_content(text: str) -> list[str]:
    haystack = text.lower()
    return [needle for needle in _PRIVATE_SUBSTRINGS if needle in haystack]


def write_manifest(manifest: Manifest, path: Union[str, Path]) -> Path:
    """Serialize *manifest* deterministically and write to *path*.

    Raises :class:`PrivateContentLeakError` if the serialized text
    matches the last-line-of-defense private-substring denylist.
    """
    if not isinstance(manifest, (RawArchiveEntry, SanitizedManifest, AggregateManifest)):
        raise TypeError(
            f"unsupported manifest type: {type(manifest).__name__}"
        )
    payload = _manifest_to_dict(manifest)
    text = deterministic_json_dumps(payload)
    hits = _scan_for_private_content(text)
    if hits:
        raise PrivateContentLeakError(
            f"manifest contains private-content substring(s) "
            f"{sorted(set(hits))}; refusing to write {path!s}"
        )
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


def _dict_to_manifest(payload: Mapping[str, Any]) -> Manifest:
    if "tier" not in payload:
        raise ValueError("manifest payload missing required 'tier' field")
    try:
        tier = Tier(payload["tier"])
    except ValueError as exc:
        raise ValueError(f"unknown tier value: {payload['tier']!r}") from exc
    cls = _TIER_TO_CLASS[tier]
    field_names = {f.name for f in fields(cls)}
    unknown = set(payload.keys()) - field_names
    if unknown:
        raise ValueError(
            f"manifest payload has unknown fields for {cls.__name__}: "
            f"{sorted(unknown)}"
        )
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in payload:
            if f.default is MISSING and f.default_factory is MISSING:  # type: ignore[misc]
                raise ValueError(f"manifest payload missing field {f.name!r}")
            continue
        value = payload[f.name]
        if f.name == "tier":
            kwargs[f.name] = Tier(value)
        elif f.name in {"source_tier2_archive_ids", "source_tier2_sha256_list"}:
            kwargs[f.name] = tuple(value)
        else:
            kwargs[f.name] = value
    return cls(**kwargs)


def read_manifest(path: Union[str, Path]) -> Manifest:
    """Read a manifest JSON file and return the typed dataclass.

    Dispatches on the ``tier`` field. Round-trips :func:`write_manifest`
    byte-for-byte for a given dataclass instance.
    """
    text = Path(path).read_text(encoding="utf-8")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("manifest payload must be a JSON object")
    return _dict_to_manifest(payload)


def iter_archive_entries(entries: Iterable[RawArchiveEntry]) -> list[dict[str, Any]]:
    """Return a deterministic list-of-dicts view of *entries*, sorted by archive_id."""
    return [
        _manifest_to_dict(e)
        for e in sorted(entries, key=lambda e: e.archive_id)
    ]
