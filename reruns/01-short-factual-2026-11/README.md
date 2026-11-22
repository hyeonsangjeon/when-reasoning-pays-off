# Benchmark 01 re-run on newer models (2026-11-22)

Kept outside `benchmarks/`, which is a protected historical tree. The dataset and
prompts are read from `benchmarks/01-short-factual/` unchanged.

Same 20 short-factual samples as `exp001` (May 2026), re-run on newer reasoning
models with every reasoning effort each model accepts.

## What was run

| Field | Value |
| --- | --- |
| Dataset | `benchmarks/01-short-factual/dataset.json` (unchanged, 20 samples) |
| Prompt | `scripts.run_benchmark.load_dataset` (system prompt + rendered user template). System prompt SHA-256 starts `95d5b8d0`, identical to exp001; user inputs identical per sample |
| Call | Responses API, `max_output_tokens` 4096, `reasoning={"effort": ...}` only (no temperature/top_p) |
| Repeats | 3 per (sample, effort) |
| Models | `gpt-5.6-sol` (2026-07-09): none, low, medium, high, xhigh, max. `gpt-6.2-sol` and `gpt-6.2-astra` (2026-11-17): low, medium, high, xhigh, max. `gpt-4o` (2024-11-20): baseline, no reasoning parameter |
| Rejected efforts | `gpt-6.2-sol` and `gpt-6.2-astra` reject `none` and `minimal` with HTTP 400 (supported: low, medium, high, xhigh, max); `gpt-5.6-sol` rejects `minimal`. Raw error payloads: `rejected_efforts_*.json` |
| Cells | 1,020 (360 + 300 + 300 + 60), 0 errors |
| Run window | 2026-11-22 10:46:55 to 10:49:42 UTC, Azure AI Foundry, Global Standard deployments, Entra ID auth |
| Judge | `scripts/run_judge.py`, `gpt-4o`, same judge prompt as exp001, one call per cell (1,020 judged) |
| Prices | `prices-2026-11-22.yaml` (this folder) (Azure Retail Prices API, Global Standard, short-context tier, read 2026-11-22) |

Costs are usage tokens x list price. They are not invoices. Output tokens
already include reasoning tokens.

## Results

| model | effort | ok | reasoning tokens per request: mean (requests with any, max) | USD per 1,000 requests | judge mean, 0–2 |
| --- | --- | --- | --- | --- | --- |
| gpt-4o | — | 60/60 | 0.00 (0/60, max 0) | $0.6664 | 1.900 (54/60 scored 2) |
| gpt-5.6-sol | none | 60/60 | 0.00 (0/60, max 0) | $1.1683 | 1.933 (56/60 scored 2) |
| gpt-5.6-sol | low | 60/60 | 0.00 (0/60, max 0) | $1.1673 | 1.933 (56/60 scored 2) |
| gpt-5.6-sol | medium | 60/60 | 0.00 (0/60, max 0) | $1.1710 | 1.900 (54/60 scored 2) |
| gpt-5.6-sol | high | 60/60 | 0.00 (0/60, max 0) | $1.1697 | 1.917 (55/60 scored 2) |
| gpt-5.6-sol | xhigh | 60/60 | 0.47 (1/60, max 28) | $1.1803 | 1.917 (55/60 scored 2) |
| gpt-5.6-sol | max | 60/60 | 2.43 (3/60, max 64) | $1.2197 | 1.917 (55/60 scored 2) |
| gpt-6.2-astra | low | 60/60 | 0.00 (0/60, max 0) | $2.3247 | 1.950 (57/60 scored 2) |
| gpt-6.2-astra | medium | 60/60 | 0.00 (0/60, max 0) | $2.3333 | 1.917 (55/60 scored 2) |
| gpt-6.2-astra | high | 60/60 | 2.65 (3/60, max 65) | $2.4400 | 1.967 (58/60 scored 2) |
| gpt-6.2-astra | xhigh | 60/60 | 8.47 (18/60, max 66) | $2.6933 | 1.917 (55/60 scored 2) |
| gpt-6.2-astra | max | 60/60 | 73.02 (60/60, max 271) | $5.3307 | 1.933 (56/60 scored 2) |
| gpt-6.2-sol | low | 60/60 | 0.00 (0/60, max 0) | $0.5817 | 1.950 (57/60 scored 2) |
| gpt-6.2-sol | medium | 60/60 | 0.00 (0/60, max 0) | $0.5845 | 1.967 (58/60 scored 2) |
| gpt-6.2-sol | high | 60/60 | 0.00 (0/60, max 0) | $0.5843 | 1.950 (57/60 scored 2) |
| gpt-6.2-sol | xhigh | 60/60 | 3.95 (6/60, max 67) | $0.6260 | 1.950 (57/60 scored 2) |
| gpt-6.2-sol | max | 60/60 | 66.48 (60/60, max 317) | $1.2715 | 1.950 (57/60 scored 2) |

Read this with the benchmark's limits: 20 authored samples, 3 repeats, one
gpt-4o judge call per cell (no confidence intervals). `gpt-4o` matched its May
2026 values ($0.665 per 1,000 requests, judge mean 1.90).

## Reproduce

```bash
python -m scripts.rerun_short_factual --endpoint https://<resource>.services.ai.azure.com \
    --deployment gpt-6.2-sol --family gpt-6.2-sol --efforts low,medium,high,xhigh,max \
    --experiment-id exp020_short-factual_rerun_gpt-6.2-sol --out reruns/01-short-factual-2026-11/runs
python -m scripts.run_judge --benchmark 01-short-factual \
    --runs-dir reruns/01-short-factual-2026-11/runs --judge-dir reruns/01-short-factual-2026-11/judge_runs \
    --experiment-prefix exp020_short-factual_rerun --deployment gpt-4o --endpoint <foundry project endpoint> --confirm
python -m scripts.analyze_rerun_short_factual
```

`--probe-rejected none,minimal` records rejected effort values with one call each.
The run JSONs record `git_commit` 5c479eb because they were produced before the
commit that adds these scripts. The re-run script wraps `AzureCliCredential` in an
async key provider because `openai>=2` `AsyncOpenAI` expects one.

Not measured here: PTU deployments, latency under load, and tasks other than
these 20 samples.
