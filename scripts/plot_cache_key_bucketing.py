"""plot_cache_key_bucketing.py — Task 018 v2.4 chart generator.

Produces the two v2.4-required charts from Stage 2 evidence summaries:

* ``results/cache-key-bucketing/cache_hit_ratio_vs_cardinality.png``
  x: ``bucket_cardinality`` (log2 scale); y: mean cache hit ratio;
  one line per ``prompt_cache_retention`` mode (``in_memory``, ``24h``).
* ``results/cache-key-bucketing/ttft_p95_vs_cardinality.png``
  Same x; y: ``first_token_latency_ms_p95_steady_state``.

Inputs are the per-run summary JSONs under
``benchmarks/06-cache-key-bucketing/runs/`` produced by
``scripts.measure_cache_key_bucketing`` Stage 2 evidence runs. Cells
flagged ``backlog_excessive`` are excluded from the plotted curves
(per v2.3+ analysis contract); failed cells are tracked but not
silently dropped.

The script is **pure / offline / deterministic**: no network, headless
matplotlib backend, fixed font and palette, paired CSV side-cars so
the chart numbers can be audited without re-rendering. v2.1 diagnostic
PNGs under ``_v2.1_diagnostic/`` are NOT touched.

CLI::

    python -m scripts.plot_cache_key_bucketing \\
        --inmemory-summary benchmarks/06-cache-key-bucketing/runs/<ts>_..._inmemory_evidence.jsonl.summary.json \\
        --h24-summary    benchmarks/06-cache-key-bucketing/runs/<ts>_..._24h_evidence.jsonl.summary.json \\
        --out-dir results/cache-key-bucketing

Both summary paths default to the most recently mtime-modified
``*_inmemory_evidence.jsonl.summary.json`` and
``*_24h_evidence.jsonl.summary.json`` respectively under
``benchmarks/06-cache-key-bucketing/runs/`` (excluding any
``_v2.*_diagnostic`` subdirectories).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import pathlib
import sys
from typing import Any

import matplotlib

matplotlib.use("Agg")
matplotlib.rcParams["svg.hashsalt"] = "wrpo-task018"

import matplotlib.pyplot as plt  # noqa: E402

logger = logging.getLogger("scripts.plot_cache_key_bucketing")

DEFAULT_RUNS_DIR = pathlib.Path("benchmarks/06-cache-key-bucketing/runs")
DEFAULT_OUT_DIR = pathlib.Path("results/cache-key-bucketing")

INMEMORY_COLOR = "#0072B2"  # Wong (2011) blue
H24_COLOR = "#D55E00"       # Wong (2011) vermillion

CHART_CACHE_HIT = "cache_hit_ratio_vs_cardinality"
CHART_TTFT_P95 = "ttft_p95_vs_cardinality"


def _discover_latest(runs_dir: pathlib.Path, retention_tag: str) -> pathlib.Path:
    """Return the newest evidence-summary JSON for ``retention_tag``.

    Args:
        runs_dir: Benchmark ``runs/`` directory.
        retention_tag: Either ``"inmemory"`` or ``"24h"``.

    Returns:
        The newest matching ``*_<tag>_evidence.jsonl.summary.json``
        under ``runs_dir`` (excluding any ``_v2.*_diagnostic/`` subdirectory).

    Raises:
        FileNotFoundError: When no matching file exists in the v2.4
            evidence directory.
    """
    if retention_tag not in {"inmemory", "24h"}:
        raise ValueError(f"retention_tag must be 'inmemory' or '24h'; got {retention_tag!r}")
    candidates = [
        p for p in runs_dir.glob(f"*_{retention_tag}_evidence.jsonl.summary.json")
        if "_diagnostic" not in p.parts[-2]
    ]
    if not candidates:
        raise FileNotFoundError(
            f"No v2.4 evidence summary found for retention={retention_tag} under {runs_dir}; "
            "expected a file matching '*_<tag>_evidence.jsonl.summary.json'."
        )
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _load_summary(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _series_from_summary(
    summary: dict[str, Any],
    retention_label: str,
) -> list[dict[str, Any]]:
    """Extract one chart series from one summary JSON.

    Only cells with ``backlog_excessive == False`` are included
    (per the v2.3+ analysis contract — backlog-excessive cells are
    excluded from cache-hit-ratio analysis).
    """
    rows: list[dict[str, Any]] = []
    for cell in summary.get("cell_summaries", []):
        if cell.get("backlog_excessive", False):
            logger.warning(
                "EXCLUDED_FROM_CHART retention=%s cardinality=%s "
                "(backlog_excessive=true)",
                retention_label, cell.get("cardinality"),
            )
            continue
        rows.append({
            "retention": retention_label,
            "cardinality": int(cell["cardinality"]),
            "cache_hit_ratio": float(cell["cache_hit_ratio_steady_state"]),
            "ttft_p95_ms": float(cell["first_token_latency_ms_p95_steady_state"]),
            "n_steady_state_records": int(cell.get("n_steady_state_records", 0) or 0),
            "n_records": int(cell.get("n_records", 0) or 0),
            "realized_admitted_per_bucket_rpm": float(
                cell.get("realized_admitted_per_bucket_rpm", 0.0) or 0.0
            ),
            "max_in_flight_observed": int(cell.get("max_in_flight_observed", 0) or 0),
            "namespace": cell.get("namespace", ""),
        })
    rows.sort(key=lambda r: r["cardinality"])
    return rows


def _write_csv(out_path: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        out_path.write_text("retention,cardinality,cache_hit_ratio,ttft_p95_ms\n", encoding="utf-8")
        return
    fields = [
        "retention",
        "cardinality",
        "cache_hit_ratio",
        "ttft_p95_ms",
        "n_steady_state_records",
        "n_records",
        "realized_admitted_per_bucket_rpm",
        "max_in_flight_observed",
        "namespace",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fields})


def _render_cache_hit_chart(
    rows: list[dict[str, Any]],
    out_path: pathlib.Path,
    *,
    inmemory_label: str,
    h24_label: str,
) -> None:
    """Render the cache-hit-ratio-vs-cardinality chart."""
    inmemory = [r for r in rows if r["retention"] == "in_memory"]
    h24 = [r for r in rows if r["retention"] == "24h"]
    fig, ax = plt.subplots(figsize=(7.0, 4.5))
    if inmemory:
        ax.plot(
            [r["cardinality"] for r in inmemory],
            [r["cache_hit_ratio"] for r in inmemory],
            color=INMEMORY_COLOR, marker="o", linewidth=2.0, markersize=8,
            label=inmemory_label,
        )
    if h24:
        ax.plot(
            [r["cardinality"] for r in h24],
            [r["cache_hit_ratio"] for r in h24],
            color=H24_COLOR, marker="s", linewidth=2.0, markersize=8,
            label=h24_label,
        )
    ax.set_xscale("log", base=2)
    all_card = sorted({r["cardinality"] for r in rows})
    if all_card:
        ax.set_xticks(all_card)
        ax.set_xticklabels([str(c) for c in all_card])
    ax.set_xlabel("bucket_cardinality (log2 scale)")
    ax.set_ylabel("cache hit ratio (steady state)")
    ax.set_ylim(0.0, 1.0)
    ax.grid(True, which="major", linestyle=":", alpha=0.6)
    ax.set_title(
        "Task 018 v2.4 — cache hit ratio vs prompt_cache_key cardinality\n"
        "(PAYG gpt-5.2; sustain_tps=0.5; sem=96; warmup excluded)"
    )
    if inmemory or h24:
        ax.legend(loc="lower right", frameon=True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, format="png")
    plt.close(fig)


def _render_ttft_p95_chart(
    rows: list[dict[str, Any]],
    out_path: pathlib.Path,
    *,
    inmemory_label: str,
    h24_label: str,
) -> None:
    """Render the TTFT-p95-vs-cardinality chart."""
    inmemory = [r for r in rows if r["retention"] == "in_memory"]
    h24 = [r for r in rows if r["retention"] == "24h"]
    fig, ax = plt.subplots(figsize=(7.0, 4.5))
    if inmemory:
        ax.plot(
            [r["cardinality"] for r in inmemory],
            [r["ttft_p95_ms"] for r in inmemory],
            color=INMEMORY_COLOR, marker="o", linewidth=2.0, markersize=8,
            label=inmemory_label,
        )
    if h24:
        ax.plot(
            [r["cardinality"] for r in h24],
            [r["ttft_p95_ms"] for r in h24],
            color=H24_COLOR, marker="s", linewidth=2.0, markersize=8,
            label=h24_label,
        )
    ax.set_xscale("log", base=2)
    all_card = sorted({r["cardinality"] for r in rows})
    if all_card:
        ax.set_xticks(all_card)
        ax.set_xticklabels([str(c) for c in all_card])
    ax.set_xlabel("bucket_cardinality (log2 scale)")
    ax.set_ylabel("first-token latency p95 (ms, steady state)")
    ax.set_ylim(bottom=0)
    ax.grid(True, which="major", linestyle=":", alpha=0.6)
    ax.set_title(
        "Task 018 v2.4 — TTFT p95 vs prompt_cache_key cardinality\n"
        "(PAYG gpt-5.2; sustain_tps=0.5; sem=96; warmup excluded)"
    )
    if inmemory or h24:
        ax.legend(loc="upper right", frameon=True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, format="png")
    plt.close(fig)


def render_all(
    *,
    inmemory_summary_path: pathlib.Path,
    h24_summary_path: pathlib.Path,
    out_dir: pathlib.Path,
) -> dict[str, pathlib.Path]:
    """Render both v2.4 charts; return a mapping ``name → output path``.

    Args:
        inmemory_summary_path: Path to the inmemory Stage 2 evidence summary JSON.
        h24_summary_path: Path to the 24h Stage 2 evidence summary JSON.
        out_dir: Directory under which to write the PNGs and sibling CSVs.

    Returns:
        ``{"cache_hit_png": ..., "cache_hit_csv": ...,
            "ttft_p95_png": ..., "ttft_p95_csv": ...}``.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    inm_summary = _load_summary(inmemory_summary_path)
    h24_summary = _load_summary(h24_summary_path)

    rows = (
        _series_from_summary(inm_summary, "in_memory")
        + _series_from_summary(h24_summary, "24h")
    )

    inm_label = (
        f"prompt_cache_retention=in_memory "
        f"(N={sum(r['n_records'] for r in rows if r['retention']=='in_memory')})"
    )
    h24_label = (
        f"prompt_cache_retention=24h "
        f"(N={sum(r['n_records'] for r in rows if r['retention']=='24h')})"
    )

    cache_hit_png = out_dir / f"{CHART_CACHE_HIT}.png"
    cache_hit_csv = out_dir / f"{CHART_CACHE_HIT}.csv"
    ttft_p95_png = out_dir / f"{CHART_TTFT_P95}.png"
    ttft_p95_csv = out_dir / f"{CHART_TTFT_P95}.csv"

    _render_cache_hit_chart(
        rows, cache_hit_png,
        inmemory_label=inm_label, h24_label=h24_label,
    )
    _render_ttft_p95_chart(
        rows, ttft_p95_png,
        inmemory_label=inm_label, h24_label=h24_label,
    )
    _write_csv(cache_hit_csv, rows)
    _write_csv(ttft_p95_csv, rows)

    logger.info("WROTE chart=%s path=%s", CHART_CACHE_HIT, cache_hit_png)
    logger.info("WROTE chart=%s path=%s", CHART_TTFT_P95, ttft_p95_png)

    return {
        "cache_hit_png": cache_hit_png,
        "cache_hit_csv": cache_hit_csv,
        "ttft_p95_png": ttft_p95_png,
        "ttft_p95_csv": ttft_p95_csv,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate Task 018 v2.4 PNG charts "
            "(cache_hit_ratio_vs_cardinality + ttft_p95_vs_cardinality) "
            "from Stage 2 evidence summary JSONs."
        )
    )
    parser.add_argument(
        "--inmemory-summary", type=pathlib.Path, default=None,
        help=(
            "Path to the inmemory Stage 2 evidence summary JSON. "
            "If omitted, auto-discovers the most recently modified "
            "*_inmemory_evidence.jsonl.summary.json under "
            "benchmarks/06-cache-key-bucketing/runs/."
        ),
    )
    parser.add_argument(
        "--h24-summary", type=pathlib.Path, default=None,
        help=(
            "Path to the 24h Stage 2 evidence summary JSON. "
            "If omitted, auto-discovers the most recently modified "
            "*_24h_evidence.jsonl.summary.json under "
            "benchmarks/06-cache-key-bucketing/runs/."
        ),
    )
    parser.add_argument(
        "--runs-dir", type=pathlib.Path, default=DEFAULT_RUNS_DIR,
        help="Override for the runs directory used by auto-discovery.",
    )
    parser.add_argument(
        "--out-dir", type=pathlib.Path, default=DEFAULT_OUT_DIR,
        help="Directory under which the two PNGs (and sibling CSVs) are written.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    inm = args.inmemory_summary or _discover_latest(args.runs_dir, "inmemory")
    h24 = args.h24_summary or _discover_latest(args.runs_dir, "24h")
    logger.info("USING inmemory_summary=%s", inm)
    logger.info("USING h24_summary=%s", h24)

    paths = render_all(
        inmemory_summary_path=inm,
        h24_summary_path=h24,
        out_dir=args.out_dir,
    )
    print(
        "Wrote:\n"
        f"  {paths['cache_hit_png']}\n"
        f"  {paths['cache_hit_csv']}\n"
        f"  {paths['ttft_p95_png']}\n"
        f"  {paths['ttft_p95_csv']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
