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

- CPython 3.11, 3.12, or 3.13 and a virtual environment.
- For the free local path: [Ollama](https://ollama.com) installed.
- For the billed path: an Azure OpenAI resource in Microsoft Foundry and the
  Azure CLI signed in (or a managed identity). No key is stored by this tool.
- Internet access is needed to *install* uncached packages and, for Ollama, to
  *pull* the model the first time. An Ollama run still makes a live HTTP call,
  but the default endpoint is loopback-only (`localhost`); a remote Ollama
  endpoint requires `--allow-remote-ollama`.

### Three non-overlapping reproducibility contracts

1. **Cold Mock functional verification** starts when tracked files are available
   in a source checkout. It materializes a checkout-equivalent tracked tree,
   creates a fresh virtual environment, builds and installs the minimal core
   wheel with pip caches disabled, runs help and Mock init/run, and ends only
   after schemas, checksums, the immutable run, and `latest.json` are inspected.
   The GitHub-hosted Ubuntu/CPython 3.13 reference threshold is **300 seconds**.
   Remote clone/fetch and runner provisioning are excluded. The uploaded
   `cold-mock-timing.json` is reference evidence, not a universal machine
   guarantee.
2. **Warm Ollama functional verification** starts immediately before
   `sample run` and ends when its immutable artifacts and pointer are published.
   Before timing, `sample doctor --json` must report
   `ollama.warm_prerequisites.ready: true`. The operator target is **300
   seconds**. Ollama installation, service startup, and model pull are excluded;
   the runner and doctor never pull a model, and doctor sends no prompt.
3. **Full research rerun** starts only after the environment, provider access,
   quota, deployment, pricing, prompts, and datasets are pinned. It ends after
   all benchmark and judge cells have terminal records and every aggregate,
   sanitizer, manifest, and chart gate has regenerated successfully. There is
   **no wall-clock SLO** because quota and live-service latency dominate.

Cold Mock and Warm Ollama prove their respective plumbing only. Neither has a
quality judge or comparable reasoning-effort sweep, and neither reproduces the
published benchmark.

Generate the Cold Mock timing report locally with the same cache policy:

```bash
python scripts/measure_cold_mock.py \
  --threshold-seconds 300 --output cold-mock-timing.json
```

### Platform support

| Surface | Linux | macOS | Windows |
| --- | --- | --- | --- |
| Minimal core/sample CLI | Supported on CPython 3.11–3.13 | Supported on CPython 3.11–3.13 | Supported on CPython 3.11–3.13 |
| Full research campaign | Reference platform: Linux x86-64, CPython 3.11 release lock | Operator-supported with platform-specific dependency resolution | **Not supported; use WSL/Linux** |

CI builds non-editable minimal wheels on Ubuntu, macOS, and Windows at CPython
3.11 and 3.13. The matrix runs only Mock and offline catalog/doctor paths,
installs no analysis or Azure extras, and makes no provider call. Full campaign
and release workflows assume POSIX/Bash; the max-output-token sweep imports
`fcntl`, and validation paths use platform shell SHA-256 tools. Windows core
locking retains exclusive creation and symlink/file-identity checks and uses
Windows process handles for liveness. Atomic replace is supported, but Python
does not expose POSIX directory `fsync` on Windows, so that stronger
power-loss-durability claim is POSIX-only.

## 3. Install the single entry point

```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install .        # minimal core/sample install
reasoning-payoff --help        # one command, discoverable with --help
```

The core install intentionally excludes NumPy, pandas, matplotlib, OpenAI, and
Azure Identity. Add `.[analysis]` for offline `analyze`/`report`, `.[azure]` for
the billed Azure provider, or `.[all]` for both. Mock and Ollama samples, retry,
doctor, and read-only experiment browsing remain in the core install.

`sample` has four operational subcommands:

| Command | What it does |
| --- | --- |
| `reasoning-payoff sample init --provider <p> --out <dir>` | Copy a ready-to-run workspace (ledger + dataset + `.env.example`). |
| `reasoning-payoff sample run --ledger <dir>/ledger.yaml` | Validate the ledger, call the model, publish a new immutable run. |
| `reasoning-payoff sample retry-failed --ledger <dir>/ledger.yaml --parent-run-id <id>` | Verify a parent and create a child run for failed attempts only. |
| `reasoning-payoff sample doctor --ledger <dir>/ledger.yaml` | Diagnose installation, workspace ownership/output structure, lock safety, and Ollama runtime/model identity. |

The durable machine-readable source for every installed CLI command's
execution, network, cost, and guard boundary is
[`batch-runner/batch_runner/data/cli_capabilities.v1.json`](../batch-runner/batch_runner/data/cli_capabilities.v1.json).
`scripts/check_docs_contracts.py` compares that manifest with the actual CLI
parser, provider choices, and the public README table.

## 4. The headline path — Ollama, local and free

The Warm Ollama five-minute target assumes Ollama is installed and running and
the exact model is already present. Installation, service startup, and every
`ollama pull` depend on the operator environment and are **not** counted.

```bash
# One-time setup in a separate shell:
ollama serve            # start the local server
ollama pull qwen2.5:0.5b   # ~0.5B-parameter Apache-2.0 model (small, fast)
ollama ls               # confirm the tag is present

# From your project directory:
reasoning-payoff sample init --provider ollama --out sample-workspace
reasoning-payoff sample doctor --ledger sample-workspace/ledger.yaml
reasoning-payoff sample run --ledger sample-workspace/ledger.yaml
```

Start the timer only after doctor reports `warm timing prerequisites: ready`
(or `warm_prerequisites.ready: true` in JSON). The default model tag is
configurable — edit `model:` in the ledger to any tag
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
expected_model_digest: null # optional Ollama sha256:... identity pin
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
  artifacts:
    [run.json, records.jsonl, summary.md, manifest.json, artifacts.sha256]
provenance:
  method_id: experiment-runner
  method_version: "1.0.0"
```

The full machine-readable contract is
`schemas/experiment_ledger.v1.schema.json`. The endpoint value is read from the
environment at run time — the ledger records only the variable **name**, never a
resolved URL or secret.

For Ollama, `expected_model_digest` may be `null` or the exact lowercase
`sha256:...` digest reported by `sample doctor --json`. Pinning it converts a
mutable model tag into an explicit allowlist: the runner rejects a missing or
different digest before submitting any prompt. It never pulls or updates a
model automatically.

For this first release the quickstart runs one row at a time: `concurrency` is
fixed at `1` and `artifacts` is exactly `[run.json, records.jsonl, summary.md,
manifest.json, artifacts.sha256]`.
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
Before an Ollama prompt, it calls official `/api/version`, `/api/tags`, and
`/api/show` endpoints. All four Ollama requests, including the final
`/api/chat`, use the validated endpoint, bypass environment proxies, and refuse
redirects. The first three calls confirm reachability, the requested installed
tag, and its digest/details; only then does `/api/chat` receive
`{model, messages, stream:false}`.

## 8. OUT — the artifacts

Output is staged and then published once in an owned run directory marked with
`.reasoning-payoff-experiment-owned`. The run ID combines UTC time, the first
eight hexadecimal digits of the ledger and input hashes, and an unpredictable
eight-digit suffix. A collision is refused; an existing run is never reused.
The runner refuses to write inside `benchmarks/**` or `results/**`, so a sample
can never contaminate published evidence. The whole workspace is gitignored.

```text
sample-workspace/out/
├── latest.json    # regular-file pointer, advanced atomically after full publish
├── .reasoning-payoff-experiment-owned
└── runs/
    ├── .reasoning-payoff-experiment-owned
    └── 20260830T090914Z_a1b2c3d4_e5f6a7b8_1a2b3c4d/
        ├── run.json          # provider/model, status, usage, and lineage
        ├── records.jsonl     # one line per attempted request
        ├── summary.md        # a short answer preview
        ├── manifest.json     # complete secret-safe provenance groups
        ├── artifacts.sha256  # hashes run/records/summary/manifest
        └── .reasoning-payoff-experiment-owned
```

`manifest.json` records repository identity, commit and dirty state when
available, package version, Python/OS/architecture, dependency-lock identity,
selected package versions, input and selected-ID hashes, a provider fingerprint,
pricing provenance, execution knobs, status/lineage, and payload artifact
hashes. It deliberately omits absolute paths, local usernames, endpoint hosts,
credentials, request IDs, row IDs, customer names, and prompt/response text.
An installed wheel running outside a Git checkout records explicit `unknown`
states for Git and lock identity instead of failing or searching user paths.

For Ollama, manifest schema `1.1.0` adds runtime version, requested tag, digest,
format, family, parameter size, quantization, and SHA-256 hashes of the
canonical JSON representation of `/api/show` template and `model_info` values.
Raw template and model metadata are never stored. A field the runtime does not
report is explicit `null`, not an inferred value. Hardware identity is
deliberately absent: operators may record hardware separately for performance
provenance, but it does not establish model-byte identity.

Two sequential runs create two sibling directories. Publishing the second only
replaces `latest.json`; every byte in the first directory remains unchanged.
Workspaces created by an older release may have flat files directly under
`out/`. Move that legacy `out/` aside before the first immutable run; the runner
refuses to overwrite or silently migrate it.

### Retry only failed attempts

If a run is partial or failed, use its ID from `latest.json`:

```bash
reasoning-payoff sample retry-failed \
  --ledger sample-workspace/ledger.yaml \
  --parent-run-id 20260830T090914Z_a1b2c3d4_e5f6a7b8_1a2b3c4d
```

The command verifies the parent's checksums and ownership, confirms the current
ledger and input hashes still match, and publishes a child run with
`parent_run_id`. Only parent records whose status is `error` are called;
successful rows are never re-called. Provider, cost, CI, endpoint, no-retry,
and redaction guards are identical to a normal run.

### Diagnose and safely recover a crashed workspace

Every sample run holds `.reasoning-payoff-sample.lock` for its full lifetime.
The versioned JSON lock stores a PID, one-way host fingerprint, creation time,
one-way process-start token when available, ledger/operation identity, and tool
version. It stores neither hostname nor username.

```bash
reasoning-payoff sample doctor --ledger sample-workspace/ledger.yaml
reasoning-payoff sample doctor --ledger sample-workspace/ledger.yaml --json

# Mutating recovery is always explicit:
reasoning-payoff sample doctor --ledger sample-workspace/ledger.yaml \
  --repair-stale-lock
```

Doctor removes a lock only when its metadata is valid, its host fingerprint
matches this machine, and the recorded PID is proven absent. A live PID is
never removed. Cross-host, malformed, symlinked, unknown-liveness, and
PID-reuse states fail closed with manual guidance. Recovery rechecks the exact
lock inode, then acquires a fresh exclusive/no-follow lock before touching
output. Under that lock it deletes only hidden staging directories whose names
match the runner format and whose ownership marker is valid. Completed run
directories and foreign paths are never removed.

Each actual repair appends a private workspace event containing only time,
tool version, the old lock-content hash, same-host proof, and cleanup count. It
does not contain usernames, absolute paths, endpoint hosts, request IDs, or
secrets. Doctor JSON conforms to `schemas/sample_doctor.v1.schema.json`.

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
python -m pip install ".[azure]"
reasoning-payoff sample init --provider azure --out azure-workspace
export AZURE_OPENAI_FOUNDRY_ENDPOINT="https://<your-resource>.openai.azure.com"
az login    # or a managed identity — no key is stored by this tool

# Billed. You must acknowledge the cost on the command line AND in the ledger:
reasoning-payoff sample run --ledger azure-workspace/ledger.yaml --confirm-cost
```

Two gates must both be satisfied: the CLI flag `--confirm-cost` **and** the
ledger's `execution.cost.confirmed: true`. In a continuous-integration (CI)
environment the billed run is hard-refused by default before any network call.
If the Azure extra is absent or below its supported version, the command stops
before endpoint resolution or authentication and prints the exact
`pip install "when-reasoning-pays-off[azure]"` remediation.

**Cost preflight (enforced against an immutable snapshot).** The Azure ledger
pins a repository snapshot by stable ID, path, and SHA-256, then selects one
record by price key plus intended model family/version, geography, region,
deployment type, and currency. Every dimension must match. Missing, unknown, or
mismatched identities fail before endpoint resolution, provider construction,
token acquisition, or any network call. Rates are never copied into the ledger:
the runner derives separate input, cached-input, and output rates from the
verified record. Before any network call, it bounds input tokens by UTF-8 bytes
plus framing overhead, assumes every request emits the full
`max_output_tokens`, and applies those snapshot rates. It prints a plan
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
  pricing:
    snapshot_id: azure-openai-payg-sample-2026-08
    snapshot_path: pricing/azure-openai-payg-sample-2026-08.yaml
    snapshot_sha256: 0b51eab30e21a52f4e963a427bd818e7ca7e13c06386a11e97c6590a1e0f60f5
    price_key: azure-openai:gpt-5.2:2025-12-11:global:global-standard
    model_family: gpt-5.2
    model_version: "2025-12-11"
    geography: global
    region: global
    deployment_type: Global Standard
    currency: USD
```

The snapshot contains only public pricing identity. The `model` field may be
your private deployment alias; manifests replace it with the intended
`family@version` and mark live Azure service metadata as not independently
verified. Endpoint values and deployment aliases are never written to the
snapshot or immutable manifest. Snapshot selection is deterministic and applies
no wall-clock freshness rule; `selection_policy.freshness_policy:
not-applied` is the explicit extension point for the later freshness policy.

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

### Dispatch one experiment: `experiment run`

`experiment run` is the single safe entry point for all 20 catalogued
experiments. It resolves an id (or config filename) to exactly one experiment,
looks up the **typed adapter** that owns it (no shell string, no `eval`), and
dispatches one of two stages:

```bash
# DRY-RUN (default): write an immutable, normalized execution plan. No socket is
# opened, no credential/endpoint is resolved, and no provider is called.
reasoning-payoff experiment run exp001_short-factual_baseline --stage dry-run
reasoning-payoff experiment run exp001_short-factual_baseline --json   # machine-readable

# LIVE: delegate through the typed adapter to the validated runner (billed Azure OpenAI). The
# runner enforces CI hard-refusal, the YAML budget confirmation, secret
# redaction, store=false, max_retries=0, output locks, and campaign gates.
reasoning-payoff experiment run exp001_short-factual_baseline --stage live --confirm-cost
```

Both stages require the **source checkout** — the `experiments/` configs and the
`scripts/` runners are clone-only and are never shipped in the wheel. From a
wheel-only install the command fails with an actionable message (exit `8`) and
no absolute path is leaked; read-only `experiment list` / `experiment describe`
still work from the wheel.

**What a dry-run writes.** Each dry-run publishes one immutable plan under an
owned, gitignored output directory (default `.reasoning-payoff-plans/plans/`).
The plan is a strict, versioned JSON document (schema
`schemas/experiment_execution_plan.v1.schema.json`) capturing the same
DATA → IN → EXECUTE → OUT view, grounded in the real bytes on disk:

- `identity` — experiment id, repo-relative config path, and the config's
  SHA-256 (the plan id is derived from it, so re-planning is deterministic).
- `adapter` — the typed adapter id + version, its source module, and whether it
  has a billed live path.
- `data.inputs` — each input corpus with its format, shape, presence, and
  SHA-256 (or `null` when a source asset is missing).
- `input` — the safe provider/model-family identity and the endpoint/auth
  environment-variable **names only**; `credentials_resolved` and
  `endpoint_resolved` are pinned `false`.
- `pricing` — the policy and snapshot identity (dry-run uses the offline-only
  `historical-replay` policy for pricing-aware runners).
- `knobs.bounded` — the swept variable and a capped set of numeric knobs.
- `outputs` — the artifacts a real run would write.
- `network_calls` and `billed_calls` — both pinned to `0`.

Plans are immutable: a second publication of the same plan id is **refused**,
never overwritten, and the command refuses to write inside a protected tree
(`benchmarks/`, `results/`, `docs/blog`).

**Coverage stays one-to-one.** Every catalogued experiment binds to exactly one
registered adapter; an unknown adapter or a duplicate id fails closed. Unknown
or ambiguous experiment ids are rejected with the candidate list (exit `3`).

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

The `sample run` and `sample retry-failed` commands return typed exit statuses.
Completed rows are always
preserved safely; partial failures are visible and never reported as success.

| Code | Meaning |
| --- | --- |
| `0` | All rows succeeded. |
| `20` | Partial — some rows failed. Completed rows are preserved; the failure list is printed. |
| `21` | All rows failed. |
| `3` | Invalid input — ledger or dataset failed validation (unknown fields, bad types, unsafe paths, oversized input). |
| `4` | Privacy violation — a redaction/capture rule was breached. |
| `5` | Output/lock conflict — the target is unsafe, a lock is live/unknown, or doctor repair cannot prove safety. |
| `6` | Filesystem error while writing artifacts. |
| `7` | Cost or provider error — billed run not confirmed, or the provider/model was unavailable or refused. |

### `experiment run` exit codes

`experiment run` returns typed statuses. A dry-run never contacts a provider; a
live run delegates to the validated runner, whose own exit code is surfaced.

| Code | Meaning |
| --- | --- |
| `0` | Dry-run plan written, or the live runner completed. |
| `3` | Unknown or ambiguous experiment id, or the config failed the runner's strict loader. |
| `5` | Plan conflict — the immutable plan already exists, the output is unowned, or the target is a protected tree. Never overwritten. |
| `6` | Filesystem error while publishing the plan. |
| `7` | Cost/live error — `--stage live` without `--confirm-cost`, or the adapter has no billed live path. |
| `8` | Source checkout (or its base runtime) is unavailable; `experiment run` needs the clone. |

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
- **exit 5 writing output or diagnosing a lock** — output is fixed to the
  workspace's gitignored `out/runs/` hierarchy. Move legacy flat
  `out/run.json` output aside. Run `sample doctor`; never delete
  `.reasoning-payoff-sample.lock` blindly. Use `--repair-stale-lock` only when
  doctor reports a proven same-host stale lock.
- **Ollama digest mismatch** — verify the installed tag with `sample doctor
  --json`. Update `expected_model_digest` only after independently approving
  the new model bytes; the runner never pulls a replacement.

## 14. Scope reminder

This is an illustrative live sample that proves the end-to-end plumbing — the
data that went in, the model/endpoint/provider selected, the command executed,
and where the output was written. It is **not** the published benchmark, has no
quality judge, and runs no comparable reasoning-effort sweep. For the recorded
benchmark evidence and the decision method, see the article and guide 19.
