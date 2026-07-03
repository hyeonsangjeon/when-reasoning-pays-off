#!/usr/bin/env python3
"""Build overview article chart-data for effort and token topics.

This compatibility CLI emits the two core ``SANITIZED_PUBLIC`` chart-data
families used by the overview article and its evidence topics:

  1. ``cost-curves-effort``  (benchmark-01/02/03 x {cost-per-request, latency,
     quality, throughput-gain})
  2. ``token-composition``   (benchmark-01/02/03 token composition)

It reads the already-public, born-clean result CSVs under ``results/`` and emits
**locale-agnostic, numeric-only** chart-data JSON under
``results/public/chart-data/**``. Per the campaign contract (docs/16):

  * Stable keys / slugs only. No titles, legends, prose, private paths,
    endpoint / deployment names, request IDs, or human labels enter the payload.
  * For the cost-curves family the pricing-provenance fields
    (``pricing_snapshot_path`` / ``pricing_source_url`` /
    ``pricing_accessed_date``) and the trailing ``#``-prefixed provenance comment
    rows are dropped — numeric fields only. (Pricing reference is carried in
    provenance, not in the chart payload.)
  * For the token-composition family the numeric fields are emitted directly.

The generator is deterministic and idempotent: re-running it over an unchanged
working tree produces byte-identical output. It also:

  * writes the committed candidate manifest ``release/public_chart_candidates.json``
    enumerating every emitted artifact (family_key, chart_data_path,
    dimension_keys, series_keys, units_key, tier, target_topic_slug,
    source_sanitized_sha256[], schema_semver), and
  * upserts ``release/public_sanitized_manifest.json`` integrity entries for every
    emitted SANITIZED_PUBLIC file (chart data + the candidate manifest), and
    refreshes the ``CHANGELOG.md`` entry's ``sanitized_sha256`` if the changelog
    changed.

This module deliberately reuses the public-manifest primitives from
``scripts/sanitize_public_artifacts.py`` so the integrity ledger stays governed
by a single contract.

Source-truth resolution
-----------------------
The source result CSVs are born-clean public-tree artifacts that never required
redaction, so they do not appear in the private raw-archive manifest. Their
source truth therefore resolves to the on-disk file and its sha256 (which match
the catalogued ``source_artifact_sha256`` values in the article-topic chart
catalog). Each emitted chart file derives from exactly one source CSV, so its
``source_raw_sha256`` pins to that CSV's sha256.

Usage::

    python scripts/promote_chart_data.py            # write + update manifests
    python scripts/promote_chart_data.py --check     # verify on-disk == regenerated
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SCRIPT_DIR))

from scripts.article_topics import publication as pub  # noqa: E402
from scripts.article_topics import registry as topics  # noqa: E402

# Backward-compatible module alias used by existing tests and ad hoc checks.
spa = pub.spa

CHART_DATA_ROOT = pub.CHART_DATA_ROOT
CANDIDATE_MANIFEST_RELPATH = pub.CANDIDATE_MANIFEST_RELPATH
CANDIDATE_MANIFEST_PATH = pub.CANDIDATE_MANIFEST_PATH

SCHEMA_SEMVER = pub.SCHEMA_SEMVER
CHART_DATA_SCHEMA = pub.CHART_DATA_SCHEMA
TIER = pub.TIER

# Dimension columns are stable enum keys, emitted as strings. Everything else in
# a kept column set is coerced to a JSON number.
DIMENSION_KEYS = ("effort", "model")

# The empty effort cell (the gpt-4o non-reasoning baseline row, which carries no
# reasoning-effort parameter) maps to this stable sentinel key.
EFFORT_NA_KEY = "na"

BENCHMARKS = topics.chart_benchmark_numbers("cost-curves-effort")

# Candidate families this first-tranche generator owns. The shared candidate
# manifest (release/public_chart_candidates.json) is multi-tranche: later
# tranches append their own families. This generator therefore upserts only its
# owned families and preserves every other tranche's candidates untouched, so
# ``--check`` does not regress when a later tranche has appended candidates.
OWNED_FAMILIES = topics.generator_family_keys("effort-evidence")


@dataclass(frozen=True)
class Metric:
    """CSV-to-chart-data metric definition."""

    key: str
    slug: str
    value_keys: tuple[str, ...]
    units_key: str


COST_CURVE_METRICS: tuple[Metric, ...] = (
    Metric(
        key="cost_per_request",
        slug="cost-per-request",
        value_keys=("mean_usd_per_request", "std_usd_per_request", "n_used"),
        units_key="usd_per_request",
    ),
    Metric(
        key="latency",
        slug="latency",
        value_keys=("mean_latency_ms", "std_latency_ms", "n_used"),
        units_key="milliseconds",
    ),
    Metric(
        key="quality",
        slug="quality",
        value_keys=("mean_judge_score", "std_judge_score", "judge_n"),
        units_key="judge_score",
    ),
    Metric(
        key="throughput_gain",
        slug="throughput-gain",
        value_keys=("throughput_gain_factor", "tokens_per_request", "baseline_tokens_per_request"),
        units_key="factor",
    ),
)

TOKEN_METRIC = Metric(
    key="token_composition",
    slug="tokens",
    value_keys=(
        "mean_input_tokens_noncached",
        "mean_cached_tokens",
        "mean_output_tokens",
        "mean_reasoning_tokens",
        "n_used",
    ),
    units_key="tokens",
)

# Integer-valued columns (everything else among value_keys is a float).
_INT_VALUE_KEYS = frozenset({"n_used", "judge_n"})


def _coerce_number(key: str, raw: str) -> int | float:
    raw = raw.strip()
    if key in _INT_VALUE_KEYS:
        return int(float(raw))
    f = float(raw)
    return f


def _read_source_rows(
    csv_path: Path,
    value_keys: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Parse a source result CSV into numeric chart rows.

    Drops ``#``-prefixed provenance comment rows and any column not in the
    dimension set or *value_keys* (this is what strips the pricing-provenance
    columns and the throughput-gain ``baseline_label`` prose column).
    """
    text = csv_path.read_text(encoding="utf-8")
    reader = csv.reader(text.splitlines())
    rows = [r for r in reader if r and not r[0].lstrip().startswith("#")]
    if not rows:
        raise ValueError(f"no data rows in {csv_path}")
    header = rows[0]
    idx = {name: i for i, name in enumerate(header)}
    for vk in value_keys:
        if vk not in idx:
            raise ValueError(f"{csv_path}: expected column {vk!r} not in header {header}")
    out: list[dict[str, Any]] = []
    for r in rows[1:]:
        rec: dict[str, Any] = {}
        effort_raw = r[idx["effort"]].strip() if "effort" in idx else ""
        rec["effort"] = effort_raw if effort_raw else EFFORT_NA_KEY
        rec["model"] = r[idx["model"]].strip()
        for vk in value_keys:
            rec[vk] = _coerce_number(vk, r[idx[vk]])
        out.append(rec)
    return out


