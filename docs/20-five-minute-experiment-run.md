# 20. Run one real experiment in five minutes

This guide covers the `reasoning-payoff sample` command, which **calls a real
model** and shows every stage of the run as one explicit flow:

```text
DATA  ->  IN  ->  EXECUTE  ->  OUT
```

- **DATA** — the exact input file, its format, and the shape of each row.
- **IN** — the *ledger*: a strict, versioned contract that names the provider,
  the model, the endpoint, the limits, and the cost boundary before anything
  runs. Unknown fields are rejected; unsafe values fail closed.
- **EXECUTE** — the single command that sends each row to the model.
- **OUT** — the artifacts written to an owned, gitignored output directory.

> **How this differs from the offline provenance report (guide 19).**
> Guide 19's `reasoning-payoff analyze` *analyzes usage you already recorded* and
> makes **no** model call. This guide's `reasoning-payoff sample run` *calls a
> model right now*. They are separate tools with separate output.

## 1. Plain-language terms

- **Large language model (LLM)** — the program that reads your text prompt and
  writes a text answer.
- **Provider** — who serves the model. Here: **Ollama** (runs on your own
  machine, no cloud bill) or **Azure OpenAI in Microsoft Foundry** (a paid
  cloud service).
- **Endpoint** — the base web address a request is sent to. For Ollama the
  default is `http://localhost:11434` (your own computer only).
- **Model / deployment** — the specific model that answers. Ollama uses a model
  *tag* such as `qwen2.5:0.5b`; Azure uses a *deployment* name you created.
- **JSON / JSONL** — JSON is one structured object or array. JSONL is one JSON
  object per line, so a dataset is one row per line.
- **Schema** — a machine-readable description of which fields a file must have
  and their types. This repo ships schemas under `schemas/`.
- **Entra ID** — Microsoft's identity service. On Azure the runner signs in with
  it and never stores a password or key.
- **Reasoning effort** — an optional Azure setting for how much internal work the
  model does before answering. Ollama does not support it.
- **Token usage** — the count of text pieces read (input) and written (output).
  Billing and speed scale with tokens.
- **Latency** — how long one request took, in milliseconds.

## 2. Prerequisites

