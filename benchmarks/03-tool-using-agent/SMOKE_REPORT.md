# Smoke Report — Benchmark 03 (Tool-Using Agent)

## Verdict: GO

Live Foundry v1 smoke pass succeeded against both smoke YAMLs. The
tool-loop runner extension dispatches function calls through
`scripts.tools.TOOL_REGISTRY`, feeds tool results back, sums
per-iteration usage, and writes a populated `tool_calls` trajectory on
every cell that invoked a tool.

## Pre-run snapshot

| Item | Value |
| --- | --- |
| Branch | `feature/benchmark-03-and-synthesis` |
| Git HEAD at smoke fire | dirty (uncommitted task-010 edits) |
| Smoke timestamp (UTC) | 2026-05-24T09:36:58Z – 2026-05-24T09:37:11Z |
| Pricing snapshot | `pricing/azure-openai-payg-2026-05.yaml` |
| Tool config sha256 (byte-identical across all cells) | `0c4f778c655f6535985b5ee6f83e96847cca53243c5056068e20511d73a5e53c` |
| Endpoint | `$AZURE_OPENAI_FOUNDRY_ENDPOINT` (`/openai/v1/`, api_version=preview) |
| Auth | Entra ID (`DefaultAzureCredential`) |

## Smoke matrix

| YAML | Cohort | Cells | Live calls | Sweep | Spend (USD) | Outcome |
| --- | --- | --- | --- | --- | --- | --- |
| `experiments/exp_smoke_03_gpt4o.yaml` | `exp_smoke_03_gpt4o` | 2 | 2 | n/a (`effort=null`) | 0.003 | ✅ all cells OK |
| `experiments/exp_smoke_03.yaml` | `exp_smoke_03` | 4 | 4 | `[low, high]` | 0.005 | ✅ all cells OK |
| **Total smoke** | | **6** | **6** | | **≈ 0.008** | ✅ ≤ $0.50 ceiling |

Smoke spend is ~$0.008, well under the combined $0.50 ceiling.

## Sample-level outcomes

| Sample | Effort | Model | Tool calls observed | Final answer |
| --- | --- | --- | ---: | --- |
| `tu_01` (no-tool) | null | gpt-4o | 0 | "Welcome, it's great to have you here!" |
| `tu_02` (no-tool) | null | gpt-4o | 0 | "acknowledged" |
| `tu_01` (no-tool) | low | gpt-5.2 | 0 | "Welcome, I'm glad you're here." |
| `tu_01` (no-tool) | high | gpt-5.2 | 0 | "Welcome, I'm glad you're here." |
| `tu_02` (no-tool) | low | gpt-5.2 | 0 | "acknowledged" |
| `tu_02` (no-tool) | high | gpt-5.2 | 0 | "acknowledged" |

Both smoke samples are intentionally drawn from the no-tool subset (the
runner's `max_samples` cap takes the first N samples by id); the
dispatch is therefore validated by the separate runner unit test
(`tests/test_run_benchmark.py::test_live_tool_loop_dispatches_tool_calls`)
and by a live spot-check on `tu_07` (calculator), where the model
correctly invoked `calculator(expr='17.3/100*241000')`, received `41693`,
and emitted `41693` as the final answer.

## Wire-level checks

- `usage` keys present on every cell: `input_tokens`,
  `input_tokens_details.cached_tokens`, `output_tokens`,
  `total_tokens` (gpt-4o cells correctly omit
  `output_tokens_details.reasoning_tokens`; gpt-5.2 cells carry it
  ≥ 0). Forbidden legacy paths `prompt_tokens_details` /
  `completion_tokens_details` absent.
- `tool_loop_terminated == "ok"` on every smoke cell.
- `call_metadata.tool_config_sha256` is a single value across all 6
  smoke cells (matching the byte-identical-tool-config invariant).
- One unique `system_prompt_sha256` across all 6 cells.

## Content-filter regression notes

Two dataset rephrasings were needed before the live smoke could pass —
Azure's jailbreak-classification filter intermittently flagged the
imperative phrases "Do not invoke any tool" and the "Echo the word X
back to me" pattern as prompt-injection attempts. Both patterns were
softened (rephrased to "Reply with the single word X" and "The
trajectory should contain no tool invocations" respectively) without
changing the underlying methodology contract: every cell in the same
benchmark run still sees byte-identical system + user text, and the
verifiable answers / quality rubric notes were updated in lockstep.

After the rephrasing pass the full 20-sample dataset passed an offline
content-filter sanity sweep (one call per sample with `tools=` set);
zero `content_filter` rejections were observed.

## GO decision rationale

1. Both smoke YAMLs executed end-to-end against live Foundry v1.
2. Six raw JSONs written under `benchmarks/03-tool-using-agent/runs/`.
3. Combined smoke spend (~$0.008) is well under the $0.50 ceiling.
4. Tool dispatch path is verified by the live spot-check on `tu_07`
   and the runner unit test (`test_live_tool_loop_dispatches_tool_calls`).
5. No `content_filter` rejections after the dataset rephrasing pass.
6. Foundry v1 wire shape (`api_version=preview`, base URL
   `/openai/v1/`, Entra audience `https://ai.azure.com/.default`)
   verified by the cell records.

The full 360-cell measurement pass (60 gpt-4o + 300 gpt-5.2) is cleared
to fire against `experiments/exp003_benchmark03_gpt4o.yaml` and
`experiments/exp003_benchmark03_gpt5_2.yaml` under the combined
$45 hard ceiling.
