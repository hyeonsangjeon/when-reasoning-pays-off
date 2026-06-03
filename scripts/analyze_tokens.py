"""analyze_tokens.py — pure / offline aggregator that turns a benchmark's raw
Responses-API run JSONs (plus its judge-run JSONs) into a deterministic
``analysis.json``.

Contract (.internal/tasks/008-analysis-pipeline.md):

* **Pure / offline.** This module MUST NOT open a network socket. It reads only
  local files under ``benchmarks/<name>/runs/`` and ``benchmarks/<name>/judge_runs/``
  plus the pricing snapshot via ``scripts.cost_calculator``.
* **Byte-stable.** ``analysis.json`` is JSON with ``sort_keys=True``,
  ``indent=2``, ``ensure_ascii=False``, and all aggregated floats rounded to
  six decimal places — so two consecutive runs over the same inputs produce
  byte-identical output.
* **Foundry v1 Responses schema invariants.** ``usage.input_tokens_details.cached_tokens``
  and ``usage.output_tokens_details.reasoning_tokens`` are the only accepted
  paths. The legacy Chat-Completions / older-Responses field names
  ``prompt_tokens_details.cached_tokens`` and
  ``completion_tokens_details.reasoning_tokens`` are **forbidden** — encounter
  raises ``LegacySchemaError`` (the runner owns translation, not this layer).
* **Outliers** flagged ONLY on operational events: ``cold_start | retry_count>0
  | truncated_output``. Quality outcomes (e.g. a partial judge score) are
  measurement *results* and MUST NOT be used as outlier exclusion criteria.
* **Statistics**: mean + sample standard deviation (``statistics.stdev``,
  ddof=1) — never CI, never SEM (methodology §8 caveat: N=20, R=3, authored
  samples).
* **Cost**: every USD figure originates from ``payg_cost_per_call`` and
  propagates the ``CostBreakdown`` citation fields (``pricing_source_url``,
  ``pricing_accessed_date``, ``pricing_snapshot_path``,
  ``pricing_archive_url``).

CLI::

    python -m scripts.analyze_tokens \
        --benchmark 01-short-factual \
        --out benchmarks/01-short-factual/analysis.json
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import statistics
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from scripts.cost_calculator import (
    PaygPricing,
    TokenUsage,
    load_payg_pricing,
    payg_cost_per_call,
    resolve_active_snapshot,
)

__all__ = [
    "CANONICAL_EFFORT_ORDER",
    "CellEvent",
    "CellRecord",
    "CellStats",
    "JudgeRecord",
    "LegacySchemaError",
    "MeasurementSchemaError",
    "build_analysis",
    "compute_cell_stats",
    "flag_outliers",
    "load_judge_records",
    "load_run_record",
    "load_run_records",
    "main",
    "peek_experiment_id",
]

logger = logging.getLogger("scripts.analyze_tokens")

CANONICAL_EFFORT_ORDER: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh")
# The historical schema was ("minimal", "low", "medium", "high", "xhigh") — the
# 5-tier scaffold authored before Task 006 discovered that the live Foundry v1
# gpt-5.2 deployment rejects ``minimal`` with HTTP 400 and emits the actual
# accepted set ``{none, low, medium, high, xhigh}`` in the error payload.
# Task 009 wired the production schema (``none|low|medium|high|xhigh``) end-to-
# end through the runner and judges; the analyzer now accepts both the legacy
# ``minimal`` cohorts (Task 008 fixtures under ``exp008_short-factual_fixture``)
# and the production ``none`` cohorts (Task 007 + Task 009 real runs) by
# expanding CANONICAL_EFFORT_ORDER to a union of both. Cohorts that only carry
# one of the two lowest-tier names render the other as an empty cell row
# (n_used == 0). Plot consumers tolerate empty rows.
# Default cohort prefix for ``benchmarks/01-short-factual/runs/``. The legacy
# pre-Task-008 production cohort ``exp001_short-factual_baseline`` used a
# 4-effort schema (``none|low|medium|high|xhigh``) that fails the strict
# 5-tier Task 008 validator; the Task 008 fixtures intentionally live under a
# distinct ID so the two cohorts can coexist in the same runs/ directory
# without cross-contamination. Operators re-running ``analyze_tokens`` against
# fresh Task 007 measurements (after the 5-effort upgrade) pass
# ``--experiment-prefix exp001_short-factual_baseline`` explicitly.
DEFAULT_EXPERIMENT_PREFIX: str = "exp008_short-factual_fixture"
FLOAT_NDIGITS: int = 6


# ----------------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------------


class MeasurementSchemaError(ValueError):
    """Raised when a raw run JSON violates the Foundry v1 Responses schema
    invariants (missing required usage paths; gpt-4o emitting reasoning_tokens;
    gpt-5.2 missing the reasoning_tokens field; unknown effort tier)."""


class LegacySchemaError(MeasurementSchemaError):
    """Raised when a raw run JSON carries legacy Chat-Completions /
    older-Responses field names (``prompt_tokens_details`` or
    ``completion_tokens_details``). The runner owns translation; this
    analysis pipeline trusts and validates the Foundry v1 shape."""


# ----------------------------------------------------------------------------
# Data classes — internal, frozen for determinism
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class CellEvent:
    """Operational instrumentation flags for one measurement cell.

    Only operational defects qualify for outlier exclusion — quality outcomes
    are measurement *results*, not instrumentation flags.
    """

    cold_start: bool
    retry_count: int
    truncated_output: bool

    @property
    def is_flagged(self) -> bool:
        return self.cold_start or self.retry_count > 0 or self.truncated_output


@dataclass(frozen=True)
class CellRecord:
    """One measurement cell (one ``(sample_id, model, effort, repeat)`` row).

    Attributes:
        source_path: Path to the raw JSON on disk.
        sample_id: Dataset sample identifier (e.g. ``sf_01``).
        model: Deployment family (``gpt-4o`` or ``gpt-5.2``).
        effort: Reasoning effort label or ``None`` (gpt-4o is always ``None``).
        repeat: 0-indexed repeat number.
        input_tokens: Billable input tokens (includes cached subset).
        cached_tokens: Cached portion of input.
        output_tokens: Visible output tokens (includes reasoning subset for gpt-5.2).
        reasoning_tokens: Reasoning portion of output (0 for gpt-4o; >=0 for gpt-5.2).
        total_tokens: input_tokens + output_tokens (or whatever the runner emitted).
        latency_ms: End-to-end wall time (float).
        event: Operational flags used for outlier detection.
        experiment_id: For audit + filtering.
    """

    source_path: str
    sample_id: str
    model: str
    effort: str | None
    repeat: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    latency_ms: float
    event: CellEvent
    experiment_id: str
    git_commit: str


@dataclass(frozen=True)
class JudgeRecord:
    """One LLM-as-judge call mapped to one measurement cell.

    Score values are bounded ``{0, 1, 2}`` per the spec rubric. ``None`` is
    not allowed at construction time; a malformed judge JSON is rejected at
    load time.

    The optional ``tool_efficiency_score`` is the Task 010 additive field
    (benchmark 03 only). It is ``None`` for any judge JSON that does not
    carry the field — that is the gating mechanism for the analyzer's
    "Tool-efficiency breakdown" sub-section: when every loaded judge record
    has ``tool_efficiency_score is None``, the analyzer skips the
    aggregation and the on-disk ``analysis.json`` for benchmarks 01/02
    remains byte-identical to pre-Task-010 outputs.
    """

    sample_id: str
    model: str
    effort: str | None
    repeat: int
    score: int
    rationale: str
    judge_prompt_sha256: str
    tool_efficiency_score: float | None = None


@dataclass(frozen=True)
class CellStats:
    """Aggregated per-(model, effort) statistics, after outlier exclusion.

    The dict-rendered form is the on-disk shape. Every ``mean_*`` is paired
    with its ``std_*`` (sample std, ddof=1). USD figures originate from
    ``payg_cost_per_call`` and carry their snapshot citation in the parent
    ``analysis.json`` document.
    """

    model: str
    effort: str | None
    n_used: int
    n_excluded: int
    mean_input_tokens: float
    std_input_tokens: float
    mean_cached_tokens: float
    std_cached_tokens: float
    mean_output_tokens: float
    std_output_tokens: float
    mean_reasoning_tokens: float
    std_reasoning_tokens: float
    mean_total_tokens: float
    std_total_tokens: float
    mean_latency_ms: float
    std_latency_ms: float
    mean_judge_score: float
    std_judge_score: float
    judge_n: int
    mean_usd_per_request: float
    std_usd_per_request: float
    pricing_citation_id: str


# ----------------------------------------------------------------------------
# Schema validation + record loading
# ----------------------------------------------------------------------------


_LEGACY_USAGE_KEYS = frozenset({"prompt_tokens_details", "completion_tokens_details"})
_REQUIRED_TOP_KEYS = frozenset(
    {
        "experiment_id",
        "sample_id",
        "model",
        "effort",
        "repeat",
        "latency_ms",
        "usage",
    }
)


def _check_usage_schema(usage: dict, *, model: str, source_path: pathlib.Path) -> None:
    """Validate the Responses v1 usage object. Raise on any deviation."""
    if not isinstance(usage, dict):
        raise MeasurementSchemaError(
            f"{source_path}: usage must be a mapping; got {type(usage).__name__}"
        )

    bad_legacy = _LEGACY_USAGE_KEYS & set(usage.keys())
    if bad_legacy:
        raise LegacySchemaError(
            f"{source_path}: usage contains legacy field name(s) {sorted(bad_legacy)}; "
            "the Foundry v1 Responses payload uses input_tokens_details.cached_tokens "
            "and output_tokens_details.reasoning_tokens. The runner owns translation, "
            "not this analysis pipeline."
        )

    if "input_tokens" not in usage:
        raise MeasurementSchemaError(f"{source_path}: usage.input_tokens missing")
    if "output_tokens" not in usage:
        raise MeasurementSchemaError(f"{source_path}: usage.output_tokens missing")

    in_details = usage.get("input_tokens_details")
    if in_details is None:
        raise MeasurementSchemaError(
            f"{source_path}: usage.input_tokens_details missing "
            "(required Foundry v1 path; legacy prompt_tokens_details is forbidden)"
        )
    if not isinstance(in_details, dict):
        raise MeasurementSchemaError(
            f"{source_path}: usage.input_tokens_details must be a mapping; got "
            f"{type(in_details).__name__}"
        )
    if "cached_tokens" not in in_details:
        raise MeasurementSchemaError(
            f"{source_path}: usage.input_tokens_details.cached_tokens missing"
        )

    out_details = usage.get("output_tokens_details")
    if out_details is None:
        raise MeasurementSchemaError(
            f"{source_path}: usage.output_tokens_details missing "
            "(required Foundry v1 path; legacy completion_tokens_details is forbidden)"
        )
    if not isinstance(out_details, dict):
        raise MeasurementSchemaError(
            f"{source_path}: usage.output_tokens_details must be a mapping; got "
            f"{type(out_details).__name__}"
        )

    reasoning = out_details.get("reasoning_tokens")
    if model == "gpt-4o":
        # gpt-4o cells: reasoning_tokens MUST be absent or 0. Any non-zero
        # value is a data-integrity failure (the dedicated separate line
        # invariant of cost_calculator).
        if reasoning is not None and int(reasoning) != 0:
            raise MeasurementSchemaError(
                f"{source_path}: gpt-4o cell carries non-zero "
                f"output_tokens_details.reasoning_tokens={reasoning}; gpt-4o has "
                "no reasoning column. Data integrity failure."
            )
    elif model == "gpt-5.2":
        # gpt-5.2 cells: reasoning_tokens MUST be present and >= 0. minimal
        # effort is allowed to be 0 but the field itself must exist.
        if reasoning is None:
            raise MeasurementSchemaError(
                f"{source_path}: gpt-5.2 cell missing "
                "usage.output_tokens_details.reasoning_tokens (must be present "
                "and >= 0 for every gpt-5.2 row, even effort=minimal)"
            )
        if int(reasoning) < 0:
            raise MeasurementSchemaError(
                f"{source_path}: gpt-5.2 reasoning_tokens must be >= 0; got {reasoning}"
            )
    else:
        raise MeasurementSchemaError(
            f"{source_path}: unknown model {model!r} (expected 'gpt-4o' or 'gpt-5.2')"
        )


def _check_top_schema(data: dict, source_path: pathlib.Path) -> None:
    missing = _REQUIRED_TOP_KEYS - set(data.keys())
    if missing:
        raise MeasurementSchemaError(
            f"{source_path}: top-level keys missing: {sorted(missing)}"
        )


def load_run_record(path: pathlib.Path) -> CellRecord:
    """Load and validate one raw measurement JSON.

    Args:
        path: Filesystem path to the run JSON.

    Returns:
        Immutable ``CellRecord`` with schema validated.

    Raises:
        MeasurementSchemaError: On top-level key issues or unknown effort.
        LegacySchemaError: On legacy field names in the usage object.
        FileNotFoundError: If ``path`` does not exist.
    """
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    if not isinstance(data, dict):
        raise MeasurementSchemaError(
            f"{path}: top-level value must be a JSON object; got {type(data).__name__}"
        )

    _check_top_schema(data, path)

    model = data["model"]
    if model not in {"gpt-4o", "gpt-5.2"}:
        raise MeasurementSchemaError(
            f"{path}: model must be 'gpt-4o' or 'gpt-5.2'; got {model!r}"
        )

    effort = data["effort"]
    if model == "gpt-4o":
        if effort is not None:
            raise MeasurementSchemaError(
                f"{path}: gpt-4o cells must have effort=null; got {effort!r}"
            )
    else:
        if effort not in CANONICAL_EFFORT_ORDER:
            raise MeasurementSchemaError(
                f"{path}: gpt-5.2 effort must be one of {list(CANONICAL_EFFORT_ORDER)}; "
                f"got {effort!r}"
            )

    usage = data["usage"]
    _check_usage_schema(usage, model=model, source_path=path)

    input_tokens = int(usage["input_tokens"])
    output_tokens = int(usage["output_tokens"])
    cached_tokens = int(usage["input_tokens_details"]["cached_tokens"])
    reasoning_tokens = int(usage["output_tokens_details"].get("reasoning_tokens") or 0)
    total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens))

    event = CellEvent(
        cold_start=bool(data.get("cold_start", False)),
        retry_count=int(data.get("retry_count", 0)),
        truncated_output=bool(data.get("truncated_output", False)),
    )

    return CellRecord(
        source_path=str(path),
        sample_id=str(data["sample_id"]),
        model=model,
        effort=effort,
        repeat=int(data["repeat"]),
        input_tokens=input_tokens,
        cached_tokens=cached_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        latency_ms=float(data["latency_ms"]),
        event=event,
        experiment_id=str(data["experiment_id"]),
        git_commit=str(data.get("git_commit") or ""),
    )


def _peek_experiment_id(path: pathlib.Path) -> str | None:
    """Read just enough of a raw JSON to extract ``experiment_id``.

    Returns ``None`` if the file is not a JSON object or lacks an
    ``experiment_id`` string. Never raises — this is the "is this file part
    of our cohort?" probe, called before schema validation, so we MUST NOT
    fail the whole batch on an unrelated file (e.g. a legacy production
    cohort using a superseded effort schema).
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    eid = data.get("experiment_id")
    if not isinstance(eid, str):
        return None
    return eid


