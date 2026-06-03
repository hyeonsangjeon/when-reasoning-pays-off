"""Release-tier classification and manifest tooling (Task 032).

This subpackage implements the *minimum* mechanical surface that the
release policy authored in ``docs/16-release-tiers-and-redaction-policy.md``
relies on:

* :mod:`batch_runner.release.tiers` — the three-tier enum
  (``RAW_PRIVATE``, ``SANITIZED_PUBLIC``, ``AGGREGATE_AZURE_SAMPLE``) and
  the ``assert_publishable`` / ``is_publishable`` helpers that downstream
  scripts call before writing any file into a publication target.
* :mod:`batch_runner.release.manifest` — frozen dataclasses for the raw
  archive entry and the public Tier 2 / Tier 3 sidecar manifests, plus
  deterministic JSON readers / writers.

Hard constraints honoured by this subpackage:

* No private filesystem paths appear in public sidecar manifests. Every
  cross-tier reference is by ``source_raw_archive_id`` (opaque
  pseudonym) plus ``sha256``.
* No endpoint hostnames, deployment names, region identifiers, customer
  names, or secret patterns are written by these modules; their callers
  are responsible for redacting the input data before invoking these
  helpers, and a small denylist of substrings in :mod:`manifest` rejects
  obviously private content if it slips through.
* All JSON output is deterministic (``sort_keys=True``, fixed indent,
  ``ensure_ascii=False``, trailing newline) so manifest bytes round-trip
  byte-for-byte.

No network calls, no environment-variable reads, no Azure / OpenAI
client instantiation happen at import time or at any time in these
modules.
"""

from batch_runner.release.tiers import (
    PublicationNotAllowedError,
    PublicationTarget,
    Tier,
    allowed_targets,
    assert_publishable,
    is_publishable,
)
from batch_runner.release.manifest import (
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

__all__ = [
    "AggregateManifest",
    "PrivateContentLeakError",
    "PublicationNotAllowedError",
    "PublicationTarget",
    "RawArchiveEntry",
    "SanitizedManifest",
    "Tier",
    "allowed_targets",
    "assert_publishable",
    "compute_sha256_bytes",
    "compute_sha256_file",
    "deterministic_json_dumps",
    "is_publishable",
    "read_manifest",
    "write_manifest",
]