- Python 3.11 or later and a virtual environment.
- For the free local path: [Ollama](https://ollama.com) installed.
- For the billed path: an Azure OpenAI resource in Microsoft Foundry and the
  Azure CLI signed in (or a managed identity). No key is stored by this tool.
- Internet access is needed to *install* uncached packages and, for Ollama, to
  *pull* the model the first time. An Ollama run still makes a live HTTP call,
  but the default endpoint is loopback-only (`localhost`); a remote Ollama
  endpoint requires `--allow-remote-ollama`.

## 3. Install the single entry point

```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install .        # or: pip install when-reasoning-pays-off (once published)
reasoning-payoff --help        # one command, discoverable with --help
```

`sample` has three subcommands:

| Command | What it does |
| --- | --- |
| `reasoning-payoff sample init --provider <p> --out <dir>` | Copy a ready-to-run workspace (ledger + dataset + `.env.example`). |
| `reasoning-payoff sample run --ledger <dir>/ledger.yaml` | Validate the ledger, call the model, write artifacts. |
| `reasoning-payoff sample run --help` | Show scope and cost before running. |

The durable machine-readable source for every installed CLI command's
execution, network, cost, and guard boundary is
[`batch-runner/batch_runner/data/cli_capabilities.v1.json`](../batch-runner/batch_runner/data/cli_capabilities.v1.json).
`scripts/check_docs_contracts.py` compares that manifest with the actual CLI
parser, provider choices, and the public README table.

## 4. The headline path — Ollama, local and free

The five-minute target assumes Ollama is installed and the small model is
available or quick to pull. The first `ollama pull` depends on your network and
hardware and is **not** counted in the five minutes.

```bash
# One-time setup in a separate shell:
ollama serve            # start the local server
ollama pull qwen2.5:0.5b   # ~0.5B-parameter Apache-2.0 model (small, fast)
ollama ls               # confirm the tag is present

# From your project directory:
reasoning-payoff sample init --provider ollama --out sample-workspace
reasoning-payoff sample run --ledger sample-workspace/ledger.yaml
```

The default model tag is configurable — edit `model:` in the ledger to any tag
you have pulled. The runner **never** pulls a model for you; if the model is
missing it stops with an actionable message telling you the exact
`ollama pull ...` command to run.

Ollama is not billed by any cloud, but it uses your local CPU/GPU and memory.

## 5. DATA — the sample dataset

`sample init` copies a tiny, license-safe dataset in two equivalent forms:
`sample.jsonl` (one row per line) and `sample.json` (the same rows as one JSON
array). Each row matches this shape:

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | A short, unique row identifier. |
| `input` | string | yes | The prompt sent to the model. |
| `expected` | string | no | An optional reference answer for your own eyeballing. It is **not** scored — there is no quality judge here. |

Exact contents of `sample.jsonl`:

```json
{"id": "q1", "input": "What is the capital of France? Answer with just the city name.", "expected": "Paris"}
{"id": "q2", "input": "What is 2 + 2? Answer with a single number.", "expected": "4"}
{"id": "q3", "input": "Name the three additive primary colors of light (RGB).", "expected": "red, green, blue"}
```

The machine-readable contract is `schemas/experiment_sample_row.v1.schema.json`.
Rows that are missing `id`/`input`, add unknown fields, use non-string values,
or exceed the ledger's `max_records` are rejected before any model call.

## 6. IN — the run ledger

The ledger is the strict, versioned contract for the run. Below is the packaged
Ollama ledger, annotated. Every field is validated; unknown fields are rejected.

```yaml
schema_version: "1.0.0"
experiment:
  id: five-minute-ollama
  purpose: >-
    Illustrative live sample. Confirms DATA -> IN -> EXECUTE -> OUT end to end.
provider: ollama            # ollama | azure | mock
model: qwen2.5:0.5b         # the model tag (Ollama) or deployment name (Azure)
endpoint:
  env_var: OLLAMA_BASE_URL  # the NAME of the env var read at run time
  default: http://localhost:11434   # localhost-only fallback for Ollama
auth:
  mode: none                # Ollama needs no credentials
  env_vars: []
input:
  path: sample.jsonl
  format: jsonl             # json | jsonl
  row_shape:
    required_fields: {id: string, input: string}
    optional_fields: {expected: string}
  max_records: 50
  sample_selector: first
execution:
  max_samples: 3            # how many rows to actually run
  concurrency: 1
  timeout_seconds: 60
  max_output_tokens: 128
  repeats: 1
  capture_io: true          # save the model's answer (safe for public prompts)
  cost: {billed: false, confirmed: false}
output:
  dir: out                  # artifacts land here (gitignored)
  artifacts: [run.json, records.jsonl, summary.md]
provenance:
  method_id: experiment-runner
  method_version: "1.0.0"
```

The full machine-readable contract is
`schemas/experiment_ledger.v1.schema.json`. The endpoint value is read from the
environment at run time — the ledger records only the variable **name**, never a
resolved URL or secret.

For this first release the quickstart runs one row at a time: `concurrency` is
fixed at `1` and `artifacts` is exactly `[run.json, records.jsonl, summary.md]`.
The ledger rejects any other value rather than silently ignoring it, so the file
never records settings the runner does not honor.

### `capture_io` and privacy

The packaged sample uses **public** prompts, so `capture_io: true` — the model's
answer is saved so you can confirm the run really happened. For your **own**
data, set `capture_io: false` to redact request/response text; the run still
records timings and token usage, but not the content. The summary always shows
only a short, safe answer preview; full text lives only inside the owned output
directory.

## 7. EXECUTE — run the model

```bash
reasoning-payoff sample run --ledger sample-workspace/ledger.yaml
```

`sample run --help` states the scope and cost before you run. The command
validates the ledger and dataset, then sends `max_samples` rows to the provider.
Ollama calls `POST http://localhost:11434/api/chat` with
`{model, messages, stream:false}` and reads the answer from `message.content`.

## 8. OUT — the artifacts

Output lands in an owned directory marked with
`.reasoning-payoff-experiment-owned`. The runner refuses to write inside
`benchmarks/**` or `results/**`, so a sample can never contaminate published
evidence. The whole workspace is gitignored.

```text
sample-workspace/out/
├── run.json     # provider, model, input schema + hash + row count, ledger hash,
│                #   start/end/status, method version, aggregate usage, cost boundary
├── records.jsonl  # one line per request: id, status, latency_ms, token usage,
│                  #   answer text (only when capture_io is true)
├── summary.md     # a short answer preview + the exact records path
└── .reasoning-payoff-experiment-owned
```

Token usage is reported per the provider's real response. Ollama does **not**
report cached-input or reasoning tokens, so those appear as `not_supported`
rather than a fabricated `0`. Durations from Ollama are converted from
nanoseconds to milliseconds without relabeling.

> **Banner honesty.** Live runs (Ollama/Azure) are labelled *illustrative live
> sample — not the published benchmark; no quality judge or comparable
> reasoning-effort sweep*. The mock provider is labelled *illustrative offline
> preview* with the same tail. Neither is described as reproducing the
> benchmark, because it does not.

## 9. The billed path — Azure OpenAI in Microsoft Foundry

Azure runs are **billed** and are refused unless you explicitly confirm the
cost, so the default command can never spend money by accident.

```bash
reasoning-payoff sample init --provider azure --out azure-workspace
export AZURE_OPENAI_FOUNDRY_ENDPOINT="https://<your-resource>.openai.azure.com"
az login    # or a managed identity — no key is stored by this tool

# Billed. You must acknowledge the cost on the command line AND in the ledger:
reasoning-payoff sample run --ledger azure-workspace/ledger.yaml --confirm-cost
```

Two gates must both be satisfied: the CLI flag `--confirm-cost` **and** the
ledger's `execution.cost.confirmed: true`. In a continuous-integration (CI)
environment the billed run is hard-refused by default before any network call.

**Cost preflight (enforced against pinned assumptions).** The Azure ledger names
the pricing model, a pricing snapshot ID, and separate input/output rates per
million tokens. Verify those values against the current price for your deployment
before setting `confirmed: true`. Before any network call, the runner bounds
input tokens by UTF-8 bytes plus framing overhead, assumes every request emits
the full `max_output_tokens`, and applies those declared rates. It prints a plan
line such as `cost plan: 3 request(s), <= 128 output tokens each; conservative
estimate $0.0055 (ceiling $1.00)`. If the estimate exceeds
`execution.cost.hard_ceiling_usd`, the run is refused with zero calls. This
ceiling governs the recorded estimate, not Azure's invoice; changed prices,
taxes, or provider billing rules can differ.

```yaml
cost:
  billed: true
  confirmed: true
  hard_ceiling_usd: 1.00
  pricing_snapshot_id: starter-assumption-gpt-5.2-2026-03-08
  pricing_model: gpt-5.2
  input_per_1m_usd: 1.75
  output_per_1m_usd: 14.00
```

**Data retention.** Every sample Responses call sets `store=false`, the
documented stateless mode: this call is not stored for later retrieval and no
conversation state is kept for it. This is only the stored-response boundary —
it does not promise zero data retention. Azure's abuse-monitoring and
data-processing policies are a separate service boundary; the default Responses
API otherwise retains request data for 30 days. See Microsoft Learn,
[Use the Responses API](https://learn.microsoft.com/azure/foundry/openai/how-to/responses).

The planned request count is `selected rows × repeats` (three by default).
The runner sends `timeout_seconds` to the SDK and sets `max_retries=0`, so each
planned request is attempted at most once by this runner; a timeout is not
silently retried.

**Authentication.** The runner uses a refreshable Entra ID bearer-token provider
with the audience `https://ai.azure.com/.default`; Azure Identity caches and
refreshes the token per request. No password, key, or resolved endpoint is
written to any artifact. The packaged Azure ledger lists `auth.env_vars: []`
(the default Azure CLI / managed-identity chain); the endpoint variable is
configuration, not an auth variable.

**Reasoning effort.** The packaged Azure ledger sets `reasoning_effort: none`
for predictable, cost-safe behavior. Supported values vary by model; current
Azure `gpt-5.2` supports `none/low/medium/high/xhigh` and rejects `minimal`, so
the runner refuses `minimal` when the selected deployment is clearly `gpt-5.2`.
Azure usage is normalized from the Responses API (`input_tokens`,
`input_tokens_details.cached_tokens`, `output_tokens`,
`output_tokens_details.reasoning_tokens`, `total_tokens`); reasoning tokens are
already counted inside output tokens and are not double-counted.

## 10. The offline preview — mock provider

No install, no network, deterministic:

```bash
reasoning-payoff sample init --provider mock --out mock-workspace
reasoning-payoff sample run --ledger mock-workspace/ledger.yaml
```

The mock provider returns a fixed, offline answer so you can see the exact
artifact shapes before running a live model. It is a **preview and test aid**,
labelled *illustrative offline preview* — not a model result.

## 11. Browse all 20 committed experiments

The `sample` command runs a tiny illustrative sample. The repository also
contains the full experiment configurations (`experiments/exp*.yaml`). To browse
every one of the 20 committed experiments — 13 primary/pair configs for
exp001–exp007 plus 7 smoke/warm-probe configs — as a DATA/IN/EXECUTE/OUT view
without running anything:

```bash
reasoning-payoff experiment list                 # human-readable table
reasoning-payoff experiment list --json          # machine-readable, experiment_count=20
reasoning-payoff experiment describe exp001       # one experiment, all four stages
reasoning-payoff experiment describe exp001 --json
```

The catalog is derived from the existing experiment families and YAML — it is
not a separate hand-maintained source of truth. Every `exp*.yaml` (excluding
`_template.yaml`) is covered exactly once. These full experiments run against
clone-relative data; the wheel only packages the tiny quickstart workspace, not
the benchmark datasets.

### Source-data shapes of the full experiments

These are the inputs the full experiments read (reported for orientation; the
`sample` command does not use them):

| Family | DATA files | Shape |
| --- | --- | --- |
| Benchmarks 01–02 (short-factual, multi-step) | `dataset.json`, `prompts/system.md` | `dataset.json` is a JSON array of 20 rows; row keys include `id`, `input`, `expected_output_shape`, `tags`, `quality_rubric_notes` (02 adds `verifiable_answer`). |
| Benchmark 03 (tool-using agent) | `dataset.json`, `prompts/system.md`, `search_kb.json`, tool schemas | `dataset.json` array of 20 rows adds `expected_tool_calls`; `search_kb.json` is a JSON object mapping query strings to answer strings. |
| Benchmarks 04–06 (spillover, dual-spillover, cache-key) | `system_prompt_corpus.json`, `user_prompts.json` | Both are JSON arrays of strings (system corpus ~132 entries; user prompts ~30). |
| Experiment 07 (max-output-tokens) | reuses benchmark 04 inputs | Same two JSON string arrays as family 04. |

## 12. Exit codes

The `sample run` command returns a typed exit status. Completed rows are always
preserved safely; partial failures are visible and never reported as success.

| Code | Meaning |
| --- | --- |
| `0` | All rows succeeded. |
| `20` | Partial — some rows failed. Completed rows are preserved; the failure list is printed. |
| `21` | All rows failed. |
| `3` | Invalid input — ledger or dataset failed validation (unknown fields, bad types, unsafe paths, oversized input). |
| `4` | Privacy violation — a redaction/capture rule was breached. |
| `5` | Output conflict — the target directory is not an owned, safe location. |
| `6` | Filesystem error while writing artifacts. |
| `7` | Cost or provider error — billed run not confirmed, or the provider/model was unavailable or refused. |

## 13. Troubleshooting

- **`ollama` connection refused** — start the server with `ollama serve` and
  confirm the endpoint is `http://localhost:11434`.
- **model not found (Ollama)** — pull it: `ollama pull qwen2.5:0.5b`, then
  `ollama ls` to confirm. The runner never pulls for you.
- **non-local Ollama endpoint rejected** — the default refuses non-localhost
  endpoints for safety; keep `OLLAMA_BASE_URL` on `localhost`.
- **Azure run refused with exit 7** — you must pass `--confirm-cost` and set
  `execution.cost.confirmed: true`; billed runs are hard-refused in CI.
- **Azure `minimal` rejected** — `gpt-5.2` does not support `minimal`; use
  `none` (default) or `low/medium/high/xhigh`.
- **exit 5 writing output** — output is fixed to the workspace's gitignored
  `out/` directory. Remove unexpected files or a stale
  `.reasoning-payoff-sample.lock` only after confirming no run is active.

## 14. Scope reminder

This is an illustrative live sample that proves the end-to-end plumbing — the
data that went in, the model/endpoint/provider selected, the command executed,
and where the output was written. It is **not** the published benchmark, has no
quality judge, and runs no comparable reasoning-effort sweep. For the recorded
benchmark evidence and the decision method, see the article and guide 19.