# Public alias — tests use this to probe cohort membership without paying
# the cost of full schema validation.
peek_experiment_id = _peek_experiment_id


def load_run_records(
    runs_dir: pathlib.Path,
    *,
    experiment_prefix: str = DEFAULT_EXPERIMENT_PREFIX,
) -> list[CellRecord]:
    """Load every ``*.json`` under ``runs_dir`` whose ``experiment_id`` starts
    with ``experiment_prefix``.

    Cross-cohort isolation invariant: files whose ``experiment_id`` does NOT
    match the prefix are skipped **without** running full schema validation,
    so unrelated cohorts (e.g. a legacy ``exp001_short-factual_baseline`` set
    with an older 4-effort schema) can safely coexist in the same
    directory. Files whose top-level value is not a JSON object, or which
    lack a string ``experiment_id``, are also skipped — that keeps README /
    FIXTURE_NOTE / future-format JSON co-located without aborting the run.

    Files that *do* match the prefix are then full-validated; any schema
    deviation in a matching file is a hard failure (raised).

    Returns:
        Deterministically sorted list (by sample_id, model, effort, repeat,
        source filename).
    """
    if not runs_dir.is_dir():
        raise FileNotFoundError(f"runs dir not found: {runs_dir}")

    out: list[CellRecord] = []
    for path in sorted(runs_dir.glob("*.json")):
        eid = _peek_experiment_id(path)
        if eid is None or not eid.startswith(experiment_prefix):
            logger.debug(
                "skipping %s — experiment_id %r does not match prefix %r",
                path,
                eid,
                experiment_prefix,
            )
            continue
        rec = load_run_record(path)
        out.append(rec)

    out.sort(
        key=lambda r: (
            r.sample_id,
            r.model,
            "" if r.effort is None else r.effort,
            r.repeat,
            r.source_path,
        )
    )
    return out


