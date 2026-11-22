"""Aggregate the 2026-11 benchmark 01 re-run: tokens, cost, latency, judge score per (model, effort).

Reads benchmarks/01-short-factual/rerun-2026-11/{runs,judge_runs}/ and the price
snapshot pricing/azure-openai-payg-2026-11.yaml. Writes analysis.json and prints a
Markdown table. Cost = (input - cached) x input price + cached x cached price
+ output x output price; output tokens already include reasoning tokens.
"""

from __future__ import annotations

import json
import pathlib
import statistics as st

import yaml

ROOT = pathlib.Path("benchmarks/01-short-factual/rerun-2026-11")
PRICES = yaml.safe_load(open("pricing/azure-openai-payg-2026-11.yaml", encoding="utf-8"))["models"]
ORDER = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6, None: -1}


def mean(xs):
    return round(st.mean(xs), 3) if xs else None


def sd(xs):
    return round(st.stdev(xs), 3) if len(xs) > 1 else None


def main() -> int:
    runs = [json.loads(p.read_text()) for p in sorted((ROOT / "runs").glob("*.json"))]
    judges = {}
    for p in sorted((ROOT / "judge_runs").glob("*.json")):
        j = json.loads(p.read_text())
        judges[(str(j["sample_id"]), j["model"], j.get("effort"), int(j["repeat"]))] = j
    cells = {}
    for r in runs:
        cells.setdefault((r["model"], r.get("effort")), []).append(r)
    out = []
    for (model, effort), rs in sorted(cells.items(), key=lambda kv: (kv[0][0], ORDER.get(kv[0][1], 9))):
        pr = PRICES[model]
        ok = [r for r in rs if r["status"] == "ok"]
        u = [r["usage"] for r in ok]
        inp = [x["input_tokens"] for x in u]
        cached = [x["input_tokens_details"]["cached_tokens"] for x in u]
        outp = [x["output_tokens"] for x in u]
        rea = [x["output_tokens_details"]["reasoning_tokens"] for x in u]
        cost = [((i - c) * pr["input_per_1m_usd"] + c * pr["cached_input_per_1m_usd"] + o * pr["output_per_1m_usd"]) / 1e6
                for i, c, o in zip(inp, cached, outp)]
        scores = [judges[k]["score"] for r in ok
                  if (k := (str(r["sample_id"]), model, effort, int(r["repeat"]))) in judges and judges[k].get("score") is not None]
        out.append({
            "model": model, "effort": effort, "n": len(rs), "n_ok": len(ok),
            "n_incomplete": sum(1 for r in ok if r.get("incomplete_reason")),
            "input_mean": mean(inp), "cached_mean": mean(cached), "output_mean": mean(outp),
            "visible_output_mean": mean([o - x for o, x in zip(outp, rea)]),
            "reasoning_mean": mean(rea), "reasoning_sd": sd(rea), "reasoning_max": max(rea) if rea else None,
            "reasoning_nonzero": sum(1 for x in rea if x > 0),
            "usd_per_1k_requests": round(st.mean(cost) * 1000, 4) if cost else None,
            "input_cost_share": round(st.mean([(i - c) * pr["input_per_1m_usd"] / 1e6 for i, c in zip(inp, cached)]) * 1000 / (st.mean(cost) * 1000), 3) if cost else None,
            "latency_ms_mean": round(st.mean([r["latency_ms"] for r in ok])) if ok else None,
            "latency_ms_sd": round(st.stdev([r["latency_ms"] for r in ok])) if len(ok) > 1 else None,
            "judge_n": len(scores), "judge_mean": mean(scores), "judge_sd": sd(scores),
            "judge_pass_2": sum(1 for s in scores if s == 2),
            "first_utc": min(r["timestamp_utc"] for r in rs), "last_utc": max(r["timestamp_utc"] for r in rs),
            "model_reported": sorted({str(r.get("model_reported")) for r in ok}),
        })
    (ROOT / "analysis.json").write_text(json.dumps({"price_snapshot": "pricing/azure-openai-payg-2026-11.yaml", "cells": out},
                                                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("| model | effort | n ok | reasoning mean (nonzero/max) | output mean | input mean | USD/1k req | latency ms | judge (2s) |")
    print("|---|---|---|---|---|---|---|---|---|")
    for c in out:
        print(f"| {c['model']} | {c['effort']} | {c['n_ok']}/{c['n']} | {c['reasoning_mean']} ({c['reasoning_nonzero']}/{c['reasoning_max']}) | "
              f"{c['output_mean']} | {c['input_mean']} | ${c['usd_per_1k_requests']} | {c['latency_ms_mean']} | "
              f"{c['judge_mean']} ({c['judge_pass_2']}/{c['judge_n']}) |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
