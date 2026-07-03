#!/usr/bin/env python3
"""Promotion-set redaction detector (Task 032 Phase C, WP-C1).

This is the production redaction-detector / promotion-set scanner that the
data-promotion campaign requires as a HARD PRECONDITION before any public
chart-data or aggregate artifact is written. It is a complement to — not a
replacement for — ``scripts/sanitize_public_artifacts.py --verify`` (which only
checks hardcoded workload tokens + manifest integrity) and
``scripts/check_public_surface.sh`` (which deliberately excludes ``results/``,
``benchmarks/``, ``logs/``, etc. and therefore cannot scan the emitted
chart-data surface).

Unlike a prose grep, this scanner inspects **file contents and structured cell
values** (CSV cells, JSON leaf values, JSONL records), applying the redaction
abort/removal categories defined in ``docs/16`` §3:

  * Secrets (API keys, bearer tokens, signed-URL/SAS tokens, storage account
    keys, connection strings) — abort.
  * Azure OpenAI / Cognitive Services / AI-services endpoint hostnames.
  * Deployment names / regions exposed as values.
  * Request / correlation IDs.
  * Email addresses.
  * Internal hostnames (``*.internal``, ``*.corp``, ``*.local``, RFC 1918).
  * Private absolute paths and private-tree references (``/Users/``, ``/home/``,
    the private internal-tree prefix, raw-archive paths).
  * Free-text payload fields (prompt / response / message / content / tool-call
    arguments) and per-request row identifiers — forbidden on the chart-data
    surface entirely.

For files in the ``AGGREGATE_AZURE_SAMPLE`` tier it additionally enforces the
aggregate contract: count fields (``n`` / ``count`` / ``sample_count`` / …) must
be ``>= 5`` where present, all free-text is dropped, and capture/wallclock
timestamps must be rounded to the UTC hour.

Scope / paths
-------------
``--promotion-root ROOT`` scans the canonical public promotion surface under
``ROOT``::

    results/public/chart-data/**
    docs/blog/data/**
    release/public_chart_candidates.json
    CHANGELOG.md

Positional ``paths`` (files or directories) are scanned directly and may be
combined with ``--promotion-root``. Directories are walked recursively.

Tier detection
--------------
A file is treated as ``AGGREGATE_AZURE_SAMPLE`` when ``--aggregate`` is passed,
when its JSON declares ``"tier": "AGGREGATE_AZURE_SAMPLE"`` (top-level or in any
record), or when its path contains an ``aggregate`` segment. Otherwise it is
scanned under the ``SANITIZED_PUBLIC`` chart-data contract (structural
free-text/identity field rejection still applies; the aggregate-only count /
wallclock rules do not).

Exit codes
----------
``0`` — clean. ``1`` — one or more findings (printed to stderr). ``2`` — usage
error (e.g. a path that does not exist).

This module is importable: :func:`scan_paths` returns a list of
:class:`Finding` objects and is used by ``scripts/verify_aggregate_manifest.py``
to scan the artifacts a manifest claims.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

AGGREGATE_TIER = "AGGREGATE_AZURE_SAMPLE"

# Canonical public promotion-surface globs under a --promotion-root.
PROMOTION_GLOBS: tuple[str, ...] = (
    "results/public/chart-data/**/*",
    "docs/blog/data/**/*",
)
PROMOTION_FILES: tuple[str, ...] = (
    "release/public_chart_candidates.json",
    "CHANGELOG.md",
)

# File extensions whose contents we attempt to scan. Anything else under a
# promotion root is treated as opaque/binary and skipped (but reported via
# --list when requested).
TEXT_SUFFIXES: frozenset[str] = frozenset(
    {
        ".json",
        ".jsonl",
        ".ndjson",
        ".csv",
        ".tsv",
        ".md",
        ".markdown",
        ".txt",
        ".yaml",
        ".yml",
    }
)


# ---------------------------------------------------------------------------
# Sensitive value patterns (applied to raw text AND every structured cell value)
# ---------------------------------------------------------------------------
# These mirror docs/16 §3 detection classes. Each is (category, compiled regex).
SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("secret-openai-sk", re.compile(r"(?:^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}")),
    ("secret-azure-key", re.compile(r"AZURE_OPENAI_API_KEY\s*[:=]\s*[^\s\"'#]+")),
    ("secret-openai-key", re.compile(r"(?:^|[^A-Z_])OPENAI_API_KEY\s*[:=]\s*[^\s\"'#]+")),
    ("secret-hf-token", re.compile(r"HF_TOKEN\s*[:=]\s*[^\s\"'#]+")),
    ("bearer-token", re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}")),
    ("api-key-generic", re.compile(r"api[_-]?key\s*[:=]\s*[A-Za-z0-9._-]{16,}", re.I)),
    # Azure Storage / SAS / connection-string secrets.
    ("storage-account-key", re.compile(r"AccountKey\s*=\s*[A-Za-z0-9+/=]{16,}", re.I)),
    ("storage-conn-string", re.compile(r"DefaultEndpointsProtocol\s*=\s*https?", re.I)),
    ("sas-signature", re.compile(r"[?&]sig=[A-Za-z0-9%]{16,}", re.I)),
    ("sas-token", re.compile(r"SharedAccessSignature\s*=", re.I)),
    # Azure / Cognitive / AI-services endpoint hostnames.
    ("azure-openai-endpoint", re.compile(r"[A-Za-z0-9-]+\.openai\.azure\.com", re.I)),
    ("cognitive-endpoint", re.compile(r"[A-Za-z0-9-]+\.cognitiveservices\.azure\.com", re.I)),
    ("cognitive-api-endpoint", re.compile(r"[A-Za-z0-9-]+\.api\.cognitive\.microsoft\.com", re.I)),
    ("ai-services-endpoint", re.compile(r"[A-Za-z0-9-]+\.services\.ai\.azure\.com", re.I)),
    ("azure-api-endpoint", re.compile(r"[A-Za-z0-9-]+\.azure-api\.net", re.I)),
    # Request / correlation IDs (header-shaped).
    (
        "request-id",
        re.compile(
            r"(?:x-request-id|apim-request-id|x-ms-request-id|x-ms-client-request-id|"
            r"client-request-id|x-ms-correlation-request-id|correlation-request-id)"
            r"\s*[:=]\s*[A-Za-z0-9-]{8,}",
            re.I,
        ),
    ),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("internal-hostname", re.compile(r"\b[A-Za-z0-9-]+\.(?:internal|corp|local)\b", re.I)),
    (
        "rfc1918-ip",
        re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    ),
    # Private absolute paths and private-tree references.
    ("absolute-home-path", re.compile(r"/Users/[A-Za-z0-9._-]+|/home/[A-Za-z0-9._-]+|/root/")),
    ("windows-user-path", re.compile(r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+", re.I)),
    ("internal-tree-ref", re.compile(re.escape("." + "internal" + "/"))),
    ("raw-archive-ref", re.compile(r"raw-archive/")),
    # Region tokens and deployment-shaped labels exposed as values.
    (
        "region-token",
        re.compile(
            r"\b(?:eastus2?|westus[23]?|swedencentral|northcentralus|southcentralus|"
            r"westeurope|northeurope|eastus2euap|francecentral|uksouth|japaneast|"
            r"australiaeast|canadacentral|brazilsouth)\b",
            re.I,
        ),
    ),
    (
        "deployment-label",
        re.compile(r"\b[a-z0-9]+-(?:deploy(?:ment)?|prod|staging)\b", re.I),
    ),
)

# A public pricing reference host that legitimately appears in provenance prose;
# it must not be flagged as an internal hostname / endpoint.
_PRICING_HOST_OK = re.compile(r"azure\.microsoft\.com/[a-z-]+/pricing", re.I)


# ---------------------------------------------------------------------------
# Structural field-name rules (applied to normalized JSON/CSV leaf keys)
# ---------------------------------------------------------------------------
# Free-text payload fields and per-request row identifiers have no place on the
# chart-data / aggregate surface and are rejected in BOTH tiers.
FORBIDDEN_FIELD_EXACT: frozenset[str] = frozenset(
    {
        # Free-text payloads (docs/16 §3 free-text category).
        "prompt",
        "prompts",
        "response",
        "responses",
        "message",
        "messages",
        "content",
        "free_text",
        "freetext",
        "completion",
        "completions",
        "transcript",
        "system_prompt",
        "user_prompt",
        "answer_text",
        "raw_response",
        "tool_call",
        "tool_calls",
        "arguments",
        "stt",
        # Per-request row identifiers (no per-request rows on public surface).
        "request_index",
        "row_id",
        "run_id",
        "trace_id",
        "attempt",
        "attempt_index",
        "headers",
        "response_headers",
        # Identity / leak fields (docs/16 §3 drop/abort categories).
        "request_id",
        "x_request_id",
        "apim_request_id",
        "x_ms_request_id",
        "x_ms_client_request_id",
        "client_request_id",
        "correlation_id",
        "x_ms_correlation_request_id",
        "correlation_request_id",
        "endpoint",
        "api_base",
        "base_url",
        "resource_name",
        "project_name",
        "region",
        "deployment",
        "deployment_name",
        "account_key",
        "api_key",
        "secret",
        "password",
        "access_token",
        # Raw, un-remapped topology identifier (docs/16: remap to namespace_id).
        "namespace",
        "namespace_name",
    }
)

# Count fields whose value must be >= 5 in AGGREGATE_AZURE_SAMPLE files.
COUNT_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "n",
        "count",
        "sample_count",
        "n_used",
        "n_records",
        "n_steady_state_records",
        "request_count",
        "n_samples",
    }
)
MIN_AGGREGATE_N = 5

# Capture/wallclock timestamp fields that must be rounded to the UTC hour on
# the public surface. Provenance-generation timestamps are exempt (they record
# the generation moment, not a captured measurement, and carry no workload
# signal).
TIMESTAMP_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "timestamp",
        "timestamp_utc",
        "wallclock_timestamp_iso",
        "captured_at",
        "captured_at_iso",
        "capture_time",
        "started_at",
        "started_at_iso",
        "start_time",
        "ended_at",
        "probe_started_at_iso",
        "probe_window_end_iso",
        "end_time",
        "window_start_iso",
        "window_end_iso",
        "start_iso",
        "end_iso",
    }
)
TIMESTAMP_PROVENANCE_EXEMPT: frozenset[str] = frozenset(
    {"generated_at", "generated_at_iso", "redacted_at", "redacted_at_iso", "created_at_iso"}
)
_ISO_TS = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:(?P<min>\d{2}):(?P<sec>\d{2})"
)

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def normalize_field_name(name: str) -> str:
    """Normalize a JSON/CSV key for matching: camelCase + separators -> snake.

    ``deploymentName`` -> ``deployment_name``; ``x-ms-request-id`` ->
    ``x_ms_request_id``; ``Sample Count`` -> ``sample_count``.
    """
    s = _CAMEL.sub("_", str(name))
    s = re.sub(r"[-.\s/]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s.lower()


@dataclass(frozen=True)
class Finding:
    """A single redaction finding.

    Attributes:
        path: Repo-relative-or-given path of the offending file.
        locator: Human-readable position (e.g. ``line 12``, ``cell n``,
            ``$.entries[3].deployment_name``).
        category: The redaction category that matched.
        snippet: A short, already-truncated excerpt of the match.
        tier: Tier the file was scanned under.
    """

    path: str
    locator: str
    category: str
    snippet: str
    tier: str

    def format(self) -> str:
        return f"FAIL [{self.path} :: {self.locator}] {self.category}: {self.snippet!r}"


def _truncate(value: str, limit: int = 80) -> str:
    v = value.strip().replace("\n", " ")
    return v if len(v) <= limit else v[:limit] + "…"


def _scan_text_value(value: str) -> list[tuple[str, str]]:
    """Return (category, snippet) for every sensitive pattern in *value*."""
    hits: list[tuple[str, str]] = []
    for category, rx in SENSITIVE_PATTERNS:
        for m in rx.finditer(value):
            matched = m.group(0)
            if category in ("internal-hostname", "ai-services-endpoint") and _PRICING_HOST_OK.search(value):
                # Public pricing host is allow-listed (provenance prose only).
                if _PRICING_HOST_OK.search(matched) or matched in _PRICING_HOST_OK.pattern:
                    continue
            hits.append((category, _truncate(matched)))
    return hits


def _looks_like_aggregate(text: str, path: Path) -> bool:
    if any(part.lower() == "aggregate" for part in path.parts):
        return True
    return AGGREGATE_TIER in text


# ---------------------------------------------------------------------------
# Per-format scanners
# ---------------------------------------------------------------------------


def _scan_raw_text(path_label: str, text: str, tier: str) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for category, snippet in _scan_text_value(line):
            findings.append(Finding(path_label, f"line {lineno}", category, snippet, tier))
    return findings


def _check_field_name(path_label: str, json_path: str, key: str, tier: str) -> list[Finding]:
    norm = normalize_field_name(key)
    findings: list[Finding] = []
    if norm in FORBIDDEN_FIELD_EXACT:
        findings.append(
            Finding(path_label, json_path, "forbidden-field-name", _truncate(str(key)), tier)
        )
    return findings


def _check_count_value(path_label: str, json_path: str, norm_key: str, value: object, tier: str) -> list[Finding]:
    if tier != AGGREGATE_TIER or norm_key not in COUNT_FIELD_NAMES:
        return []
    try:
        n = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return []
    if n < MIN_AGGREGATE_N:
        return [
            Finding(
                path_label,
                json_path,
                "aggregate-n-below-min",
                f"{norm_key}={value} (< {MIN_AGGREGATE_N})",
                tier,
            )
        ]
    return []


def _check_timestamp_value(path_label: str, json_path: str, norm_key: str, value: object, tier: str) -> list[Finding]:
    if norm_key in TIMESTAMP_PROVENANCE_EXEMPT or norm_key not in TIMESTAMP_FIELD_NAMES:
        return []
    if not isinstance(value, str):
        return []
    m = _ISO_TS.match(value)
    if m and (m.group("min") != "00" or m.group("sec") != "00"):
        return [
            Finding(
                path_label,
                json_path,
                "unrounded-wallclock",
                f"{norm_key}={_truncate(value)}",
                tier,
            )
        ]
    return []


def _walk_json(path_label: str, node: object, json_path: str, tier: str) -> Iterator[Finding]:
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{json_path}.{key}"
            norm = normalize_field_name(key)
            yield from _check_field_name(path_label, child_path, key, tier)
            yield from _check_count_value(path_label, child_path, norm, value, tier)
            yield from _check_timestamp_value(path_label, child_path, norm, value, tier)
            yield from _walk_json(path_label, value, child_path, tier)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk_json(path_label, value, f"{json_path}[{i}]", tier)
    elif isinstance(node, str):
        for category, snippet in _scan_text_value(node):
            yield Finding(path_label, json_path, category, snippet, tier)


def _scan_json(path_label: str, text: str, tier: str) -> list[Finding]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Not valid JSON despite the extension — fall back to raw text scan so
        # nothing is silently skipped.
        return _scan_raw_text(path_label, text, tier)
    return list(_walk_json(path_label, data, "$", tier))


def _scan_jsonl(path_label: str, text: str, tier: str) -> list[Finding]:
    findings: list[Finding] = []
    for i, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            findings.extend(_scan_raw_text(f"{path_label}#L{i + 1}", line, tier))
            continue
        findings.extend(_walk_json(path_label, record, f"$[record {i + 1}]", tier))
    return findings


def _scan_delimited(path_label: str, text: str, tier: str, delimiter: str) -> list[Finding]:
    findings: list[Finding] = []
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        return findings
    header = rows[0]
    norm_header = [normalize_field_name(h) for h in header]
    for col, (raw, norm) in enumerate(zip(header, norm_header)):
        findings.extend(_check_field_name(path_label, f"header col {col} ({raw})", raw, tier))
    for r, row in enumerate(rows[1:], start=2):
        for col, cell in enumerate(row):
            locator = f"row {r} col {col}"
            norm = norm_header[col] if col < len(norm_header) else f"col{col}"
            for category, snippet in _scan_text_value(cell):
                findings.append(Finding(path_label, locator, category, snippet, tier))
            findings.extend(_check_count_value(path_label, locator, norm, cell, tier))
            findings.extend(_check_timestamp_value(path_label, locator, norm, cell, tier))
    return findings


def scan_file(path: Path, *, path_label: str | None = None, force_aggregate: bool = False) -> list[Finding]:
    """Scan a single file and return findings (possibly empty)."""
    label = path_label if path_label is not None else str(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:  # pragma: no cover - unreadable file
        return [Finding(label, "open", "unreadable-file", str(exc), "UNKNOWN")]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Binary content under a promotion path: not text-scannable. Report so a
        # reviewer can confirm the artifact is intentionally binary (e.g. PNG).
        return [Finding(label, "decode", "binary-not-scanned", f"{len(raw)} bytes", "UNKNOWN")]

    aggregate = force_aggregate or _looks_like_aggregate(text, path)
    tier = AGGREGATE_TIER if aggregate else "SANITIZED_PUBLIC"
    suffix = path.suffix.lower()

    if suffix == ".json":
        return _scan_json(label, text, tier)
    if suffix in (".jsonl", ".ndjson"):
        return _scan_jsonl(label, text, tier)
    if suffix == ".csv":
        return _scan_delimited(label, text, tier, ",")
    if suffix == ".tsv":
        return _scan_delimited(label, text, tier, "\t")
    # Markdown / yaml / txt / unknown text: raw-text scan.
    return _scan_raw_text(label, text, tier)


# ---------------------------------------------------------------------------
# Path collection
# ---------------------------------------------------------------------------


def _iter_dir(directory: Path) -> Iterator[Path]:
    for p in sorted(directory.rglob("*")):
        if p.is_file():
            yield p


def collect_targets(paths: Iterable[str], promotion_root: str | None) -> tuple[list[Path], list[str]]:
    """Resolve scan targets from positional paths and an optional promotion root.

    Returns ``(files, errors)``. ``errors`` holds messages for non-existent
    explicit paths (usage errors).
    """
    files: list[Path] = []
    errors: list[str] = []
    seen: set[Path] = set()

    def add(p: Path) -> None:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            files.append(p)

    for raw in paths:
        p = Path(raw)
        if not p.exists():
            errors.append(f"path does not exist: {raw}")
            continue
        if p.is_dir():
            for f in _iter_dir(p):
                add(f)
        else:
            add(p)

    if promotion_root is not None:
        root = Path(promotion_root)
        if not root.exists():
            errors.append(f"--promotion-root does not exist: {promotion_root}")
        else:
            for glob in PROMOTION_GLOBS:
                for f in sorted(root.glob(glob)):
                    if f.is_file():
                        add(f)
            for rel in PROMOTION_FILES:
                f = root / rel
                if f.is_file():
                    add(f)

    return files, errors


def scan_paths(
    paths: Iterable[str],
    *,
    promotion_root: str | None = None,
    force_aggregate: bool = False,
    only_text: bool = True,
) -> tuple[list[Finding], list[str]]:
    """Scan all resolved targets. Returns ``(findings, errors)``."""
    files, errors = collect_targets(paths, promotion_root)
    findings: list[Finding] = []
    for f in files:
        if only_text and f.suffix.lower() not in TEXT_SUFFIXES:
            # Skip clearly-binary artifacts (e.g. .png) silently; the manifest
            # verifier handles binary provenance via SHA, not content scan.
            continue
        findings.extend(scan_file(f, force_aggregate=force_aggregate))
    return findings, errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", help="Files or directories to scan.")
    parser.add_argument(
        "--promotion-root",
        help="Repo root whose canonical public promotion surface should be scanned.",
    )
    parser.add_argument(
        "--aggregate",
        action="store_true",
        help="Force AGGREGATE_AZURE_SAMPLE tier rules for every scanned file.",
    )
    parser.add_argument(
        "--include-binary",
        action="store_true",
        help="Also report binary (non-UTF-8) files under the targets instead of skipping them.",
    )
    args = parser.parse_args(argv)

    if not args.paths and not args.promotion_root:
        parser.error("provide at least one path or --promotion-root")

    findings, errors = scan_paths(
        args.paths,
        promotion_root=args.promotion_root,
        force_aggregate=args.aggregate,
        only_text=not args.include_binary,
    )

    for err in errors:
        print(f"ERROR: {err}", file=sys.stderr)
    if errors:
        return 2

    if findings:
        for f in findings:
            print(f.format(), file=sys.stderr)
        n_files = len({f.path for f in findings})
        print(
            f"\npromotion-set scan: {len(findings)} finding(s) across {n_files} file(s).",
            file=sys.stderr,
        )
        return 1

    print("promotion-set scan: clean — no redaction-category matches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
