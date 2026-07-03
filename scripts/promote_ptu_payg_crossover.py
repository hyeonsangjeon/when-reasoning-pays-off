#!/usr/bin/env python3
"""Build PTU/PAYG planning chart-data for article topics.

Emits a lean ``SANITIZED_PUBLIC`` modeled companion dataset for the PTU/PAYG
crossover framing under ``results/public/chart-data/ptu-payg-crossover/**``.

This companion does **not** size PTU counts, model deployment capacity, publish
utilization assumptions, or recompute PAYG billing from token composition. It:

  * carries the canonical PAYG ``mean_usd_per_request`` value through **exactly**
    from the first-tranche ``cost-curves-effort/<benchmark>/cost-per-request.json``
    rows (no recomputation, no tolerance check);
  * joins the same benchmark's ``throughput-gain.json``, ``quality.json`` and
    ``token-composition/<benchmark>/tokens.json`` rows by exact ``(model, effort)``
    — token composition is an explanatory presence join only and never drives a
    billing recomputation;
  * resolves the modeled PTU hourly rate and minimum PTU from the pinned PTU
    pricing snapshot YAML; and
  * derives a normalized modeled break-even RPM lens:

        modeled_break_even_rpm =
            (ptu_hourly_rate_usd * min_ptu / mean_usd_per_request)
                / 60 * throughput_gain_factor

    rounded to six decimal places after full-precision calculation. This is a
    modeled hypothesis lens (``framing_key: throughput_gain_hypothesis``), not a
    measured PTU throughput claim.

Public-safety: payloads are numeric/stable-key only. Pricing provenance (URLs,
access dates, archive URLs, local paths) never enters a payload — only SHA256
pins of the pricing snapshots are recorded under ``pricing_snapshot_sha256``.

The shared candidate manifest ``release/public_chart_candidates.json`` and the
``release/public_sanitized_manifest.json`` integrity ledger are append/upsert
only: this generator touches solely its own ``ptu-payg-crossover`` family and
preserves other article-topic chart families.

Usage::

    python3 scripts/promote_ptu_payg_crossover.py            # write + update manifests
    python3 scripts/promote_ptu_payg_crossover.py --check     # verify on-disk == regenerated
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
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

FAMILY_KEY = "ptu-payg-crossover"
METRIC_KEY = "ptu_payg_crossover"
UNITS_KEY = "mixed_usd_rpm"
FRAMING_KEY = "throughput_gain_hypothesis"
TOPIC_SLUG = topics.target_topic_slug(FAMILY_KEY)

DIMENSION_KEYS = ("effort", "model")
SERIES_KEYS = (
    "mean_usd_per_request",
    "throughput_gain_factor",
    "modeled_break_even_rpm",
    "ptu_hourly_rate_usd",
    "min_ptu",
    "n_used",
)

BENCHMARKS = topics.chart_benchmark_numbers(FAMILY_KEY)

# Family this generator owns in the shared candidate manifest.
OWNED_FAMILIES = topics.generator_family_keys("ptu-payg-planning")

# Pricing snapshots (read-only inputs). The PTU snapshot resolves the modeled
# hourly rate and minimum PTU; the PAYG snapshot is carried only as a SHA
# lineage pin (the canonical USD/request is carried through from the public
# cost-curves chart data, never recomputed from this file).
PTU_PRICING_RELPATH = "pricing/azure-openai-ptu-2026-05.yaml"
PAYG_PRICING_RELPATH = "pricing/azure-openai-payg-2026-05.yaml"


# ---------------------------------------------------------------------------
# Source readers (first-tranche public chart data)
# ---------------------------------------------------------------------------


def _source_relpaths(bench: str) -> dict[str, str]:
    bkey = f"benchmark-{bench}"
    return {
        "cost": f"results/public/chart-data/cost-curves-effort/{bkey}/cost-per-request.json",
        "throughput": f"results/public/chart-data/cost-curves-effort/{bkey}/throughput-gain.json",
        "quality": f"results/public/chart-data/cost-curves-effort/{bkey}/quality.json",
        "tokens": f"results/public/chart-data/token-composition/{bkey}/tokens.json",
    }


def _read_chart(relpath: str) -> dict[str, Any]:
    path = REPO_ROOT / relpath
    if not path.is_file():
        raise FileNotFoundError(f"source chart data missing: {relpath}")
    return json.loads(path.read_text(encoding="utf-8"))


def _rows_by_key(chart: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in chart.get("rows", []):
        out[(row["model"], row["effort"])] = row
    return out


def _sha256_relpath(relpath: str) -> str:
    return pub.sha256_file(REPO_ROOT / relpath)


# ---------------------------------------------------------------------------
# Pricing resolution (PTU snapshot only resolves hourly rate + min PTU)
# ---------------------------------------------------------------------------


def _load_ptu_models() -> dict[str, dict[str, Any]]:
    """Parse the per-model PTU rate / min_ptu rows from the snapshot YAML.

    Deliberately does not pull in a YAML dependency for two scalar fields per
    model; parses the small, stable ``models:`` block directly so the only
    thing that ever enters a payload is the resolved numeric value.
    """
    text = (REPO_ROOT / PTU_PRICING_RELPATH).read_text(encoding="utf-8")
    resolved: dict[str, dict[str, Any]] = {}
    in_models = False
    current_model: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line == "models:":
            in_models = True
            continue
        if not in_models:
            continue
        if line.startswith("  ") and not line.startswith("    "):
            current_model = line.strip().removesuffix(":")
            resolved[current_model] = {}
            continue
        if current_model and line.startswith("    ") and ":" in line:
            key, value = line.strip().split(":", 1)
            if key == "ptu_hourly_rate_usd":
                resolved[current_model][key] = float(value.strip())
            elif key == "min_ptu":
                resolved[current_model][key] = int(value.strip())
    for model, fields in resolved.items():
        missing = {"ptu_hourly_rate_usd", "min_ptu"} - set(fields)
        if missing:
            raise ValueError(f"{PTU_PRICING_RELPATH}: {model} missing {sorted(missing)}")
    return resolved


# ---------------------------------------------------------------------------
# Payload assembly
# ---------------------------------------------------------------------------


def _modeled_break_even_rpm(
    *, ptu_hourly_rate_usd: float, min_ptu: int, mean_usd_per_request: float,
    throughput_gain_factor: float,
) -> float:
    value = (
        (ptu_hourly_rate_usd * min_ptu / mean_usd_per_request) / 60.0
        * throughput_gain_factor
    )
    return round(value, 6)


def _combined_source_digest(source_shas: list[str], pricing_shas: list[str]) -> str:
    """Deterministic derived source sha for the public manifest.

    Each emitted chart file derives from several public source chart-data files
    plus the pinned pricing snapshots. There is no single upstream artifact, so
    the manifest ``source_raw_sha256`` pins to a documented deterministic
    digest: sha256 over the sorted contributing source/pricing sha256 strings,
    one per line.
    """
    parts = sorted(source_shas) + sorted(pricing_shas)
    blob = "\n".join(parts) + "\n"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _build_benchmark(
    bench: str,
    ptu_models: dict[str, dict[str, Any]],
) -> pub.PublishedArtifact:
    bkey = f"benchmark-{bench}"
    srcs = _source_relpaths(bench)

    cost_chart = _read_chart(srcs["cost"])
    tg_rows = _rows_by_key(_read_chart(srcs["throughput"]))
    quality_rows = _rows_by_key(_read_chart(srcs["quality"]))
    token_rows = _rows_by_key(_read_chart(srcs["tokens"]))

    ptu_sha = _sha256_relpath(PTU_PRICING_RELPATH)
    payg_sha = _sha256_relpath(PAYG_PRICING_RELPATH)

    rows: list[dict[str, Any]] = []
    # Iterate in the canonical first-tranche cost-per-request row order so the
    # PAYG carry-through is row-for-row faithful to the source.
    for cost_row in cost_chart["rows"]:
        model = cost_row["model"]
        effort = cost_row["effort"]
        key = (model, effort)
        if key not in tg_rows:
            raise ValueError(f"{bkey}: throughput-gain missing row for {key}")
        if key not in quality_rows:
            raise ValueError(f"{bkey}: quality missing row for {key}")
        if key not in token_rows:
            # Explanatory presence join only; absence signals a source mismatch.
            raise ValueError(f"{bkey}: token-composition missing row for {key}")
        if model not in ptu_models:
            raise ValueError(f"{bkey}: PTU pricing missing model {model!r}")

        # PAYG carry-through invariant: copy the canonical source value exactly.
        mean_usd_per_request = cost_row["mean_usd_per_request"]
        throughput_gain_factor = tg_rows[key]["throughput_gain_factor"]
        ptu_hourly_rate_usd = ptu_models[model]["ptu_hourly_rate_usd"]
        min_ptu = ptu_models[model]["min_ptu"]

        rows.append(
            {
                "effort": effort,
                "model": model,
                "mean_usd_per_request": mean_usd_per_request,
                "throughput_gain_factor": throughput_gain_factor,
                "modeled_break_even_rpm": _modeled_break_even_rpm(
                    ptu_hourly_rate_usd=ptu_hourly_rate_usd,
                    min_ptu=min_ptu,
                    mean_usd_per_request=mean_usd_per_request,
                    throughput_gain_factor=throughput_gain_factor,
                ),
                "ptu_hourly_rate_usd": ptu_hourly_rate_usd,
                "min_ptu": min_ptu,
                "n_used": cost_row["n_used"],
            }
        )

    source_shas = [
        _sha256_relpath(srcs["cost"]),
        _sha256_relpath(srcs["throughput"]),
        _sha256_relpath(srcs["quality"]),
        _sha256_relpath(srcs["tokens"]),
    ]
    combined_source_sha = _combined_source_digest(source_shas, [ptu_sha, payg_sha])

    payload = {
        "schema": CHART_DATA_SCHEMA,
        "schema_semver": SCHEMA_SEMVER,
        "tier": TIER,
        "family_key": FAMILY_KEY,
        "metric_key": METRIC_KEY,
        "benchmark_key": bkey,
        "dimension_keys": list(DIMENSION_KEYS),
        "series_keys": list(SERIES_KEYS),
        "units_key": UNITS_KEY,
        "framing_key": FRAMING_KEY,
        "source_sanitized_sha256": source_shas,
        "pricing_snapshot_sha256": {
            "ptu_pricing_sha256": ptu_sha,
            "payg_pricing_sha256": payg_sha,
        },
        "quality_pairing": {
            "quality_family_key": "cost-curves-effort",
            "quality_metric_key": "quality",
            "quality_chart_data_path": srcs["quality"],
            "quality_benchmark_key": bkey,
        },
        "rows": rows,
    }

    out_relpath = f"results/public/chart-data/ptu-payg-crossover/{bkey}/crossover.json"
    candidate = {
        "family_key": FAMILY_KEY,
        "chart_data_path": out_relpath,
        "dimension_keys": list(DIMENSION_KEYS),
        "series_keys": list(SERIES_KEYS),
        "units_key": UNITS_KEY,
        "tier": TIER,
        "target_topic_slug": TOPIC_SLUG,
        "source_sanitized_sha256": source_shas,
        "schema_semver": SCHEMA_SEMVER,
    }
    return pub.PublishedArtifact(
        relpath=out_relpath,
        payload=payload,
        source_raw_sha=combined_source_sha,
        candidate=candidate,
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def build_emitted() -> list[pub.PublishedArtifact]:
    ptu_models = _load_ptu_models()
    return [_build_benchmark(bench, ptu_models) for bench in BENCHMARKS]


def generate(check: bool = False) -> int:
    emitted = build_emitted()
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
        print("check: on-disk ptu-payg-crossover chart data + candidate manifest match regeneration.")
        return 0

    pub.update_public_manifest(emitted, candidate_sha=candidate_sha)
    _print_summary(emitted, candidate_sha)
    return 0


def _print_summary(emitted: list[pub.PublishedArtifact], candidate_sha: str) -> None:
    print("chart-data promotion (ptu-payg-crossover) - summary")
    print(f"  family: {FAMILY_KEY}")
    print(f"  framing_key: {FRAMING_KEY}")
    print(f"  emitted chart-data files: {len(emitted)}")
    for art in emitted:
        on_disk = pub.sha256_file(REPO_ROOT / art.relpath)
        print(f"    {art.relpath}  (sha={on_disk[:12]}...)")
    print(f"  candidate manifest: {CANDIDATE_MANIFEST_RELPATH}  (sha={candidate_sha[:12]}...)")
    print(f"  public manifest updated: {spa.PUBLIC_MANIFEST_RELPATH}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify on-disk chart data + candidate manifest match regeneration; do not write.",
    )
    args = parser.parse_args(argv)
    return generate(check=args.check)


if __name__ == "__main__":
    sys.exit(main())
