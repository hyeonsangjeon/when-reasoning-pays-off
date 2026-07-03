"""Pairwise effort effect sizes per benchmark (revision Phase 2, T-022 / D-003).

What this does
--------------
For every benchmark, this script reads the **raw** Responses-API run JSONs
(``benchmarks/<name>/runs/*.json``) and judge JSONs
(``benchmarks/<name>/judge_runs/*.json``), joins them exactly as the canonical
aggregator does (via :func:`scripts.analyze_tokens.build_analysis`), and computes
a pairwise effect size between every pair of non-empty canonical
``(model, effort)`` cells within the benchmark, for four per-cell metrics:

* ``cost``             — USD per request (PAYG; see "Cost provenance" below)
* ``quality``          — LLM-judge score (0|1|2)
* ``latency``          — wall-clock latency in milliseconds
* ``reasoning_tokens`` — reasoning-token count (0 for gpt-4o)

The output is written to
``results/supplementary/<benchmark>/cohens_d.json``.

Why a nonparametric effect size (the ``method`` field)
------------------------------------------------------
The revision task (T-022) requires Cohen's *d* "where it is reliable" and a
"suitable nonparametric alternative (e.g. Cliff's delta) instead" where the
small authored sample makes *d* unreliable — recording which was used via a
per-row ``method`` field.

This corpus has **N=20 authored samples** measured under **R=3 repeats**, so a
cell's ~60 raw rows are *not* 60 independent observations: the repeats are
pseudoreplicates of the same authored prompt. To report an honest,
distribution-free effect size we:

1. Aggregate each cell to **one value per authored sample** (the mean over its
   repeats), yielding ~20 independent units per cell. This is the ``N=20`` the
   task refers to and removes the R=3 pseudoreplication.
2. Select the method per ``(pair, metric)`` with a documented rule
   (:data:`COHENS_D_MIN_N`): Cohen's *d* is only used when the effective
   independent N clears the rule-of-thumb reliability threshold **and** the
   metric is continuous **and** the pooled variance is non-degenerate.
   Otherwise — which, at N=20, is **every** comparison here, and always for the
   ordinal/ties-heavy ``quality`` score — we emit Cliff's delta. The ``method``
   field records the choice on every row.

Cliff's delta is the right default for this corpus: it is a rank/dominance
statistic, makes no normality assumption, and is well-defined on the ordinal,
ceiling-prone judge scores (heavy ties) where a mean/SD-based *d* is
ill-behaved. Per D-003 this is **supplementary, descriptive** reporting; the
main text continues to use mean +/- SD per frozen methodology v1.0.

Direction convention
--------------------
Pairs are formed in canonical cell order (gpt-4o baseline first, then gpt-5.2
by ascending effort). For a pair ``(A, B)`` a **positive** effect size means
cell **B** tends to have the larger metric value (B > A). ``comparison_kind``
labels each pair as a within-gpt-5.2 effort contrast or a gpt-4o-baseline
contrast so consumers can filter the baseline out.

Cost provenance
---------------
Cost is recomputed per raw row exactly as in T-021 (``bootstrap_ci.py``): the
benchmark's billing mode (reasoning-billed vs output-only) is detected by
reproducing the committed ``analysis.json`` ``mean_usd_per_request``, and every
cell's cost mean is validated against that committed mean. The detection,
formula and validation outcome are recorded under ``cost_provenance`` and each
cell's ``cost_validation``. The shared cost/cohort helpers are imported from
:mod:`scripts.stats.bootstrap_ci` so the two supplementary artifacts agree.

Determinism
-----------
There is no resampling; all statistics are closed-form. Output is JSON with
``sort_keys=True`` and carries no wall-clock timestamps, so it is byte-stable
across runs (suitable for the CI reproducibility check, T-064).
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from typing import Any

import numpy as np

# Reuse the canonical raw join (reads runs/*.json + judge_runs/*.json, applies
# the experiment-prefix cohort filter, judge join and operational-outlier
# exclusion) so the kept-row population matches the committed analysis.json.
from scripts.analyze_tokens import build_analysis
from scripts.cost_calculator import load_payg_pricing, resolve_active_snapshot

# Shared cost-provenance and cohort helpers from T-021 keep the two
# supplementary artifacts (bootstrap_ci.json / cohens_d.json) consistent.
from scripts.stats.bootstrap_ci import (
    COST_VALIDATION_TOL,
    FLOAT_NDIGITS,
    METRICS,
    REPO_ROOT,
    _cell_sort,
    _committed_cost_means,
    _detect_billing_mode,
    _rel,
    _resolve_experiment_prefix,
    _row_cost_usd,
)
from scripts.stats import common as stats_common

# Rule-of-thumb minimum independent-sample count below which Cohen's d (a
# normal-theory, mean/SD statistic) is treated as unreliable for a cell. The
# common guidance is N>=30 per group for the sampling distribution of the mean
# to be stable; this corpus has N=20 authored samples, so the nonparametric
# branch is taken throughout. Continuous metrics only — the ordinal quality
# score always uses the rank-based statistic regardless of N.
COHENS_D_MIN_N: int = 30

# Metrics whose values are continuous enough for a mean/SD effect size; the
# judge ``quality`` score is ordinal (0|1|2, heavy ties) and is excluded.
CONTINUOUS_METRICS: frozenset[str] = frozenset(
    {"cost", "latency", "reasoning_tokens"}
)

# Cohen's d magnitude bins (Cohen 1988), on |d|.
_COHENS_D_THRESHOLDS: dict[str, float] = {
    "negligible": 0.2,
    "small": 0.5,
    "medium": 0.8,
}

# Cliff's delta magnitude bins (Romano et al. 2006), on |delta|.
_CLIFFS_DELTA_THRESHOLDS: dict[str, float] = {
    "negligible": 0.147,
    "small": 0.33,
    "medium": 0.474,
}


# ----------------------------------------------------------------------------
# Effect-size core
# ----------------------------------------------------------------------------


def _magnitude(value: float, thresholds: dict[str, float]) -> str:
    """Bin ``|value|`` into negligible/small/medium/large by ``thresholds``."""
    mag = abs(value)
    if mag < thresholds["negligible"]:
        return "negligible"
    if mag < thresholds["small"]:
        return "small"
    if mag < thresholds["medium"]:
        return "medium"
    return "large"


def cliffs_delta(a: list[float], b: list[float]) -> float:
    """Cliff's delta of ``b`` relative to ``a``.

    ``delta = (#(b > a) - #(b < a)) / (n_a * n_b)`` over all cross pairs, so a
    positive value means ``b`` dominates ``a``. Ties contribute zero. Range is
    ``[-1, 1]``; ``0`` means complete stochastic overlap.
    """
    arr_a = np.asarray(a, dtype=float)
    arr_b = np.asarray(b, dtype=float)
    diff = arr_b[:, None] - arr_a[None, :]
    greater = int((diff > 0).sum())
    lesser = int((diff < 0).sum())
    denom = arr_a.size * arr_b.size
    if denom == 0:
        return 0.0
    return (greater - lesser) / denom


def cohens_d(a: list[float], b: list[float]) -> float | None:
    """Cohen's d of ``b`` relative to ``a`` using pooled SD (ddof=1).

    Positive means ``b`` has the larger mean. Returns ``None`` when the pooled
    variance is zero (no spread in either group) so the caller can fall back to
    the rank-based statistic instead of dividing by zero.
    """
    arr_a = np.asarray(a, dtype=float)
    arr_b = np.asarray(b, dtype=float)
    n_a = arr_a.size
    n_b = arr_b.size
    if n_a < 2 or n_b < 2:
        return None
    var_a = float(arr_a.var(ddof=1))
    var_b = float(arr_b.var(ddof=1))
    pooled_var = ((n_a - 1) * var_a + (n_b - 1) * var_b) / (n_a + n_b - 2)
    if pooled_var <= 0.0:
        return None
    return (float(arr_b.mean()) - float(arr_a.mean())) / (pooled_var**0.5)


# ----------------------------------------------------------------------------
# Per-cell, per-metric independent-sample extraction
# ----------------------------------------------------------------------------


def _cell_metric_samples(
    rows: list[dict[str, Any]],
    metric: str,
    *,
    pricing: Any,
    bill_reasoning: bool,
) -> tuple[list[float], int, int, list[str]]:
    """Aggregate a cell's used rows to one value per authored sample.

    Returns ``(per_sample_values, n_rows_used, n_missing_rows, warnings)``.
    Each authored ``sample_id`` contributes the mean of its repeat-level
    values, collapsing the R=3 pseudoreplicates into one independent unit.
    Values are ordered by ``sample_id`` for determinism.
    """
    by_sample: dict[str, list[float]] = {}
    n_rows_used = 0
    n_missing = 0
    warnings: list[str] = []

    for row in rows:
        sid = str(row.get("sample_id"))
        if metric == "cost":
            try:
                value: float | None = float(
                    _row_cost_usd(row, pricing, bill_reasoning=bill_reasoning)
                )
            except Exception as exc:  # data-integrity / missing token field
                value = None
                warnings.append(
                    f"cost skipped for sample={sid!r} "
                    f"repeat={row.get('repeat')}: {type(exc).__name__}: {exc}"
                )
        elif metric == "quality":
            score = row.get("judge_score")
            value = None if score is None else float(score)
        elif metric == "latency":
            lat = row.get("latency_ms")
            value = None if lat is None else float(lat)
        elif metric == "reasoning_tokens":
            rt = row.get("reasoning_tokens")
            value = None if rt is None else float(rt)
        else:  # pragma: no cover - guarded by METRICS
            raise ValueError(f"unknown metric: {metric!r}")

        if value is None:
            n_missing += 1
            continue
        by_sample.setdefault(sid, []).append(value)
        n_rows_used += 1

    per_sample = [
        float(np.mean(by_sample[sid])) for sid in sorted(by_sample)
    ]
    if n_missing:
        warnings.append(
            f"{metric}: {n_missing} of {len(rows)} used rows missing this field"
        )
    return per_sample, n_rows_used, n_missing, warnings


def _summary_stats(values: list[float]) -> dict[str, float | None]:
    """Mean / median / SD (ddof=1) over per-sample values, rounded."""
    if not values:
        return {"mean": None, "median": None, "sd": None}
    arr = np.asarray(values, dtype=float)
    sd = float(arr.std(ddof=1)) if arr.size >= 2 else 0.0
    return {
        "mean": round(float(arr.mean()), FLOAT_NDIGITS),
        "median": round(float(np.median(arr)), FLOAT_NDIGITS),
        "sd": round(sd, FLOAT_NDIGITS),
    }


# ----------------------------------------------------------------------------
# Method selection
# ----------------------------------------------------------------------------


def _select_method(
    metric: str,
    values_a: list[float],
    values_b: list[float],
) -> tuple[str, float | None, dict[str, Any], list[str]]:
    """Pick the effect-size method for one pair+metric and compute it.

    Cohen's d is used only when every condition holds: the effective
    independent N (min of the two per-sample counts) clears
    :data:`COHENS_D_MIN_N`, the metric is continuous, and the pooled variance
    is non-degenerate. Otherwise Cliff's delta is used. Returns
    ``(method, effect_size, selection_detail, warnings)``.
    """
    n_a = len(values_a)
    n_b = len(values_b)
    effective_n = min(n_a, n_b)
    metric_type = "continuous" if metric in CONTINUOUS_METRICS else "ordinal"
    warnings: list[str] = []

    n_ok = effective_n >= COHENS_D_MIN_N
    continuous = metric in CONTINUOUS_METRICS
    reasons: list[str] = []
    if not n_ok:
        reasons.append(
            f"effective independent N={effective_n} < {COHENS_D_MIN_N} "
            "(N=20 authored samples, R=3 repeats collapsed): Cohen's d "
            "mean/SD assumptions are unreliable"
        )
    if not continuous:
        reasons.append(
            f"metric {metric!r} is ordinal (judge score 0|1|2 with heavy "
            "ties): a rank/dominance statistic is appropriate"
        )

    if n_ok and continuous:
        d = cohens_d(values_a, values_b)
        if d is None:
            reasons.append(
                "pooled variance is degenerate (zero spread): Cohen's d "
                "undefined, fell back to rank statistic"
            )
        else:
            detail = {
                "method": "cohens_d",
                "effective_n": effective_n,
                "min_n_for_cohens_d": COHENS_D_MIN_N,
                "metric_type": metric_type,
                "reason": (
                    "sufficient independent N and continuous metric with "
                    "non-degenerate variance"
                ),
                "scale": "Cohen 1988",
                "thresholds": dict(_COHENS_D_THRESHOLDS),
            }
            return "cohens_d", round(d, FLOAT_NDIGITS), detail, warnings

    delta = cliffs_delta(values_a, values_b)
    warnings.append(
        "supplementary nonparametric effect size over N=20 authored samples "
        "(R=3 repeats collapsed to per-sample means); descriptive only per "
        "D-003, not a universal inference claim"
    )
    detail = {
        "method": "cliffs_delta",
        "effective_n": effective_n,
        "min_n_for_cohens_d": COHENS_D_MIN_N,
        "metric_type": metric_type,
        "reason": "; ".join(reasons) or "nonparametric default",
        "scale": "Romano et al. 2006",
        "thresholds": dict(_CLIFFS_DELTA_THRESHOLDS),
    }
    return "cliffs_delta", round(delta, FLOAT_NDIGITS), detail, warnings


# ----------------------------------------------------------------------------
# Per-benchmark driver
# ----------------------------------------------------------------------------


def _cell_label(model: str, effort: str | None) -> str:
    """Human-readable ``model/effort`` label (``effort=None`` -> ``baseline``)."""
    return f"{model}/{effort if effort is not None else 'baseline'}"


def compute_benchmark(
    benchmark: str,
    *,
    benchmarks_dir: pathlib.Path,
    pricing_dir: pathlib.Path,
) -> dict[str, Any]:
    """Compute the pairwise effect-size payload for one benchmark."""
    bench_root = benchmarks_dir / benchmark
    runs_dir = bench_root / "runs"
    judge_dir = bench_root / "judge_runs"
    dataset_path = bench_root / "dataset.json"

    experiment_prefix, prefix_source = _resolve_experiment_prefix(
        benchmark, bench_root
    )
    benchmark_warnings: list[str] = []

    raw_run_files = sorted(runs_dir.glob("*.json")) if runs_dir.is_dir() else []
    raw_judge_files = (
        sorted(judge_dir.glob("*.json")) if judge_dir.is_dir() else []
    )

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

    used_by_cell: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    n_excluded = 0
    for row in cells:
        if row.get("outlier_reason") is None:
            used_by_cell.setdefault(
                (str(row["model"]), row.get("effort")), []
            ).append(row)
        else:
            n_excluded += 1

    snapshot_path = resolve_active_snapshot(
        kind="payg", target_date=None, pricing_dir=pricing_dir
    )
    pricing = load_payg_pricing(snapshot_path)
    snap = pathlib.Path(pricing.snapshot_path)
    snapshot_rel = (
        _rel(snap) if not snap.is_absolute() or snap.exists() else str(snap)
    )

    committed_means = _committed_cost_means(bench_root)
    if committed_means:
        bill_reasoning, billing_detail = _detect_billing_mode(
            used_by_cell, committed_means, pricing
        )
    else:
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

    canonical_cells: list[tuple[str, str | None]] = [
        (str(s["model"]), s.get("effort")) for s in cell_stats
    ]
    if not canonical_cells:
        canonical_cells = list(used_by_cell.keys())
    ordered_cells = sorted(set(canonical_cells), key=_cell_sort)

    # Per-cell, per-metric independent-sample values + per-cell summaries.
    cell_values: dict[
        tuple[str, str | None], dict[str, list[float]]
    ] = {}
    cell_summaries: list[dict[str, Any]] = []
    skipped_cells: list[dict[str, Any]] = []
    nonempty_cells: list[tuple[str, str | None]] = []

    for cell_key in ordered_cells:
        model, effort = cell_key
        rows = used_by_cell.get(cell_key, [])
        if not rows:
            skipped_cells.append(
                {
                    "benchmark": benchmark,
                    "model": model,
                    "effort": effort,
                    "n_used": 0,
                    "status": "empty_canonical_cell",
                    "reason": (
                        "canonical analysis reports n_used=0 for this "
                        "(model, effort) cell; no observations for an "
                        "effect-size comparison"
                    ),
                }
            )
            continue

        nonempty_cells.append(cell_key)
        per_metric: dict[str, list[float]] = {}
        metric_stats: dict[str, Any] = {}
        n_rows_cell = len(rows)
        n_samples_cell = 0
        cost_validation: dict[str, Any] | None = None

        for metric in METRICS:
            values, _n_rows_used, n_missing, mwarn = _cell_metric_samples(
                rows, metric, pricing=pricing, bill_reasoning=bill_reasoning
            )
            per_metric[metric] = values
            n_samples_cell = max(n_samples_cell, len(values))
            stats = _summary_stats(values)
            stats["n_samples"] = len(values)
            stats["n_missing_rows"] = n_missing
            if mwarn:
                stats["warnings"] = mwarn
            metric_stats[metric] = stats

            if metric == "cost":
                committed = committed_means.get(cell_key)
                if committed is None:
                    cost_validation = {
                        "status": "no_reference",
                        "committed_mean_usd": None,
                        "delta": None,
                    }
                else:
                    cell_cost_mean = (
                        sum(
                            _row_cost_usd(
                                r, pricing, bill_reasoning=bill_reasoning
                            )
                            for r in rows
                        )
                        / len(rows)
                    )
                    delta = round(cell_cost_mean - committed, FLOAT_NDIGITS)
                    ok = abs(delta) <= COST_VALIDATION_TOL
                    cost_validation = {
                        "status": "match" if ok else "mismatch",
                        "committed_mean_usd": round(committed, FLOAT_NDIGITS),
                        "delta": delta,
                        "tolerance": COST_VALIDATION_TOL,
                    }
                    if not ok:
                        benchmark_warnings.append(
                            f"cost mean mismatch in cell ({model}, {effort})"
                        )

        cell_values[cell_key] = per_metric
        cell_summaries.append(
            {
                "benchmark": benchmark,
                "model": model,
                "effort": effort,
                "label": _cell_label(model, effort),
                "n_rows": n_rows_cell,
                "n_samples": n_samples_cell,
                "cost_validation": cost_validation,
                "metric_stats": metric_stats,
            }
        )

    cost_means_validated = all(
        (s["cost_validation"] or {}).get("status")
        in (None, "match", "no_reference")
        for s in cell_summaries
    )

    # Pairwise comparisons over non-empty cells, deterministic order.
    comparisons: list[dict[str, Any]] = []
    for i in range(len(nonempty_cells)):
        for j in range(i + 1, len(nonempty_cells)):
            cell_a = nonempty_cells[i]
            cell_b = nonempty_cells[j]
            model_a, effort_a = cell_a
            model_b, effort_b = cell_b
            comparison_kind = (
                "within_gpt52_effort"
                if model_a == model_b
                else "gpt4o_baseline_vs_gpt52"
            )
            for metric in METRICS:
                values_a = cell_values[cell_a][metric]
                values_b = cell_values[cell_b][metric]
                method, effect, detail, warns = _select_method(
                    metric, values_a, values_b
                )
                thresholds = detail["thresholds"]
                if effect is None:
                    magnitude = None
                    direction = "undefined"
                else:
                    magnitude = _magnitude(effect, thresholds)
                    if effect > 0:
                        direction = "b_greater"
                    elif effect < 0:
                        direction = "a_greater"
                    else:
                        direction = "equal"
                stats_a = _summary_stats(values_a)
                stats_b = _summary_stats(values_b)
                comparisons.append(
                    {
                        "benchmark": benchmark,
                        "metric": metric,
                        "comparison_kind": comparison_kind,
                        "label": (
                            f"{_cell_label(model_a, effort_a)} vs "
                            f"{_cell_label(model_b, effort_b)}"
                        ),
                        "model_a": model_a,
                        "effort_a": effort_a,
                        "model_b": model_b,
                        "effort_b": effort_b,
                        "method": method,
                        "effect_size": effect,
                        "magnitude": magnitude,
                        "direction": direction,
                        "direction_convention": (
                            "positive effect_size => cell B (effort_b) tends "
                            "to the larger metric value"
                        ),
                        "n_a": len(values_a),
                        "n_b": len(values_b),
                        "n_rows_a": len(used_by_cell.get(cell_a, [])),
                        "n_rows_b": len(used_by_cell.get(cell_b, [])),
                        "mean_a": stats_a["mean"],
                        "mean_b": stats_b["mean"],
                        "median_a": stats_a["median"],
                        "median_b": stats_b["median"],
                        "method_selection": detail,
                        "warnings": warns,
                    }
                )

    payload: dict[str, Any] = {
        "method": {
            "name": "pairwise effect size",
            "primary_statistic": "cliffs_delta",
            "alternate_statistic": "cohens_d",
            "selection_rule": (
                "Cohen's d only when effective independent N >= "
                f"{COHENS_D_MIN_N}, the metric is continuous, and pooled "
                "variance is non-degenerate; otherwise Cliff's delta. The "
                "per-comparison 'method' field records the choice."
            ),
            "independent_unit": (
                "one authored sample (mean over its R repeats); the ~60 raw "
                "rows per cell are R=3 pseudoreplicates of N=20 samples"
            ),
            "metrics": list(METRICS),
            "continuous_metrics": sorted(CONTINUOUS_METRICS),
            "cohens_d_thresholds": dict(_COHENS_D_THRESHOLDS),
            "cliffs_delta_thresholds": dict(_CLIFFS_DELTA_THRESHOLDS),
            "direction_convention": (
                "pairs in canonical cell order (gpt-4o baseline, then gpt-5.2 "
                "by ascending effort); positive effect_size => cell B > cell A"
            ),
            "row_population": (
                "outlier_reason is None (matches analysis.json n_used); rows "
                "joined from raw runs/*.json + judge_runs/*.json via "
                "scripts.analyze_tokens.build_analysis"
            ),
            "note": (
                "Supplementary, descriptive effect sizes per revision D-003. "
                "At N=20 authored samples (R=3 repeats) Cohen's d is treated "
                "as unreliable, so Cliff's delta is reported throughout and "
                "for the ordinal judge score always. Main-text reporting "
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
            "included_used_row_count": sum(
                len(v) for v in used_by_cell.values()
            ),
            "excluded_outlier_row_count": n_excluded,
            "skipped_non_cohort_run_files": max(
                len(raw_run_files) - included_run_count, 0
            ),
            "note": (
                "raw_run_file_count counts every *.json in runs/ before cohort "
                "filtering; included_run_count is the canonical authored "
                "cohort (experiment_prefix match). They differ when sibling "
                "smoke / warm-probe runs share the directory."
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
                "chart means, matching scripts.stats.bootstrap_ci (T-021). "
                "scripts.cost_calculator.payg_cost_per_call is not used "
                "because, under current code, it bills output-only and would "
                "disagree with benchmark 01's canonical (reasoning-billed) "
                "means."
            ),
            "cost_means_validated_against_committed_analysis": (
                cost_means_validated
            ),
            **billing_detail,
        },
        "cells": cell_summaries,
        "skipped_cells": sorted(
            skipped_cells,
            key=lambda c: _cell_sort((c["model"], c["effort"])),
        ),
        "comparisons": comparisons,
        "warnings": benchmark_warnings,
    }
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cohens_d",
        description=(
            "Compute pairwise (model, effort) effect sizes (Cohen's d where "
            "reliable, else Cliff's delta) for cost / quality / latency / "
            "reasoning-token metrics from raw benchmark runs/*.json + "
            "judge_runs/*.json and write "
            "results/supplementary/<benchmark>/cohens_d.json."
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
        help="Directory with benchmark subdirectories (default: ./benchmarks).",
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
        "--stdout",
        action="store_true",
        help=(
            "Also print each benchmark payload to stdout (does not skip the "
            "file write)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

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
        )
        out_path = args.output_dir / benchmark / "cohens_d.json"
        text = stats_common.write_json(out_path, payload)
        if args.stdout:
            sys.stdout.write(text)

        methods = {c["method"] for c in payload["comparisons"]}
        sys.stderr.write(
            f"[cohens_d] {benchmark}: {len(payload['cells'])} cells "
            f"({len(payload['skipped_cells'])} empty), "
            f"{len(payload['comparisons'])} comparisons, "
            f"method(s)={','.join(sorted(methods)) or 'none'} -> {out_path}\n"
        )

    if skipped:
        sys.stderr.write(
            f"[cohens_d] skipped {len(skipped)} benchmark(s) without "
            f"runs/: {', '.join(skipped)}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
