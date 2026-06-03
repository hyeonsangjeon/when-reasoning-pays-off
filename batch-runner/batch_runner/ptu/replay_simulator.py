"""Offline PTU utilization replay simulator (Task 024).

This module is **pure**: no network I/O, no HTTP/OpenAI clients, no env
reads. The CLI in ``scripts/replay_ptu_utilization.py`` is the only
side-effecting layer.

Architectural overview
----------------------

1. ``NormalizedReplayRecord`` is the deterministic in-memory shape the
   simulator consumes. Source-aware legacy adapters
   (``adapt_task013_record``, ``adapt_task019_record``) map raw JSONL
   dicts into this shape, recording per-record ``fallback_reasons``.

2. ``replay_stream`` runs the Guide §0 admission formula against a
   token-denominated bucket and a fitted leak constant ``k``. It emits
   ``ReplayEvent`` records per source record processed. Validation mode
   does not mutate the bucket on observed-429 source records (false
   negatives are reported explicitly).

3. ``calibrate_k`` fits a single scalar
   ``k_leak_tokens_per_ptu_per_second`` via deterministic 1-D
   optimization (grid + golden-section refine) using onset-timestamp
   squared residuals between predicted and observed 429 events.

4. ``leave_one_source_run_out`` produces a deterministic fold list for
   the holdout protocol.

Task019 inputs are PAYG-throttled-quota proxy evidence, not direct PTU
validation. Task013 inputs use PAYG deployments shaped to expose a
PTU-like saturation pattern, not customer-attributed native PTU
evidence. Source labels are preserved in every aggregate.

No reasoning-token or Guide §3 model-specific output-weight ratio is
modeled in v1; reasoning tokens may be reported as observed output
composition only.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .utilization_model import (
    INPUT_TPM_PER_PTU,
    admission_cost_tokens,
    capacity_tokens,
    leak_tokens,
)

# --- Source labels ---------------------------------------------------------

SOURCE_TASK013 = "task013_dual_spillover"
SOURCE_TASK019 = "task019_max_output_tokens_proxy"

# Task013 max_output_tokens recovery: matches Task 024 spec, which
# pins value 1024 from the matching experiment YAML
# `experiments/exp005_dual_spillover_{reactive,proactive}.yaml`.
TASK013_MAX_OUTPUT_TOKENS_FROM_YAML = 1024
TASK013_YAML_PATHS: tuple[str, ...] = (
    "experiments/exp005_dual_spillover_reactive.yaml",
    "experiments/exp005_dual_spillover_proactive.yaml",
)


# --- Normalized record ----------------------------------------------------


@dataclass(frozen=True)
class NormalizedReplayRecord:
    """Deterministic, source-aware replay record.

    Built from raw legacy JSONL dicts by ``adapt_*`` functions. The
    simulator only consumes this shape; it never reaches into raw
    source records directly.
    """

    source_label: str
    source_path: str
    source_run_id: str
    timestamp_seconds: float
    model: str
    prompt_tokens: int
    cached_tokens: int
    max_output_tokens: int
    observed_output_tokens: int | None
    observed_accepted: bool
    observed_429: bool
    observed_retry_after_ms: float | None
    fallback_reasons: tuple[str, ...]
    capacity_source: str
    total_latency_ms: float | None = None
    original_line_number: int = 0
    cell_key: str | None = None
    excluded_reason: str | None = None


# --- Adapter helpers -------------------------------------------------------


def _parse_iso_seconds(value: Any) -> float | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # Tolerate trailing "Z".
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _coerce_int_nonneg(value: Any) -> int | None:
    if value is None:
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        try:
            v = int(float(value))
        except (TypeError, ValueError):
            return None
    if v < 0:
        return None
    return v


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_retry_after_ms(record: dict) -> tuple[float | None, list[str]]:
    """Return retry-after milliseconds and any fallback reasons recorded."""
    reasons: list[str] = []
    rams = record.get("retry_after_ms")
    if rams is not None:
        v = _coerce_float(rams)
        if v is None:
            reasons.append("retry_after_unparseable")
        elif v < 0:
            reasons.append("retry_after_unparseable")
            return None, reasons
        else:
            return v, reasons
    # Try seconds-based fields.
    for key in ("retry_after_seconds", "retry_after"):
        if key in record and record[key] is not None:
            v = _coerce_float(record[key])
            if v is None:
                reasons.append("retry_after_unparseable")
                continue
            if v < 0:
                reasons.append("retry_after_unparseable")
                continue
            return v * 1000.0, reasons
    return None, reasons


def _select_prompt_cached(record: dict) -> tuple[int | None, int | None, list[str]]:
    """Prompt and cached token selection per spec priority. Returns
    (prompt, cached, fallback_reasons).
    """
    reasons: list[str] = []
    usage = record.get("usage") if isinstance(record.get("usage"), dict) else None
    prompt: int | None = None
    cached: int | None = None
    if usage:
        p = _coerce_int_nonneg(usage.get("input_tokens"))
        details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else None
        c = _coerce_int_nonneg(details.get("cached_tokens")) if details else None
        if p is not None:
            prompt = p
        else:
            p = _coerce_int_nonneg(usage.get("prompt_tokens"))
            if p is not None:
                prompt = p
                reasons.append("prompt_tokens_from_legacy_usage")
        if c is not None:
            cached = c
        else:
            c = _coerce_int_nonneg(usage.get("cached_tokens"))
            if c is not None:
                cached = c
                reasons.append("cached_tokens_from_legacy_usage")
    # Top-level fallbacks (only if usage missing or zero on failed records).
    record_failed = bool(record.get("failed")) or bool(record.get("real_429_observed")) or bool(record.get("429_observed"))
    if (prompt is None or (prompt == 0 and record_failed)):
        for key in ("canonical_input_tokens", "request_estimated_processed_tokens"):
            v = _coerce_int_nonneg(record.get(key))
            if v is not None and v > 0:
                prompt = v
                reasons.append(f"prompt_tokens_from_{key}")
                break
    if (cached is None or (cached == 0 and record_failed)):
        for key in ("canonical_cached_tokens", "cached_tokens"):
            v = _coerce_int_nonneg(record.get(key))
            if v is not None:
                if v > 0 or cached is None:
                    cached = v
                    reasons.append(f"cached_tokens_from_{key}")
                    break
    return prompt, cached, reasons


def _select_observed_output(record: dict) -> int | None:
    usage = record.get("usage") if isinstance(record.get("usage"), dict) else None
    if usage:
        v = _coerce_int_nonneg(usage.get("output_tokens"))
        if v is not None:
            return v
        v = _coerce_int_nonneg(usage.get("completion_tokens"))
        if v is not None:
            return v
    v = _coerce_int_nonneg(record.get("canonical_output_tokens"))
    if v is not None:
        return v
    visible = _coerce_int_nonneg(record.get("visible_output_tokens"))
    reasoning = _coerce_int_nonneg(record.get("reasoning_tokens"))
    if visible is not None and reasoning is not None:
        return visible + reasoning
    if visible is not None:
        return visible
    return None


def _file_run_id(source_path: str) -> str:
    return os.path.basename(source_path)


# --- Source adapters -------------------------------------------------------


def adapt_task013_record(
    record: dict,
    *,
    source_path: str,
    line_number: int,
    capacity_source: str,
) -> NormalizedReplayRecord:
    """Adapt one raw Task013 dual-spillover JSONL record."""
    fallback: list[str] = []
    # Timestamp priority: wallclock -> timestamp_utc -> relative_time_s.
    ts = _parse_iso_seconds(record.get("wallclock_timestamp_iso"))
    if ts is None:
        ts = _parse_iso_seconds(record.get("timestamp_utc"))
    if ts is None:
        rel = _coerce_float(record.get("relative_time_s"))
        if rel is not None:
            ts = float(rel)
            fallback.append("relative_time_only")
    if ts is None:
        fallback.append("timestamp_unparseable")
        ts = float(line_number)

    model = record.get("model") or ""
    prompt, cached, p_reasons = _select_prompt_cached(record)
    fallback.extend(p_reasons)

    if cached is None:
        cached = 0
        fallback.append("cached_tokens_defaulted_zero")
    observed_429 = bool(record.get("real_429_observed"))
    observed_accepted = not observed_429 and not record.get("failed")

    if prompt is None:
        if observed_429:
            fallback.append("zero_usage_429_needs_recovery")
        else:
            fallback.append("prompt_tokens_missing")
        prompt = 0

    observed_output = _select_observed_output(record)

    # Task013 JSONL does not carry max_output; recover from YAML.
    max_output = TASK013_MAX_OUTPUT_TOKENS_FROM_YAML
    fallback.append("max_output_tokens_from_yaml")

    retry_ms, retry_reasons = _normalize_retry_after_ms(record)
    fallback.extend(retry_reasons)

    total_latency_ms = _coerce_float(record.get("total_latency_ms"))

    return NormalizedReplayRecord(
        source_label=SOURCE_TASK013,
        source_path=source_path,
        source_run_id=_file_run_id(source_path),
        timestamp_seconds=float(ts),
        model=str(model) if model else "gpt-5.2",
        prompt_tokens=int(prompt),
        cached_tokens=int(cached),
        max_output_tokens=int(max_output),
        observed_output_tokens=observed_output,
        observed_accepted=bool(observed_accepted),
        observed_429=bool(observed_429),
        observed_retry_after_ms=retry_ms,
        fallback_reasons=tuple(fallback),
        capacity_source=capacity_source,
        total_latency_ms=total_latency_ms,
        original_line_number=line_number,
        cell_key=None,
    )


def adapt_task019_record(
    record: dict,
    *,
    source_path: str,
    line_number: int,
    capacity_source: str,
) -> NormalizedReplayRecord:
    """Adapt one raw Task019 max-output-tokens-sweep JSONL record."""
    fallback: list[str] = []
    # Timestamp priority per spec.
    ts = _parse_iso_seconds(record.get("admitted_dispatch_iso"))
    if ts is None:
        ts = _parse_iso_seconds(record.get("scheduled_dispatch_iso"))
    if ts is None:
        ts = _parse_iso_seconds(record.get("wallclock_timestamp_iso"))
    if ts is None:
        ts = _parse_iso_seconds(record.get("timestamp_utc"))
    if ts is None:
        rel = _coerce_float(record.get("relative_time_s"))
        if rel is not None:
            ts = float(rel)
            fallback.append("relative_time_only")
    if ts is None:
        fallback.append("timestamp_unparseable")
        ts = float(line_number)

    model = record.get("model") or ""
    prompt, cached, p_reasons = _select_prompt_cached(record)
    fallback.extend(p_reasons)

    if cached is None:
        cached = 0
        fallback.append("cached_tokens_defaulted_zero")

    observed_429 = bool(record.get("429_observed")) or (record.get("first_429_metadata") is not None)
    failed = bool(record.get("failed"))
    observed_accepted = not observed_429 and not failed

    if prompt is None or (prompt == 0 and observed_429):
        if observed_429:
            fallback.append("zero_usage_429_needs_recovery")
        else:
            fallback.append("prompt_tokens_missing")
        if prompt is None:
            prompt = 0

    observed_output = _select_observed_output(record)

    # Max-output priority: max_output_tokens_sent, cell_max_output_tokens,
    # cell_metadata.max_output_tokens.
    max_output: int | None = None
    for key in ("max_output_tokens_sent", "cell_max_output_tokens"):
        v = _coerce_int_nonneg(record.get(key))
        if v is not None and v > 0:
            max_output = v
            break
    if max_output is None:
        cm = record.get("cell_metadata") if isinstance(record.get("cell_metadata"), dict) else None
        if cm:
            v = _coerce_int_nonneg(cm.get("max_output_tokens"))
            if v is not None and v > 0:
                max_output = v

    excluded_reason = None
    if max_output is None:
        excluded_reason = "missing_max_output_tokens"
        fallback.append("missing_max_output_tokens")
        max_output = 0

    retry_ms, retry_reasons = _normalize_retry_after_ms(record)
    fallback.extend(retry_reasons)

    total_latency_ms = _coerce_float(record.get("total_latency_ms"))

    cell_idx = record.get("cell_idx")
    cell_key = f"cell{cell_idx}" if cell_idx is not None else None

    return NormalizedReplayRecord(
        source_label=SOURCE_TASK019,
        source_path=source_path,
        source_run_id=_file_run_id(source_path),
        timestamp_seconds=float(ts),
        model=str(model) if model else "gpt-5.2",
        prompt_tokens=int(prompt),
        cached_tokens=int(cached),
        max_output_tokens=int(max_output),
        observed_output_tokens=observed_output,
        observed_accepted=bool(observed_accepted),
        observed_429=bool(observed_429),
        observed_retry_after_ms=retry_ms,
        fallback_reasons=tuple(fallback),
        capacity_source=capacity_source,
        total_latency_ms=total_latency_ms,
        original_line_number=line_number,
        cell_key=cell_key,
        excluded_reason=excluded_reason,
    )


# --- File loading ----------------------------------------------------------


def load_jsonl(path: str) -> list[dict]:
    """Load a JSONL file. Returns an empty list for empty/whitespace-only files."""
    out: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            out.append(json.loads(stripped))
    return out


def adapt_records(
    raw_records: Iterable[dict],
    *,
    source_label: str,
    source_path: str,
    capacity_source: str,
) -> list[NormalizedReplayRecord]:
    """Apply the matching adapter and return normalized records sorted by
    ``(timestamp_seconds, original_line_number)``.
    """
    if source_label == SOURCE_TASK013:
        adapt_fn = adapt_task013_record
    elif source_label == SOURCE_TASK019:
        adapt_fn = adapt_task019_record
    else:
        raise ValueError(f"Unknown source_label: {source_label}")
    normalized = [
        adapt_fn(
            raw,
            source_path=source_path,
            line_number=i,
            capacity_source=capacity_source,
        )
        for i, raw in enumerate(raw_records)
    ]
    return sorted(normalized, key=lambda r: (r.timestamp_seconds, r.original_line_number))


def recover_zero_usage_429_demand(
    records: list[NormalizedReplayRecord],
) -> list[NormalizedReplayRecord]:
    """For 429 records with zero prompt/cached, copy demand shape from
    nearest accepted neighbor with the same source-run + cell_key.

    Adds the fallback reason ``"demand_recovered_from_neighbor"`` when a
    neighbor is found; otherwise ``"demand_recovery_no_neighbor"``.
    """
    # Index accepted neighbors by (source_run_id, cell_key).
    accepted: dict[tuple[str, str | None], NormalizedReplayRecord] = {}
    for r in records:
        if r.observed_accepted and r.prompt_tokens > 0:
            key = (r.source_run_id, r.cell_key)
            if key not in accepted:
                accepted[key] = r
    out: list[NormalizedReplayRecord] = []
    for r in records:
        if r.observed_429 and r.prompt_tokens == 0:
            key = (r.source_run_id, r.cell_key)
            neighbor = accepted.get(key)
            if neighbor is None:
                # Try source-only fallback.
                for (run_id, _), n in accepted.items():
                    if run_id == r.source_run_id:
                        neighbor = n
                        break
            if neighbor is not None:
                fallback = tuple(list(r.fallback_reasons) + ["demand_recovered_from_neighbor"])
                r = NormalizedReplayRecord(
                    **{**asdict(r),
                       "prompt_tokens": neighbor.prompt_tokens,
                       "cached_tokens": neighbor.cached_tokens,
                       "max_output_tokens": r.max_output_tokens or neighbor.max_output_tokens,
                       "fallback_reasons": fallback,
                       })
            else:
                fallback = tuple(list(r.fallback_reasons) + ["demand_recovery_no_neighbor"])
                r = NormalizedReplayRecord(**{**asdict(r), "fallback_reasons": fallback})
        out.append(r)
    return out


# --- Replay loop -----------------------------------------------------------


@dataclass(frozen=True)
class ReplayEvent:
    """One event emitted per source record during replay.

    ``kind`` is one of: ``"accepted"``, ``"predicted_429"``,
    ``"observed_429"`` (when the source record was observed-429 in
    default validation mode), ``"excluded"``.
    """
    kind: str
    timestamp_seconds: float
    record: NormalizedReplayRecord
    util_tokens_before: float
    admission_cost: int
    util_tokens_after: float
    predicted_retry_after_ms: float | None = None
    notes: tuple[str, ...] = ()


@dataclass
class ReplayState:
    util_tokens: float = 0.0
    last_event_time: float | None = None
    # Pending completion releases keyed by completion_time, stable order.
    pending_completions: list[tuple[float, int, int]] = field(default_factory=list)
    # tuple is (completion_time, reserved_tokens, actual_cost_or_negative)


def _apply_leak(state: ReplayState, now: float, k: float, ptu_count: float) -> None:
    if state.last_event_time is None:
        state.last_event_time = now
        return
    dt = now - state.last_event_time
    if dt > 0:
        state.util_tokens = max(0.0, state.util_tokens - leak_tokens(k, ptu_count, dt))
    state.last_event_time = now


def _drain_completions_up_to(state: ReplayState, now: float, k: float, ptu_count: float) -> None:
    state.pending_completions.sort(key=lambda x: x[0])
    while state.pending_completions and state.pending_completions[0][0] <= now:
        comp_time, reserved, actual = state.pending_completions.pop(0)
        _apply_leak(state, comp_time, k, ptu_count)
        if actual < 0:
            # observed_output_tokens missing: retain reservation.
            continue
        unused = max(0, reserved - actual)
        if unused > 0:
            state.util_tokens = max(0.0, state.util_tokens - float(unused))


def replay_stream(
    records: Sequence[NormalizedReplayRecord],
    *,
    k_leak_tokens_per_ptu_per_second: float,
    ptu_count: float,
    validation_mode: bool = True,
) -> list[ReplayEvent]:
    """Replay a stream of normalized records and emit events.

    Pure function. ``validation_mode=True`` (default) means observed-429
    source records do **not** mutate simulator state and do **not**
    schedule completion events; state is anchored to observed accepted
    traffic. Predicted retry-after for predicted-429 events is derived
    from token overshoot divided by token leak rate.
    """
    if not records:
        return []
    if k_leak_tokens_per_ptu_per_second <= 0:
        raise ValueError("k_leak_tokens_per_ptu_per_second must be positive")
    if ptu_count <= 0:
        raise ValueError("ptu_count must be positive")

    # All records in one stream must share a model for capacity lookup.
    model = records[0].model
    cap_tokens = capacity_tokens(model, ptu_count)

    state = ReplayState()
    events: list[ReplayEvent] = []

    sorted_recs = sorted(records, key=lambda r: (r.timestamp_seconds, r.original_line_number))
    for rec in sorted_recs:
        now = rec.timestamp_seconds
        # Drain any completions that landed before now (each drains in time order
        # and re-applies leak to the completion timestamp).
        _drain_completions_up_to(state, now, k_leak_tokens_per_ptu_per_second, ptu_count)
        # Leak to now after completions are drained.
        _apply_leak(state, now, k_leak_tokens_per_ptu_per_second, ptu_count)

        if rec.excluded_reason is not None:
            events.append(ReplayEvent(
                kind="excluded",
                timestamp_seconds=now,
                record=rec,
                util_tokens_before=state.util_tokens,
                admission_cost=0,
                util_tokens_after=state.util_tokens,
                notes=(rec.excluded_reason,),
            ))
            continue

        cost = admission_cost_tokens(
            rec.prompt_tokens, rec.cached_tokens, rec.max_output_tokens
        )

        # Observed 429/rejected source records: in default validation mode,
        # do not mutate state, do not schedule completion. Still emit an
        # event so callers can compare predicted vs observed.
        if rec.observed_429 and validation_mode:
            would_exceed = (state.util_tokens + cost) > cap_tokens
            predicted_retry_ms: float | None = None
            if would_exceed:
                overshoot = state.util_tokens + cost - cap_tokens
                predicted_retry_ms = (
                    overshoot / (k_leak_tokens_per_ptu_per_second * ptu_count) * 1000.0
                )
            events.append(ReplayEvent(
                kind="observed_429",
                timestamp_seconds=now,
                record=rec,
                util_tokens_before=state.util_tokens,
                admission_cost=cost,
                util_tokens_after=state.util_tokens,
                predicted_retry_after_ms=predicted_retry_ms,
                notes=("predicted_429_match",) if would_exceed else ("predicted_accept_false_negative",),
            ))
            continue

        would_exceed = (state.util_tokens + cost) > cap_tokens
        if would_exceed:
            overshoot = state.util_tokens + cost - cap_tokens
            predicted_retry_ms = (
                overshoot / (k_leak_tokens_per_ptu_per_second * ptu_count) * 1000.0
            )
            events.append(ReplayEvent(
                kind="predicted_429",
                timestamp_seconds=now,
                record=rec,
                util_tokens_before=state.util_tokens,
                admission_cost=cost,
                util_tokens_after=state.util_tokens,
                predicted_retry_after_ms=predicted_retry_ms,
                notes=(),
            ))
        else:
            state.util_tokens += float(cost)
            # Schedule reservation release at completion if accepted.
            if rec.observed_accepted and rec.total_latency_ms is not None and rec.total_latency_ms >= 0:
                comp_time = now + (rec.total_latency_ms / 1000.0)
                if rec.observed_output_tokens is None:
                    state.pending_completions.append((comp_time, cost, -1))
                else:
                    actual = max(
                        0,
                        max(0, rec.prompt_tokens - rec.cached_tokens) + rec.observed_output_tokens,
                    )
                    state.pending_completions.append((comp_time, cost, actual))
            events.append(ReplayEvent(
                kind="accepted",
                timestamp_seconds=now,
                record=rec,
                util_tokens_before=state.util_tokens - float(cost),
                admission_cost=cost,
                util_tokens_after=state.util_tokens,
            ))

    # Drain remaining completions (after last record) - not required for
    # event emission but keeps state consistent if caller inspects it.
    return events


# --- Calibration ----------------------------------------------------------


def _pair_predicted_observed(
    events: list[ReplayEvent],
) -> list[tuple[float, float]]:
    """Pair predicted and observed 429 events deterministically.

    Pairing rule (recorded in calibration.json/validation.md): within
    each ``(source_run_id, cell_key)``, pair events in source-event
    order. Predicted 429 events without an observed counterpart, and
    observed 429 events without a predicted counterpart, are dropped
    from residuals (they are reported separately as confusion-matrix
    false positives / false negatives).
    """
    predicted: dict[tuple[str, str | None], list[float]] = {}
    observed: dict[tuple[str, str | None], list[float]] = {}
    for ev in events:
        key = (ev.record.source_run_id, ev.record.cell_key)
        if ev.kind == "predicted_429":
            predicted.setdefault(key, []).append(ev.timestamp_seconds)
        elif ev.kind == "observed_429":
            observed.setdefault(key, []).append(ev.timestamp_seconds)
    pairs: list[tuple[float, float]] = []
    for key in sorted(set(predicted) | set(observed), key=lambda k: (k[0], k[1] or "")):
        p = predicted.get(key, [])
        o = observed.get(key, [])
        for pp, oo in zip(sorted(p), sorted(o)):
            pairs.append((pp, oo))
    return pairs


def _sum_sq_timestamp_residuals(pairs: list[tuple[float, float]]) -> float:
    return sum((p - o) ** 2 for p, o in pairs)


def _objective(
    k: float,
    *,
    streams: list[tuple[list[NormalizedReplayRecord], float]],
) -> float:
    if k <= 0:
        return float("inf")
    total_sq = 0.0
    n_pairs = 0
    for recs, ptu_count in streams:
        try:
            events = replay_stream(
                recs,
                k_leak_tokens_per_ptu_per_second=k,
                ptu_count=ptu_count,
                validation_mode=True,
            )
        except (ValueError, KeyError):
            return float("inf")
        pairs = _pair_predicted_observed(events)
        if pairs:
            total_sq += _sum_sq_timestamp_residuals(pairs)
            n_pairs += len(pairs)
    if n_pairs == 0:
        # No observed 429s to fit against: return a sentinel that pushes
        # the optimizer away without crashing.
        return float("inf")
    return total_sq


def calibrate_k(
    streams: list[tuple[list[NormalizedReplayRecord], float]],
    *,
    grid: Sequence[float] | None = None,
    refine_iters: int = 24,
) -> tuple[float, dict]:
    """Fit a single global ``k_leak_tokens_per_ptu_per_second``.

    Deterministic 1-D minimization: grid search over ``grid``, then
    golden-section refinement around the best grid point. Optimizer
    settings are returned alongside the fitted value so they can be
    written into ``calibration.json``.
    """
    if grid is None:
        # Geometric grid spanning a wide tokens/PTU/sec range.
        grid = tuple(round(2.0 ** i, 8) for i in range(-3, 13))  # ~0.125 .. 4096
    grid = tuple(sorted(set(float(x) for x in grid if x > 0)))
    obj_values = [(g, _objective(g, streams=streams)) for g in grid]
    finite = [(g, v) for g, v in obj_values if math.isfinite(v)]
    if not finite:
        return float("nan"), {
            "fit_status": "no_pairs",
            "grid": list(grid),
            "grid_values": [v if math.isfinite(v) else None for _, v in obj_values],
        }
    best_idx = min(range(len(finite)), key=lambda i: finite[i][1])
    best_k, best_val = finite[best_idx]
    # Golden-section refinement within neighbors.
    lo = finite[max(0, best_idx - 1)][0]
    hi = finite[min(len(finite) - 1, best_idx + 1)][0]
    if hi <= lo:
        hi = best_k * 1.5
        lo = best_k / 1.5
    phi = (1 + 5 ** 0.5) / 2
    invphi = 1 / phi
    a, b = lo, hi
    c = b - (b - a) * invphi
    d = a + (b - a) * invphi
    fc = _objective(c, streams=streams)
    fd = _objective(d, streams=streams)
    for _ in range(refine_iters):
        if fc < fd:
            b = d
            d, fd = c, fc
            c = b - (b - a) * invphi
            fc = _objective(c, streams=streams)
        else:
            a = c
            c, fc = d, fd
            d = a + (b - a) * invphi
            fd = _objective(d, streams=streams)
    refined_k = 0.5 * (a + b)
    refined_val = _objective(refined_k, streams=streams)
    if refined_val < best_val:
        best_k, best_val = refined_k, refined_val
    return best_k, {
        "fit_status": "ok",
        "grid": list(grid),
        "grid_values": [v if math.isfinite(v) else None for _, v in obj_values],
        "golden_section_refine_iters": refine_iters,
        "fit_value_sum_sq_seconds": best_val,
        "pairing_rule": "per_source_run_per_cell_then_event_order",
    }


# --- Holdout protocol ----------------------------------------------------


def leave_one_source_run_out(
    streams: list[tuple[str, list[NormalizedReplayRecord], float]],
) -> list[tuple[str, list[tuple[list[NormalizedReplayRecord], float]], tuple[list[NormalizedReplayRecord], float]]]:
    """Build deterministic LOSO folds.

    Input ``streams`` is a list of ``(source_run_id, records, ptu_count)``
    tuples. Output is a list of ``(holdout_source_run_id, fit_streams,
    holdout_stream)`` tuples in deterministic order. Streams without
    any observed 429 or any observed accepted record are not eligible
    holdouts.
    """
    eligible_ids = []
    for sid, recs, _ in streams:
        any_429 = any(r.observed_429 for r in recs)
        any_acc = any(r.observed_accepted for r in recs)
        if any_429 and any_acc:
            eligible_ids.append(sid)
    eligible_ids.sort()
    folds = []
    for hid in eligible_ids:
        fit = [(recs, ptu) for sid, recs, ptu in streams if sid != hid]
        holdout = next(((recs, ptu) for sid, recs, ptu in streams if sid == hid))
        folds.append((hid, fit, holdout))
    return folds


# --- Summary helpers ------------------------------------------------------


def confusion_matrix(events: list[ReplayEvent]) -> dict[str, int]:
    """Count true/false positives/negatives at the event level."""
    tp = fp = fn = tn = 0
    for ev in events:
        if ev.kind == "predicted_429":
            if ev.record.observed_429:
                tp += 1
            else:
                fp += 1
        elif ev.kind == "observed_429":
            # In validation mode this is the source-observed 429 branch.
            if "predicted_429_match" in ev.notes:
                tp += 1
            else:
                fn += 1
        elif ev.kind == "accepted":
            if ev.record.observed_429:
                # Cannot happen by construction.
                fn += 1
            else:
                tn += 1
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn}


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    vs = sorted(values)
    if len(vs) == 1:
        return vs[0]
    pos = q * (len(vs) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vs[lo]
    return vs[lo] + (vs[hi] - vs[lo]) * (pos - lo)


def summarize_timestamp_gaps(events: list[ReplayEvent]) -> dict[str, Any]:
    pairs = _pair_predicted_observed(events)
    gaps = [abs(p - o) for p, o in pairs]
    return {
        "n_paired": len(pairs),
        "p50_abs_gap_seconds": percentile(gaps, 0.5),
        "p95_abs_gap_seconds": percentile(gaps, 0.95),
    }


def summarize_retry_after_residuals(events: list[ReplayEvent]) -> dict[str, Any]:
    diffs = []
    for ev in events:
        if ev.kind != "observed_429":
            continue
        if ev.record.observed_retry_after_ms is None:
            continue
        if ev.predicted_retry_after_ms is None:
            continue
        diffs.append(abs(ev.predicted_retry_after_ms - ev.record.observed_retry_after_ms))
    return {
        "n_paired": len(diffs),
        "p50_abs_ms": percentile(diffs, 0.5),
        "p95_abs_ms": percentile(diffs, 0.95),
    }


__all__ = [
    "INPUT_TPM_PER_PTU",
    "SOURCE_TASK013",
    "SOURCE_TASK019",
    "TASK013_MAX_OUTPUT_TOKENS_FROM_YAML",
    "TASK013_YAML_PATHS",
    "NormalizedReplayRecord",
    "ReplayEvent",
    "ReplayState",
    "adapt_task013_record",
    "adapt_task019_record",
    "adapt_records",
    "calibrate_k",
    "confusion_matrix",
    "leave_one_source_run_out",
    "load_jsonl",
    "percentile",
    "recover_zero_usage_429_demand",
    "replay_stream",
    "summarize_retry_after_residuals",
    "summarize_timestamp_gaps",
]
