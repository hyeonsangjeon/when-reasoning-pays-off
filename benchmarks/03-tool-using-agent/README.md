# Benchmark 03 — Tool-Using Agent (Mixed Case)

## Why this benchmark exists

This benchmark is the **mixed-realistic case** for reasoning effort. The 20
samples here cover three subtypes:

- **no-tool** (6 samples): the correct trajectory invokes **zero** tools; the
  task is answerable from general knowledge or is a trivial echo. Invoking a
  tool is excessive and lowers tool-efficiency.
- **one-tool** (8 samples): exactly one tool call is expected — either a
  single arithmetic computation (calculator) or a single fact lookup
  (web_search) against the canned knowledge base.
- **multi-tool** (6 samples): 2–3 tool calls expected, chaining
  ``web_search → calculator`` (or longer). This is the inferential surface:
  the model must plan the tool sequence and combine intermediate results.

Paired with benchmarks 01 (the null floor) and 02 (the multi-step
reasoning ceiling), this benchmark completes the comparison needed to choose
a defensible model × effort × consumption-model default for a new production
task.

## Position in the repo

- Methodology contract: [`docs/05-methodology.md`](../../docs/05-methodology.md)
  §2 (Variables) and §5 (Quality Evaluation). This benchmark inherits the
  same controlled-variables regime (byte-identical system prompt across
  effort levels, R = 3 repeats, N = 20).
- Build Order: Phase 3, steps 12–15 — dataset + prompts + tool
  implementation + tool-loop runner extension + analysis +
  cross-benchmark synthesis. Final scaffold step.
- Tooling: the runner (`scripts/run_benchmark.py`), the judge
  (`scripts/run_judge.py`), and the analyzer (`scripts/analyze_tokens.py`)
  receive coordinated, additive edits in this task for tool-loop mode and
  the new `tool_efficiency_score` rubric field. The cost calculator
  (`scripts/cost_calculator.py`) and chart generator
  (`scripts/plot_results.py`) are unchanged.

## What is in this directory

```
03-tool-using-agent/
├── README.md            # this file
├── dataset.json         # 20 verifiable-answer samples (tu_01 .. tu_20)
├── search_kb.json       # canned KB for the web_search tool
├── prompts/
│   ├── system.md        # neutral system prompt, byte-identical across efforts
│   ├── user_template.md # deterministic user-prompt template
│   └── tool_schemas/
│       ├── calculator.json
│       └── web_search.json
├── runs/                # per-cell raw response JSON (with tool_calls trajectory)
├── judge_runs/          # per-cell judge JSON (correctness score + tool_efficiency_score)
├── SMOKE_REPORT.md      # Phase-1 smoke gate
├── RUN_REPORT.md        # Phase-2 full-run summary
├── analysis.json        # Phase-3 deterministic aggregation output
└── analysis.md          # Phase-3 narrative + cost-vs-quality verdict
```

## Dataset design

- **N = 20** samples, IDs `tu_01` through `tu_20`. The set is exact: no
  gaps, no duplicates.
- **Synthetic only.** Fictional entities (Vega City, Atlas Foundation,
  Helio Robotics, Northbridge Owls) carry the knowledge-base facts so the
  benchmark is reproducible — a model that "knows" the answer from
  pre-training cannot bypass the web_search tool.
- Each sample carries:
  - `id` — `tu_NN`
  - `input` — the task instruction
  - `expected_tool_calls` — list of tool names that should appear in a
    correct trajectory, or `null` for no-tool samples
  - `verifiable_answer` — the exact correct final answer
  - `expected_output_shape` — answer-shape description (used by the user
    prompt template and the judge)
  - `quality_rubric_notes` — concrete acceptance criteria the judge keys
    against
  - `tags` — subtype labels

## Task taxonomy

| Subtype     | Count | Samples           | expected_tool_calls |
| ----------- | ----: | ----------------- | ------------------- |
| no-tool     |     6 | tu_01 .. tu_06    | `null`              |
| one-tool    |     8 | tu_07 .. tu_14    | one of: `[calculator]` or `[web_search]` |
| multi-tool  |     6 | tu_15 .. tu_20    | 2–3 calls, mixing `web_search` and `calculator` |

The distribution (30 % / 40 % / 30 %) is the task spec's pre-registered
target — the benchmark therefore spans the realistic mix of trivial,
single-tool, and chained-tool production calls.

## Tools

Two deterministic tools (no live network):

- `calculator(expr)` — evaluates an arithmetic expression via Python's
  `decimal` module. Whitelisted operators: `+ - * / ** % //`. Whitelisted
  functions: `sqrt`, `abs`, `round`. Source: `scripts/tools.py::calculator`.
- `web_search(query)` — exact-match lookup against
  `benchmarks/03-tool-using-agent/search_kb.json`. Cache misses return the
  literal string `"no results"`; a model that searches with a malformed
  query gets this signal and is expected to recover. Source:
  `scripts/tools.py::make_web_search` (with `web_search` as a default stub
  for tests).

