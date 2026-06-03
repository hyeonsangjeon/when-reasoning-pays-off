"""plot_results.py — pure / offline chart generator that consumes
``benchmarks/<name>/analysis.json`` and emits a PNG + paired CSV for each
required chart.

Contract (.internal/tasks/008-analysis-pipeline.md):

* **Pure / offline.** No network. No live re-aggregation. The only inputs are
  the local ``analysis.json`` produced by ``scripts.analyze_tokens`` and the
  CSVs are the source of truth — PNGs are derived artifacts.
* **Every chart has a sibling CSV.** A chart without its underlying data is
  not auditable; the spec calls this out explicitly.
* **Two-lens cost.** A PAYG ``cost-per-request`` chart (USD axis) AND a
  sibling PTU ``throughput-gain`` chart (dimensionless ratio axis). Both
  exist for every benchmark.
* **Quality bar uses std**, not SEM, not CI. The methodology §8 caveat
  (N=20, R=3, authored samples) does not support inferential uncertainty
  claims.
* **Color-blind-friendly palette.** Six discrete colors drawn from Wong
  (2011) — distinguishable in deuteranopia / protanopia / tritanopia and in
  greyscale.
* **Deterministic output.** Matplotlib is configured with ``Agg``, the SVG
  hash-salt is disabled, and font cache lookups are avoided where possible —
  the same input ``analysis.json`` produces visually identical PNGs across
  re-runs on the same matplotlib version.

CLI::

    python -m scripts.plot_results \
        --benchmark 01-short-factual \
        --out results/

Output tree::

    results/
    ├── cost-curves/
    │   ├── benchmark-01-cost-per-request.png + .csv
    │   ├── benchmark-01-throughput-gain.png + .csv
    │   ├── benchmark-01-latency.png + .csv
    │   └── benchmark-01-quality.png + .csv
    └── token-composition/
        ├── benchmark-01-tokens.png + .csv
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import pathlib
import sys
from typing import Any

import matplotlib

# Headless backend — must be set BEFORE importing pyplot so plotting works in
# CI / containers and the output is byte-deterministic.
matplotlib.use("Agg")
matplotlib.rcParams["svg.hashsalt"] = "wrpo-plot-results"

import matplotlib.pyplot as plt  # noqa: E402

__all__ = [
    "BENCH_CHART_PREFIX",
    "CHART_PALETTE",
    "build_chart_payloads",
    "main",
    "render_all",
]

logger = logging.getLogger("scripts.plot_results")

# Wong (2011) colour-blind-friendly palette. The six discrete colours map 1:1
# to the six (model, effort) cells: index 0 = gpt-4o baseline; 1..5 =
# gpt-5.2 effort ladder (minimal → xhigh). The ordering matches
# CANONICAL_EFFORT_ORDER in scripts.analyze_tokens.
CHART_PALETTE: tuple[str, ...] = (
    "#000000",  # gpt-4o baseline (black)
    "#0072B2",  # minimal — blue
    "#009E73",  # low      — bluish-green
    "#E69F00",  # medium   — orange
    "#D55E00",  # high     — vermillion
    "#CC79A7",  # xhigh    — reddish-purple
)

BENCH_CHART_PREFIX = "benchmark-01"
BENCH_CHART_TITLE = "Benchmark 01"
# Both constants are mutated by ``main()`` based on the ``--benchmark`` slug —
# Task 009 wired benchmark 02 (and a future benchmark 03) through the same
# rendering surface. The slug-to-prefix derivation is implemented in
# ``_derive_chart_labels()``: the leading numeric segment of the folder name
# (e.g. ``"01-short-factual"`` → ``"01"``) becomes the filename suffix and the
# zero-padded display title (e.g. ``"Benchmark 01"``). Direct callers of
# ``render_all`` who target a non-default benchmark should call
# ``_derive_chart_labels`` and reassign the two module globals before invoking
# ``render_all`` (or, in tests, monkeypatch via the standard pytest fixture).
COST_DIR = "cost-curves"
TOKEN_DIR = "token-composition"


def _derive_chart_labels(benchmark_slug: str) -> tuple[str, str]:
    """Return ``(filename_prefix, title_prefix)`` for a benchmark folder slug.

    The benchmark folder convention is ``NN-{description}``; the chart
    artefacts use ``benchmark-NN`` as the filename prefix and
    ``Benchmark NN`` as the display title. Slugs that do not start with a
    numeric prefix fall back to the raw slug as both prefix and title.

    Examples::

        _derive_chart_labels("01-short-factual")
            ==> ("benchmark-01", "Benchmark 01")
        _derive_chart_labels("02-multi-step-reasoning")
            ==> ("benchmark-02", "Benchmark 02")
    """
    head = benchmark_slug.split("-", 1)[0]
    if head.isdigit():
        return (f"benchmark-{head}", f"Benchmark {head}")
    return (benchmark_slug, benchmark_slug)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _cell_label(model: str, effort: str | None) -> str:
    if effort is None:
        return f"{model}\n(baseline)"
    return f"{model}\neffort={effort}"


def _ordered_stats(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Return cell_stats in canonical order: gpt-4o baseline first, then
    gpt-5.2 lowest→xhigh. Drops empty zero-cell entries to keep charts honest.

    Both ``"none"`` and ``"minimal"`` are listed as the lowest-tier candidates
    because Task 009 wired through the production Foundry v1 schema
    (``none|low|medium|high|xhigh``) while the legacy Task 008 fixtures use
    ``minimal``. Cohorts always carry one or the other; the ``n_used > 0``
    filter drops the absent tier per benchmark.
    """
    order_keys: list[tuple[str, str | None]] = [
        ("gpt-4o", None),
        ("gpt-5.2", "none"),
        ("gpt-5.2", "minimal"),
        ("gpt-5.2", "low"),
        ("gpt-5.2", "medium"),
        ("gpt-5.2", "high"),
        ("gpt-5.2", "xhigh"),
    ]
    index = {(s["model"], s["effort"]): s for s in analysis["cell_stats"]}
    out: list[dict[str, Any]] = []
    for k in order_keys:
        if k in index and index[k].get("n_used", 0) > 0:
            out.append(index[k])
    return out


