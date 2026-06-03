#!/usr/bin/env python3
"""Task 024 CLI: offline PTU utilization replay simulator.

Loads explicit source JSONLs, normalizes them through legacy adapters,
fits the leak constant ``k_leak_tokens_per_ptu_per_second`` via
deterministic 1-D optimization, runs source-run holdout where feasible,
and writes calibration + validation artifacts under
``benchmarks/10-replay-validation/`` and charts under
``results/replay-validation/``.

This module performs **no** network I/O. It uses only Python stdlib
plus matplotlib (Agg backend) for chart generation. The Azure / OpenAI
SDK is not imported.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Make ``batch_runner`` importable when invoked from repo root without a
# wheel install (mirrors ``batch-runner/tests/conftest.py``).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BR_ROOT = _REPO_ROOT / "batch-runner"
if str(_BR_ROOT) not in sys.path:
    sys.path.insert(0, str(_BR_ROOT))

from batch_runner.ptu.replay_simulator import (  # noqa: E402
    INPUT_TPM_PER_PTU,
    SOURCE_TASK013,
    SOURCE_TASK019,
    adapt_records,
    calibrate_k,
    confusion_matrix,
    leave_one_source_run_out,
    load_jsonl,
    recover_zero_usage_429_demand,
    replay_stream,
    summarize_retry_after_residuals,
    summarize_timestamp_gaps,
)


def _model_from_records(records) -> str:
    for r in records:
        if r.model:
            return r.model
    return "gpt-5.2"


def _effective_ptu_from_quota(model: str, deployment_tpm_quota: float) -> float:
    return float(deployment_tpm_quota) / float(INPUT_TPM_PER_PTU[model])


def _classify_input(path: Path) -> str:
    p = str(path)
    if "05-dual-spillover" in p:
        return SOURCE_TASK013
    if "07-max-output-tokens-reservation" in p:
        return SOURCE_TASK019
    raise ValueError(f"Cannot classify source for path: {path}")


def _build_streams(
    inputs: list[Path],
    *,
    ptu_count: float | None,
    deployment_tpm_quota: float | None,
) -> tuple[list[tuple[str, list, float]], dict]:
    """Return (streams, diagnostics).

    Each stream is ``(source_run_id, normalized_records, effective_ptu_count)``.
    """
    streams = []
    empty_files = []
    diagnostics: dict[str, Any] = {
        "inputs": [str(p) for p in inputs],
        "empty_files_skipped": [],
        "per_stream": [],
    }
    for p in inputs:
        if not p.exists():
            raise FileNotFoundError(p)
        raw = load_jsonl(str(p))
        if not raw:
            empty_files.append(str(p))
            continue
        label = _classify_input(p)
        # Capacity source label.
        if ptu_count is not None:
            capacity_source = "declared_ptu_count"
            effective_ptu = float(ptu_count)
        elif deployment_tpm_quota is not None:
            capacity_source = "payg_quota_proxy"
            # Model is read from the first record after normalization.
            tmp = adapt_records(raw[:1], source_label=label, source_path=str(p), capacity_source="tentative")
            model = _model_from_records(tmp)
            effective_ptu = _effective_ptu_from_quota(model, deployment_tpm_quota)
        else:
            diagnostics.setdefault("excluded_streams", []).append({
                "path": str(p),
                "reason": "missing_capacity_denominator",
            })
            continue
        normalized = adapt_records(
            raw,
            source_label=label,
            source_path=str(p),
            capacity_source=capacity_source,
        )
        normalized = recover_zero_usage_429_demand(normalized)
        sid = os.path.basename(str(p))
        streams.append((sid, normalized, effective_ptu))
        diagnostics["per_stream"].append({
            "source_run_id": sid,
            "source_label": label,
            "source_path": str(p),
            "capacity_source": capacity_source,
            "effective_ptu_count": effective_ptu,
            "n_records": len(normalized),
            "n_observed_429": sum(1 for r in normalized if r.observed_429),
            "n_observed_accepted": sum(1 for r in normalized if r.observed_accepted),
            "n_excluded": sum(1 for r in normalized if r.excluded_reason),
            "fallback_reason_counts": _count_fallbacks(normalized),
        })
    diagnostics["empty_files_skipped"] = empty_files
    diagnostics["empty_files_skipped_count"] = len(empty_files)
    return streams, diagnostics


def _count_fallbacks(records) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in records:
        for reason in r.fallback_reasons:
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def _events_to_summary(events) -> dict[str, Any]:
    cm = confusion_matrix(events)
    ts = summarize_timestamp_gaps(events)
    ra = summarize_retry_after_residuals(events)
    return {
        "confusion_matrix": cm,
        "onset_timestamp_residuals": ts,
        "retry_after_residuals": ra,
        "n_events": len(events),
    }


def _write_charts(events_all, out_dir: Path) -> None:
    import matplotlib  # local import keeps imports lazy
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: E402

    out_dir.mkdir(parents=True, exist_ok=True)

    # Chart 1: predicted vs observed 429 timestamps (paired per stream).
    # Build per-stream pairing using same rule as calibration.
    pred_xs: list[float] = []
    obs_xs: list[float] = []
    per_run_pred: dict[tuple[str, str | None], list[float]] = {}
    per_run_obs: dict[tuple[str, str | None], list[float]] = {}
    for ev in events_all:
        key = (ev.record.source_run_id, ev.record.cell_key)
        if ev.kind == "predicted_429":
            per_run_pred.setdefault(key, []).append(ev.timestamp_seconds)
        elif ev.kind == "observed_429":
            per_run_obs.setdefault(key, []).append(ev.timestamp_seconds)
    for key in sorted(set(per_run_pred) | set(per_run_obs), key=lambda k: (k[0], k[1] or "")):
        p = sorted(per_run_pred.get(key, []))
        o = sorted(per_run_obs.get(key, []))
        for pp, oo in zip(p, o):
            pred_xs.append(pp)
            obs_xs.append(oo)

    fig, ax = plt.subplots(figsize=(6, 6))
    if pred_xs and obs_xs:
        ax.scatter(obs_xs, pred_xs, s=12, alpha=0.7)
        lim_lo = min(min(obs_xs), min(pred_xs))
        lim_hi = max(max(obs_xs), max(pred_xs))
        ax.plot([lim_lo, lim_hi], [lim_lo, lim_hi], "k--", linewidth=1, alpha=0.5)
    else:
        ax.text(0.5, 0.5, "no paired 429 events", ha="center", va="center", transform=ax.transAxes)
    ax.set_xlabel("Observed 429 timestamp (seconds)")
    ax.set_ylabel("Predicted 429 timestamp (seconds)")
    ax.set_title("Predicted vs observed 429 onset (paired)")
    fig.tight_layout()
    fig.savefig(out_dir / "predicted_vs_observed_429_timestamps.png", dpi=120)
    plt.close(fig)

    # Chart 2: predicted vs observed retry-after-ms scatter.
    px: list[float] = []
    py: list[float] = []
    for ev in events_all:
        if ev.kind != "observed_429":
            continue
        if ev.predicted_retry_after_ms is None or ev.record.observed_retry_after_ms is None:
            continue
        px.append(ev.record.observed_retry_after_ms)
        py.append(ev.predicted_retry_after_ms)
    fig, ax = plt.subplots(figsize=(6, 6))
    if px and py:
        ax.scatter(px, py, s=12, alpha=0.7)
        lim_lo = min(min(px), min(py))
        lim_hi = max(max(px), max(py))
        ax.plot([lim_lo, lim_hi], [lim_lo, lim_hi], "k--", linewidth=1, alpha=0.5)
    else:
        ax.text(0.5, 0.5, "no paired retry-after observations", ha="center", va="center", transform=ax.transAxes)
    ax.set_xlabel("Observed retry-after (ms)")
    ax.set_ylabel("Predicted retry-after (ms)")
    ax.set_title("Predicted vs observed retry-after magnitude")
    fig.tight_layout()
    fig.savefig(out_dir / "predicted_vs_observed_retry_after_ms.png", dpi=120)
    plt.close(fig)


def _format_validation_md(
    *,
    fitted_k: float,
    optimizer_info: dict,
    diagnostics: dict,
    in_sample_summary: dict,
    holdout_summaries: list[dict],
    n_eligible_streams: int,
) -> str:
    parts = ["# Task 024 — Replay validation report\n"]
    parts.append(
        "This report is an approximate replay of legacy source runs against "
        "the Guide §0 admission-reservation formula and a fitted continuous "
        "leak constant. It is a candidate input for capacity planning with "
        "source-run caveats. It is not a generalized PTU predictor across "
        "deployments without re-calibration.\n"
    )
    parts.append("## Source caveats\n")
    parts.append(
        "- `task013_dual_spillover` runs use Azure PAYG deployments shaped to "
        "expose a PTU-like saturation pattern; they are not customer-attributed "
        "native PTU evidence.\n"
        "- `task019_max_output_tokens_proxy` runs are PAYG-throttled-quota "
        "proxy evidence, not direct PTU validation.\n"
        "- Inputs are normalized through legacy adapters; Task 028 canonical "
        "schema is not used here.\n"
        "- Guide §3 model-specific output-token weighting ratios are not "
        "modeled in v1.\n"
        "- Divergences may be consistent with burst tolerance, the "
        "reserve/release completion approximation, missing source fields, or "
        "proxy-source mismatch. Those are not causal conclusions.\n"
    )
    parts.append(f"## Fitted leak constant\n\n- `k_leak_tokens_per_ptu_per_second = {fitted_k:.6g}`\n")
    parts.append(f"- Optimizer settings: `{json.dumps(optimizer_info, sort_keys=True)}`\n")
    parts.append("\n## In-sample calibration residuals\n")
    parts.append(f"- Confusion matrix: `{json.dumps(in_sample_summary['confusion_matrix'], sort_keys=True)}`\n")
    parts.append(
        f"- Onset timestamp residuals (paired, abs seconds): "
        f"p50={in_sample_summary['onset_timestamp_residuals'].get('p50_abs_gap_seconds')}, "
        f"p95={in_sample_summary['onset_timestamp_residuals'].get('p95_abs_gap_seconds')}, "
        f"n_paired={in_sample_summary['onset_timestamp_residuals'].get('n_paired')}\n"
    )
    parts.append(
        f"- Retry-after-ms residuals (paired, abs ms): "
        f"p50={in_sample_summary['retry_after_residuals'].get('p50_abs_ms')}, "
        f"p95={in_sample_summary['retry_after_residuals'].get('p95_abs_ms')}, "
        f"n_paired={in_sample_summary['retry_after_residuals'].get('n_paired')}\n"
    )

    if holdout_summaries:
        label = "source-run holdout, limited N" if n_eligible_streams < 5 else "source-run holdout"
        parts.append(f"\n## Out-of-sample residuals ({label})\n")
        for h in holdout_summaries:
            parts.append(
                f"- Held-out source `{h['holdout_source_run_id']}` "
                f"(fitted on N={h['n_fit_streams']}): "
                f"k={h['fitted_k']:.6g}; "
                f"confusion={json.dumps(h['summary']['confusion_matrix'], sort_keys=True)}; "
                f"onset p50/p95 seconds={h['summary']['onset_timestamp_residuals'].get('p50_abs_gap_seconds')}/"
                f"{h['summary']['onset_timestamp_residuals'].get('p95_abs_gap_seconds')}; "
                f"retry-after p50/p95 ms={h['summary']['retry_after_residuals'].get('p50_abs_ms')}/"
                f"{h['summary']['retry_after_residuals'].get('p95_abs_ms')}\n"
            )
    else:
        parts.append("\n## Out-of-sample residuals\n")
        parts.append("Holdout is not feasible (fewer than two eligible source runs). "
                     "Residuals above are in-sample calibration residuals only.\n")

    parts.append("\n## Source diagnostics\n")
    parts.append(f"- Empty files skipped: {diagnostics.get('empty_files_skipped_count', 0)}\n")
    for s in diagnostics.get("per_stream", []):
        parts.append(
            f"- `{s['source_run_id']}` ({s['source_label']}, "
            f"capacity_source={s['capacity_source']}, "
            f"effective_ptu_count={s['effective_ptu_count']}): "
            f"n_records={s['n_records']}, observed_429={s['n_observed_429']}, "
            f"observed_accepted={s['n_observed_accepted']}, excluded={s['n_excluded']}\n"
        )
    if diagnostics.get("excluded_streams"):
        parts.append("\n### Streams excluded from calibration\n")
        for s in diagnostics["excluded_streams"]:
            parts.append(f"- `{s['path']}`: {s['reason']}\n")
    parts.append(
        "\n## Methodology notes\n"
        "- Predicted/observed 429 events are paired per `(source_run_id, cell_key)` "
        "in event order. Unpaired predicted/observed events surface as confusion-matrix "
        "false positives / false negatives.\n"
        "- The simulator uses a single token-denominated bucket: `util_tokens`, "
        "`capacity_tokens`, token leak, token overshoot, and token-derived "
        "`predicted_retry_after_ms`. Capacity comes from the Guide §3 Input TPM / PTU "
        "table; only `k_leak_tokens_per_ptu_per_second` is fitted.\n"
        "- Default validation mode does not mutate simulator state on observed-429 "
        "source records.\n"
    )
    return "".join(parts)


def _format_readme_md() -> str:
    return (
        "# Task 024 replay validation\n\n"
        "This directory is generated by `scripts/replay_ptu_utilization.py` "
        "from explicit local JSONL inputs. The replay is offline and uses the "
        "Guide admission-reservation formula with a fitted continuous leak "
        "constant.\n\n"
        "## Artifacts\n\n"
        "- `calibration.json`: fitted leak constant, official-spec inputs, "
        "capacity-source labels, optimizer details, diagnostics, and residual summaries.\n"
        "- `validation.md`: source caveats, calibration summary, residual summaries, "
        "and methodology notes.\n"
        "- `runs/.gitkeep`: placeholder for optional future replay-run snapshots.\n\n"
        "Charts are written to `results/replay-validation/`:\n\n"
        "- `predicted_vs_observed_429_timestamps.png`\n"
        "- `predicted_vs_observed_retry_after_ms.png`\n\n"
        "The outputs are a candidate input for capacity planning with source-run "
        "caveats, not a generalized predictor across deployments.\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Task 024 offline PTU utilization replay simulator (zero-spend)."
    )
    parser.add_argument(
        "--input", action="append", required=True,
        help="Explicit JSONL source path. Pass --input multiple times.",
    )
    parser.add_argument("--ptu-count", type=float, default=None,
                        help="Declared PTU count for capacity denominator.")
    parser.add_argument("--deployment-tpm-quota", type=float, default=None,
                        help="PAYG TPM quota; mapped to effective PTU via Guide §3 table "
                             "(labeled capacity_source=payg_quota_proxy, operational inference).")
    parser.add_argument("--benchmarks-out", type=Path,
                        default=Path("benchmarks/10-replay-validation"))
    parser.add_argument("--results-out", type=Path,
                        default=Path("results/replay-validation"))
    parser.add_argument("--skip-charts", action="store_true",
                        help="Do not emit chart PNGs (use for fast smoke).")
    args = parser.parse_args(argv)

    if args.ptu_count is None and args.deployment_tpm_quota is None:
        parser.error("Must pass either --ptu-count or --deployment-tpm-quota")
    if args.ptu_count is not None and args.ptu_count <= 0:
        parser.error("--ptu-count must be positive")
    if args.deployment_tpm_quota is not None and args.deployment_tpm_quota <= 0:
        parser.error("--deployment-tpm-quota must be positive")

    inputs = [Path(p) for p in args.input]
    streams, diagnostics = _build_streams(
        inputs,
        ptu_count=args.ptu_count,
        deployment_tpm_quota=args.deployment_tpm_quota,
    )

    if not streams:
        print("No eligible streams to calibrate; writing diagnostics only.", file=sys.stderr)

    # In-sample calibration over all streams.
    fit_streams = [(recs, ptu) for _, recs, ptu in streams]
    fitted_k, optimizer_info = calibrate_k(fit_streams) if fit_streams else (float("nan"), {"fit_status": "no_streams"})

    # Run replay with fitted_k on every stream to gather in-sample events.
    in_sample_events = []
    if not (fitted_k != fitted_k):  # not NaN
        for recs, ptu in fit_streams:
            in_sample_events.extend(replay_stream(
                recs,
                k_leak_tokens_per_ptu_per_second=fitted_k,
                ptu_count=ptu,
                validation_mode=True,
            ))
    in_sample_summary = _events_to_summary(in_sample_events)

    # Holdout protocol.
    holdout_summaries = []
    folds = leave_one_source_run_out(streams) if len(streams) >= 2 else []
    for hid, fit_set, holdout in folds:
        h_k, _ = calibrate_k(fit_set)
        if h_k != h_k:  # NaN
            continue
        h_events = replay_stream(
            holdout[0],
            k_leak_tokens_per_ptu_per_second=h_k,
            ptu_count=holdout[1],
            validation_mode=True,
        )
        holdout_summaries.append({
            "holdout_source_run_id": hid,
            "fitted_k": h_k,
            "n_fit_streams": len(fit_set),
            "summary": _events_to_summary(h_events),
        })

    # Write calibration.json
    args.benchmarks_out.mkdir(parents=True, exist_ok=True)
    args.results_out.mkdir(parents=True, exist_ok=True)
    runs_dir = args.benchmarks_out / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / ".gitkeep").touch()
    (args.results_out / ".gitkeep").touch()

    calibration_payload = {
        "schema_version": "task024_v1",
        "k_leak_tokens_per_ptu_per_second": fitted_k,
        "official_spec_inputs": {
            "admission_formula": "max(0, prompt_tokens - cached_tokens) + max_output_tokens",
            "capacity_formula": "input_tpm_per_ptu[model] * ptu_count",
            "input_tpm_per_ptu_table": dict(INPUT_TPM_PER_PTU),
            "source_guide_sections": ["Guide §0 (admission reservation, continuous leak)",
                                       "Guide §3 (Input TPM / PTU)"],
        },
        "operational_inference": {
            "k_leak_tokens_per_ptu_per_second": "fitted; numeric constant is operational inference per Task 029",
            "payg_quota_proxy_mapping": "effective_ptu_count = deployment_tpm_quota / input_tpm_per_ptu[model]",
            "completion_reconciliation": "reserve at admission; release unused reservation at completion",
            "demand_recovery": "neighbor (source_run_id, cell_key) accepted record",
        },
        "optimizer": {
            "method": "deterministic grid + golden-section refine",
            **optimizer_info,
            "pairing_rule": "per (source_run_id, cell_key), event order",
        },
        "capacity_inputs": {
            "ptu_count": args.ptu_count,
            "deployment_tpm_quota": args.deployment_tpm_quota,
        },
        "source_streams": diagnostics.get("per_stream", []),
        "excluded_streams": diagnostics.get("excluded_streams", []),
        "empty_files_skipped": diagnostics.get("empty_files_skipped", []),
        "empty_files_skipped_count": diagnostics.get("empty_files_skipped_count", 0),
        "in_sample_summary": in_sample_summary,
        "holdout_eligible_n_streams": len(folds),
        "holdout_summaries": holdout_summaries,
    }
    (args.benchmarks_out / "calibration.json").write_text(
        json.dumps(calibration_payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.benchmarks_out / "README.md").write_text(_format_readme_md(), encoding="utf-8")

    md = _format_validation_md(
        fitted_k=fitted_k,
        optimizer_info=optimizer_info,
        diagnostics=diagnostics,
        in_sample_summary=in_sample_summary,
        holdout_summaries=holdout_summaries,
        n_eligible_streams=len(folds),
    )
    (args.benchmarks_out / "validation.md").write_text(md, encoding="utf-8")

    if not args.skip_charts:
        _write_charts(in_sample_events, args.results_out)

    print(json.dumps({
        "fitted_k": fitted_k,
        "n_streams": len(streams),
        "n_in_sample_events": len(in_sample_events),
        "n_holdout_folds": len(holdout_summaries),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