Schemas: `prompts/tool_schemas/{calculator,web_search}.json`. The runner
serializes the schema list once per benchmark, SHA-256 hashes it, and
records the digest as `call_metadata.tool_config_sha256` on every cell — a
single unique value across all 360 cells (byte-identical tool config
invariant; the tool-loop analogue of the byte-identical prompt invariant).

## Prompts

- `prompts/system.md` — single neutral system prompt under 2000
  characters. **Does not** contain reasoning-trigger phrases ("think step
  by step", "reason carefully", "show your work"). Tool selection rules
  are stated declaratively but never framed as reasoning instructions.
- `prompts/user_template.md` — two-line template with `{input}` and
  `{expected_output_shape}` placeholders. Rendered per sample via
  `str.format_map`, producing a byte-identical rendered prompt across
  effort levels and repeats.

## Pre-registered expectations

- **gpt-4o:** strong on no-tool (~95 %) and one-tool (~90 %); falls on
  multi-tool because tool sequencing requires planning the chain. Expected
  multi-tool pass-rate: 40–60 %.
- **gpt-5.2 effort=none:** matches gpt-4o on no-tool / one-tool;
  **possibly worse** on multi-tool because the model has no reasoning
  surface to plan the tool sequence.
- **gpt-5.2 effort=low → medium:** lifts multi-tool pass-rate. The Pareto
  knee should land at `medium` or `low`.
- **gpt-5.2 effort=high / xhigh:** quality saturates; cost-per-correct
  grows monotonically. Bonus risk: high-effort over-call on no-tool
  samples (the model "exhaustively verifies" trivial answers via a
  redundant calculator call), penalising tool-efficiency on the no-tool
  subtype.

The quality metric used in `analysis.md` is the **binarized 3-tier judge
rubric** (`pass = (score == 2)`), reused verbatim from
[`scripts/run_judge.py`](../../scripts/run_judge.py). The new
**`tool_efficiency_score`** rubric field (continuous `[0.0, 1.0]`) is
reported **separately** under "Tool-efficiency breakdown" — it is **never**
folded into the correctness rubric and **never** used to gate the
cost-per-correct ratio.

## Pre-registered cost surface

- **PAYG lens:** `cost_per_correct = mean_usd_per_cell / pass_rate`. On
  the multi-tool subset gpt-4o's absolute pass-rate is too low for
  production; gpt-5.2 effort=medium should win the cost-per-correct
  comparison **despite higher per-call cost** because pass-rate
  multiplication recovers the premium.
- **PTU lens:** `throughput_gain = baseline_tokens / cell_tokens` at
  matched quality. Tool-loop is token-heavy on the gpt-5.2 effort tiers
  (reasoning escalates), so PTU customers see lower throughput on every
  gpt-5.2 cell vs gpt-4o. The PTU correct-answers-per-minute lens may
  invert the PAYG winner.

## Verification (deterministic)

```bash
# Sample count and ID uniqueness
python -c "import json; d=json.load(open('benchmarks/03-tool-using-agent/dataset.json')); assert len(d)==20 and len({s['id'] for s in d})==20"

# IDs are exactly tu_01 .. tu_20
python -c "import json; d=json.load(open('benchmarks/03-tool-using-agent/dataset.json')); assert {s['id'] for s in d}=={f'tu_{i:02d}' for i in range(1,21)}"

# Required fields present on every sample
python -c "import json; d=json.load(open('benchmarks/03-tool-using-agent/dataset.json')); req={'id','input','verifiable_answer','expected_output_shape','quality_rubric_notes','tags','expected_tool_calls'}; assert all(req.issubset(s.keys()) for s in d)"

# Subtype distribution (30/40/30 spec)
python -c "
import json
d = json.load(open('benchmarks/03-tool-using-agent/dataset.json'))
no_tool = sum(1 for s in d if s['expected_tool_calls'] is None)
one_tool = sum(1 for s in d if isinstance(s['expected_tool_calls'], list) and len(s['expected_tool_calls'])==1)
multi   = sum(1 for s in d if isinstance(s['expected_tool_calls'], list) and len(s['expected_tool_calls'])>=2)
assert no_tool==6 and one_tool==8 and multi==6, (no_tool, one_tool, multi)
"

# Tag diversity >= 6 distinct tags
test "$(jq -r '.[].tags[]' benchmarks/03-tool-using-agent/dataset.json | sort -u | wc -l | tr -d ' ')" -ge 6

# Forbidden reasoning-trigger phrases absent from prompts
grep -iE "step by step|reason carefully|reason through|let's think|show your work|carefully consider" \
    benchmarks/03-tool-using-agent/prompts/system.md \
    benchmarks/03-tool-using-agent/prompts/user_template.md

# Tool implementation passes
pytest tests/test_tools.py -q
```

## Out of scope for this directory

- Running the benchmark — see `experiments/exp003_benchmark03_*.yaml` and
  the smoke YAMLs under `experiments/exp_smoke_03*.yaml`.
- Cross-benchmark synthesis — `results/summary.md` and
  `docs/04-decision-framework.md` (also in Task 010 scope, but live above
  this directory).
- Live web search integration — out of scope per Task 010 spec; the
  canned KB is the reproducibility surface.