def _pricing_citation(analysis: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    cid = stats["pricing_citation_id"]
    return analysis["pricing_citations"][cid]


def _ptu_baseline_tokens(analysis: dict[str, Any]) -> tuple[float, str]:
    base = analysis.get("ptu_baseline", {})
    return (
        float(base.get("tokens_per_request", 0.0)),
        str(base.get("label", "unknown")),
    )


def _write_csv(rows: list[dict[str, Any]], path: pathlib.Path, *, trailing: list[str] | None = None) -> None:
    """Write a CSV with deterministic key ordering (sorted by header)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    headers = sorted({k for r in rows for k in r.keys()})
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=headers, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in headers})
    body = buf.getvalue()
    if trailing:
        for line in trailing:
            body += f"# {line}\n"
    path.write_text(body, encoding="utf-8")


# ----------------------------------------------------------------------------
# Per-chart builders — each builds a CSV "data table" then renders the PNG
# ----------------------------------------------------------------------------


def _cost_per_request_csv_rows(
    analysis: dict[str, Any], stats: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for s in stats:
        cite = _pricing_citation(analysis, s)
        rows.append(
            {
                "model": s["model"],
                "effort": s["effort"] if s["effort"] is not None else "",
                "mean_usd_per_request": s["mean_usd_per_request"],
                "std_usd_per_request": s["std_usd_per_request"],
                "n_used": s["n_used"],
                "pricing_source_url": cite["source_url"],
                "pricing_accessed_date": cite["accessed_date"],
                "pricing_snapshot_path": cite["snapshot_path"],
            }
        )
    return rows


def _render_cost_per_request(
    analysis: dict[str, Any], stats: list[dict[str, Any]], out_dir: pathlib.Path
) -> None:
    rows = _cost_per_request_csv_rows(analysis, stats)
    csv_path = out_dir / f"{BENCH_CHART_PREFIX}-cost-per-request.csv"
    # Cite the snapshot path in a trailing comment row so a reader of the CSV
    # alone (no PNG, no MD) still gets the citation.
    citation = next(iter(analysis["pricing_citations"].values()))
    trailing = [
        f"pricing_source_url: {citation['source_url']}",
        f"pricing_accessed_date: {citation['accessed_date']}",
        f"pricing_snapshot_path: {citation['snapshot_path']}",
    ]
    _write_csv(rows, csv_path, trailing=trailing)

    labels = [_cell_label(s["model"], s["effort"]) for s in stats]
    means = [s["mean_usd_per_request"] for s in stats]
    stds = [s["std_usd_per_request"] for s in stats]
    colors = list(CHART_PALETTE[: len(stats)])

    fig, ax = plt.subplots(figsize=(8.5, 5.0))
    xs = list(range(len(stats)))
    ax.bar(xs, means, yerr=stds, color=colors, capsize=4, edgecolor="black", linewidth=0.6)
    ax.set_xticks(xs)
    ax.set_xticklabels(labels)
    ax.set_ylabel("USD per request (PAYG)")
    ax.set_xlabel("Model / effort")
    ax.set_title(
        f"{BENCH_CHART_TITLE}: cost per request (PAYG lens, ± std)\n"
        f"source={citation['source_url']} accessed={citation['accessed_date']}",
        fontsize=10,
    )
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    fig.tight_layout()
    fig.savefig(out_dir / f"{BENCH_CHART_PREFIX}-cost-per-request.png", dpi=144)
    plt.close(fig)


def _throughput_gain_csv_rows(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    base_tokens, base_label = _ptu_baseline_tokens(analysis)
    rows: list[dict[str, Any]] = []
    for g in analysis.get("ptu_gain_by_cell", []):
        rows.append(
            {
                "model": g["model"],
                "effort": g["effort"] if g["effort"] is not None else "",
                "tokens_per_request": g["tokens_per_request"],
                "throughput_gain_factor": g["throughput_gain_factor"],
                "baseline_label": g["baseline_label"],
                "baseline_tokens_per_request": base_tokens,
            }
        )
    return rows


def _render_throughput_gain(
    analysis: dict[str, Any], stats: list[dict[str, Any]], out_dir: pathlib.Path
) -> None:
    rows = _throughput_gain_csv_rows(analysis)
    # Filter to ordered stats to keep PNG ordering consistent.
    selected = [
        next(
            (
                r
                for r in rows
                if r["model"] == s["model"]
                and (r["effort"] == (s["effort"] or ""))
            ),
            None,
        )
        for s in stats
    ]
    csv_rows = [r for r in selected if r is not None]
    csv_path = out_dir / f"{BENCH_CHART_PREFIX}-throughput-gain.csv"
    base_tokens, base_label = _ptu_baseline_tokens(analysis)
    trailing = [
        f"baseline_label: {base_label}",
        f"baseline_tokens_per_request: {base_tokens}",
        "throughput_gain_factor = baseline.tokens_per_request / target.tokens_per_request",
    ]
    _write_csv(csv_rows, csv_path, trailing=trailing)

    labels = [_cell_label(s["model"], s["effort"]) for s in stats]
    gains = [r["throughput_gain_factor"] if r else 0.0 for r in selected]
    colors = list(CHART_PALETTE[: len(stats)])

    fig, ax = plt.subplots(figsize=(8.5, 5.0))
    xs = list(range(len(stats)))
    ax.bar(xs, gains, color=colors, edgecolor="black", linewidth=0.6)
    ax.axhline(1.0, color="grey", linestyle=":", linewidth=1)
    ax.set_xticks(xs)
    ax.set_xticklabels(labels)
    ax.set_ylabel("Throughput-gain factor (× baseline)")
    ax.set_xlabel("Model / effort")
    ax.set_title(
        f"{BENCH_CHART_TITLE}: PTU throughput gain (factor vs baseline)\n"
        f"baseline = {base_label}; tokens-per-request = {base_tokens}",
        fontsize=10,
    )
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    fig.tight_layout()
    fig.savefig(out_dir / f"{BENCH_CHART_PREFIX}-throughput-gain.png", dpi=144)
    plt.close(fig)


def _tokens_csv_rows(stats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for s in stats:
        non_cached_input = s["mean_input_tokens"] - s["mean_cached_tokens"]
        rows.append(
            {
                "model": s["model"],
                "effort": s["effort"] if s["effort"] is not None else "",
                "mean_input_tokens_noncached": round(non_cached_input, 6),
                "mean_cached_tokens": s["mean_cached_tokens"],
                "mean_output_tokens": s["mean_output_tokens"],
                "mean_reasoning_tokens": s["mean_reasoning_tokens"],
                "n_used": s["n_used"],
            }
        )
    return rows


def _render_tokens(
    analysis: dict[str, Any], stats: list[dict[str, Any]], out_dir: pathlib.Path
) -> None:
    rows = _tokens_csv_rows(stats)
    csv_path = out_dir / f"{BENCH_CHART_PREFIX}-tokens.csv"
    _write_csv(rows, csv_path)

    labels = [_cell_label(s["model"], s["effort"]) for s in stats]
    xs = list(range(len(stats)))
    nci = [s["mean_input_tokens"] - s["mean_cached_tokens"] for s in stats]
    cached = [s["mean_cached_tokens"] for s in stats]
    out_tokens = [s["mean_output_tokens"] for s in stats]
    reasoning = [s["mean_reasoning_tokens"] for s in stats]

    fig, ax = plt.subplots(figsize=(9.0, 5.5))
    # Stacked: input(non-cached) / cached / output / reasoning. Distinct hatches
    # double-encode the channels so the chart survives B/W printing.
    ax.bar(xs, nci, color="#56B4E9", edgecolor="black", linewidth=0.6, label="input (non-cached)")
    base = list(nci)
    ax.bar(xs, cached, bottom=base, color="#F0E442", edgecolor="black", linewidth=0.6, hatch="//", label="cached input")
    base = [a + b for a, b in zip(base, cached)]
    ax.bar(xs, out_tokens, bottom=base, color="#009E73", edgecolor="black", linewidth=0.6, hatch="..", label="output (visible)")
    base = [a + b for a, b in zip(base, out_tokens)]
    ax.bar(xs, reasoning, bottom=base, color="#D55E00", edgecolor="black", linewidth=0.6, hatch="xx", label="reasoning")
    ax.set_xticks(xs)
    ax.set_xticklabels(labels)
    ax.set_ylabel("Tokens per request (mean)")
    ax.set_xlabel("Model / effort")
    ax.set_title(f"{BENCH_CHART_TITLE}: token composition by model / effort")
    ax.legend(loc="upper left")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    fig.tight_layout()
    fig.savefig(out_dir / f"{BENCH_CHART_PREFIX}-tokens.png", dpi=144)
    plt.close(fig)


def _latency_csv_rows(
    analysis: dict[str, Any], stats: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[list[float]]]:
    """CSV rows for the latency summary + box-plot raw data per cell.

    The CSV summarizes mean/std; the raw per-cell latency list is also
    returned so the box plot can render distributions without re-reading.
    """
    rows: list[dict[str, Any]] = []
    cells = analysis.get("cells", [])
    raw_by_group: list[list[float]] = []
    for s in stats:
        gkey = (s["model"], s["effort"])
        values = [
            float(c["latency_ms"])
            for c in cells
            if c["model"] == gkey[0]
            and c["effort"] == gkey[1]
            and c["outlier_reason"] is None
        ]
        raw_by_group.append(values)
        rows.append(
            {
                "model": s["model"],
                "effort": s["effort"] if s["effort"] is not None else "",
                "mean_latency_ms": s["mean_latency_ms"],
                "std_latency_ms": s["std_latency_ms"],
                "n_used": s["n_used"],
            }
        )
    return rows, raw_by_group


def _render_latency(
    analysis: dict[str, Any], stats: list[dict[str, Any]], out_dir: pathlib.Path
) -> None:
    rows, raw_by_group = _latency_csv_rows(analysis, stats)
    csv_path = out_dir / f"{BENCH_CHART_PREFIX}-latency.csv"
    _write_csv(rows, csv_path)

    labels = [_cell_label(s["model"], s["effort"]) for s in stats]
    fig, ax = plt.subplots(figsize=(8.5, 5.0))
    bp = ax.boxplot(
        raw_by_group,
        tick_labels=labels,
        showfliers=False,
        patch_artist=True,
    )
    for patch, color in zip(bp["boxes"], CHART_PALETTE[: len(stats)]):
        patch.set_facecolor(color)
        patch.set_alpha(0.7)
        patch.set_edgecolor("black")
    ax.set_ylabel("Latency (ms)")
    ax.set_xlabel("Model / effort")
    ax.set_title(f"{BENCH_CHART_TITLE}: latency distribution by model / effort (outliers excluded)")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    fig.tight_layout()
    fig.savefig(out_dir / f"{BENCH_CHART_PREFIX}-latency.png", dpi=144)
    plt.close(fig)


def _quality_csv_rows(stats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for s in stats:
        rows.append(
            {
                "model": s["model"],
                "effort": s["effort"] if s["effort"] is not None else "",
                "mean_judge_score": s["mean_judge_score"],
                "std_judge_score": s["std_judge_score"],
                "judge_n": s["judge_n"],
            }
        )
    return rows


def _render_quality(
    analysis: dict[str, Any], stats: list[dict[str, Any]], out_dir: pathlib.Path
) -> None:
    rows = _quality_csv_rows(stats)
    csv_path = out_dir / f"{BENCH_CHART_PREFIX}-quality.csv"
    _write_csv(rows, csv_path)

    labels = [_cell_label(s["model"], s["effort"]) for s in stats]
    means = [s["mean_judge_score"] for s in stats]
    stds = [s["std_judge_score"] for s in stats]
    colors = list(CHART_PALETTE[: len(stats)])

    fig, ax = plt.subplots(figsize=(8.5, 5.0))
    xs = list(range(len(stats)))
    ax.bar(xs, means, yerr=stds, color=colors, capsize=4, edgecolor="black", linewidth=0.6)
    ax.set_xticks(xs)
    ax.set_xticklabels(labels)
    ax.set_ylabel("Judge score (0|1|2; ± std)")
    ax.set_xlabel("Model / effort")
    ax.set_ylim(0, 2.2)
    ax.set_title(
        f"{BENCH_CHART_TITLE}: judge quality by model / effort (± std; N=20, R=3 — no CI)",
        fontsize=10,
    )
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    fig.tight_layout()
    fig.savefig(out_dir / f"{BENCH_CHART_PREFIX}-quality.png", dpi=144)
    plt.close(fig)


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------


def build_chart_payloads(
    analysis: dict[str, Any], *, out_root: pathlib.Path
) -> dict[str, pathlib.Path]:
    """Resolve the on-disk output paths without performing any I/O.

    Returns a mapping ``chart_id → path`` so callers (tests, dry-runs) can
    inspect targets before any PNG is written.
    """
    cost_dir = out_root / COST_DIR
    token_dir = out_root / TOKEN_DIR
    return {
        "cost_per_request_png": cost_dir / f"{BENCH_CHART_PREFIX}-cost-per-request.png",
        "cost_per_request_csv": cost_dir / f"{BENCH_CHART_PREFIX}-cost-per-request.csv",
        "throughput_gain_png": cost_dir / f"{BENCH_CHART_PREFIX}-throughput-gain.png",
        "throughput_gain_csv": cost_dir / f"{BENCH_CHART_PREFIX}-throughput-gain.csv",
        "tokens_png": token_dir / f"{BENCH_CHART_PREFIX}-tokens.png",
        "tokens_csv": token_dir / f"{BENCH_CHART_PREFIX}-tokens.csv",
        "latency_png": cost_dir / f"{BENCH_CHART_PREFIX}-latency.png",
        "latency_csv": cost_dir / f"{BENCH_CHART_PREFIX}-latency.csv",
        "quality_png": cost_dir / f"{BENCH_CHART_PREFIX}-quality.png",
        "quality_csv": cost_dir / f"{BENCH_CHART_PREFIX}-quality.csv",
    }


def render_all(
    analysis_path: pathlib.Path, out_root: pathlib.Path
) -> dict[str, pathlib.Path]:
    """Render every chart for one benchmark's analysis.json.

    Returns the mapping returned by :func:`build_chart_payloads` so the
    caller can verify on-disk presence.
    """
    with analysis_path.open("r", encoding="utf-8") as fh:
        analysis = json.load(fh)

    stats = _ordered_stats(analysis)
    if not stats:
        raise ValueError(
            f"{analysis_path}: no non-empty cell_stats; cannot render charts."
        )

    cost_dir = out_root / COST_DIR
    token_dir = out_root / TOKEN_DIR
    cost_dir.mkdir(parents=True, exist_ok=True)
    token_dir.mkdir(parents=True, exist_ok=True)

    _render_cost_per_request(analysis, stats, cost_dir)
    _render_throughput_gain(analysis, stats, cost_dir)
    _render_tokens(analysis, stats, token_dir)
    _render_latency(analysis, stats, cost_dir)
    _render_quality(analysis, stats, cost_dir)

    return build_chart_payloads(analysis, out_root=out_root)


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scripts.plot_results",
        description=(
            "Render per-benchmark PNG + CSV chart pairs from analysis.json. "
            "Pure / offline; the CSV next to each PNG is the source of truth."
        ),
    )
    p.add_argument("--benchmark", required=True, help="Benchmark folder name.")
    p.add_argument(
        "--analysis",
        default=None,
        help="Override analysis.json path (default: benchmarks/<bench>/analysis.json).",
    )
    p.add_argument(
        "--out",
        default="results",
        help="Output root (default: results/).",
    )
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: list[str] | None = None) -> int:
    ns = _build_argparser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, ns.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Derive the per-benchmark chart filename + display title from the slug.
    # Mutates the module-level constants that the render functions reference;
    # this is the minimal-diff CLI-side switch (Task 009 deviation: existing
    # default was hard-coded ``benchmark-01``, blocking benchmark 02 / 03 from
    # rendering distinct chart files).
    global BENCH_CHART_PREFIX, BENCH_CHART_TITLE  # noqa: PLW0603
    BENCH_CHART_PREFIX, BENCH_CHART_TITLE = _derive_chart_labels(ns.benchmark)
    bench_root = pathlib.Path("benchmarks") / ns.benchmark
    analysis_path = (
        pathlib.Path(ns.analysis) if ns.analysis else bench_root / "analysis.json"
    )
    out_root = pathlib.Path(ns.out)
    targets = render_all(analysis_path, out_root)
    for name, p in targets.items():
        logger.info("plot: %s -> %s", name, p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
