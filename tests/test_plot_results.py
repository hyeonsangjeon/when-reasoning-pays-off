"""Tests for ``scripts.plot_results``.

Coverage focus (.internal/tasks/008-analysis-pipeline.md):

* Every required chart pair is materialized (PNG + sibling CSV).
* CSV is the source of truth; the PNG is a derived artifact (the test
  inspects CSV contents directly).
* Throughput-gain CSV declares its baseline (no naked PTU figures).
* PAYG CSV carries the pricing citation (URL + accessed date + snapshot path).
* The quality chart's CSV uses ``std`` only — never ``ci`` / ``sem`` columns.
"""

from __future__ import annotations

import csv
import io
import json
import pathlib
import subprocess
import sys

from scripts.plot_results import (
    BENCH_CHART_PREFIX,
    CHART_PALETTE,
    build_chart_payloads,
    render_all,
)


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PRICING_DIR = REPO_ROOT / "pricing"


# ----------------------------------------------------------------------------
# Helpers — minimal analysis.json synthesis
# ----------------------------------------------------------------------------


def _make_cell_stat(
    *,
    model: str,
    effort: str | None,
    mean_input: float = 250.0,
    mean_output: float = 14.0,
    mean_reasoning: float = 100.0,
    mean_judge: float = 1.8,
    mean_latency: float = 1500.0,
    mean_usd: float = 0.002,
) -> dict:
    return {
        "model": model,
        "effort": effort,
        "n_used": 60,
        "n_excluded": 0,
        "mean_input_tokens": mean_input,
        "std_input_tokens": 18.0,
        "mean_cached_tokens": 0.0,
        "std_cached_tokens": 0.0,
        "mean_output_tokens": mean_output,
        "std_output_tokens": 3.5,
        "mean_reasoning_tokens": mean_reasoning,
        "std_reasoning_tokens": 20.0,
        "mean_total_tokens": mean_input + mean_output,
        "std_total_tokens": 19.0,
        "mean_latency_ms": mean_latency,
        "std_latency_ms": 220.0,
        "mean_judge_score": mean_judge,
        "std_judge_score": 0.4,
        "judge_n": 60,
        "mean_usd_per_request": mean_usd,
        "std_usd_per_request": 0.0003,
        "pricing_citation_id": "payg_primary",
    }


def _make_analysis_payload() -> dict:
    cell_stats = [
        _make_cell_stat(model="gpt-4o", effort=None, mean_reasoning=0.0, mean_usd=0.000747),
        _make_cell_stat(model="gpt-5.2", effort="minimal", mean_reasoning=4.0, mean_usd=0.000663),
        _make_cell_stat(model="gpt-5.2", effort="low", mean_reasoning=30.0, mean_usd=0.001037),
        _make_cell_stat(model="gpt-5.2", effort="medium", mean_reasoning=88.0, mean_usd=0.001865),
        _make_cell_stat(model="gpt-5.2", effort="high", mean_reasoning=185.0, mean_usd=0.003257),
        _make_cell_stat(model="gpt-5.2", effort="xhigh", mean_reasoning=310.0, mean_usd=0.005043),
    ]
    ptu_table = []
    base_tokens = 250.0 + 14.0  # gpt-4o
    for s in cell_stats:
        # tokens_per_request = input + output (Azure GPT-5.x contract:
        # reasoning is a labelled subset of output and would double-count if
        # added separately).
        tpr = s["mean_input_tokens"] + s["mean_output_tokens"]
        ptu_table.append(
            {
                "model": s["model"],
                "effort": s["effort"],
                "tokens_per_request": tpr,
                "throughput_gain_factor": base_tokens / tpr,
                "baseline_label": "gpt-4o baseline (mean tokens-per-request)",
            }
        )

    cells = []
    for s in cell_stats:
        for r in range(3):
            cells.append(
                {
                    "sample_id": "sf_01",
                    "model": s["model"],
                    "effort": s["effort"],
                    "repeat": r,
                    "input_tokens": int(s["mean_input_tokens"]),
                    "cached_tokens": 0,
                    "output_tokens": int(s["mean_output_tokens"]),
                    "reasoning_tokens": int(s["mean_reasoning_tokens"]),
                    "total_tokens": int(s["mean_input_tokens"] + s["mean_output_tokens"]),
                    "latency_ms": s["mean_latency_ms"] + r * 5.0,
                    "judge_score": 2,
                    "outlier_reason": None,
                    "event_cold_start": False,
                    "event_retry_count": 0,
                    "event_truncated_output": False,
                    "source_path": "fake",
                }
            )

    return {
        "schema_version": "008.1",
        "benchmark": "01-short-factual",
        "experiment_prefix": "exp001_short-factual_baseline",
        "run_count": len(cells),
        "cells_count": len(cells),
        "experiment_ids": ["exp001_short-factual_baseline", "exp001_short-factual_baseline_gpt4o"],
        "git_commits": ["test-commit"],
        "pricing_citations": {
            "payg_primary": {
                "lens": "payg",
                "snapshot_path": "pricing/azure-openai-payg-2026-05.yaml",
                "source_url": "https://azure.microsoft.com/en-us/pricing/details/azure-openai/",
                "accessed_date": "2026-05-19",
                "archive_url": None,
                "currency": "USD",
            }
        },
        "ptu_baseline": {
            "label": "gpt-4o baseline (mean tokens-per-request)",
            "tokens_per_request": base_tokens,
        },
        "cells": cells,
        "cell_stats": cell_stats,
        "ptu_gain_by_cell": ptu_table,
        "outliers": [],
        "judge_breakdown_by_tag": {},
    }


