"""Per-cell 95% bootstrap confidence intervals (revision Phase 2, T-021 / D-003).

What this does
--------------
For every benchmark, this script reads the **raw** Responses-API run JSONs
(``benchmarks/<name>/runs/*.json``) and judge JSONs
(``benchmarks/<name>/judge_runs/*.json``), joins them exactly as the canonical
aggregator does, and computes a percentile bootstrap confidence interval
(default 95%, 10,000 resamples) for four per-cell, per-observation metrics:

* ``cost``             — USD per request (PAYG; see "Cost provenance" below)
* ``quality``          — LLM-judge score (0|1|2)
* ``latency``          — wall-clock latency in milliseconds
* ``reasoning_tokens`` — reasoning-token count (0 for gpt-4o)

The output is written to
``results/supplementary/<benchmark>/bootstrap_ci.json``.

Why the raw runs are the input (and how grouping stays canonical)
-----------------------------------------------------------------
Earlier drafts read ``analysis.json`` directly. Codex review (Phase 2
REQUEST-CHANGES, finding #1) required reading the raw run/judge files instead,
so the supplementary CI rests on durable audit evidence rather than a derived
artifact, and so the output can report honest source accounting (raw run-file
count vs. the canonical *included* cohort).

To keep the ``(model, effort)`` grouping, experiment-prefix cohort filtering,
judge join, and operational-outlier exclusion byte-for-byte consistent with the
committed ``analysis.json``, this module reuses
:func:`scripts.analyze_tokens.build_analysis`. That function reads the raw
``runs/*.json`` and ``judge_runs/*.json`` files (it does **not** read
``analysis.json``) and returns the same per-row population the canonical
pipeline keeps (``outlier_reason is None`` == ``n_used``). The bootstrap then
operates on those per-row values — never on aggregate cell statistics.

The only per-benchmark configuration this module needs is the cohort selector
``experiment_prefix``. It is resolved from a stable built-in map (with a
fallback to the committed ``analysis.json`` *config* value, then the analyzer
default). All metric *values* come from the raw files via ``build_analysis``.

Cost provenance (finding #2)
----------------------------
``analysis.json`` does not persist per-row USD, so cost is recomputed here from
each raw row's token usage. The committed canonical analyses (and the public
chart data they back) do **not** all bill cost the same way:

* benchmark 01's canonical means bill reasoning tokens as a **separate** line
  (``output_tokens * output_rate + reasoning_tokens * reasoning_rate``), and
* benchmarks 02/03's canonical means bill **output-only**
  (``output_tokens * output_rate``; reasoning not added).

This divergence is a property of the committed/public data, not a choice made
here. To keep the supplementary CI consistent with the canonical analysis it
supplements, this module **detects** each benchmark's billing mode by
reproducing its committed ``cell_stats`` ``mean_usd_per_request`` and then
applies the matching per-row formula. Every cost cell is validated against the
committed mean; the detected mode and validation outcome are recorded in the
output metadata under ``cost_provenance`` and per-cell ``cost_validation``. The
canonical cost function ``scripts.cost_calculator.payg_cost_per_call`` is not
called for the per-row figure because, under today's code, it bills output-only
and would silently disagree with benchmark 01's canonical means. The pricing
*rates* and snapshot citation still come from the same PAYG snapshot the
analyzer resolves.

Empty canonical cells (finding #3)
----------------------------------
A canonical ``(model, effort)`` cell with ``n_used == 0`` (e.g. benchmark 03's
``gpt-5.2 / minimal``) is represented explicitly: each of its metric rows is
emitted with ``n_used = 0`` and null ``mean`` / ``ci_low`` / ``ci_high`` plus a
warning, and the cell is also listed in a top-level ``skipped_cells`` section.

Determinism
-----------
Resampling uses an independent NumPy ``Generator`` per ``(benchmark, model,
effort, metric)``, seeded from a stable BLAKE2b digest of those identifiers plus
``--seed``. Output is JSON with ``sort_keys=True`` and carries no wall-clock
timestamps, so it is byte-stable across runs (suitable for the CI
reproducibility check, T-064).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any

import numpy as np

# T-021 must mirror analysis.json semantics exactly (grouping, experiment-prefix
# filtering, judge join, operational-outlier exclusion). Reusing the canonical
# builder — which reads the raw runs/*.json and judge_runs/*.json — is the only
# way to guarantee the kept-row population matches the committed cell_stats.
from scripts.analyze_tokens import (
    DEFAULT_EXPERIMENT_PREFIX,
    build_analysis,
)
from scripts.cost_calculator import load_payg_pricing, resolve_active_snapshot
from scripts.stats import common as stats_common
from scripts._pricing_types import Gpt52Rates

# Round to the same precision the rest of the analysis pipeline uses
# (``scripts.analyze_tokens.FLOAT_NDIGITS``) so supplementary numbers line up
# with the main tables.
FLOAT_NDIGITS: int = 6

# Cost means are tiny (~1e-3 USD); validate to a tolerance just above the
# six-decimal rounding floor of the committed means.
COST_VALIDATION_TOL: float = 2e-6

# Canonical metric order — fixed so output ordering is deterministic.
METRICS: tuple[str, ...] = ("cost", "quality", "latency", "reasoning_tokens")

REPO_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[2]

# Stable cohort selectors for the three target benchmarks. These are the same
# experiment_prefix values the committed analysis.json files were built with;
# they isolate the canonical authored cohort from sibling smoke / warm-probe
# runs that co-exist in the same runs/ directory.
EXPERIMENT_PREFIXES: dict[str, str] = {
    "01-short-factual": "exp008_short-factual_fixture",
    "02-multi-step-reasoning": "exp002",
    "03-tool-using-agent": "exp003",
}

# Canonical effort ordering for deterministic cell iteration.
_EFFORT_RANK: dict[str | None, int] = {
    None: -1,
    "none": 0,
    "minimal": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
    "xhigh": 5,
}


# ----------------------------------------------------------------------------
# Bootstrap core
# ----------------------------------------------------------------------------


def _stable_seed(*parts: object, base_seed: int) -> int:
    """Derive a stable 64-bit seed from ``base_seed`` and identifier parts.

    Uses BLAKE2b so the result is independent of Python's per-process hash
    randomization — two runs with the same ``--seed`` produce identical
    resamples regardless of iteration order.
    """
    payload = "\x1f".join([str(base_seed), *(str(p) for p in parts)])
    digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def bootstrap_ci(
    values: list[float],
    *,
    resamples: int,
    ci_level: float,
    seed: int,
) -> dict[str, Any]:
    """Percentile bootstrap mean CI for one metric of one cell.

    Args:
        values: Per-observation metric values (already missing-filtered).
        resamples: Number of bootstrap resamples (e.g. 10,000).
        ci_level: Two-sided coverage, e.g. 0.95.
        seed: Stable integer seed for this cell+metric.

    Returns:
        Dict with ``n_used``, ``mean``, ``ci_low``, ``ci_high`` (rounded), and
        a ``warnings`` list. For ``n_used == 0`` every estimate is ``None``.
        For ``n_used < 2`` the CI degenerates to the point estimate and a
        warning is recorded (a CI is not meaningful at n<2).
    """
    n = len(values)
    warnings: list[str] = []
    if n == 0:
        return {
            "n_used": 0,
            "mean": None,
            "ci_low": None,
            "ci_high": None,
            "warnings": ["no observations for this metric in this cell"],
        }

    arr = np.asarray(values, dtype=float)
    mean = float(arr.mean())

    if n < 2:
        warnings.append(
            f"n={n} < 2: bootstrap CI is not meaningful; "
            "reporting point estimate with degenerate interval"
        )
        return {
            "n_used": n,
            "mean": round(mean, FLOAT_NDIGITS),
            "ci_low": round(mean, FLOAT_NDIGITS),
            "ci_high": round(mean, FLOAT_NDIGITS),
            "warnings": warnings,
        }

    rng = np.random.default_rng(seed)
    # Each row of ``idx`` is one resample of n indices drawn with replacement.
    idx = rng.integers(0, n, size=(resamples, n))
    resample_means = arr[idx].mean(axis=1)

    alpha = (1.0 - ci_level) / 2.0
    ci_low, ci_high = np.percentile(
        resample_means, [100.0 * alpha, 100.0 * (1.0 - alpha)]
    )
    return {
        "n_used": n,
        "mean": round(mean, FLOAT_NDIGITS),
        "ci_low": round(float(ci_low), FLOAT_NDIGITS),
        "ci_high": round(float(ci_high), FLOAT_NDIGITS),
        "warnings": warnings,
    }


# ----------------------------------------------------------------------------
# Cost (per-row), with per-benchmark billing-mode detection
# ----------------------------------------------------------------------------


def _row_cost_usd(row: dict[str, Any], pricing: Any, *, bill_reasoning: bool) -> float:
    """Per-row PAYG USD, reproducing the committed analysis billing.

    The formula mirrors ``scripts.cost_calculator.payg_cost_per_call`` for the
    input/cached/output terms. ``bill_reasoning`` adds the reasoning line
    (``reasoning_tokens * reasoning_per_1m_usd``) when the benchmark's canonical
    analysis does so (benchmark 01); for output-only benchmarks (02/03) it is
    ``False``. gpt-4o never has a reasoning column.
    """
    model = str(row["model"])
    rates = pricing.models[model]
    inp = float(row["input_tokens"])
    cached = float(row.get("cached_tokens", 0) or 0)
    out = float(row["output_tokens"])
    reasoning = float(row.get("reasoning_tokens", 0) or 0)
    non_cached = inp - cached

    cost = (
        non_cached * rates.input_per_1m_usd
        + cached * rates.cached_input_per_1m_usd
        + out * rates.output_per_1m_usd
    )
    if bill_reasoning and isinstance(rates, Gpt52Rates):
        cost += reasoning * rates.reasoning_per_1m_usd
    return cost / 1_000_000.0


def _detect_billing_mode(
    used_by_cell: dict[tuple[str, str | None], list[dict[str, Any]]],
    committed_mean_usd: dict[tuple[str, str | None], float],
    pricing: Any,
) -> tuple[bool, dict[str, Any]]:
    """Detect whether this benchmark's canonical analysis bills reasoning.

    For each non-empty cell, compute the per-row mean USD under both the
    output-only and reasoning-inclusive formulas and compare to the committed
    ``mean_usd_per_request``. Pick the mode with the smaller total absolute
    error across cells. Returns ``(bill_reasoning, detail)``.
    """
    err_incl = 0.0
    err_excl = 0.0
    n_compared = 0
    for cell_key, rows in used_by_cell.items():
        committed = committed_mean_usd.get(cell_key)
        if committed is None or not rows:
            continue
        mean_incl = sum(
            _row_cost_usd(r, pricing, bill_reasoning=True) for r in rows
        ) / len(rows)
        mean_excl = sum(
            _row_cost_usd(r, pricing, bill_reasoning=False) for r in rows
        ) / len(rows)
        err_incl += abs(mean_incl - committed)
        err_excl += abs(mean_excl - committed)
        n_compared += 1

    bill_reasoning = err_incl <= err_excl
    detail = {
        "billing_mode": (
            "reasoning_billed_separately" if bill_reasoning else "output_only"
        ),
        "detection": (
            "selected the per-row formula whose cell means best reproduce the "
            "committed analysis.json mean_usd_per_request"
        ),
        "cells_compared": n_compared,
        "total_abs_error_reasoning_billed": round(err_incl, 9),
        "total_abs_error_output_only": round(err_excl, 9),
    }
    return bill_reasoning, detail


# ----------------------------------------------------------------------------
# Metric extraction from raw used rows
# ----------------------------------------------------------------------------


def _extract_metric(
    rows: list[dict[str, Any]],
    metric: str,
    *,
    pricing: Any,
    bill_reasoning: bool,
) -> tuple[list[float], int, list[str]]:
    """Pull one metric's per-observation values from a cell's used rows.

    Returns ``(values, n_missing, warnings)``. Rows lacking the field — or, for
    cost, failing the per-row cost computation — are skipped and counted.
    """
    values: list[float] = []
    n_missing = 0
    warnings: list[str] = []

    for row in rows:
        if metric == "cost":
            try:
                values.append(
                    float(_row_cost_usd(row, pricing, bill_reasoning=bill_reasoning))
                )
            except Exception as exc:  # data-integrity / missing token field
                n_missing += 1
                warnings.append(
                    f"cost skipped for sample={row.get('sample_id')!r} "
                    f"repeat={row.get('repeat')}: {type(exc).__name__}: {exc}"
                )
        elif metric == "quality":
            score = row.get("judge_score")
            if score is None:
                n_missing += 1
            else:
                values.append(float(score))
        elif metric == "latency":
            lat = row.get("latency_ms")
            if lat is None:
                n_missing += 1
            else:
                values.append(float(lat))
        elif metric == "reasoning_tokens":
            rt = row.get("reasoning_tokens")
            if rt is None:
                n_missing += 1
            else:
                values.append(float(rt))
        else:  # pragma: no cover - guarded by METRICS
            raise ValueError(f"unknown metric: {metric!r}")

    if n_missing:
        warnings.append(
            f"{metric}: {n_missing} of {len(rows)} used rows missing this field"
        )
    return values, n_missing, warnings


# ----------------------------------------------------------------------------
# Pricing + experiment-prefix resolution
# ----------------------------------------------------------------------------


def _resolve_experiment_prefix(
    benchmark: str, bench_root: pathlib.Path
) -> tuple[str, str]:
    """Resolve the cohort selector for a benchmark.

    Returns ``(prefix, source)``. Preference: built-in stable map, then the
    committed analysis.json *config* value, then the analyzer default. Only the
    cohort selector is read from analysis.json (never measurement values).
    """
    if benchmark in EXPERIMENT_PREFIXES:
        return EXPERIMENT_PREFIXES[benchmark], "builtin_map"
    analysis_path = bench_root / "analysis.json"
    if analysis_path.is_file():
        try:
            with analysis_path.open("r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            prefix = cfg.get("experiment_prefix")
            if isinstance(prefix, str) and prefix:
                return prefix, "analysis_json_config"
        except (OSError, json.JSONDecodeError):
            pass
    return DEFAULT_EXPERIMENT_PREFIX, "analyzer_default"


def _committed_cost_means(
    bench_root: pathlib.Path,
) -> dict[tuple[str, str | None], float]:
    """Read committed analysis.json cell_stats mean_usd for validation only.

    Returns ``{}`` when no analysis.json exists (validation then degrades to a
    recorded warning rather than failing).
    """
    analysis_path = bench_root / "analysis.json"
    if not analysis_path.is_file():
        return {}
    try:
        with analysis_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[tuple[str, str | None], float] = {}
    for s in data.get("cell_stats") or []:
        if s.get("n_used"):
            out[(str(s["model"]), s.get("effort"))] = float(
                s.get("mean_usd_per_request", 0.0)
            )
    return out


# ----------------------------------------------------------------------------
# Per-benchmark driver
# ----------------------------------------------------------------------------


def _cell_sort(k: tuple[str, str | None]) -> tuple[int, int, str]:
    model, effort = k
    model_rank = 0 if model == "gpt-4o" else 1
    return (model_rank, _EFFORT_RANK.get(effort, 99), str(effort))


def _rel(path: pathlib.Path) -> str:
    """Repo-relative string path when possible, else absolute."""
    resolved = path.resolve()
    if resolved.is_relative_to(REPO_ROOT):
        return str(resolved.relative_to(REPO_ROOT))
    return str(resolved)


def compute_benchmark(
    benchmark: str,
    *,
    benchmarks_dir: pathlib.Path,
    pricing_dir: pathlib.Path,
    resamples: int,
    ci_level: float,
    seed: int,
) -> dict[str, Any]:
    """Compute the bootstrap-CI payload for one benchmark from raw runs."""
    bench_root = benchmarks_dir / benchmark
    runs_dir = bench_root / "runs"
    judge_dir = bench_root / "judge_runs"
    dataset_path = bench_root / "dataset.json"

    experiment_prefix, prefix_source = _resolve_experiment_prefix(benchmark, bench_root)

    benchmark_warnings: list[str] = []

    # Raw source accounting (finding #1): count every raw file on disk before
    # cohort filtering, so the output can distinguish the raw run-file count
    # from the canonical included cohort.
    raw_run_files = sorted(runs_dir.glob("*.json")) if runs_dir.is_dir() else []
    raw_judge_files = sorted(judge_dir.glob("*.json")) if judge_dir.is_dir() else []

    # Canonical raw join: build_analysis reads runs/*.json + judge_runs/*.json
    # (NOT analysis.json) and applies the same prefix filter / outlier logic /
    # judge join as the committed pipeline.
    analysis = build_analysis(
        benchmark_name=benchmark,
        runs_dir=runs_dir,
        judge_dir=judge_dir,
        dataset_path=dataset_path,
        pricing_dir=pricing_dir,
        experiment_prefix=experiment_prefix,
    )

    cells: list[dict[str, Any]] = analysis.get("cells") or []
    cell_stats: list[dict[str, Any]] = analysis.get("cell_stats") or []
    included_run_count = int(analysis.get("run_count", len(cells)))

    # Group included (kept) rows by (model, effort).
    used_by_cell: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    n_excluded = 0
    for row in cells:
        if row.get("outlier_reason") is None:
            used_by_cell.setdefault(
                (str(row["model"]), row.get("effort")), []
            ).append(row)
        else:
            n_excluded += 1

    # Pricing rates + snapshot citation (same snapshot the analyzer resolves).
    snapshot_path = resolve_active_snapshot(
        kind="payg", target_date=None, pricing_dir=pricing_dir
    )
    pricing = load_payg_pricing(snapshot_path)
    snap = pathlib.Path(pricing.snapshot_path)
    snapshot_rel = _rel(snap) if not snap.is_absolute() or snap.exists() else str(snap)

    # Detect this benchmark's billing mode by reproducing committed means.
    committed_means = _committed_cost_means(bench_root)
    if committed_means:
        bill_reasoning, billing_detail = _detect_billing_mode(
            used_by_cell, committed_means, pricing
        )
    else:
        # No canonical reference: default to the economically-complete formula
        # (reasoning billed) and flag that validation could not run.
        bill_reasoning = True
        billing_detail = {
            "billing_mode": "reasoning_billed_separately",
            "detection": "no committed analysis.json found; defaulted to "
            "reasoning-inclusive billing (could not validate)",
            "cells_compared": 0,
        }
        benchmark_warnings.append(
            "no committed analysis.json: cost means could not be validated"
        )

    # Canonical cell roster (includes n_used==0 empties, e.g. gpt-5.2/minimal).
    canonical_cells: list[tuple[str, str | None]] = [
        (str(s["model"]), s.get("effort")) for s in cell_stats
    ]
    # Fall back to observed cells if cell_stats is empty for some reason.
    if not canonical_cells:
        canonical_cells = list(used_by_cell.keys())

    results: list[dict[str, Any]] = []
    skipped_cells: list[dict[str, Any]] = []

    for cell_key in sorted(set(canonical_cells), key=_cell_sort):
        model, effort = cell_key
        rows = used_by_cell.get(cell_key, [])

        if not rows:
            # Empty canonical cell (finding #3): explicit n_used=0 metric rows
            # with null CI fields + a skipped_cells entry.
            skipped_cells.append(
                {
                    "benchmark": benchmark,
                    "model": model,
                    "effort": effort,
                    "n_used": 0,
                    "status": "empty_canonical_cell",
                    "reason": (
                        "canonical analysis reports n_used=0 for this "
                        "(model, effort) cell; no observations to resample"
                    ),
                }
            )
            for metric in METRICS:
                results.append(
                    {
                        "benchmark": benchmark,
                        "model": model,
                        "effort": effort,
                        "metric": metric,
                        "status": "empty_canonical_cell",
                        "n_cell_rows": 0,
                        "n_used": 0,
                        "n_missing": 0,
                        "mean": None,
                        "ci_low": None,
                        "ci_high": None,
                        "warnings": [
                            "n_used=0 canonical empty cell; CI not computed"
                        ],
                    }
                )
            continue

        for metric in METRICS:
            values, n_missing, metric_warnings = _extract_metric(
                rows, metric, pricing=pricing, bill_reasoning=bill_reasoning
            )
            cell_seed = _stable_seed(
                benchmark, model, effort, metric, base_seed=seed
            )
            ci = bootstrap_ci(
                values,
                resamples=resamples,
                ci_level=ci_level,
                seed=cell_seed,
            )
            entry: dict[str, Any] = {
                "benchmark": benchmark,
                "model": model,
                "effort": effort,
                "metric": metric,
                "status": "ok",
                "n_cell_rows": len(rows),
                "n_used": ci["n_used"],
                "n_missing": n_missing,
                "mean": ci["mean"],
                "ci_low": ci["ci_low"],
                "ci_high": ci["ci_high"],
                "warnings": metric_warnings + ci["warnings"],
            }

            # Validate cost mean against committed canonical mean (finding #2).
            if metric == "cost":
                committed = committed_means.get(cell_key)
                if committed is None:
                    entry["cost_validation"] = {
                        "status": "no_reference",
                        "committed_mean_usd": None,
                        "delta": None,
                    }
                else:
                    delta = (
                        None
                        if ci["mean"] is None
                        else round(float(ci["mean"]) - committed, FLOAT_NDIGITS)
                    )
                    ok = delta is not None and abs(delta) <= COST_VALIDATION_TOL
                    entry["cost_validation"] = {
                        "status": "match" if ok else "mismatch",
                        "committed_mean_usd": round(committed, FLOAT_NDIGITS),
                        "delta": delta,
                        "tolerance": COST_VALIDATION_TOL,
                    }
                    if not ok:
                        entry["warnings"].append(
                            "cost mean disagrees with committed analysis.json "
                            f"({ci['mean']} vs {round(committed, FLOAT_NDIGITS)}); "
                            "see cost_validation"
                        )
                        benchmark_warnings.append(
                            f"cost mean mismatch in cell ({model}, {effort})"
                        )

            results.append(entry)

    cost_means_validated = all(
        r.get("cost_validation", {}).get("status") in (None, "match", "no_reference")
        for r in results
        if r["metric"] == "cost"
    )

    payload: dict[str, Any] = {
        "method": {
            "name": "percentile bootstrap",
            "statistic": "mean",
            "resamples": resamples,
            "ci_level": ci_level,
            "resample_with_replacement": True,
            "seed": seed,
            "seed_derivation": "blake2b(seed, benchmark, model, effort, metric)",
            "metrics": list(METRICS),
            "row_population": (
                "outlier_reason is None (matches analysis.json n_used); rows "
                "joined from raw runs/*.json + judge_runs/*.json via "
                "scripts.analyze_tokens.build_analysis"
            ),
            "note": (
                "Supplementary, descriptive 95% CI per revision D-003. This is "
                "bootstrap reporting over N/R-limited authored samples (N=20, "
                "R=3), not a universal inference claim. Main-text reporting "
                "continues to use mean +/- SD per frozen methodology v1.0."
            ),
        },
        "benchmark": benchmark,
        "source": {
            "input_mode": "raw_runs_and_judge_files",
            "runs_dir": _rel(runs_dir),
            "judge_dir": _rel(judge_dir),
            "experiment_prefix": experiment_prefix,
            "experiment_prefix_source": prefix_source,
            "raw_run_file_count": len(raw_run_files),
            "raw_judge_file_count": len(raw_judge_files),
            "included_run_count": included_run_count,
            "parsed_cell_count": len(cells),
            "included_used_row_count": sum(len(v) for v in used_by_cell.values()),
            "excluded_outlier_row_count": n_excluded,
            "skipped_non_cohort_run_files": max(
                len(raw_run_files) - included_run_count, 0
            ),
            "note": (
                "raw_run_file_count counts every *.json in runs/ before cohort "
                "filtering; included_run_count is the canonical authored cohort "
                "(experiment_prefix match). They differ when sibling smoke / "
                "warm-probe runs share the directory."
            ),
        },
        "cost_provenance": {
            "pricing_snapshot": snapshot_rel,
            "pricing_source_url": pricing.source_url,
            "pricing_accessed_date": pricing.accessed_date,
            "formula": (
                "((input - cached) * input_rate + cached * cached_rate + "
                "output * output_rate"
                + (
                    " + reasoning * reasoning_rate) / 1e6"
                    if billing_detail["billing_mode"]
                    == "reasoning_billed_separately"
                    else ") / 1e6"
                )
            ),
            "derived_per_row_from_raw_usage": True,
            "uses_cost_calculator_payg_cost_per_call": False,
            "rationale": (
                "Per-row cost is derived directly so the supplementary means "
                "reproduce this benchmark's committed analysis.json / public "
                "chart means. scripts.cost_calculator.payg_cost_per_call is not "
                "used because, under current code, it bills output-only and "
                "would disagree with benchmark 01's canonical (reasoning-"
                "billed) means."
            ),
            "cost_means_validated_against_committed_analysis": cost_means_validated,
            **billing_detail,
        },
        "skipped_cells": sorted(
            skipped_cells, key=lambda c: _cell_sort((c["model"], c["effort"]))
        ),
        "warnings": benchmark_warnings,
        "results": results,
    }
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bootstrap_ci",
        description=(
            "Compute per-cell 95% bootstrap confidence intervals "
            "(cost / quality / latency / reasoning-token mean) from raw "
            "benchmark runs/*.json + judge_runs/*.json and write "
            "results/supplementary/<benchmark>/bootstrap_ci.json."
        ),
    )
    parser.add_argument(
        "--benchmark",
        action="append",
        dest="benchmarks",
        metavar="NAME",
        help=(
            "Benchmark directory name under --benchmarks-dir (e.g. "
            "'01-short-factual'). Repeatable. Default: every benchmark with a "
            "runs/ directory."
        ),
    )
    parser.add_argument(
        "--benchmarks-dir",
        type=pathlib.Path,
        default=REPO_ROOT / "benchmarks",
        help="Directory containing benchmark subdirectories (default: ./benchmarks).",
    )
    parser.add_argument(
        "--output-dir",
        type=pathlib.Path,
        default=REPO_ROOT / "results" / "supplementary",
        help="Root for output (default: ./results/supplementary).",
    )
    parser.add_argument(
        "--pricing-dir",
        type=pathlib.Path,
        default=REPO_ROOT / "pricing",
        help="Pricing snapshot directory (default: ./pricing).",
    )
    parser.add_argument(
        "--resamples",
        type=int,
        default=10000,
        help="Bootstrap resamples per cell+metric (default: 10000).",
    )
    parser.add_argument(
        "--ci",
        type=float,
        default=0.95,
        dest="ci_level",
        help="Two-sided confidence level (default: 0.95).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260605,
        help="Deterministic base seed (default: 20260605).",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Also print each benchmark payload to stdout (does not skip file write).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not (0.0 < args.ci_level < 1.0):
        parser.error("--ci must be in the open interval (0, 1)")
    if args.resamples < 1:
        parser.error("--resamples must be >= 1")

    benchmarks_dir: pathlib.Path = args.benchmarks_dir
    if not benchmarks_dir.is_dir():
        parser.error(f"benchmarks dir not found: {benchmarks_dir}")

    names, skipped = stats_common.discover_benchmarks(benchmarks_dir, args.benchmarks)
    if not names:
        sys.stderr.write(
            "no benchmarks with a runs/ directory found "
            f"(skipped: {', '.join(skipped) or 'none'})\n"
        )
        return 1

    for benchmark in names:
        payload = compute_benchmark(
            benchmark,
            benchmarks_dir=benchmarks_dir,
            pricing_dir=args.pricing_dir,
            resamples=args.resamples,
            ci_level=args.ci_level,
            seed=args.seed,
        )
        out_path = args.output_dir / benchmark / "bootstrap_ci.json"
        text = stats_common.write_json(out_path, payload)
        if args.stdout:
            sys.stdout.write(text)

        n_cells = len({(r["model"], r["effort"]) for r in payload["results"]})
        n_skipped = len(payload["skipped_cells"])
        sys.stderr.write(
            f"[bootstrap_ci] {benchmark}: {n_cells} cells "
            f"({n_skipped} empty), {len(payload['results'])} cell+metric CIs, "
            f"billing={payload['cost_provenance']['billing_mode']} -> {out_path}\n"
        )

    if skipped:
        sys.stderr.write(
            f"[bootstrap_ci] skipped {len(skipped)} benchmark(s) without "
            f"runs/: {', '.join(skipped)}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
