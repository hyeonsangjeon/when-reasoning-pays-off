"""LLM-judge vs manual spot-check inter-rater agreement (revision Phase 2, T-023 / D-003).

What this does
--------------
For every benchmark, this script reads the **raw** Responses-API run JSONs
(``benchmarks/<name>/runs/*.json``) and judge JSONs
(``benchmarks/<name>/judge_runs/*.json``), joins them exactly as the canonical
aggregator does (via :func:`scripts.analyze_tokens.build_analysis`), and then
attempts to measure inter-rater agreement between the **LLM-judge** quality
score and a **manual spot-check** score over a 10% sample.

The output is written to
``results/supplementary/<benchmark>/inter_rater.json``.

Rubric type and statistic
-------------------------
Per ``benchmarks/02-multi-step-reasoning/README.md`` and
``benchmarks/03-tool-using-agent/README.md`` the quality metric is the
**3-tier judge rubric** ``0 = fail``, ``1 = partial``, ``2 = pass`` — an
**integer ordinal** score, not a continuous graded score. For this corpus the
appropriate agreement statistic is therefore **percent agreement + Cohen's
kappa** (with a supplementary linear-weighted kappa to acknowledge the
ordering), *not* ICC. ICC is only appropriate for a continuous graded rubric;
this corpus has none for the gating quality score, so ICC is reported as
``null`` with that reason recorded in ``method``.

Manual spot-check score discovery
---------------------------------
Manual reviewer scores are looked for, in this order, at these deterministic
locations. **All** existing sources are loaded and their records merged (not
just the first one found); when two sources carry the same
``(sample_id, model, effort, repeat)`` join key, the later source in this order
overrides the earlier one. ``*.json`` globs are concatenated in sorted filename
order, so ``found_paths`` can list more than one contributing source:

* ``benchmarks/<benchmark>/manual_spot_checks.json``
* ``benchmarks/<benchmark>/manual_spot_checks/*.json``
* ``results/supplementary/<benchmark>/manual_spot_checks.json``

Accepted manual-score record format (either a top-level JSON array, or an
object with a ``"scores"`` / ``"records"`` array). Each record must carry the
join key and a reviewer score::

    {"sample_id": "sf_01", "model": "gpt-5.2", "effort": "low",
     "repeat": 0, "reviewer_score": 2}

``effort`` may be ``null`` (gpt-4o baseline); ``reviewer_score`` must be an
integer in ``{0, 1, 2}``.

Missing-data honesty
--------------------
If **no** manual spot-check score file is committed (the current state of this
repository), agreement is **not computable** and this script does **not**
fabricate human scores. It still emits ``inter_rater.json`` per benchmark with
``status = "manual_scores_missing"``, ``percent_agreement`` / ``cohens_kappa``
/ ``icc`` set to ``null`` (with a reason), the accepted paths it checked, the
LLM-judge score count, the ``expected_min_manual_count`` under the 10%
per-non-empty-cell rule, and a deterministic ``manual_review_queue`` listing
enough canonical rows to satisfy that 10% quota per cell (with
``reviewer_score = null`` for a human to fill in). Top-level ``warnings``
states plainly that manual spot-check data is not committed, so inter-rater
agreement is not computable yet.

Determinism
-----------
There is no resampling; all statistics are closed-form and the review-queue
sampling is a fixed even-spaced selection over rows sorted by
``(sample_id, repeat)``. Output is JSON with ``sort_keys=True`` and carries no
wall-clock timestamps, so it is byte-stable across runs (suitable for the CI
reproducibility check, T-064).
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
from typing import Any

# Reuse the canonical raw join (reads runs/*.json + judge_runs/*.json, applies
# the experiment-prefix cohort filter, judge join and operational-outlier
# exclusion) so the kept-row population matches the committed analysis.json and
# the sibling supplementary artifacts (bootstrap_ci.json / cohens_d.json).
from scripts.analyze_tokens import build_analysis
from scripts.stats.bootstrap_ci import (
    FLOAT_NDIGITS,
    REPO_ROOT,
    _cell_sort,
    _rel,
    _resolve_experiment_prefix,
)
from scripts.stats import common as stats_common

# The judge rubric categories for this corpus (integer ordinal 0|1|2).
SCORE_CATEGORIES: tuple[int, ...] = (0, 1, 2)

# Spot-check sampling fraction mandated by the task (10% per non-empty cell).
SAMPLING_FRACTION: float = 0.10

# Deterministic manual-score source locations, checked in this order.
_MANUAL_SCORE_SOURCES: tuple[tuple[str, str], ...] = (
    ("benchmarks/<benchmark>/manual_spot_checks.json", "file"),
    ("benchmarks/<benchmark>/manual_spot_checks/*.json", "glob"),
    ("results/supplementary/<benchmark>/manual_spot_checks.json", "file"),
)

# Landis & Koch (1977) interpretation bands for kappa, evaluated on the raw
# (signed) kappa value.
_KAPPA_BANDS: tuple[tuple[float, str], ...] = (
    (0.0, "poor"),
    (0.20, "slight"),
    (0.40, "fair"),
    (0.60, "moderate"),
    (0.80, "substantial"),
    (1.01, "almost_perfect"),
)


# ----------------------------------------------------------------------------
# Agreement statistics
# ----------------------------------------------------------------------------


def _kappa_interpretation(kappa: float | None) -> str | None:
    """Bin a kappa value into a Landis & Koch (1977) strength label."""
    if kappa is None:
        return None
    for upper, label in _KAPPA_BANDS:
        if kappa < upper:
            return label
    return "almost_perfect"


def cohens_kappa(
    pairs: list[tuple[int, int]],
    *,
    weighted: bool = False,
) -> float | None:
    """Cohen's kappa between two raters over ``(judge, manual)`` score pairs.

    ``weighted`` selects linear (absolute-distance) weighting suitable for the
    ordinal 0|1|2 scale; otherwise unweighted (exact-match) kappa is returned.
    Returns ``None`` when agreement is undefined (no pairs, or perfect expected
    agreement so the ``1 - p_e`` denominator vanishes).
    """
    n = len(pairs)
    if n == 0:
        return None

    cats = SCORE_CATEGORIES
    k = len(cats)
    index = {c: i for i, c in enumerate(cats)}

    observed = [[0 for _ in range(k)] for _ in range(k)]
    for judge, manual in pairs:
        observed[index[judge]][index[manual]] += 1

    row_marg = [sum(observed[i]) / n for i in range(k)]
    col_marg = [sum(observed[i][j] for i in range(k)) / n for j in range(k)]

    if weighted:
        max_dist = k - 1
        weight = [
            [1.0 - abs(cats[i] - cats[j]) / max_dist for j in range(k)]
            for i in range(k)
        ]
    else:
        weight = [[1.0 if i == j else 0.0 for j in range(k)] for i in range(k)]

    p_o = sum(
        weight[i][j] * observed[i][j] / n for i in range(k) for j in range(k)
    )
    p_e = sum(
        weight[i][j] * row_marg[i] * col_marg[j]
        for i in range(k)
        for j in range(k)
    )
    if abs(1.0 - p_e) < 1e-12:
        return None
    return (p_o - p_e) / (1.0 - p_e)


def percent_agreement(pairs: list[tuple[int, int]]) -> float | None:
    """Proportion of ``(judge, manual)`` pairs that match exactly."""
    if not pairs:
        return None
    matches = sum(1 for judge, manual in pairs if judge == manual)
    return matches / len(pairs)


def _confusion_matrix(pairs: list[tuple[int, int]]) -> dict[str, dict[str, int]]:
    """Judge (rows) x manual (cols) count matrix over the 0|1|2 categories."""
    matrix = {
        str(j): {str(m): 0 for m in SCORE_CATEGORIES}
        for j in SCORE_CATEGORIES
    }
    for judge, manual in pairs:
        matrix[str(judge)][str(manual)] += 1
    return matrix


# ----------------------------------------------------------------------------
# Manual spot-check discovery + parsing
# ----------------------------------------------------------------------------


def _manual_paths_for(
    benchmark: str,
    *,
    benchmarks_dir: pathlib.Path,
    output_dir: pathlib.Path,
) -> list[pathlib.Path]:
    """Resolve the accepted manual-score source locations for a benchmark."""
    return [
        benchmarks_dir / benchmark / "manual_spot_checks.json",
        benchmarks_dir / benchmark / "manual_spot_checks",  # glob dir
        output_dir / benchmark / "manual_spot_checks.json",
    ]


def _manual_source_label(
    path: pathlib.Path,
    *,
    output_dir: pathlib.Path,
) -> str:
    """Output-root-invariant repo-relative label for a manual-score source.

    Manual scores discovered under ``output_dir`` are recorded with their
    stable logical location
    (``results/supplementary/<benchmark>/manual_spot_checks.json``) rather than
    the physical path. This keeps ``found_paths`` (and any path-bearing
    warnings) identical whether the report was generated with
    ``--output-dir results/supplementary`` (the committed artifact) or with a
    throwaway ``--output-dir <tmp>`` (the T-064 repro check pre-seed), so the
    byte-for-byte comparison does not spuriously fail on an absolute ``/tmp``
    path. Sources outside ``output_dir`` (the canonical ``benchmarks/`` inputs)
    fall through to the ordinary repo-relative :func:`_rel`.
    """
    resolved = path.resolve()
    out_resolved = output_dir.resolve()
    if resolved.is_relative_to(out_resolved):
        within = resolved.relative_to(out_resolved)
        return str(pathlib.Path("results", "supplementary") / within)
    return _rel(path)


def _normalize_effort(value: Any) -> str | None:
    """Coerce an effort field to ``str`` or ``None`` for join-key parity."""
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text.lower() in {"null", "none", "baseline"}:
        # gpt-4o baseline rows use effort=None; treat the textual 'none' for the
        # gpt-5.2 'none' tier separately (it is a real effort), so only the
        # empty / explicit-null spellings collapse to None here.
        return None if text.lower() in {"", "null"} else text.lower()
    return text


def _records_from_obj(obj: Any) -> list[dict[str, Any]]:
    """Pull manual-score records from a parsed JSON array or wrapper object."""
    if isinstance(obj, list):
        return [r for r in obj if isinstance(r, dict)]
    if isinstance(obj, dict):
        for key in ("scores", "records", "manual_spot_checks"):
            inner = obj.get(key)
            if isinstance(inner, list):
                return [r for r in inner if isinstance(r, dict)]
    return []


def _load_manual_scores(
    benchmark: str,
    *,
    benchmarks_dir: pathlib.Path,
    output_dir: pathlib.Path,
) -> tuple[
    dict[tuple[str, str | None, str | None, int], int],
    list[str],
    list[str],
]:
    """Discover and parse manual spot-check scores for a benchmark.

    Returns ``(scores_by_key, found_paths, warnings)`` where ``scores_by_key``
    maps ``(sample_id, model, effort, repeat)`` to an integer reviewer score in
    ``{0, 1, 2}``. ``found_paths`` lists the repo-relative paths that actually
    contributed records (output-root-invariant: sources discovered under
    ``output_dir`` are recorded as their stable
    ``results/supplementary/<benchmark>/...`` logical path — see
    :func:`_manual_source_label`); an empty list means no manual data is
    committed.
    """
    candidates = _manual_paths_for(
        benchmark, benchmarks_dir=benchmarks_dir, output_dir=output_dir
    )
    scores: dict[tuple[str, str | None, str | None, int], int] = {}
    found: list[str] = []
    warnings: list[str] = []

    source_files: list[pathlib.Path] = []
    for path in candidates:
        if path.name == "manual_spot_checks" and path.is_dir():
            source_files.extend(sorted(path.glob("*.json")))
        elif path.is_file():
            source_files.append(path)

    for path in source_files:
        try:
            with path.open("r", encoding="utf-8") as fh:
                obj = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(
                f"could not read manual scores at "
                f"{_manual_source_label(path, output_dir=output_dir)}: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        records = _records_from_obj(obj)
        if not records:
            warnings.append(
                f"manual score source "
                f"{_manual_source_label(path, output_dir=output_dir)} "
                "held no usable records"
            )
            continue
        contributed = False
        for rec in records:
            sid = rec.get("sample_id")
            model = rec.get("model")
            raw_score = rec.get("reviewer_score", rec.get("manual_score"))
            if sid is None or model is None or raw_score is None:
                warnings.append(
                    f"skipped malformed manual record in "
                    f"{_manual_source_label(path, output_dir=output_dir)}: "
                    "missing sample_id/model/reviewer_score"
                )
                continue
            try:
                score = int(raw_score)
            except (TypeError, ValueError):
                warnings.append(
                    f"skipped manual record in "
                    f"{_manual_source_label(path, output_dir=output_dir)}: "
                    f"reviewer_score {raw_score!r} is not an integer"
                )
                continue
            if score not in SCORE_CATEGORIES:
                warnings.append(
                    f"skipped manual record in "
                    f"{_manual_source_label(path, output_dir=output_dir)}: "
                    f"reviewer_score {score} outside rubric categories "
                    f"{list(SCORE_CATEGORIES)}"
                )
                continue
            effort = _normalize_effort(rec.get("effort"))
            try:
                repeat = int(rec.get("repeat"))
            except (TypeError, ValueError):
                warnings.append(
                    f"skipped manual record in "
                    f"{_manual_source_label(path, output_dir=output_dir)}: "
                    f"repeat {rec.get('repeat')!r} is not an integer"
                )
                continue
            scores[(str(sid), str(model), effort, repeat)] = score
            contributed = True
        if contributed:
            found.append(_manual_source_label(path, output_dir=output_dir))

    return scores, found, warnings


# ----------------------------------------------------------------------------
# Review-queue sampling
# ----------------------------------------------------------------------------


def _even_sample_indices(n: int, k: int) -> list[int]:
    """Pick ``min(k, n)`` evenly-spaced distinct indices over ``[0, n)``.

    Deterministic and representative: for ``k == 1`` returns the first index;
    otherwise spreads picks across the range, de-duplicating any collisions and
    back-filling from the front so the result always has the requested count
    (capped at ``n``).
    """
    if n <= 0 or k <= 0:
        return []
    k = min(k, n)
    if k == 1:
        return [0]
    picks: list[int] = []
    seen: set[int] = set()
    for i in range(k):
        idx = round(i * (n - 1) / (k - 1))
        if idx not in seen:
            seen.add(idx)
            picks.append(idx)
    if len(picks) < k:
        for idx in range(n):
            if idx not in seen:
                seen.add(idx)
                picks.append(idx)
                if len(picks) == k:
                    break
    return sorted(picks)


def _expected_min_manual(n_used: int) -> int:
    """10%-of-cell quota, rounded up, with at least one row for a non-empty cell."""
    if n_used <= 0:
        return 0
    return max(1, math.ceil(SAMPLING_FRACTION * n_used))


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
    output_dir: pathlib.Path,
) -> dict[str, Any]:
    """Compute the inter-rater payload for one benchmark."""
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

    canonical_cells: list[tuple[str, str | None]] = [
        (str(s["model"]), s.get("effort")) for s in cell_stats
    ]
    if not canonical_cells:
        canonical_cells = list(used_by_cell.keys())
    ordered_cells = sorted(set(canonical_cells), key=_cell_sort)

    # Discover committed manual spot-check scores (likely absent).
    manual_scores, manual_found, manual_warnings = _load_manual_scores(
        benchmark, benchmarks_dir=benchmarks_dir, output_dir=output_dir
    )
    benchmark_warnings.extend(manual_warnings)
    accepted_paths = [
        s.replace("<benchmark>", benchmark) for s, _ in _MANUAL_SCORE_SOURCES
    ]

    # Per-cell accounting + global join.
    all_pairs: list[tuple[int, int]] = []
    n_missing_judge = 0
    cell_summaries: list[dict[str, Any]] = []
    skipped_cells: list[dict[str, Any]] = []
    review_queue: list[dict[str, Any]] = []
    judge_score_count = 0
    expected_min_total = 0

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
                        "(model, effort) cell; no judge scores to review"
                    ),
                }
            )
            continue

        # Stable ordering for both the queue and any join.
        ordered_rows = sorted(
            rows,
            key=lambda r: (str(r.get("sample_id")), int(r.get("repeat", 0))),
        )
        n_used = len(ordered_rows)
        score_dist = {str(c): 0 for c in SCORE_CATEGORIES}
        n_cell_missing_judge = 0
        for r in ordered_rows:
            score = r.get("judge_score")
            if score is None:
                n_cell_missing_judge += 1
            else:
                score_dist[str(int(score))] += 1
        judge_score_count += n_used - n_cell_missing_judge
        n_missing_judge += n_cell_missing_judge

        expected_min = _expected_min_manual(n_used)
        expected_min_total += expected_min

        # Cell-level join against any manual scores.
        cell_pairs: list[tuple[int, int]] = []
        for r in ordered_rows:
            jscore = r.get("judge_score")
            key = (
                str(r.get("sample_id")),
                model,
                effort,
                int(r.get("repeat", 0)),
            )
            mscore = manual_scores.get(key)
            if mscore is not None and jscore is not None:
                cell_pairs.append((int(jscore), mscore))
        all_pairs.extend(cell_pairs)

        cell_summaries.append(
            {
                "benchmark": benchmark,
                "model": model,
                "effort": effort,
                "label": _cell_label(model, effort),
                "n_used": n_used,
                "n_missing_judge": n_cell_missing_judge,
                "judge_score_distribution": score_dist,
                "expected_min_manual": expected_min,
                "n_manual_matched": len(cell_pairs),
                "percent_agreement": (
                    round(percent_agreement(cell_pairs), FLOAT_NDIGITS)
                    if cell_pairs
                    else None
                ),
            }
        )

        # Build the deterministic review queue for this cell (rows a human
        # would score to reach the 10% quota). Always emitted so the queue is
        # actionable whether or not manual data already exists.
        idxs = _even_sample_indices(n_used, expected_min)
        for idx in idxs:
            r = ordered_rows[idx]
            key = (
                str(r.get("sample_id")),
                model,
                effort,
                int(r.get("repeat", 0)),
            )
            existing = manual_scores.get(key)
            jscore = r.get("judge_score")
            review_queue.append(
                {
                    "benchmark": benchmark,
                    "model": model,
                    "effort": effort,
                    "sample_id": str(r.get("sample_id")),
                    "repeat": int(r.get("repeat", 0)),
                    "llm_judge_score": None if jscore is None else int(jscore),
                    "reviewer_score": existing,
                    "source_path": _rel(pathlib.Path(str(r.get("source_path")))),
                }
            )

    # Sort the queue deterministically across cells.
    review_queue.sort(
        key=lambda q: (
            *_cell_sort((q["model"], q["effort"])),
            q["sample_id"],
            q["repeat"],
        )
    )

    n_overlap = len(all_pairs)
    n_missing_manual = max(judge_score_count - n_overlap, 0)
    has_manual = bool(manual_found) and n_overlap > 0

    if has_manual:
        status = "computed"
        pa = percent_agreement(all_pairs)
        kappa = cohens_kappa(all_pairs, weighted=False)
        wkappa = cohens_kappa(all_pairs, weighted=True)
        agreement = {
            "status": "computed",
            "n_overlap": n_overlap,
            "n_missing_manual": n_missing_manual,
            "n_missing_judge": n_missing_judge,
            "percent_agreement": (
                None if pa is None else round(pa, FLOAT_NDIGITS)
            ),
            "cohens_kappa": (
                None if kappa is None else round(kappa, FLOAT_NDIGITS)
            ),
            "cohens_kappa_interpretation": _kappa_interpretation(kappa),
            "linear_weighted_kappa": (
                None if wkappa is None else round(wkappa, FLOAT_NDIGITS)
            ),
            "linear_weighted_kappa_interpretation": _kappa_interpretation(
                wkappa
            ),
            "icc": None,
            "icc_reason": (
                "rubric is integer ordinal (0|1|2); ICC is reserved for a "
                "continuous graded rubric, which this corpus does not use for "
                "the gating quality score"
            ),
            "confusion_matrix": _confusion_matrix(all_pairs),
            "method": "cohens_kappa",
            "interpretation_scale": "Landis & Koch 1977",
        }
        if n_overlap < expected_min_total:
            benchmark_warnings.append(
                f"manual overlap n={n_overlap} is below the 10% per-cell quota "
                f"({expected_min_total}); agreement is provisional"
            )
    else:
        status = "manual_scores_missing"
        agreement = {
            "status": "manual_scores_missing",
            "n_overlap": 0,
            "n_missing_manual": judge_score_count,
            "n_missing_judge": n_missing_judge,
            "percent_agreement": None,
            "cohens_kappa": None,
            "cohens_kappa_interpretation": None,
            "linear_weighted_kappa": None,
            "linear_weighted_kappa_interpretation": None,
            "icc": None,
            "icc_reason": (
                "rubric is integer ordinal (0|1|2); ICC would only apply to a "
                "continuous graded rubric, which this corpus does not use"
            ),
            "confusion_matrix": _confusion_matrix([]),
            "method": "cohens_kappa",
            "interpretation_scale": "Landis & Koch 1977",
            "reason": (
                "no committed manual spot-check score file was found at any "
                "accepted location, so judge-vs-human agreement is not "
                "computable; populate one of the accepted paths and re-run"
            ),
        }
        benchmark_warnings.insert(
            0,
            "manual spot-check score data is NOT committed for this benchmark, "
            "so inter-rater agreement (percent agreement / Cohen's kappa) is "
            "not computable yet; this is a methodology gap, not a result. A "
            "deterministic manual_review_queue of "
            f"{len(review_queue)} rows (10% per non-empty cell) is provided "
            "for a reviewer to fill in.",
        )

    payload: dict[str, Any] = {
        "benchmark": benchmark,
        "status": status,
        "method": {
            "name": "judge-vs-human inter-rater agreement",
            "rubric_type": "ordinal_3tier",
            "score_scale": list(SCORE_CATEGORIES),
            "score_scale_meaning": {
                "0": "fail",
                "1": "partial",
                "2": "pass",
            },
            "rubric_source": (
                "benchmarks/<name>/README.md 'Quality metric' section: "
                "binarized 3-tier judge rubric (0=fail, 1=partial, 2=pass)"
            ),
            "primary_statistic": "cohens_kappa",
            "supplementary_statistics": [
                "percent_agreement",
                "linear_weighted_kappa",
            ],
            "icc_used": False,
            "icc_rationale": (
                "ICC is reserved for a continuous graded rubric; the gating "
                "quality score here is integer ordinal 0|1|2, so Cohen's kappa "
                "(exact-match) is primary with a linear-weighted kappa to "
                "respect the ordering"
            ),
            "sampling_rule": (
                "manual spot-check covers >=10% of judge scores per non-empty "
                "(model, effort) cell (ceil, >=1)"
            ),
            "agreement_definition": (
                "percent_agreement = fraction of overlapping (judge, manual) "
                "pairs with identical scores; kappa corrects for chance using "
                "the observed marginal score distributions"
            ),
            "interpretation_scale": "Landis & Koch 1977",
            "row_population": (
                "outlier_reason is None (matches analysis.json n_used); rows "
                "joined from raw runs/*.json + judge_runs/*.json via "
                "scripts.analyze_tokens.build_analysis"
            ),
            "manual_join_key": ["sample_id", "model", "effort", "repeat"],
            "note": (
                "Supplementary, descriptive inter-rater reporting per revision "
                "D-003. Manual scores are loaded from committed files only; no "
                "human scores are ever synthesized."
            ),
        },
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
        "manual_spot_checks": {
            "status": status,
            "sampling_fraction": SAMPLING_FRACTION,
            "accepted_manual_score_paths_checked": accepted_paths,
            "accepted_record_format": (
                "JSON array (or object with a 'scores'/'records' array) of "
                "{sample_id, model, effort, repeat, reviewer_score} with "
                "reviewer_score in {0,1,2}; effort may be null for the gpt-4o "
                "baseline"
            ),
            "found_paths": sorted(manual_found),
            "manual_score_record_count": len(manual_scores),
        },
        "judge_score_count": judge_score_count,
        "expected_min_manual_count": expected_min_total,
        "agreement": agreement,
        "cells": cell_summaries,
        "skipped_cells": sorted(
            skipped_cells,
            key=lambda c: _cell_sort((c["model"], c["effort"])),
        ),
        "manual_review_queue": review_queue,
        "warnings": benchmark_warnings,
    }
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inter_rater",
        description=(
            "Compute LLM-judge vs manual spot-check inter-rater agreement "
            "(percent agreement + Cohen's kappa for the ordinal 0|1|2 judge "
            "rubric) per benchmark from raw runs/*.json + judge_runs/*.json. "
            "Manual scores are read from committed files only; when none "
            "exist the script emits a missing-data report plus a deterministic "
            "10%-per-cell manual_review_queue instead of fabricating scores. "
            "Output: results/supplementary/<benchmark>/inter_rater.json. "
            "Accepted manual-score locations: "
            "benchmarks/<benchmark>/manual_spot_checks.json, "
            "benchmarks/<benchmark>/manual_spot_checks/*.json, "
            "results/supplementary/<benchmark>/manual_spot_checks.json."
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
            output_dir=args.output_dir,
        )
        out_path = args.output_dir / benchmark / "inter_rater.json"
        text = stats_common.write_json(out_path, payload)
        if args.stdout:
            sys.stdout.write(text)

        sys.stderr.write(
            f"[inter_rater] {benchmark}: status={payload['status']}, "
            f"judge_scores={payload['judge_score_count']}, "
            f"overlap={payload['agreement']['n_overlap']}, "
            f"expected_min_manual={payload['expected_min_manual_count']}, "
            f"queue={len(payload['manual_review_queue'])} -> {out_path}\n"
        )

    if skipped:
        sys.stderr.write(
            f"[inter_rater] skipped {len(skipped)} benchmark(s) without "
            f"runs/: {', '.join(skipped)}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
