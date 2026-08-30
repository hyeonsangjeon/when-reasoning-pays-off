# 19. Five-minute offline provenance report

This guide is the operating contract for the offline `reasoning-payoff
analyze` and `reasoning-payoff report` commands. They turn strict usage
metadata into a local, evidence-first report bundle without calling Azure,
uploading data, starting a server, or enabling telemetry. The separate
`reasoning-payoff sample run` command has provider-specific network boundaries
documented in guide 20 and the CLI capability manifest.

The target is practical: from a fresh clone, produce a useful `report.html`
within five minutes. The bundled sample normally completes in seconds.

## 1. Scope and evidence boundary

The first release analyzes operational metadata only:

- token counts, including cached input and reasoning-token subsets;
- latency and HTTP status behavior;
- modeled pay-as-you-go (PAYG) cost from a pinned local pricing snapshot (versioned local pricing file);
- optional provisioned throughput unit (PTU)/PAYG sizing from declared capacity inputs and pinned local
  pricing/density snapshots.

It does **not** run a model or quality evaluator. Therefore every usage-only
report sets quality to `NOT_MEASURED`. It never claims that lowering reasoning
effort preserves quality. Instead, it recommends a controlled experiment.

## 2. Prerequisites

- Python 3.11 or later.
- A local clone of this repository.
- No Azure account, credential, application programming interface (API) key, browser uploader, or runtime network
  access. Initial dependency installation can require package-index access
  unless dependencies are already cached.

Create an isolated environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install ".[analysis]"
```

The normal, non-editable install exposes one official console command:

```bash
reasoning-payoff --version
```

## 3. Run the committed sample

From the repository root:

```bash
reasoning-payoff analyze examples/five-minute/usage.jsonl \
  --workload examples/five-minute/workload.yaml \
  --out report
```

Open `report/report.html` directly from disk. The command creates exactly:

```text
report/
  report.json
  report.md
  report.html
  policy.json
```

The output directory must be absent or empty. `analyze` refuses to overwrite a
non-empty directory, which protects prior evidence bundles from partial
replacement.

## 4. Initialize a portable local sample

`init` uses packaged resources, not the current working directory:

```bash
reasoning-payoff init --out .reasoning-payoff
reasoning-payoff analyze .reasoning-payoff/usage.jsonl \
  --workload .reasoning-payoff/workload.yaml \
  --out report
```

It writes `usage.jsonl`, `workload.yaml`, `pricing.yaml`,
`ptu-pricing.yaml`, and `density.yaml`. These files are byte-matched to the
committed `examples/five-minute/` fixtures by tests.

## 5. Analyze your own workload

Export only the UsageEnvelope fields in section 6. Do not export prompts,
completions, endpoint URLs, subscription or tenant IDs, credentials, request
IDs, user IDs, email addresses, hostnames, IP addresses, or arbitrary extra
fields.

Place the JSONL, workload YAML, and referenced snapshot YAML files together:

```text
my-analysis/
  usage.jsonl
  workload.yaml
  pricing.yaml
  ptu-pricing.yaml
  density.yaml
```

Then run:

```bash
reasoning-payoff analyze my-analysis/usage.jsonl \
  --workload my-analysis/workload.yaml \
  --out report
```

No adapter is allowed to pass unknown fields into the core contract. Strip or
redact provider-specific fields before writing UsageEnvelope JSONL.

## 6. UsageEnvelope JSONL v1

Each non-empty line is one JSON object. Unknown fields fail closed.

| Field | Type | Meaning |
| --- | --- | --- |
| `timestamp` | RFC 3339 string with timezone | Request observation time |
| `provider` | `"azure-openai"` | Narrow provider label for this release |
| `model` | safe identifier | Model family or deployment-independent model ID |
| `reasoning_effort` | enum | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`; model support varies |
| `input_tokens` | non-negative integer | Total input tokens |
| `cached_input_tokens` | non-negative integer | Cached subset of input tokens |
| `output_tokens` | non-negative integer | Full output count, including the reasoning subset |
| `reasoning_tokens` | non-negative integer | Labeled subset of `output_tokens` |
| `latency_ms` | non-negative number | Consistent client-side latency boundary |
| `status_code` | integer 100-599 | HTTP response status |
| `retry_after_ms` | non-negative number or null | Retry delay metadata when present |

