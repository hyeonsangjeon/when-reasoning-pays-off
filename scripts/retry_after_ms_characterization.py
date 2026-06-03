"""Task 020 — retry-after-ms recovery curve characterization.

Pure re-aggregation over existing immutable JSONL streams from
Task 013 (`benchmarks/05-dual-spillover/runs/*.jsonl`) and Task 019
(`benchmarks/07-max-output-tokens-reservation/runs/*.jsonl`).

Zero LLM spend. Zero network. Read-only over source JSONLs.

Source-aware 429 selection:
  - Task 013 record => 429 when `real_429_observed == true`.
  - Task 019 record => 429 when `429_observed == true` OR the record
    carries a `first_429_metadata` block.

No CIs, no p-values, no significance language, no causal / reset-formula
language. Descriptive empirical percentiles only, scoped to these source runs.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

ALLOWED_BENCHMARKS = {
    "05-dual-spillover": "task013",
    "07-max-output-tokens-reservation": "task019",
}

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Event:
    source: str
    benchmark_id: str
    retry_after_ms: float
    arrival_rpm_at_request_time: Optional[float] = None


@dataclass
class Counts:
    total_429: int = 0
    task013_429: int = 0
    task019_429: int = 0
    http_date_retry_after_skipped: int = 0
    unparseable_retry_after_ms_skipped: int = 0
    missing_retry_after_skipped: int = 0
    records_scanned: int = 0
    files_scanned: int = 0


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def parse_retry_after(record: dict) -> tuple[Optional[float], Optional[str]]:
    """Return (retry-after delay in MILLISECONDS, skip_reason).

    - retry_after_ms numeric  => milliseconds, as-is
    - retry_after numeric     => seconds * 1000
    - retry_after non-numeric => (None, "http_date") — caller counts and skips
    - missing                 => (None, "missing")
    """
    v_ms = record.get("retry_after_ms")
    if v_ms is not None:
        try:
            return float(v_ms), None
        except (TypeError, ValueError):
            return None, "unparseable_retry_after_ms"
    # accept both `retry_after` and (legacy) `retry_after_seconds`
    v = record.get("retry_after")
    if v is None:
        v = record.get("retry_after_seconds")
    if v is not None:
        try:
            return float(v) * 1000.0, None
        except (TypeError, ValueError):
            return None, "http_date"
    return None, "missing"


def is_429_event(record: dict, source: str) -> bool:
    """Source-aware 429 selector.

    Must NOT use a single shared field for both sources.
    """
    if source == "task013":
        return record.get("real_429_observed") is True
    if source == "task019":
        if record.get("429_observed") is True:
            return True
        if record.get("first_429_metadata"):
            return True
        return False
    return False


def empirical_percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolation empirical percentile. q in [0, 100]."""
    if not sorted_values:
        return float("nan")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = (q / 100.0) * (len(sorted_values) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_values[lo])
    frac = pos - lo
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac)


def summarize(values: list[float]) -> dict:
    if not values:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "p10": None,
            "p50": None,
            "p90": None,
            "p99": None,
        }
    s = sorted(values)
    return {
        "count": len(s),
        "min": float(s[0]),
        "max": float(s[-1]),
        "p10": empirical_percentile(s, 10),
        "p50": empirical_percentile(s, 50),
        "p90": empirical_percentile(s, 90),
        "p99": empirical_percentile(s, 99),
    }


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def resolve_jsonl_paths(benchmark_id: str, repo_root: Path) -> list[Path]:
    """Resolve allowlisted JSONL paths for a benchmark id.

    Fails closed on any benchmark id outside the allowlist.
    """
    if benchmark_id not in ALLOWED_BENCHMARKS:
        raise SystemExit(
            f"ERROR: benchmark id {benchmark_id!r} not in allowlist "
            f"{sorted(ALLOWED_BENCHMARKS)}"
        )
    runs_dir = repo_root / "benchmarks" / benchmark_id / "runs"
    if not runs_dir.is_dir():
        return []
    # only top-level *.jsonl, not summary/result sidecars and not quarantine dirs
    paths = sorted(
        p for p in runs_dir.glob("*.jsonl")
        if p.is_file() and not p.name.endswith(".summary.json")
    )
    # Defense in depth: ensure resolved paths remain under the allowed dir.
    resolved_dir = runs_dir.resolve()
    safe: list[Path] = []
    for p in paths:
        rp = p.resolve()
        try:
            rp.relative_to(resolved_dir)
        except ValueError:
            raise SystemExit(f"ERROR: path {p} escapes allowed runs dir")
        safe.append(rp)
    return safe


