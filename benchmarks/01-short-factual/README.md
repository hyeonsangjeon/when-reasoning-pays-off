# Benchmark 01 — Short-Factual (Null Case)

## Why this benchmark exists

This benchmark is the **null case** for reasoning effort. The 20 samples here are short,
structured-input to natural-language synthesis tasks that a non-reasoning model is
expected to solve correctly on the first try. Reasoning is **not required** to answer any
sample: every task is one extraction, one format conversion, one classification, or one
trivial arithmetic step on input that is already explicit.

If GPT-5.2 produces no measurable quality improvement here while spending additional
input, reasoning, and output tokens, then this benchmark establishes the **floor of
"when reasoning does not pay off."** That floor is the single most important number this
repo produces — without it, every cost-saving claim on more sophisticated benchmarks is
unanchored.

## Position in the repo

- Methodology contract: `docs/05-methodology.md` §2 (Variables) and §5 (Quality
  Evaluation). This benchmark inherits the full controlled-variables regime (byte-
  identical system prompt across efforts, R = 3 repeats, N = 20).
- Build Order: Phase 1, step 6 — dataset and prompts only. The smoke run lives in
  `experiments/exp001_short-factual_baseline.yaml` (and its gpt-4o counterpart). The
  cost-vs-quality verdict lands later in `analysis.md`.

## What is in this directory

```
01-short-factual/
├── README.md            # this file
├── dataset.json         # 20 frozen synthetic samples (sf_01 .. sf_20)
├── prompts/
│   ├── system.md        # neutral system prompt, byte-identical across efforts
│   └── user_template.md # deterministic user-prompt template, two placeholders
└── runs/                # per-experiment raw response JSON (populated later)
```

## Dataset design

- **N = 20** samples, IDs `sf_01` through `sf_20`. The set is exact: no gaps, no
  duplicates, no out-of-range IDs.
- **Synthetic only.** No real customer information, no real email domains
  (only `example.com` / `example.org` / `example.net` are permitted, and the current
  dataset contains zero email addresses), no US phone formats.
- Each sample carries:
  - `id` — `sf_NN`
  - `input` — the actual task payload, varied in shape (object, list, table-like
    record, or free text) so the benchmark is not a single-shape stress test
  - `expected_output_shape` — a one-sentence description of the answer shape, used by
    the user-prompt template and (later) by the LLM-as-judge prompt. This is the
    *shape* of the answer, not the literal expected string.
  - `quality_rubric_notes` — concrete acceptance criteria for the future judge pass:
    which values must appear, what wording flexibility is allowed, what disqualifies a
    response.
  - `tags` — task subtype labels for slicing the aggregate.

## Task taxonomy

The 20 samples span at least the following subtypes, intentionally diversified so no
single subtype dominates the aggregate:

| Subtype             | Example                                              | Samples |
| ------------------- | ---------------------------------------------------- | ------- |
| extraction          | pull a named field out of a structured record       | sf_01, sf_02, sf_06, sf_20 |
| formatting          | render a value in a fixed shape (ISO date, currency) | sf_03, sf_04, sf_07, sf_17, sf_18, sf_20 |
| summarization       | one-sentence summary of a 2–3 line input            | sf_05, sf_06, sf_19 |
| enumeration         | list items meeting a trivial filter                 | sf_07, sf_08 |
| classification      | label one of N obvious categories                   | sf_09, sf_10 |
| normalization       | trim, case, or ordering canonicalization            | sf_11, sf_12 |
| transliteration     | spell out a number or year                          | sf_13, sf_14 |
| arithmetic-trivial  | sum of two integers, count of a list                | sf_15, sf_16 |

Additional refinement tags (`synthesis`, `filtering`, `sentiment`, `triage`, `ordering`,
`counting`, `unit-conversion`) appear on individual samples to make later aggregation
slices cleaner. The deterministic diversity check requires at least 8 distinct tag
values across the dataset; the current dataset satisfies this with margin.

## Prompts

- `prompts/system.md` — a single neutral system prompt under 2000 characters. It is
  byte-identical for every `(model, effort, repeat)` cell. It does not contain any
  reasoning-triggering language — the forbidden-phrase audit (see Verification below)
  is enforced by CI grep.