Integrity rules include:

- `cached_input_tokens <= input_tokens`;
- `reasoning_tokens <= output_tokens`;
- numeric strings such as `"100"` are rejected rather than coerced;
- every timestamp must carry a timezone;
- the reader streams and hashes input with file, row, and line-size limits.

The public schema is
[`schemas/usage_envelope.v1.schema.json`](../schemas/usage_envelope.v1.schema.json).

## 7. WorkloadSpec YAML v1

The workload file pins the method, pricing snapshot, quality boundary, and safe
thresholds:

```yaml
schema_version: "1.0.0"
name: my-workload-v1
method:
  id: usage-profile
  version: "1.0.0"
pricing:
  snapshot_id: my-payg-snapshot
  snapshot_file: pricing.yaml
quality:
  status: NOT_MEASURED
thresholds:
  max_429_rate: 0.05
  max_p95_latency_ms: 5000
  min_cached_input_ratio: 0.25
  max_reasoning_output_ratio: 0.50
```

`name` is a report-visible safe slug, not free text or a customer name.
Snapshot paths must be safe relative paths beside the workload file.

### Optional PTU sizing

Add this narrow block only when its forecast inputs and snapshots are valid:

```yaml
ptu_sizing:
  expected_rpm: 120.0
  mean_max_output_tokens: 1024
  target_utilization: 0.70
  pricing_snapshot_id: my-ptu-pricing-snapshot
  pricing_snapshot_file: ptu-pricing.yaml
  density_snapshot_id: my-density-snapshot
  density_snapshot_file: density.yaml
```

The CLI constructs the existing `WorkloadShape` and calls the repository's
existing PTU/PAYG calculator. It does not duplicate the sizing formula. When
the block is absent or the report contains multiple models, PTU sizing is
explicitly `NOT_MODELED`. The calculator applies the pinned minimum,
deployment maximum, and regional scale increment; it rounds required PTUs up
to the next deployable increment before modeling cost and crossover.

The public schema is
[`schemas/workload_spec.v1.schema.json`](../schemas/workload_spec.v1.schema.json).

## 8. Artifact contract

### `report.json`

The machine-readable source of truth. It contains:

- input hashes and row/time-window boundaries, never input paths;
- method, schema, CLI, pricing, density, and claim-registry versions/hashes;
- aggregate and model/effort-cell measurements (one cell is one model-and-effort setting);
- measured, modeled, and not-measured boundaries;
- conclusions with evidence, assumptions, confidence, source row ranges,
  selectors, and provenance;
- a copy of the review-only policy candidates.

### `report.md`

A deterministic review summary suitable for a pull request or issue. It keeps
the quality boundary and snapshot hashes visible.

### `report.html`

A self-contained, script-free local report. It has inline CSS only and escapes
all interpolated values with quote escaping. It makes no network requests.

### `policy.json`

Review-only operating candidates. Every candidate has conclusion references,
evidence, assumptions, confidence, source rows, a selector, and immutable
method/input/snapshot/claim hashes. `auto_apply` is always false.

## 9. Interpret the report

Use the boundary before the number:

- `MEASURED`: calculated directly from accepted UsageEnvelope rows.
- `MODELED`: calculated from measured rows plus a pinned local snapshot and
  explicit assumptions.
- `NOT_MEASURED`: no evidence was supplied for that dimension.
- `NOT_MODELED`: required modeling inputs or applicability conditions were not
  satisfied.

Important interpretation rules:

1. Reasoning tokens are a labeled subset of output tokens. The PAYG calculator
   bills `output_tokens` once; it does not add `reasoning_tokens` again.
2. A high reasoning/output ratio identifies an experiment candidate, not a
   quality-safe reduction.
3. Cache behavior is model- and API-version-specific. Core fields retain only
   stable accounting values; current provider semantics stay in snapshots and
   citations.