def iter_jsonl(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def extract_events(
    benchmark_ids: list[str], repo_root: Path
) -> tuple[list[Event], Counts]:
    counts = Counts()
    events: list[Event] = []
    for bid in benchmark_ids:
        source = ALLOWED_BENCHMARKS[bid]
        for path in resolve_jsonl_paths(bid, repo_root):
            counts.files_scanned += 1
            for record in iter_jsonl(path):
                counts.records_scanned += 1
                if not is_429_event(record, source):
                    continue
                ms, skip = parse_retry_after(record)
                if ms is None:
                    if skip == "http_date":
                        counts.http_date_retry_after_skipped += 1
                    elif skip == "unparseable_retry_after_ms":
                        counts.unparseable_retry_after_ms_skipped += 1
                    else:
                        counts.missing_retry_after_skipped += 1
                    # Still count the 429 in totals
                    counts.total_429 += 1
                    if source == "task013":
                        counts.task013_429 += 1
                    else:
                        counts.task019_429 += 1
                    continue
                counts.total_429 += 1
                if source == "task013":
                    counts.task013_429 += 1
                else:
                    counts.task019_429 += 1
                arr = record.get("arrival_rpm_at_request_time")
                try:
                    arr_val = float(arr) if arr is not None else None
                except (TypeError, ValueError):
                    arr_val = None
                events.append(
                    Event(
                        source=source,
                        benchmark_id=bid,
                        retry_after_ms=ms,
                        arrival_rpm_at_request_time=arr_val,
                    )
                )
    return events, counts


# ---------------------------------------------------------------------------
# Analysis assembly
# ---------------------------------------------------------------------------


def compute_overshoot(
    events: list[Event], counts: Counts
) -> dict:
    """Overshoot correlation is `not_computable` for these source runs.

    Task 013 v2 records expose no numeric per-record projected/admitted
    utilization proxy and no capacity denominator. Task 019 records
    expose `arrival_rpm_at_request_time` but no calibrated capacity
    denominator (selected_peak_tps is null in the available calibration
    outcomes), so an overshoot-above-100% quantity cannot be computed.
    """
    return {
        "status": "not_computable",
        "reason": (
            "no numeric projected/admitted utilization proxy with a calibrated "
            "capacity denominator is present in Task 013 v2 records; Task 019 v2 "
            "records expose arrival_rpm_at_request_time but no calibrated capacity "
            "denominator (selected_peak_tps null in available calibration outcomes), "
            "so overshoot-above-100% cannot be computed for these source runs"
        ),
    }


def _shape_for_values(values: list[float]) -> dict:
    if not values:
        return {
            "count": 0,
            "unique_count": 0,
            "unique_ratio": None,
            "integer_ms_share": None,
            "top_values": [],
            "appearance": "no retry-after values observed",
        }
    counts = Counter(values)
    unique_count = len(counts)
    n = len(values)
    integer_count = sum(1 for v in values if abs(v - round(v)) < 1e-9)
    unique_ratio = unique_count / n
    mode_share = counts.most_common(1)[0][1] / n
    top_values = [
        {"retry_after_ms": float(v), "count": int(c)}
        for v, c in counts.most_common(10)
    ]
    if unique_count == 1:
        appearance = "single repeated value"
    elif unique_ratio <= 0.30 or mode_share >= 0.10:
        appearance = "clustered / integer-ms quantized"
    else:
        appearance = "relatively continuous"
    return {
        "count": n,
        "unique_count": unique_count,
        "unique_ratio": unique_ratio,
        "integer_ms_share": integer_count / n,
        "top_values": top_values,
        "appearance": appearance,
    }


def describe_distribution_shape(events: list[Event]) -> dict:
    all_values = [e.retry_after_ms for e in events]
    t013 = [e.retry_after_ms for e in events if e.source == "task013"]
    t019 = [e.retry_after_ms for e in events if e.source == "task019"]
    overall = _shape_for_values(all_values)
    task013 = _shape_for_values(t013)
    task019 = _shape_for_values(t019)
    return {
        "summary": (
            "Observed retry-after values appear clustered / integer-ms quantized, "
            "not continuous, in these source runs."
            if overall["appearance"] != "relatively continuous"
            else "Observed retry-after values appear relatively continuous in these source runs."
        ),
        "overall": overall,
        "by_source": {
            "task013": task013,
            "task019": task019,
        },
    }


def build_analysis(
    events: list[Event], counts: Counts
) -> dict:
    all_values = [e.retry_after_ms for e in events]
    t013 = [e.retry_after_ms for e in events if e.source == "task013"]
    t019 = [e.retry_after_ms for e in events if e.source == "task019"]

    sparse = counts.total_429 < 50
    imbalanced = False
    if counts.total_429 > 0:
        max_side = max(counts.task013_429, counts.task019_429)
        imbalanced = (max_side / counts.total_429) >= 0.80

    return {
        "schema_version": "task020.v1",
        "scope": "single-tenant source-run-scoped re-aggregation; descriptive only",
        "counts": {
            "total_429": counts.total_429,
            "task013_429": counts.task013_429,
            "task019_429": counts.task019_429,
            "http_date_retry_after_skipped": counts.http_date_retry_after_skipped,
            "unparseable_retry_after_ms_skipped": counts.unparseable_retry_after_ms_skipped,
            "missing_retry_after_skipped": counts.missing_retry_after_skipped,
            "records_scanned": counts.records_scanned,
            "files_scanned": counts.files_scanned,
        },
        "flags": {
            "sparse": sparse,
            "imbalanced": imbalanced,
            "sparse_threshold": "total_429 < 50",
            "imbalanced_threshold": "max(task013_429, task019_429)/total_429 >= 0.80",
        },
        "overall_distribution": summarize(all_values),
        "per_source_distribution": {
            "task013": summarize(t013),
            "task019": summarize(t019),
        },
        "by_mechanism": {
            "task013_burst_driven": {
                "source": "task013",
                "benchmark_id": "05-dual-spillover",
                "description": (
                    "Primary-deployment 429s observed under dual-endpoint burst load "
                    "(Task 013 Phase 2 dual-spillover); workload-shaped, not "
                    "customer-attributed"
                ),
                "distribution": summarize(t013),
            },
            "task019_reservation_driven": {
                "source": "task019",
                "benchmark_id": "07-max-output-tokens-reservation",
                "description": (
                    "429s observed during max_output_tokens reservation sweep on a "
                    "PAYG-throttled deployment (proxy for admission-control behavior, "
                    "not direct PTU evidence)"
                ),
                "distribution": summarize(t019),
            },
        },
        "distribution_shape": describe_distribution_shape(events),
        "correlation_with_overshoot": compute_overshoot(events, counts),
        "caveats": [
            "Task 019 source is PAYG-throttled-quota, not direct PTU evidence.",
            "Task 013 source is workload-shaped and not customer-attributed.",
            "Findings are descriptive of these source runs only; do not generalize "
            "across tenants, regions, deployments, model versions, or time periods.",
            "Practical advice: honor the retry-after / retry-after-ms header Azure "
            "returns. The observed p50/p99 are descriptive context only.",
        ],
        "methodology": {
            "stat_rules": (
                "empirical percentiles only (p10/p50/p90/p99/min/max/count); "
                "no CIs, no p-values, no significance language, no causal/reset-formula "
                "language"
            ),
            "parse_rules": (
                "retry_after_ms numeric => ms; retry_after numeric => seconds*1000; "
                "non-numeric retry_after (HTTP-date / token) => skipped and counted "
                "in counts.http_date_retry_after_skipped"
            ),
            "selectors": {
                "task013": "real_429_observed == true",
                "task019": "429_observed == true OR first_429_metadata present",
            },
        },
    }


# ---------------------------------------------------------------------------
# Charts and CSVs
# ---------------------------------------------------------------------------


def write_event_csv(events: list[Event], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["source", "benchmark_id", "retry_after_ms"])
        for e in events:
            w.writerow([e.source, e.benchmark_id, f"{e.retry_after_ms:.3f}"])


def write_percentile_csv(analysis: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        ("scope", "count", "min", "p10", "p50", "p90", "p99", "max"),
    ]
    for label, dist in (
        ("overall", analysis["overall_distribution"]),
        ("task013", analysis["per_source_distribution"]["task013"]),
        ("task019", analysis["per_source_distribution"]["task019"]),
    ):
        rows.append(
            (
                label,
                dist["count"],
                dist["min"],
                dist["p10"],
                dist["p50"],
                dist["p90"],
                dist["p99"],
                dist["max"],
            )
        )
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        for r in rows:
            w.writerow(r)


README_TEMPLATE = """# Benchmark 08 — `retry-after-ms` recovery curve characterization

**Task 020.** Pure re-aggregation over existing immutable JSONL streams
from Task 013 (`benchmarks/05-dual-spillover/runs/*.jsonl`) and Task 019
(`benchmarks/07-max-output-tokens-reservation/runs/*.jsonl`).

- **Zero new LLM spend.** No API calls. No network. No client imports.
- **Read-only** over source JSONLs (allowlisted paths only).
- **Descriptive only**, scoped to these source runs. **No** causal /
  reset-formula language. **No** confidence intervals, p-values, or
  significance claims. **No** universal PTU claim.

## What this benchmark is (and is not)

This benchmark **does not** capture new `retry-after-ms` data. Tasks 013
and 019 own that capture. This benchmark re-aggregates the
`retry-after-ms` / `retry-after` values already recorded in their raw
JSONLs into an empirical distribution, and labels every event with its
**source benchmark id** so per-source provenance is never erased.

## Source-aware 429 selection

The two source streams use **different** 429 detection field names. The
aggregator must honor both — using a single shared field would silently
drop one stream.

| Source   | Benchmark id                          | 429 selector |
|----------|---------------------------------------|--------------|
| Task 013 | `05-dual-spillover`                   | `real_429_observed == true` |
| Task 019 | `07-max-output-tokens-reservation`    | `429_observed == true` **OR** `first_429_metadata` present |

Per-source counts (`counts.task013_429`, `counts.task019_429`) are
reported separately in `analysis.json` in addition to the combined view;
combined percentiles never erase per-source provenance.

## Parsing rules

`scripts/retry_after_ms_characterization.py` applies these rules
verbatim:

- `retry_after_ms` numeric → milliseconds as-is
- `retry_after` numeric → seconds × 1000
- `retry_after` non-numeric (HTTP-date / token per RFC 9110) → **skipped
  and counted** in `counts.http_date_retry_after_skipped` (never silently
  dropped). An HTTP-date branch may be added later behind an explicit
  flag.
- missing → skipped and counted in `counts.missing_retry_after_skipped`

## How to regenerate

```bash
python -m scripts.retry_after_ms_characterization \\
  --benchmarks 05-dual-spillover,07-max-output-tokens-reservation \\
  --out benchmarks/08-retry-after-characterization/analysis.json
```

Charts and CSVs are written to `results/retry-after-characterization/`.
This `README.md` and the sibling `analysis.md` are bootstrapped by the
same command from embedded templates in the script.

## PTU / PAYG / customer-scope caveats (carried forward)

- **Task 019 source is PAYG-throttled-quota, not direct PTU evidence.**
  Use it as a proxy for admission-control behavior; do not state PTU
  causal claims based on Task 019 data alone.
- **Task 013 source is workload-shaped and not customer-attributed.** Do
  not generalize observed `retry-after-ms` shapes to other tenants,
  regions, deployments, model versions, or time periods.
- "Operationally confirmed" framing is **not** used here. Findings are
  described as "observed in these source runs" or "consistent with the
  documented Azure guidance" only.
- Customer-facing advice is limited to: **honor the `retry-after` /
  `retry-after-ms` header Azure returns**. The observed p50 / p99 are
  descriptive context only.

## Reference

The Azure PTU concept documentation describes `retry-after-ms` (and the
HTTP-standard `retry-after`) on a 429 response as the dynamic admission
signal — there is no deterministic reset window. This benchmark
re-aggregates the values we observed in two specific source runs; it is
**not** a universal characterization of Azure behavior and does not
propose any reset formula.
"""


def _fmt(v):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


def render_analysis_md(analysis: dict) -> str:
    c = analysis["counts"]
    f = analysis["flags"]
    over = analysis["overall_distribution"]
    t013 = analysis["per_source_distribution"]["task013"]
    t019 = analysis["per_source_distribution"]["task019"]

    sparse_banner = ""
    if f.get("sparse") or f.get("imbalanced"):
        flags = []
        if f.get("sparse"):
            flags.append("**sparse** (total_429 < 50)")
        if f.get("imbalanced"):
            flags.append("**imbalanced** (one source ≥ 80% of events)")
        sparse_banner = (
            "> **Caveat (lead):** results are " + " and ".join(flags) + ". "
            "Treat percentiles as shape-only context, not as calibrated targets. "
            "Follow-up captures needed to lift this caveat: (a) additional Task 013 "
            "Phase 2 proactive runs with primary-overcommit configuration that "
            "produces ≥ 50 real_429 events with retry-after headers; (b) additional "
            "Task 019 calibration runs whose explored TPS envelope crosses the "
            "deployment 429 admission ceiling so 429s with retry-after headers are "
            "observed at non-trivial depth.\n"
        )

    overshoot = analysis["correlation_with_overshoot"]
    shape = analysis["distribution_shape"]
    shape_overall = shape["overall"]
    shape_t013 = shape["by_source"]["task013"]
    shape_t019 = shape["by_source"]["task019"]
    over_block = (
        f"`correlation_with_overshoot.status = {overshoot.get('status')!r}`. "
        f"Reason: {overshoot.get('reason','')}\n\n"
        "No scatter plot is emitted because the proxy is not computable for these "
        "source runs.\n"
    )

    p50_advice = _fmt(over.get("p50"))
    p99_advice = _fmt(over.get("p99"))

    return f"""# Task 020 — `retry-after-ms` characterization (analysis)

> Decision-grade narrative — descriptive of **these source runs only**.
> No CIs, no p-values, no significance language, no causal /
> reset-formula language. No universal PTU claim.

## TL;DR

**Recommendation:** honor the `retry-after-ms` (or `retry-after`) header
that Azure returns on every 429. Do not substitute a fixed-window timer.
In these source runs the observed p50 ≈ `{p50_advice}` ms and p99 ≈
`{p99_advice}` ms (combined; see per-source breakdown below). These are
**descriptive context only** — do not generalize across tenants,
regions, deployments, model versions, or time periods.

{sparse_banner}
## What we measured

Re-aggregation of the `retry-after-ms` / `retry-after` field already
captured on every 429 in two existing source streams:

- Task 013 — `benchmarks/05-dual-spillover/runs/*.jsonl` (dual-endpoint
  burst load; 429 selector `real_429_observed == true`)
- Task 019 — `benchmarks/07-max-output-tokens-reservation/runs/*.jsonl`
  (max_output_tokens reservation sweep on a PAYG-throttled deployment;
  429 selector `429_observed == true` OR `first_429_metadata` present)

Counts (`counts` block in `analysis.json`):

| metric | value |
|---|---|
| files_scanned | {_fmt(c.get("files_scanned"))} |
| records_scanned | {_fmt(c.get("records_scanned"))} |
| total_429 | {_fmt(c.get("total_429"))} |
| task013_429 | {_fmt(c.get("task013_429"))} |
| task019_429 | {_fmt(c.get("task019_429"))} |
| http_date_retry_after_skipped | {_fmt(c.get("http_date_retry_after_skipped"))} |
| unparseable_retry_after_ms_skipped | {_fmt(c.get("unparseable_retry_after_ms_skipped"))} |
| missing_retry_after_skipped | {_fmt(c.get("missing_retry_after_skipped"))} |

Sparse flag: `{f.get("sparse")}` (threshold: total_429 < 50).
Imbalanced flag: `{f.get("imbalanced")}` (threshold: one source ≥ 80%).

## Findings

### Distribution of `retry-after-ms` (descriptive)

Empirical percentiles (linear-interpolated), in milliseconds:

| scope   | count | min | p10 | p50 | p90 | p99 | max |
|---------|-------|-----|-----|-----|-----|-----|-----|
| overall | {_fmt(over.get("count"))} | {_fmt(over.get("min"))} | {_fmt(over.get("p10"))} | {_fmt(over.get("p50"))} | {_fmt(over.get("p90"))} | {_fmt(over.get("p99"))} | {_fmt(over.get("max"))} |
| task013 (burst) | {_fmt(t013.get("count"))} | {_fmt(t013.get("min"))} | {_fmt(t013.get("p10"))} | {_fmt(t013.get("p50"))} | {_fmt(t013.get("p90"))} | {_fmt(t013.get("p99"))} | {_fmt(t013.get("max"))} |
| task019 (reservation) | {_fmt(t019.get("count"))} | {_fmt(t019.get("min"))} | {_fmt(t019.get("p10"))} | {_fmt(t019.get("p50"))} | {_fmt(t019.get("p90"))} | {_fmt(t019.get("p99"))} | {_fmt(t019.get("max"))} |

Charts:

- `results/retry-after-characterization/retry_after_ms_histogram.png` —
  source-labeled histogram overlay
- `results/retry-after-characterization/retry_after_ms_cdf.png` —
  empirical CDF per source plus combined

CSVs:

- `results/retry-after-characterization/retry_after_ms_events.csv`
- `results/retry-after-characterization/retry_after_ms_percentiles.csv`

### Correlation with overshoot

{over_block}

## Interpretation

These are **two different 429 mechanisms**:

- Task 013 429s come from primary-deployment burst overload in a
  dual-endpoint experiment. Workload-shaped; not customer-attributed.
- Task 019 429s come from PAYG admission control on a throttled
  deployment exercised by a `max_output_tokens` reservation sweep. PAYG
  throttled-quota is a **proxy** for admission-control behavior, not
  direct PTU evidence.

Quantization / continuity answer: **{shape.get("summary")}** Overall,
{_fmt(shape_overall.get("unique_count"))} unique values appeared across
{_fmt(shape_overall.get("count"))} events (unique ratio
{_fmt(shape_overall.get("unique_ratio"))}; integer-ms share
{_fmt(shape_overall.get("integer_ms_share"))}). Task 013 was classified
as `{shape_t013.get("appearance")}`; Task 019 was classified as
`{shape_t019.get("appearance")}`. This is a descriptive observation
about these source runs only and not a universal property of the service.

The observed shape is **consistent with the documented Azure guidance**
that there is no fixed reset window, but it does **not** "operationally
confirm" any universal PTU behavior.

## Decision

For a customer's retry wrapper, the two operational answers are:

1. In these source runs, observed `retry-after-ms` values look
   **{shape_overall.get("appearance")}**, not like a smooth continuous
   distribution.
2. **Honor the `retry-after-ms` (or `retry-after`) header Azure returns
   on every 429.** Do not substitute a fixed timer.
3. Treat the observed p50 / p99 above as descriptive context only.
   Calibrate retry behavior against your own traffic.
4. Do not infer a deterministic reset window or a universal PTU formula
   from this data. None is supported by these source runs.

## Limitations

- Single tenant, single region, snapshot in time. Source runs scoped.
- Task 019 source is PAYG-throttled-quota, not direct PTU.
- Task 013 source is workload-shaped, not customer-attributed.
- No CIs / no significance tests by methodology rule (§8). Percentiles
  are point estimates.
- `correlation_with_overshoot` is not computable for these source runs:
  Task 013 v2 records expose no numeric per-record projected/admitted
  utilization proxy; Task 019 records expose `arrival_rpm_at_request_time`
  but no calibrated capacity denominator (`selected_peak_tps` is null in
  the available calibration outcomes), so overshoot-above-100% cannot be
  computed.
- HTTP-date `retry-after` headers (per RFC 9110) are skipped and
  counted; an explicit HTTP-date parsing branch may be added later
  behind a flag.
"""


def render_charts(events: list[Event], out_dir: Path) -> dict:
    """Render histogram + CDF PNGs. Returns dict describing what was written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    if not events:
        return written
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return written

    t013 = [e.retry_after_ms for e in events if e.source == "task013"]
    t019 = [e.retry_after_ms for e in events if e.source == "task019"]
    all_v = sorted(e.retry_after_ms for e in events)

    # Histogram (overlay)
    fig, ax = plt.subplots(figsize=(8, 4.5))
    if t013:
        ax.hist(t013, bins=20, alpha=0.55, label=f"task013 (n={len(t013)})", color="#1f77b4")
    if t019:
        ax.hist(t019, bins=20, alpha=0.55, label=f"task019 (n={len(t019)})", color="#ff7f0e")
    ax.set_xlabel("retry_after_ms (ms)")
    ax.set_ylabel("count")
    ax.set_title("Task 020 — retry_after_ms histogram (source-labeled, descriptive)")
    ax.legend()
    fig.tight_layout()
    hist_path = out_dir / "retry_after_ms_histogram.png"
    fig.savefig(hist_path, dpi=120)
    plt.close(fig)
    written["histogram"] = str(hist_path)

    # CDF
    def cdf_xy(values: list[float]) -> tuple[list[float], list[float]]:
        s = sorted(values)
        n = len(s)
        ys = [(i + 1) / n for i in range(n)]
        return s, ys

    fig, ax = plt.subplots(figsize=(8, 4.5))
    if t013:
        xs, ys = cdf_xy(t013)
        ax.plot(xs, ys, label=f"task013 (n={len(t013)})", color="#1f77b4")
    if t019:
        xs, ys = cdf_xy(t019)
        ax.plot(xs, ys, label=f"task019 (n={len(t019)})", color="#ff7f0e")
    if all_v:
        xs, ys = cdf_xy(all_v)
        ax.plot(xs, ys, label=f"combined (n={len(all_v)})", color="#2ca02c", linestyle="--")
    ax.set_xlabel("retry_after_ms (ms)")
    ax.set_ylabel("empirical CDF")
    ax.set_title("Task 020 — retry_after_ms CDF (source-labeled, descriptive)")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    cdf_path = out_dir / "retry_after_ms_cdf.png"
    fig.savefig(cdf_path, dpi=120)
    plt.close(fig)
    written["cdf"] = str(cdf_path)

    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="retry_after_ms_characterization",
        description="Task 020 — retry-after-ms re-aggregation (zero spend, no network)",
    )
    p.add_argument(
        "--benchmarks",
        required=True,
        help="Comma-separated benchmark ids. Allowlist: "
        + ",".join(sorted(ALLOWED_BENCHMARKS)),
    )
    p.add_argument(
        "--out",
        required=True,
        help="Path to write analysis.json",
    )
    p.add_argument(
        "--results-dir",
        default="results/retry-after-characterization",
        help="Directory for charts/CSVs",
    )
    p.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help=argparse.SUPPRESS,
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    bids = [b.strip() for b in args.benchmarks.split(",") if b.strip()]
    bad = [b for b in bids if b not in ALLOWED_BENCHMARKS]
    if bad:
        print(
            f"ERROR: benchmark id(s) {bad!r} not in allowlist "
            f"{sorted(ALLOWED_BENCHMARKS)}",
            file=sys.stderr,
        )
        return 2

    repo_root = Path(args.repo_root).resolve()
    events, counts = extract_events(bids, repo_root)
    analysis = build_analysis(events, counts)

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = repo_root / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(analysis, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Bootstrap README.md and analysis.md alongside analysis.json from
    # embedded templates. These are decision-grade narrative; values are
    # injected from the freshly computed analysis dict.
    readme_path = out_path.parent / "README.md"
    readme_path.write_text(README_TEMPLATE, encoding="utf-8")
    analysis_md_path = out_path.parent / "analysis.md"
    analysis_md_path.write_text(render_analysis_md(analysis), encoding="utf-8")

    results_dir = Path(args.results_dir)
    if not results_dir.is_absolute():
        results_dir = repo_root / results_dir
    write_event_csv(events, results_dir / "retry_after_ms_events.csv")
    write_percentile_csv(analysis, results_dir / "retry_after_ms_percentiles.csv")
    render_charts(events, results_dir)

    print(
        json.dumps(
            {
                "wrote": str(out_path),
                "results_dir": str(results_dir),
                "counts": analysis["counts"],
                "flags": analysis["flags"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