def load_judge_records(judge_dir: pathlib.Path) -> list[JudgeRecord]:
    """Load every judge run JSON. Returns deterministically sorted list.

    Missing directory returns ``[]`` (judge pass is optional from the
    aggregator's perspective; ``cell_stats.mean_judge_score`` will be ``NaN``
    if no records exist).
    """
    if not judge_dir.is_dir():
        return []

    out: list[JudgeRecord] = []
    for path in sorted(judge_dir.glob("*.json")):
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            continue
        try:
            score = int(data["score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MeasurementSchemaError(
                f"{path}: judge score must be int 0|1|2; got {data.get('score')!r}"
            ) from exc
        if score not in (0, 1, 2):
            raise MeasurementSchemaError(
                f"{path}: judge score out of range; got {score} (must be 0|1|2)"
            )
        effort = data.get("effort")
        if effort is not None and effort not in CANONICAL_EFFORT_ORDER:
            raise MeasurementSchemaError(
                f"{path}: judge effort {effort!r} not in canonical order"
            )
        # Task 010 additive parse: emit ``tool_efficiency_score`` when the
        # judge JSON carries the field; absence is treated as None (the
        # default), preserving byte-identical output for benchmarks 01/02
        # whose judge JSONs never carry this field.
        tef_raw = data.get("tool_efficiency_score")
        tef: float | None
        if tef_raw is None:
            tef = None
        elif isinstance(tef_raw, (int, float)) and not isinstance(tef_raw, bool):
            v = float(tef_raw)
            if v < 0.0 or v > 1.0:
                raise MeasurementSchemaError(
                    f"{path}: tool_efficiency_score out of range; got {v}"
                )
            tef = v
        else:
            raise MeasurementSchemaError(
                f"{path}: tool_efficiency_score must be a number; got {tef_raw!r}"
            )
        out.append(
            JudgeRecord(
                sample_id=str(data["sample_id"]),
                model=str(data["model"]),
                effort=effort,
                repeat=int(data["repeat"]),
                score=score,
                rationale=str(data.get("rationale", "")),
                judge_prompt_sha256=str(data.get("judge_prompt_sha256", "")),
                tool_efficiency_score=tef,
            )
        )

    out.sort(
        key=lambda r: (
            r.sample_id,
            r.model,
            "" if r.effort is None else r.effort,
            r.repeat,
        )
    )
    return out


# ----------------------------------------------------------------------------
# Outlier flagging
# ----------------------------------------------------------------------------


def flag_outliers(
    values: Sequence[float], events: Sequence[CellEvent]
) -> list[str | None]:
    """Flag a row as an outlier iff it is > 3 SDs from the cell mean AND its
    operational event-mask is flagged (cold_start / retry / truncated).

    Implements the spec snippet (.internal/tasks/008-analysis-pipeline.md
    Implementation Notes "Outlier flagging") verbatim — the threshold is 3
    SDs and the AND-gate with ``ev.is_flagged`` is **non-negotiable**: a row
    > 3 SDs from the mean WITHOUT an operational flag is a finding, not an
    outlier, and remains in the aggregate.

    Args:
        values: Token counts (or latencies) for one ``(model, effort)`` cell.
        events: Aligned ``CellEvent`` per row.

    Returns:
        List of ``"3sigma_with_flagged_event"`` (str) or ``None`` per row.
    """
    if len(values) != len(events):
        raise ValueError(
            f"flag_outliers: len(values)={len(values)} != len(events)={len(events)}"
        )
    n = len(values)
    if n < 2:
        return [None] * n
    m = statistics.mean(values)
    sd = statistics.stdev(values)
    flagged: list[str | None] = []
    for v, ev in zip(values, events):
        if sd > 0 and abs(v - m) > 3 * sd and ev.is_flagged:
            flagged.append("3sigma_with_flagged_event")
        else:
            flagged.append(None)
    return flagged


def _row_outlier_reasons(cells: list[CellRecord]) -> list[str | None]:
    """For one ``(model, effort)`` group, run the flagger over every numeric
    category (input/cached/output/reasoning/total tokens + latency). A row is
    excluded if *any* category flags it; the reason is fixed to
    ``"3sigma_with_flagged_event"`` per the spec.
    """
    if not cells:
        return []
    events = [c.event for c in cells]
    categories: list[list[float]] = [
        [float(c.input_tokens) for c in cells],
        [float(c.cached_tokens) for c in cells],
        [float(c.output_tokens) for c in cells],
        [float(c.reasoning_tokens) for c in cells],
        [float(c.total_tokens) for c in cells],
        [float(c.latency_ms) for c in cells],
    ]
    reasons: list[str | None] = [None] * len(cells)
    for vals in categories:
        flags = flag_outliers(vals, events)
        for i, f in enumerate(flags):
            if f is not None and reasons[i] is None:
                reasons[i] = f
    return reasons


# ----------------------------------------------------------------------------
# Statistics + cost aggregation
# ----------------------------------------------------------------------------


def _mean_std(values: Sequence[float]) -> tuple[float, float]:
    """Return ``(mean, sample_std)`` rounded; std=0 when n<2 (single sample)."""
    if not values:
        return (0.0, 0.0)
    m = statistics.mean(values)
    sd = statistics.stdev(values) if len(values) >= 2 else 0.0
    return (round(m, FLOAT_NDIGITS), round(sd, FLOAT_NDIGITS))


def _round6(x: float) -> float:
    return round(float(x), FLOAT_NDIGITS)


def _percentile(values: Sequence[float], p: float) -> float:
    """Linear-interpolation percentile (matches numpy's default).

    Implemented here in pure Python so the analyzer has no numpy dependency.

    Args:
        values: Non-empty sequence of numeric values.
        p: Percentile in [0, 100].

    Returns:
        The interpolated percentile value.
    """
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    sorted_vals = sorted(values)
    rank = (p / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return float(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac)


def compute_cell_stats(
    cells_used: list[CellRecord],
    cells_excluded: list[CellRecord],
    judge_scores: list[int],
    pricing: PaygPricing,
    *,
    model: str,
    effort: str | None,
    pricing_citation_id: str,
) -> CellStats:
    """Aggregate one ``(model, effort)`` group into a ``CellStats``.

    Args:
        cells_used: Rows kept after outlier exclusion.
        cells_excluded: Rows tagged with an ``outlier_reason``.
        judge_scores: Judge scores for the used rows (0|1|2). May be empty.
        pricing: Loaded PAYG snapshot (used to compute mean USD via
            ``payg_cost_per_call``).
        model: Deployment family.
        effort: Effort label (or ``None`` for gpt-4o).
        pricing_citation_id: Shared key into the document-level
            ``pricing_citations`` table; identical for every cell using the
            same snapshot.

    The mean USD figure is derived from the mean token counts of ``cells_used``
    by calling ``payg_cost_per_call`` on a ``TokenUsage`` built from those
    means. The PAYG formula is linear in tokens (§6.1), so this equals the
    arithmetic mean of per-row USD; computing once on the means matches the
    canonical reporting pattern in ``cost_calculator.render_cost_report``.
    """
    in_t = [float(c.input_tokens) for c in cells_used]
    ca_t = [float(c.cached_tokens) for c in cells_used]
    ou_t = [float(c.output_tokens) for c in cells_used]
    re_t = [float(c.reasoning_tokens) for c in cells_used]
    to_t = [float(c.total_tokens) for c in cells_used]
    la_t = [float(c.latency_ms) for c in cells_used]

    mean_in, std_in = _mean_std(in_t)
    mean_ca, std_ca = _mean_std(ca_t)
    mean_ou, std_ou = _mean_std(ou_t)
    mean_re, std_re = _mean_std(re_t)
    mean_to, std_to = _mean_std(to_t)
    mean_la, std_la = _mean_std(la_t)

    if judge_scores:
        mean_j, std_j = _mean_std([float(s) for s in judge_scores])
    else:
        mean_j, std_j = (0.0, 0.0)

    if cells_used:
        usage_mean = TokenUsage(
            input_tokens=mean_in,
            cached_tokens=mean_ca,
            output_tokens=mean_ou,
            reasoning_tokens=mean_re,
        )
        breakdown = payg_cost_per_call(usage_mean, pricing, model=model)
        # Per-row USD for std: compute over used rows.
        per_row_usd: list[float] = []
        for c in cells_used:
            row_usage = TokenUsage(
                input_tokens=float(c.input_tokens),
                cached_tokens=float(c.cached_tokens),
                output_tokens=float(c.output_tokens),
                reasoning_tokens=float(c.reasoning_tokens),
            )
            per_row_usd.append(payg_cost_per_call(row_usage, pricing, model=model).usd_per_request)
        mean_usd = _round6(breakdown.usd_per_request)
        std_usd = (
            _round6(statistics.stdev(per_row_usd)) if len(per_row_usd) >= 2 else 0.0
        )
    else:
        mean_usd = 0.0
        std_usd = 0.0

    return CellStats(
        model=model,
        effort=effort,
        n_used=len(cells_used),
        n_excluded=len(cells_excluded),
        mean_input_tokens=mean_in,
        std_input_tokens=std_in,
        mean_cached_tokens=mean_ca,
        std_cached_tokens=std_ca,
        mean_output_tokens=mean_ou,
        std_output_tokens=std_ou,
        mean_reasoning_tokens=mean_re,
        std_reasoning_tokens=std_re,
        mean_total_tokens=mean_to,
        std_total_tokens=std_to,
        mean_latency_ms=mean_la,
        std_latency_ms=std_la,
        mean_judge_score=mean_j,
        std_judge_score=std_j,
        judge_n=len(judge_scores),
        mean_usd_per_request=mean_usd,
        std_usd_per_request=std_usd,
        pricing_citation_id=pricing_citation_id,
    )


# ----------------------------------------------------------------------------
# Top-level orchestration
# ----------------------------------------------------------------------------


def _judge_score_for(
    cell: CellRecord, judge_index: dict[tuple[str, str, str | None, int], int]
) -> int | None:
    """Look up the judge score for one measurement cell.

    Returns ``None`` if no judge record matches (graceful — judge pass is
    optional from the aggregator's perspective).
    """
    return judge_index.get((cell.sample_id, cell.model, cell.effort, cell.repeat))


def _group_key(cell: CellRecord) -> tuple[str, str | None]:
    return (cell.model, cell.effort)


def _per_tag_judge_breakdown(
    cells_with_scores: list[tuple[CellRecord, int]],
    sample_tags: dict[str, list[str]],
) -> dict:
    """Aggregate judge score by sample tag → ``{tag: {n, mean, std}}``."""
    by_tag: dict[str, list[float]] = {}
    for cell, score in cells_with_scores:
        for tag in sample_tags.get(cell.sample_id, []):
            by_tag.setdefault(tag, []).append(float(score))
    out: dict[str, dict[str, float]] = {}
    for tag in sorted(by_tag):
        m, sd = _mean_std(by_tag[tag])
        out[tag] = {"n": len(by_tag[tag]), "mean": m, "std": sd}
    return out


def build_analysis(
    *,
    benchmark_name: str,
    runs_dir: pathlib.Path,
    judge_dir: pathlib.Path,
    dataset_path: pathlib.Path,
    pricing_dir: pathlib.Path,
    experiment_prefix: str = DEFAULT_EXPERIMENT_PREFIX,
    snapshot_date: str | None = None,
) -> dict[str, Any]:
    """Produce the byte-stable ``analysis.json`` payload.

    Args:
        benchmark_name: e.g. ``"01-short-factual"``.
        runs_dir: Measurement runs directory.
        judge_dir: Judge runs directory (may be empty / missing).
        dataset_path: ``dataset.json`` (for per-tag judge breakdown).
        pricing_dir: Directory with ``azure-openai-payg-*.yaml`` snapshots.
        experiment_prefix: Only experiment IDs starting with this prefix are
            included — keeps smoke / unrelated runs out of the aggregate.
        snapshot_date: Optional ``YYYY-MM-DD`` override for snapshot resolution.

    Returns:
        Dict ready for ``json.dumps(payload, indent=2, sort_keys=True)``.
    """
    import datetime

    target_date: datetime.date | None = None
    if snapshot_date is not None:
        target_date = datetime.date.fromisoformat(snapshot_date)
    snapshot_path = resolve_active_snapshot(
        kind="payg", target_date=target_date, pricing_dir=pricing_dir
    )
    pricing = load_payg_pricing(snapshot_path)

    runs = load_run_records(runs_dir, experiment_prefix=experiment_prefix)
    judge_records = load_judge_records(judge_dir)

    # Index judge by (sample_id, model, effort, repeat) — at most one per cell.
    judge_index: dict[tuple[str, str, str | None, int], int] = {}
    for j in judge_records:
        key = (j.sample_id, j.model, j.effort, j.repeat)
        if key in judge_index:
            raise MeasurementSchemaError(
                f"judge_runs/: duplicate judge record for cell {key}"
            )
        judge_index[key] = j.score

    # Per-sample tags (for judge per-tag breakdown).
    sample_tags: dict[str, list[str]] = {}
    if dataset_path.is_file():
        with dataset_path.open("r", encoding="utf-8") as fh:
            ds = json.load(fh)
        if isinstance(ds, list):
            for s in ds:
                if isinstance(s, dict) and "id" in s:
                    tags = s.get("tags") or []
                    if isinstance(tags, list):
                        sample_tags[str(s["id"])] = [str(t) for t in tags]

    # Group rows by (model, effort), flag outliers within each group.
    by_group: dict[tuple[str, str | None], list[CellRecord]] = {}
    for r in runs:
        by_group.setdefault(_group_key(r), []).append(r)

    cell_rows: list[dict[str, Any]] = []
    cell_stats_list: list[CellStats] = []
    outlier_summary: list[dict[str, Any]] = []

    pricing_citation_id = "payg_primary"
    pricing_citations: dict[str, dict[str, Any]] = {
        pricing_citation_id: {
            "lens": "payg",
            "snapshot_path": pricing.snapshot_path,
            "source_url": pricing.source_url,
            "accessed_date": pricing.accessed_date,
            "archive_url": pricing.archive_url,
            "currency": pricing.currency,
        }
    }

    # Iterate groups in canonical order so the output is deterministic
    # regardless of filesystem enumeration order.
    canonical_groups: list[tuple[str, str | None]] = [("gpt-4o", None)] + [
        ("gpt-5.2", e) for e in CANONICAL_EFFORT_ORDER
    ]
    seen_groups: set[tuple[str, str | None]] = set()
    for gkey in canonical_groups:
        group_cells = by_group.get(gkey, [])
        if not group_cells:
            # Empty group; emit a zero-cell stats so downstream consumers see
            # all six entries (the success-criteria invariant).
            cell_stats_list.append(
                CellStats(
                    model=gkey[0],
                    effort=gkey[1],
                    n_used=0,
                    n_excluded=0,
                    mean_input_tokens=0.0,
                    std_input_tokens=0.0,
                    mean_cached_tokens=0.0,
                    std_cached_tokens=0.0,
                    mean_output_tokens=0.0,
                    std_output_tokens=0.0,
                    mean_reasoning_tokens=0.0,
                    std_reasoning_tokens=0.0,
                    mean_total_tokens=0.0,
                    std_total_tokens=0.0,
                    mean_latency_ms=0.0,
                    std_latency_ms=0.0,
                    mean_judge_score=0.0,
                    std_judge_score=0.0,
                    judge_n=0,
                    mean_usd_per_request=0.0,
                    std_usd_per_request=0.0,
                    pricing_citation_id=pricing_citation_id,
                )
            )
            continue
        seen_groups.add(gkey)
        reasons = _row_outlier_reasons(group_cells)
        used: list[CellRecord] = []
        excluded: list[CellRecord] = []
        for r, reason in zip(group_cells, reasons):
            judge_score = _judge_score_for(r, judge_index)
            row = {
                "sample_id": r.sample_id,
                "model": r.model,
                "effort": r.effort,
                "repeat": r.repeat,
                "input_tokens": r.input_tokens,
                "cached_tokens": r.cached_tokens,
                "output_tokens": r.output_tokens,
                "reasoning_tokens": r.reasoning_tokens,
                "total_tokens": r.total_tokens,
                "latency_ms": _round6(r.latency_ms),
                "judge_score": judge_score,
                "outlier_reason": reason,
                "event_cold_start": r.event.cold_start,
                "event_retry_count": r.event.retry_count,
                "event_truncated_output": r.event.truncated_output,
                "source_path": r.source_path,
            }
            cell_rows.append(row)
            if reason is None:
                used.append(r)
            else:
                excluded.append(r)
                outlier_summary.append(
                    {
                        "sample_id": r.sample_id,
                        "model": r.model,
                        "effort": r.effort,
                        "repeat": r.repeat,
                        "reason": reason,
                        "source_path": r.source_path,
                    }
                )

        judge_used = [
            _judge_score_for(r, judge_index)
            for r in used
            if _judge_score_for(r, judge_index) is not None
        ]
        judge_used_int: list[int] = [s for s in judge_used if s is not None]

        stats_entry = compute_cell_stats(
            cells_used=used,
            cells_excluded=excluded,
            judge_scores=judge_used_int,
            pricing=pricing,
            model=gkey[0],
            effort=gkey[1],
            pricing_citation_id=pricing_citation_id,
        )
        cell_stats_list.append(stats_entry)

    # Per-tag judge breakdown (across all cells, per (model, effort) group).
    judge_breakdown_by_tag: dict[str, dict[str, dict[str, dict[str, float]]]] = {}
    for gkey in canonical_groups:
        group_cells = [c for c in runs if _group_key(c) == gkey]
        if not group_cells:
            continue
        scored: list[tuple[CellRecord, int]] = []
        for c in group_cells:
            s = _judge_score_for(c, judge_index)
            if s is not None:
                scored.append((c, s))
        if not scored:
            continue
        label = f"{gkey[0]}__{gkey[1] if gkey[1] is not None else 'baseline'}"
        judge_breakdown_by_tag[label] = _per_tag_judge_breakdown(scored, sample_tags)

    # Task 010 additive aggregation: per-(model, effort) tool-efficiency
    # breakdown, gated on at least one loaded judge record carrying a
    # non-None ``tool_efficiency_score``. When the gate is open (benchmark
    # 03), the analyzer also reads each raw measurement JSON's
    # ``tool_calls`` list length so the breakdown can report mean tool-call
    # count per cell alongside the rubric-graded score distribution. When
    # closed (benchmarks 01/02), the analyzer skips this entirely and
    # ``analysis.json`` for prior benchmarks remains byte-identical to the
    # pre-Task-010 contract.
    tool_efficiency_breakdown: dict[str, Any] | None = None
    if any(j.tool_efficiency_score is not None for j in judge_records):
        # Build a per-cell tool-call count by re-reading raw measurement
        # JSONs. The runner's per-iteration trajectory under ``tool_calls``
        # is the source of truth — we count list length, not API call count.
        tool_calls_len_by_cell: dict[
            tuple[str, str, str | None, int], int
        ] = {}
        for r in runs:
            try:
                with pathlib.Path(r.source_path).open("r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            tc = raw.get("tool_calls") if isinstance(raw, dict) else None
            if isinstance(tc, list):
                tool_calls_len_by_cell[
                    (r.sample_id, r.model, r.effort, r.repeat)
                ] = len(tc)

        # Index tool_efficiency_score by cell key, mirroring judge_index.
        tef_index: dict[tuple[str, str, str | None, int], float] = {}
        for j in judge_records:
            if j.tool_efficiency_score is None:
                continue
            tef_index[(j.sample_id, j.model, j.effort, j.repeat)] = (
                j.tool_efficiency_score
            )

        per_group: list[dict[str, Any]] = []
        for gkey in canonical_groups:
            group_cells = by_group.get(gkey, [])
            if not group_cells:
                continue
            tef_values: list[float] = []
            call_counts: list[int] = []
            for c in group_cells:
                k = (c.sample_id, c.model, c.effort, c.repeat)
                if k in tef_index:
                    tef_values.append(float(tef_index[k]))
                if k in tool_calls_len_by_cell:
                    call_counts.append(int(tool_calls_len_by_cell[k]))
            if not tef_values:
                continue
            mean_tef = _round6(statistics.mean(tef_values))
            std_tef = (
                _round6(statistics.stdev(tef_values))
                if len(tef_values) >= 2
                else 0.0
            )
            per_group.append(
                {
                    "model": gkey[0],
                    "effort": gkey[1],
                    "n": len(tef_values),
                    "mean_tool_efficiency_score": mean_tef,
                    "std_tool_efficiency_score": std_tef,
                    "p10_tool_efficiency_score": _round6(
                        _percentile(tef_values, 10)
                    ),
                    "p50_tool_efficiency_score": _round6(
                        _percentile(tef_values, 50)
                    ),
                    "p90_tool_efficiency_score": _round6(
                        _percentile(tef_values, 90)
                    ),
                    "mean_tool_call_count": (
                        _round6(statistics.mean(call_counts))
                        if call_counts
                        else 0.0
                    ),
                    "std_tool_call_count": (
                        _round6(statistics.stdev(call_counts))
                        if len(call_counts) >= 2
                        else 0.0
                    ),
                }
            )

        tool_efficiency_breakdown = {
            "by_cell": per_group,
            "judge_prompt_field": "tool_efficiency_score",
            "scale": "continuous [0.0, 1.0] (two decimal places); 1.0 = optimal tool use, 0.0 = inadequate",
        }

    # Sort cells deterministically for byte-stable output.
    cell_rows.sort(
        key=lambda r: (
            r["sample_id"],
            r["model"],
            "" if r["effort"] is None else r["effort"],
            r["repeat"],
        )
    )
    outlier_summary.sort(
        key=lambda r: (
            r["sample_id"],
            r["model"],
            "" if r["effort"] is None else r["effort"],
            r["repeat"],
        )
    )

    # PTU baseline: spec requires "every PTU figure declares its baseline".
    # The canonical baseline for benchmark 01 is the gpt-4o cell (migration
    # lens — readers asking "should I switch from gpt-4o to gpt-5.2 at
    # effort=X?"). Tokens-per-request convention is ``input + output``
    # (Azure's ``total_tokens`` on the Responses API for GPT-5.x), matching
    # ``cost_calculator._cell_tokens``. Reasoning is NOT added — under
    # Azure's current usage contract ``output_tokens`` already includes the
    # reasoning subset (``total_tokens == input_tokens + output_tokens``),
    # so adding it would double-count.
    baseline_stats = next(
        (s for s in cell_stats_list if s.model == "gpt-4o"), None
    )
    if baseline_stats is None or baseline_stats.n_used == 0:
        baseline_tokens = 0.0
        baseline_label = "gpt-4o (baseline; no data)"
    else:
        baseline_tokens = (
            baseline_stats.mean_input_tokens
            + baseline_stats.mean_output_tokens
        )
        baseline_label = "gpt-4o baseline (mean tokens-per-request)"

    ptu_gain_table: list[dict[str, Any]] = []
    for s in cell_stats_list:
        target_tokens = s.mean_input_tokens + s.mean_output_tokens
        if target_tokens > 0 and baseline_tokens > 0:
            gain = baseline_tokens / target_tokens
        else:
            gain = 0.0
        ptu_gain_table.append(
            {
                "model": s.model,
                "effort": s.effort,
                "tokens_per_request": _round6(target_tokens),
                "throughput_gain_factor": _round6(gain),
                "baseline_label": baseline_label,
            }
        )

    # Run provenance: extract one representative experiment_id + git_commit
    # per (model) for the audit trail. We do not embed wall-clock here — the
    # provenance must be byte-stable across re-runs.
    experiment_ids = sorted({r.experiment_id for r in runs})
    git_commits = sorted({r.git_commit for r in runs if r.git_commit})

    payload: dict[str, Any] = {
        "schema_version": "008.1",
        "benchmark": benchmark_name,
        "experiment_prefix": experiment_prefix,
        "run_count": len(runs),
        "cells_count": len(cell_rows),
        "experiment_ids": experiment_ids,
        "git_commits": git_commits,
        "pricing_citations": pricing_citations,
        "ptu_baseline": {
            "label": baseline_label,
            "tokens_per_request": _round6(baseline_tokens),
        },
        "cells": cell_rows,
        "cell_stats": [_cell_stats_to_dict(s) for s in cell_stats_list],
        "ptu_gain_by_cell": ptu_gain_table,
        "outliers": outlier_summary,
        "judge_breakdown_by_tag": judge_breakdown_by_tag,
    }
    # Task 010 additive emission, gated on tool_efficiency_score presence.
    # Adding the key only when set keeps the on-disk JSON for benchmarks
    # 01/02 byte-identical to pre-Task-010 outputs (verified by the
    # analyzer-regression check at the end of task 010).
    if tool_efficiency_breakdown is not None:
        payload["tool_efficiency_breakdown"] = tool_efficiency_breakdown
    return payload


def _cell_stats_to_dict(s: CellStats) -> dict[str, Any]:
    """Render ``CellStats`` to a stable dict (keys sorted by ``json.dumps``)."""
    return {
        "model": s.model,
        "effort": s.effort,
        "n_used": s.n_used,
        "n_excluded": s.n_excluded,
        "mean_input_tokens": s.mean_input_tokens,
        "std_input_tokens": s.std_input_tokens,
        "mean_cached_tokens": s.mean_cached_tokens,
        "std_cached_tokens": s.std_cached_tokens,
        "mean_output_tokens": s.mean_output_tokens,
        "std_output_tokens": s.std_output_tokens,
        "mean_reasoning_tokens": s.mean_reasoning_tokens,
        "std_reasoning_tokens": s.std_reasoning_tokens,
        "mean_total_tokens": s.mean_total_tokens,
        "std_total_tokens": s.std_total_tokens,
        "mean_latency_ms": s.mean_latency_ms,
        "std_latency_ms": s.std_latency_ms,
        "mean_judge_score": s.mean_judge_score,
        "std_judge_score": s.std_judge_score,
        "judge_n": s.judge_n,
        "mean_usd_per_request": s.mean_usd_per_request,
        "std_usd_per_request": s.std_usd_per_request,
        "pricing_citation_id": s.pricing_citation_id,
    }


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def render_json(payload: dict[str, Any]) -> str:
    """Deterministic JSON serialization for byte-stable diffs."""
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scripts.analyze_tokens",
        description=(
            "Aggregate a benchmark's raw Responses-API run JSONs + judge JSONs "
            "into a deterministic, byte-stable analysis.json. Pure / offline; "
            "no network calls. Every USD figure originates from "
            "scripts.cost_calculator.payg_cost_per_call with full citation."
        ),
    )
    p.add_argument(
        "--benchmark",
        required=True,
        help="Benchmark folder name (e.g. 01-short-factual).",
    )
    p.add_argument(
        "--out",
        default=None,
        help=(
            "Path to write the analysis.json. Default: "
            "benchmarks/<benchmark>/analysis.json."
        ),
    )
    p.add_argument(
        "--runs-dir",
        default=None,
        help="Override runs/ directory (default benchmarks/<bench>/runs).",
    )
    p.add_argument(
        "--judge-dir",
        default=None,
        help="Override judge_runs/ directory (default benchmarks/<bench>/judge_runs).",
    )
    p.add_argument(
        "--dataset",
        default=None,
        help="Override dataset.json path.",
    )
    p.add_argument(
        "--pricing-dir",
        default="pricing",
        help="Pricing snapshot directory (default: pricing).",
    )
    p.add_argument(
        "--experiment-prefix",
        default=DEFAULT_EXPERIMENT_PREFIX,
        help=(
            "Only include run JSONs whose experiment_id starts with this "
            f"prefix (default: {DEFAULT_EXPERIMENT_PREFIX!r}). Sibling cohorts "
            "(smoke runs, the legacy 4-effort production set "
            "'exp001_short-factual_baseline', etc.) are skipped before schema "
            "validation, so they may safely coexist in the same runs/ "
            "directory."
        ),
    )
    p.add_argument(
        "--snapshot-date",
        default=None,
        help="Pin the pricing snapshot by YYYY-MM-DD (default: newest).",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (default: INFO).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    ns = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, ns.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    benchmark = ns.benchmark
    bench_root = pathlib.Path("benchmarks") / benchmark
    runs_dir = pathlib.Path(ns.runs_dir) if ns.runs_dir else bench_root / "runs"
    judge_dir = (
        pathlib.Path(ns.judge_dir) if ns.judge_dir else bench_root / "judge_runs"
    )
    dataset_path = (
        pathlib.Path(ns.dataset) if ns.dataset else bench_root / "dataset.json"
    )
    pricing_dir = pathlib.Path(ns.pricing_dir)
    out_path = pathlib.Path(ns.out) if ns.out else bench_root / "analysis.json"

    logger.info("analyze: benchmark=%s runs_dir=%s judge_dir=%s", benchmark, runs_dir, judge_dir)
    payload = build_analysis(
        benchmark_name=benchmark,
        runs_dir=runs_dir,
        judge_dir=judge_dir,
        dataset_path=dataset_path,
        pricing_dir=pricing_dir,
        experiment_prefix=ns.experiment_prefix,
        snapshot_date=ns.snapshot_date,
    )

    text = render_json(payload)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    logger.info(
        "analyze: wrote %s (cells=%d, cell_stats=%d, outliers=%d)",
        out_path,
        len(payload.get("cells", [])),
        len(payload.get("cell_stats", [])),
        len(payload.get("outliers", [])),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