4. A PTU result is a planning model. PTU quota is a policy limit and does not guarantee deployable
   capacity.
5. An HTTP rate-limit status 429 rate is measured response behavior, not proof of one root cause.

## 10. Privacy and failure behavior

The core and report contract have no fields for prompt or completion content,
endpoint, hostname, subscription/tenant ID, API key, user/request ID, email, or
IP address, or authorization token. Unknown fields fail closed.

Errors report a line number and field location only. They do not echo the raw
JSONL line, input path, workload free text, or rejected value.

Artifacts are staged before publication. `report` uses a failure-recoverable
directory replacement: it restores the previous complete bundle after a
reported swap failure, and the next invocation recovers an abandoned backup or
staging directory after process interruption. Portable filesystems can still
observe a brief rename gap, so this is not an atomic visibility guarantee:

```bash
reasoning-payoff report report/report.json
reasoning-payoff report report/report.json --out rerendered-report
```

The CLI keeps a sibling `.report.reasoning-payoff.lock` ownership file. It
refuses to replace a four-file lookalike directory without that durable marker,
so unrelated files named `report.json`, `report.md`, `report.html`, and
`policy.json` are not treated as an owned bundle.

With pinned `report.json`, both commands render byte-stable artifacts.

## 11. Troubleshooting and exit codes

| Exit | Meaning | Action |
| ---: | --- | --- |
| `0` | Success | Open `report.html` or inspect `report.json` |
| `2` | CLI usage error | Run `reasoning-payoff --help` |
| `3` | Input/schema/snapshot error | Fix the reported line and field |
| `4` | Privacy rejection | Remove or redact prohibited metadata |
| `5` | Report/output conflict | Use an empty directory or a complete generated bundle |
| `6` | Local I/O failure | Check permissions and available disk space |

Common checks:

```bash
python scripts/check_claim_integrity.py check
python -m pytest -q batch-runner/tests/test_offline_cli.py
```

## 12. Verify the five-minute target

Measure a normal installed run:

```bash
/usr/bin/time -p reasoning-payoff analyze \
  examples/five-minute/usage.jsonl \
  --workload examples/five-minute/workload.yaml \
  --out report
```

Confirm all four files exist and rerender deterministically:

```bash
shasum -a 256 report/report.json report/report.md \
  report/report.html report/policy.json
reasoning-payoff report report/report.json
shasum -a 256 report/report.json report/report.md \
  report/report.html report/policy.json
```

The two hash sets must match. The acceptance ceiling (highest allowed runtime) is five minutes after a
fresh clone; the bundled sample target is under 60 seconds.

On 2026-08-20, an isolated normal `pip install .` smoke run measured the
installed sample `analyze` command at **0.12 seconds**. That runtime excludes
environment creation and dependency installation; it is a validation data
point, not a universal performance guarantee.

## 13. First-party Microsoft references

These links explain provider behavior; they are not network dependencies of the
CLI or generated report.

1. [Azure OpenAI reasoning models](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/reasoning):
   reasoning tokens appear under
   `completion_tokens_details.reasoning_tokens` for Chat Completions or
   `output_tokens_details.reasoning_tokens` for Responses, are billed as output,
   and supported effort values vary by model.
2. [Azure OpenAI prompt caching](https://learn.microsoft.com/en-us/azure/ai-services/openai/how-to/prompt-caching):
   cached token accounting and newer cache-write, key, and breakpoint semantics
   vary by model family and deployment type.
3. [Provisioned throughput](https://learn.microsoft.com/en-us/azure/ai-services/openai/concepts/provisioned-throughput):
   provisioned throughput is a deployment type with dedicated fixed capacity
   billed per PTU-hour; sizing uses request profile, output weighting, cache
   rate, and expected request rate.
4. [Azure OpenAI quotas and limits](https://learn.microsoft.com/en-us/azure/foundry/openai/quotas-limits):
   quota is a limit, not a guarantee that deployment capacity is available.

References were reviewed on 2026-08-20. Recheck model-specific behavior before
using a report as a production change approval.