def _seed_analysis(tmp_path: pathlib.Path) -> pathlib.Path:
    payload = _make_analysis_payload()
    p = tmp_path / "analysis.json"
    p.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    return p


def _read_csv(path: pathlib.Path) -> tuple[list[str], list[dict[str, str]]]:
    text = path.read_text(encoding="utf-8")
    # Strip trailing comment rows starting with "#" before parsing.
    lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
    rdr = csv.DictReader(io.StringIO("\n".join(lines)))
    return rdr.fieldnames or [], list(rdr)


# ----------------------------------------------------------------------------
# Existence + completeness of every required artifact
# ----------------------------------------------------------------------------


def test_render_all_produces_every_required_pair(tmp_path: pathlib.Path) -> None:
    analysis_path = _seed_analysis(tmp_path)
    out_root = tmp_path / "results"
    targets = render_all(analysis_path, out_root)
    for key, p in targets.items():
        assert p.exists(), f"missing {key} -> {p}"
    # Spec-mandated filenames (exact strings):
    expected = {
        out_root / "cost-curves" / f"{BENCH_CHART_PREFIX}-cost-per-request.png",
        out_root / "cost-curves" / f"{BENCH_CHART_PREFIX}-cost-per-request.csv",
        out_root / "cost-curves" / f"{BENCH_CHART_PREFIX}-throughput-gain.png",
        out_root / "cost-curves" / f"{BENCH_CHART_PREFIX}-throughput-gain.csv",
        out_root / "cost-curves" / f"{BENCH_CHART_PREFIX}-latency.png",
        out_root / "cost-curves" / f"{BENCH_CHART_PREFIX}-latency.csv",
        out_root / "cost-curves" / f"{BENCH_CHART_PREFIX}-quality.png",
        out_root / "cost-curves" / f"{BENCH_CHART_PREFIX}-quality.csv",
        out_root / "token-composition" / f"{BENCH_CHART_PREFIX}-tokens.png",
        out_root / "token-composition" / f"{BENCH_CHART_PREFIX}-tokens.csv",
    }
    actual_targets = set(targets.values())
    assert expected.issubset(actual_targets)


# ----------------------------------------------------------------------------
# CSV is the source of truth — auditable without reading the PNG
# ----------------------------------------------------------------------------


def test_cost_csv_carries_pricing_citation(tmp_path: pathlib.Path) -> None:
    analysis_path = _seed_analysis(tmp_path)
    out_root = tmp_path / "results"
    render_all(analysis_path, out_root)
    csv_path = out_root / "cost-curves" / f"{BENCH_CHART_PREFIX}-cost-per-request.csv"
    headers, rows = _read_csv(csv_path)
    assert "mean_usd_per_request" in headers
    assert "pricing_source_url" in headers
    assert "pricing_accessed_date" in headers
    assert "pricing_snapshot_path" in headers
    for row in rows:
        assert row["pricing_source_url"].startswith("https://")
        assert row["pricing_accessed_date"]
        assert row["pricing_snapshot_path"]


