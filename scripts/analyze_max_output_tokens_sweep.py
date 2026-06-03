"""scripts/analyze_max_output_tokens_sweep.py — Task 019 v2.1/v2.2.1
analysis + chart generator.

Reads ONE evidence- or smoke-stage JSONL + its companion ``*.summary.json``
and writes three headline charts plus a markdown table to populate
``benchmarks/07-max-output-tokens-reservation/analysis.md``:

  1. visible_output_tokens_p50_vs_cap.png — flat (predicted) vs slope
     (would indicate the cap drives the reasoning budget upward, an
     unexpected confound).
  2. first_429_arrival_rpm_vs_cap.png — predicted to decrease
     monotonically if the throttled admission layer reserves at the
     cap. None/NaN cells (no 429 observed in the ramp budget) are
     marked.
  3. cache_hit_ratio_vs_cap.png — sanity check that prompt-cache
     namespacing is per-cell × per-run and that the warm criterion
     holds.

Also prints the Stage-1 smoke gate / Stage-2 429-contrast verdict block
verbatim (PASS / FAIL with reason), so a smoke run that completed warm
but produced 0 429s in the largest cell is loudly marked unfit for
promotion to Stage 2 (Task 019 v2.1 protocol-correction requirement).

v2.2.1 note — this analyzer is intentionally protocol-version-agnostic
because both v2.1 and v2.2.1 emit the same per-cell ``n_429_records`` +
``warm_criterion_passed`` + ``cache_hit_ratio_steady_state`` fields. The
only operational difference is that v2.2.1 smoke/evidence summaries
additionally carry ``selected_peak_tps``, ``calibration_result_path``,
``calibration_result_sha256``, and ``calibration_run_id_short`` linkage
fields, which are echoed in the run-totals footer for traceability.

PAYG-not-PTU: this script does NOT call Azure. All inputs are local
artifacts under ``benchmarks/07-max-output-tokens-reservation/runs/``.

Usage:
  python -m scripts.analyze_max_output_tokens_sweep \\
      --summary benchmarks/07-max-output-tokens-reservation/runs/<TS>_exp007_max_output_tokens_sweep_evidence.jsonl.summary.json \\
      [--out-dir benchmarks/07-max-output-tokens-reservation/runs/figures]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any


def load_summary(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate_smoke_gate(summary: dict[str, Any]) -> dict[str, Any]:
    """Return the smoke / evidence 429-contrast gate block for a summary
    dict, preferring the runner-written field but falling back to a local
    recomputation from ``cell_summaries`` and ``sweep_planned`` for
    backwards compatibility with summaries written before the
    protocol-correction hotfix.

    The fallback uses the same rule as
    :func:`scripts.measure_max_output_tokens_sweep.evaluate_smoke_gate_block`:

    - PASS only if (a) the largest planned cap was reached, (b) its
      ``n_429_records`` ≥ 1, and (c) the smallest planned cap has
      ``n_429_records`` == 0.
    - FAIL otherwise, with a machine-readable ``reason`` string.

    A FAIL gate means the smoke run **cannot** be used as a Stage-2 go
    signal.
    """
    stage = summary.get("stage")
    key = (
        "smoke_gate" if stage == "smoke"
        else "evidence_429_contrast_gate"
    )
    block = summary.get(key)
    if block is not None:
        return block
    # Fallback recomputation (older smoke summaries did not embed this).
    cell_summaries = summary.get("cell_summaries", [])
    sweep_planned = summary.get("sweep_planned", []) or [
        int(c["max_output_tokens"]) for c in cell_summaries
    ]
    try:
        from scripts.measure_max_output_tokens_sweep import (
            evaluate_smoke_gate_block,
        )
    except ImportError:  # pragma: no cover — fallback for partial repos
        return _evaluate_smoke_gate_inline(cell_summaries, sweep_planned)
    return evaluate_smoke_gate_block(
        cell_summaries=cell_summaries,
        sweep_planned=list(sweep_planned),
    )


def _evaluate_smoke_gate_inline(
    cell_summaries: list[dict[str, Any]],
    sweep_planned: list[int],
) -> dict[str, Any]:
    """Pure-Python fallback identical in shape to
    ``evaluate_smoke_gate_block`` for the case where the runner module is
    unavailable (e.g. an external reader analysing a summary in isolation).
    """
    if not cell_summaries:
        return {
            "passed": False, "reason": "no_cell_summaries",
            "largest_cell_max_output_tokens": None,
            "largest_cell_n_429": 0,
            "smallest_cell_max_output_tokens": None,
            "smallest_cell_n_429": 0,
            "cells_completed": 0,
            "cells_planned": len(sweep_planned),
            "stage2_promotable": False,
        }
    by_mo = {int(c["max_output_tokens"]): c for c in cell_summaries}
    smallest_mo = min(by_mo)
    largest_mo = max(by_mo)
    smallest_n = int(by_mo[smallest_mo].get("n_429_records", 0) or 0)
    largest_n = int(by_mo[largest_mo].get("n_429_records", 0) or 0)
    planned_largest = max(sweep_planned) if sweep_planned else None
    if planned_largest is None or largest_mo != planned_largest:
        reason = "largest_cell_not_reached"
        passed = False
    elif largest_n < 1:
        reason = "no_429_in_largest_cell"
        passed = False
    elif smallest_n > 0:
        reason = "unexpected_429_in_smallest_cell"
        passed = False
    else:
        reason = "ok"
        passed = True
    return {
        "passed": passed,
        "reason": reason,
        "largest_cell_max_output_tokens": largest_mo,
        "largest_cell_n_429": largest_n,
        "smallest_cell_max_output_tokens": smallest_mo,
        "smallest_cell_n_429": smallest_n,
        "cells_completed": len(cell_summaries),
        "cells_planned": len(sweep_planned),
        "stage2_promotable": passed,
    }


def render_charts(summary: dict[str, Any], out_dir: pathlib.Path) -> list[pathlib.Path]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print(
            "matplotlib not installed; install with `pip install matplotlib` "
            "to render figures. Numeric tables still emitted below.",
            file=sys.stderr,
        )
        return []

    cells = sorted(
        summary["cell_summaries"], key=lambda c: int(c["max_output_tokens"])
    )
    caps = [int(c["max_output_tokens"]) for c in cells]

    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[pathlib.Path] = []

    # 1. visible_output_tokens p50 vs cap
    visible_p50 = [c["visible_output_tokens_p50_steady_state"] for c in cells]
    visible_p95 = [c["visible_output_tokens_p95_steady_state"] for c in cells]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(caps, visible_p50, marker="o", label="p50")
    ax.plot(caps, visible_p95, marker="s", linestyle="--", label="p95")
    ax.plot(caps, caps, color="gray", linestyle=":", alpha=0.5, label="cap (max_output_tokens)")
    ax.set_xscale("log", base=2)
    ax.set_yscale("log", base=2)
    ax.set_xlabel("cell max_output_tokens (cap)")
    ax.set_ylabel("visible_output_tokens (steady-state)")
    ax.set_title(
        "Task 019 v2.1 — visible_output_tokens vs cap (PAYG, throttled)\n"
        "If reservation-at-cap is the *only* effect, this should be roughly flat."
    )
    ax.grid(alpha=0.3)
    ax.legend()
    p = out_dir / "visible_output_tokens_p50_vs_cap.png"
    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    paths.append(p)

    # 2. first_429_arrival_rpm vs cap
    f429 = summary.get("first_429_arrival_rpm_per_cell", {})
    rpms_by_cap = []
    for cap in caps:
        v = f429.get(str(cap))
        rpms_by_cap.append(v if v is not None else float("nan"))
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(caps, rpms_by_cap, marker="o", label="first 429-onset RPM")
    if any(v is None or (isinstance(v, float) and v != v) for v in rpms_by_cap):
        no_429 = [
            cap for cap, v in zip(caps, rpms_by_cap)
            if v is None or (isinstance(v, float) and v != v)
        ]
        ax.scatter(no_429, [0] * len(no_429), color="red", marker="x", s=60, label="no 429 in ramp")
    ax.set_xscale("log", base=2)
    ax.set_xlabel("cell max_output_tokens (cap)")
    ax.set_ylabel("admitted RPM at first 429")
    ax.set_title(
        "Task 019 v2.1 — first 429-onset RPM vs cap (PAYG, throttled)\n"
        "Reservation-at-cap predicts monotone DECREASE as cap grows."
    )
    ax.grid(alpha=0.3)
    ax.legend()
    p = out_dir / "first_429_arrival_rpm_vs_cap.png"
    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    paths.append(p)

    # 3. cache_hit_ratio vs cap
    cache = [c["cache_hit_ratio_steady_state"] for c in cells]
    warm = [c["warm_criterion_passed"] for c in cells]
    fig, ax = plt.subplots(figsize=(8, 5))
    colors = ["tab:blue" if w else "tab:red" for w in warm]
    ax.bar([str(c) for c in caps], cache, color=colors)
    ax.set_xlabel("cell max_output_tokens (cap)")
    ax.set_ylabel("cache_hit_ratio (steady state)")
    ax.set_title(
        "Task 019 v2.1 — cache-hit ratio per cell (blue = warm, red = cold)\n"
        "Each cell has its own prompt_cache_key namespace."
    )
    ax.set_ylim(0, 1)
    ax.grid(alpha=0.3, axis="y")
    p = out_dir / "cache_hit_ratio_vs_cap.png"
    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    paths.append(p)

    return paths


def render_markdown(summary: dict[str, Any]) -> str:
    cells = sorted(
        summary["cell_summaries"], key=lambda c: int(c["max_output_tokens"])
    )
    stage = summary.get("stage", "unknown")
    lines = []
    lines.append("### Headline tables (auto-populated)\n")

    # Gate verdict — always first so a smoke FAIL is impossible to miss.
    gate = evaluate_smoke_gate(summary)
    verdict_label = (
        "SMOKE GATE" if stage == "smoke"
        else "EVIDENCE 429-CONTRAST GATE" if stage == "evidence"
        else "GATE"
    )
    verdict_state = "PASS ✅" if gate["passed"] else "FAIL ❌"
    lines.append(f"#### 0. {verdict_label}: **{verdict_state}**\n")
    lines.append(f"- reason: `{gate['reason']}`")
    lines.append(
        f"- largest planned cap = "
        f"`{gate['largest_cell_max_output_tokens']}` → "
        f"`n_429_records = {gate['largest_cell_n_429']}`"
    )
    lines.append(
        f"- smallest planned cap = "
        f"`{gate['smallest_cell_max_output_tokens']}` → "
        f"`n_429_records = {gate['smallest_cell_n_429']}`"
    )
    lines.append(
        f"- cells_completed = {gate['cells_completed']} / "
        f"cells_planned = {gate['cells_planned']}"
    )
    lines.append(
        f"- stage2_promotable: **{gate['stage2_promotable']}** "
        f"(a Stage-1 smoke is promotable to Stage 2 ONLY if this is True)"
    )
    if not gate["passed"]:
        lines.append(
            "- **Action:** do NOT promote to Stage 2. Investigate (likely "
            "candidates: peak_ramp_tps too low, deployment quota larger "
            "than the documented 60 K TPM, sweep largest cap too small, "
            "cache-key namespacing absorbing the reservation, etc.). "
            "Document a v2.x spec revision before any further live run."
        )
    lines.append("")

    lines.append("#### 1. `visible_output_tokens` vs cap\n")
    lines.append("| cap | n | p50 visible | p95 visible | reasoning p50 |")
    lines.append("|---:|---:|---:|---:|---:|")
    for c in cells:
        lines.append(
            f"| {c['max_output_tokens']} | {c['n_records']} | "
            f"{c['visible_output_tokens_p50_steady_state']:.0f} | "
            f"{c['visible_output_tokens_p95_steady_state']:.0f} | "
            f"{c['reasoning_tokens_p50_steady_state']:.0f} |"
        )
    lines.append("")
    lines.append("#### 2. `first_429_arrival_rpm` vs cap\n")
    f429 = summary.get("first_429_arrival_rpm_per_cell", {})
    n429 = summary.get("n_429_records_per_cell", {}) or {
        # Fallback for older summaries written before the
        # protocol-correction hotfix: pull n_429 directly from each
        # cell-summary entry so the table column never shows `None` for a
        # count (a count is always an int, never missing).
        str(c["max_output_tokens"]): int(c.get("n_429_records", 0) or 0)
        for c in cells
    }
    lines.append("| cap | n_429 | first-429 RPM | warm | backlog excess |")
    lines.append("|---:|---:|---:|---|---|")
    for c in cells:
        cap = c["max_output_tokens"]
        rpm = f429.get(str(cap))
        rpm_s = f"{rpm}" if rpm is not None else "—"
        count = int(n429.get(str(cap), 0) or 0)
        lines.append(
            f"| {cap} | {count} | {rpm_s} | "
            f"{'✅' if c['warm_criterion_passed'] else '❌'} | "
            f"{'⚠️ yes' if c['backlog_excessive'] else 'no'} |"
        )
    lines.append("")
    lines.append("#### 3. Cache-hit ratio per cell\n")
    lines.append("| cap | cache_hit_ratio | cell USD |")
    lines.append("|---:|---:|---:|")
    for c in cells:
        lines.append(
            f"| {c['max_output_tokens']} | "
            f"{c['cache_hit_ratio_steady_state']:.3f} | "
            f"${c['cell_usd']:.4f} |"
        )
    lines.append("")
    lines.append(
        f"**Run totals:** {summary['cells_completed']}/{summary['cells_planned']} "
        f"cells; total USD ${summary['total_usd']:.4f} "
        f"(hard ceiling ${summary['hard_ceiling_usd']}; mid-run threshold "
        f"${summary['midrun_threshold_usd']}); partial = {summary['partial']}; "
        f"halt_reason = {summary.get('halt_reason')!r}; "
        f"backlog_excessive_any = {summary['backlog_excessive_any']}; "
        f"cache_not_warm_any = {summary['cache_not_warm_any']}; "
        f"max_in_flight_observed = {summary['max_in_flight_observed_run']}."
    )
    # v2.2.1 — echo calibration linkage when present (smoke + evidence
    # summaries from v2.2.1 carry these fields; v2.1 summaries do not).
    sel_tps = summary.get("selected_peak_tps")
    if sel_tps is not None:
        lines.append("")
        lines.append(
            f"**Calibration linkage (v2.2.1):** selected_peak_tps = "
            f"`{sel_tps}`; calibration_run_id_short = "
            f"`{summary.get('calibration_run_id_short')}`; "
            f"calibration_result_sha256 = "
            f"`{summary.get('calibration_result_sha256')}`; "
            f"calibration_result_path = "
            f"`{summary.get('calibration_result_path')}`."
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--summary", required=True, type=pathlib.Path)
    ap.add_argument(
        "--out-dir",
        type=pathlib.Path,
        default=pathlib.Path(
            "benchmarks/07-max-output-tokens-reservation/runs/figures"
        ),
    )
    ap.add_argument(
        "--require-gate-pass",
        action="store_true",
        help=(
            "Exit non-zero (3) if the smoke / 429-contrast gate fails. "
            "Use in CI / orchestrator scripts that must refuse to promote "
            "a smoke run to Stage 2 unless the gate verdict is PASS."
        ),
    )
    args = ap.parse_args(argv)
    summary = load_summary(args.summary)
    md = render_markdown(summary)
    print(md)
    chart_paths = render_charts(summary, args.out_dir)
    if chart_paths:
        print("Chart paths:", file=sys.stderr)
        for p in chart_paths:
            print(f"  {p}", file=sys.stderr)
    if args.require_gate_pass:
        gate = evaluate_smoke_gate(summary)
        if not gate["passed"]:
            print(
                f"GATE_VERDICT=FAIL reason={gate['reason']!r} — refusing "
                f"to certify promotion to Stage 2.",
                file=sys.stderr,
            )
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