- `prompts/user_template.md` — a two-line template with exactly two placeholders,
  `{input}` and `{expected_output_shape}`. The runner (`scripts/run_benchmark.py`)
  renders these per sample via `str.format_map`, producing a byte-identical rendered
  prompt across efforts and repeats.

The two prompt files together are the entire **controlled-prompt surface** for this
benchmark. A single-character edit to either file forces a new experiment ID and a
re-baseline; the cache invalidation cost of an unintentional edit would silently
contaminate every cross-effort comparison.

## Expected baseline behavior

- **gpt-4o quality success rate: ≥ 95 %.** This is the design intent and the
  pre-registered expectation. Every sample is solvable by a non-reasoning model on the
  first try; the rare failure modes we expect are formatting drift (e.g., missing a
  bullet prefix, adding a preface) rather than incorrect content.
- **gpt-5.2 quality success rate: also ≥ 95 %,** at every effort level from `minimal`
  through `high`. The null-case hypothesis is that quality is statistically
  indistinguishable across efforts on this benchmark.
- **Token cost:** the question this benchmark answers. We expect input and output
  tokens to be roughly flat across efforts (same prompt, same task) and reasoning
  tokens to grow non-trivially from `minimal` to `high`. The size of that reasoning-
  token climb on a task that does not need it is the single number that anchors the
  "when reasoning does not pay off" claim.

A finding of < 95 % gpt-4o success would invalidate the null-case framing: either the
dataset has ambiguous samples or the prompts have a defect, and the benchmark would
need to be repaired before the cost surface is trusted.

## Reasoning is not required

Every sample in this dataset can be answered in a single forward pass with no chain-
of-thought. There is no multi-step inference, no math beyond a single addition or
unit conversion, no logical puzzle, and no tool call. If a future reader believes a
sample requires reasoning to answer correctly, that sample is mis-classified and
belongs in benchmark 02 (`benchmarks/02-multi-step-reasoning/`) — file an issue.

## Verification (deterministic)

The following checks are enforced; each is a single shell or python one-liner and
must pass for the dataset to be considered correct.

```bash
# Sample count and ID uniqueness
python -c "import json; d = json.load(open('benchmarks/01-short-factual/dataset.json')); assert len(d) == 20; assert len({s['id'] for s in d}) == 20"

# IDs are exactly the set sf_01 .. sf_20 (strict set equality, not a regex)
python -c "import json; d = json.load(open('benchmarks/01-short-factual/dataset.json')); expected = {f'sf_{i:02d}' for i in range(1, 21)}; actual = {s['id'] for s in d}; assert actual == expected"

# Required fields present on every sample
python -c "import json; d = json.load(open('benchmarks/01-short-factual/dataset.json')); req = {'id','input','expected_output_shape','quality_rubric_notes','tags'}; assert all(req.issubset(s.keys()) for s in d)"

# Tag diversity >= 8 distinct tags
test "$(jq -r '.[].tags[]' benchmarks/01-short-factual/dataset.json | sort -u | wc -l | tr -d ' ')" -ge 8

# Forbidden phrases absent from prompts and dataset (exit code 1 = pass)
grep -iE 'step by step|reason carefully|reason through|let'\''s think|show your work|carefully consider' benchmarks/01-short-factual/prompts/system.md benchmarks/01-short-factual/prompts/user_template.md benchmarks/01-short-factual/dataset.json

# Synthetic data audit (no real email domains, no US phone formats)
python -c "import re; data = open('benchmarks/01-short-factual/dataset.json').read(); emails = re.findall(r'@([\w.-]+)', data); bad = [e for e in emails if e.lower() not in ('example.com','example.org','example.net')]; assert not bad; assert not re.search(r'\b\d{3}-\d{3}-\d{4}\b', data); assert not re.search(r'\(\d{3}\)\s?\d{3}-\d{4}', data)"
```

The exact set of checks that gate this task is documented in
`.internal/tasks/005-benchmark-01-dataset.md` (Test / Verification Plan).

## Out of scope for this directory

- Running the benchmark — see `experiments/exp001_short-factual_baseline.yaml` and the
  Task 006 smoke run.
- Quality scoring rubric beyond `quality_rubric_notes` — the LLM-as-judge prompt lives
  in `prompts/judge.md` (added by a later task).
- The cost-vs-quality conclusion — lands in `analysis.md` (Task 008) after raw runs
  exist in `runs/`.