def test_throughput_csv_declares_baseline(tmp_path: pathlib.Path) -> None:
    """Spec: 'Every PTU figure declares its baseline.'"""
    analysis_path = _seed_analysis(tmp_path)
    out_root = tmp_path / "results"
    render_all(analysis_path, out_root)
    csv_path = out_root / "cost-curves" / f"{BENCH_CHART_PREFIX}-throughput-gain.csv"
    headers, rows = _read_csv(csv_path)
    assert "baseline_label" in headers
    assert "throughput_gain_factor" in headers
    for row in rows:
        assert row["baseline_label"]
        # Factor must be parseable as float
        float(row["throughput_gain_factor"])

    # Trailing comment row must restate the baseline (so a CSV-only reader
    # still sees it even if the column is filtered out).
    text = csv_path.read_text(encoding="utf-8")
    assert "baseline_label:" in text
    assert "throughput_gain_factor =" in text


def test_quality_csv_uses_std_only_never_ci_or_sem(tmp_path: pathlib.Path) -> None:
    analysis_path = _seed_analysis(tmp_path)
    out_root = tmp_path / "results"
    render_all(analysis_path, out_root)
    csv_path = out_root / "cost-curves" / f"{BENCH_CHART_PREFIX}-quality.csv"
    headers, _ = _read_csv(csv_path)
    assert "std_judge_score" in headers
    assert "mean_judge_score" in headers
    # The spec forbids inferential uncertainty columns.
    forbidden = {"ci_judge_score", "ci_low", "ci_high", "sem_judge_score"}
    assert forbidden.isdisjoint(set(headers))


def test_tokens_csv_decomposes_input_cached_output_reasoning(
    tmp_path: pathlib.Path,
) -> None:
    analysis_path = _seed_analysis(tmp_path)
    out_root = tmp_path / "results"
    render_all(analysis_path, out_root)
    csv_path = out_root / "token-composition" / f"{BENCH_CHART_PREFIX}-tokens.csv"
    headers, rows = _read_csv(csv_path)
    for col in (
        "mean_input_tokens_noncached",
        "mean_cached_tokens",
        "mean_output_tokens",
        "mean_reasoning_tokens",
    ):
        assert col in headers, f"missing column {col} in tokens CSV"
    # Six rows = 6 cell_stats.
    assert len(rows) == 6


def test_latency_csv_has_mean_and_std(tmp_path: pathlib.Path) -> None:
    analysis_path = _seed_analysis(tmp_path)
    out_root = tmp_path / "results"
    render_all(analysis_path, out_root)
    csv_path = out_root / "cost-curves" / f"{BENCH_CHART_PREFIX}-latency.csv"
    headers, rows = _read_csv(csv_path)
    assert "mean_latency_ms" in headers
    assert "std_latency_ms" in headers
    assert len(rows) == 6


# ----------------------------------------------------------------------------
# Palette + deterministic chart-payload mapping
# ----------------------------------------------------------------------------


def test_palette_size_matches_six_cells() -> None:
    """The Wong palette covers the 6 canonical cells; charts must not run
    out of distinguishable colors."""
    assert len(CHART_PALETTE) >= 6
    # All entries must be valid hex colors.
    for c in CHART_PALETTE:
        assert c.startswith("#") and len(c) == 7


def test_build_chart_payloads_is_pure(tmp_path: pathlib.Path) -> None:
    """build_chart_payloads MUST be I/O-free — it only resolves paths."""
    analysis = _make_analysis_payload()
    targets = build_chart_payloads(analysis, out_root=tmp_path / "results")
    for p in targets.values():
        # Nothing should have been written yet.
        assert not p.exists()


# ----------------------------------------------------------------------------
# CLI smoke
# ----------------------------------------------------------------------------


def test_cli_invocation_writes_charts(tmp_path: pathlib.Path) -> None:
    analysis_path = _seed_analysis(tmp_path)
    out_root = tmp_path / "results"
    bench_dir = tmp_path / "benchmarks" / "01-short-factual"
    bench_dir.mkdir(parents=True)
    (bench_dir / "analysis.json").write_text(
        analysis_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.plot_results",
            "--benchmark",
            "01-short-factual",
            "--analysis",
            str(bench_dir / "analysis.json"),
            "--out",
            str(out_root),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert res.returncode == 0, res.stderr
    assert (out_root / "cost-curves" / f"{BENCH_CHART_PREFIX}-cost-per-request.png").exists()