def _chart_payload(
    *,
    family_key: str,
    benchmark_key: str,
    metric: Metric,
    source_sha: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": CHART_DATA_SCHEMA,
        "schema_semver": SCHEMA_SEMVER,
        "tier": TIER,
        "family_key": family_key,
        "benchmark_key": benchmark_key,
        "metric_key": metric.key,
        "dimension_keys": list(DIMENSION_KEYS),
        "series_keys": list(metric.value_keys),
        "units_key": metric.units_key,
        "source_sanitized_sha256": [source_sha],
        "rows": rows,
    }


def build_plan() -> list[tuple[str, str, Metric, Path, str]]:
    """Return the ordered emission plan.

    Each item: (family_key, benchmark_key, metric, source_csv_path, out_relpath).
    """
    plan: list[tuple[str, str, Metric, Path, str]] = []
    for bench in BENCHMARKS:
        bkey = f"benchmark-{bench}"
        for metric in COST_CURVE_METRICS:
            src = REPO_ROOT / "results" / "cost-curves" / f"benchmark-{bench}-{metric.slug}.csv"
            out = f"results/public/chart-data/cost-curves-effort/{bkey}/{metric.slug}.json"
            plan.append(("cost-curves-effort", bkey, metric, src, out))
    for bench in BENCHMARKS:
        bkey = f"benchmark-{bench}"
        src = REPO_ROOT / "results" / "token-composition" / f"benchmark-{bench}-tokens.csv"
        out = f"results/public/chart-data/token-composition/{bkey}/tokens.json"
        plan.append(("token-composition", bkey, TOKEN_METRIC, src, out))
    return plan


def _topic_slug(family_key: str) -> str:
    return topics.target_topic_slug(family_key)


def generate(check: bool = False) -> int:
    plan = build_plan()
    emitted: list[pub.PublishedArtifact] = []

    for family_key, bkey, metric, src, out_relpath in plan:
        if not src.is_file():
            print(f"ABORT: source CSV missing: {src}", file=sys.stderr)
            return 3
        source_sha = pub.sha256_file(src)
        rows = _read_source_rows(src, metric.value_keys)
        payload = _chart_payload(
            family_key=family_key,
            benchmark_key=bkey,
            metric=metric,
            source_sha=source_sha,
            rows=rows,
        )
        candidate = {
            "family_key": family_key,
            "chart_data_path": out_relpath,
            "dimension_keys": list(DIMENSION_KEYS),
            "series_keys": list(metric.value_keys),
            "units_key": metric.units_key,
            "tier": TIER,
            "target_topic_slug": _topic_slug(family_key),
            "source_sanitized_sha256": [source_sha],
            "schema_semver": SCHEMA_SEMVER,
        }
        emitted.append(
            pub.PublishedArtifact(
                relpath=out_relpath,
                payload=payload,
                source_raw_sha=source_sha,
                candidate=candidate,
            )
        )

    drift = pub.write_or_check_artifacts(emitted, check=check)
    candidate_manifest = pub.merged_candidate_manifest(
        emitted,
        owned_family_keys=OWNED_FAMILIES,
    )
    candidate_sha, candidate_drifted = pub.write_or_check_candidate_manifest(
        candidate_manifest,
        check=check,
    )
    if check:
        if candidate_drifted:
            drift.append(CANDIDATE_MANIFEST_RELPATH)
        if drift:
            pub.report_drift(drift)
            return 1
        print("check: on-disk chart data + candidate manifest match regeneration.")
        return 0

    pub.update_public_manifest(emitted, candidate_sha=candidate_sha)
    _print_summary(emitted, candidate_sha)
    return 0


def _print_summary(emitted: list[pub.PublishedArtifact], candidate_sha: str) -> None:
    print("chart-data promotion (first tranche) - summary")
    print("  families: cost-curves-effort, token-composition")
    print(f"  emitted chart-data files: {len(emitted)}")
    for art in emitted:
        print(f"    {art.relpath}  (source_sha={art.source_raw_sha[:12]}...)")
    print(f"  candidate manifest: {CANDIDATE_MANIFEST_RELPATH}  (sha={candidate_sha[:12]}...)")
    print(f"  public manifest updated: {spa.PUBLIC_MANIFEST_RELPATH}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify on-disk chart data + candidate manifest match regeneration; do not write.",
    )
    args = parser.parse_args(argv)
    return generate(check=args.check)


if __name__ == "__main__":
    sys.exit(main())
