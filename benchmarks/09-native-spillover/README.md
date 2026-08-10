# Benchmark 09 — Native server-side spillover vs custom-router (Task 021 v2.1)

> **Status (this commit): feasibility-gated Stage 0 only.**
> Per the Task 021 v2.1 spec, Stage 1 spillover-fire proof smoke and
> the full head-to-head comparison are BLOCKED until Stage 0 emits the
> appropriate verdict in `PREFLIGHT_LOG.md`. This commit lands ONLY:
>
> 1. `scripts/preflight_native_spillover.py` (Stage 0a read-only az
>    CLI verification + Stage 0b capped same-API Responses/Foundry v1
>    preflight, ≤ `preflight_hard_ceiling_usd: 0.10`).
> 2. `tests/test_preflight_native_spillover.py` (focused unit tests
>    covering mutation refusal, anonymization, cost-ceiling
>    enforcement, header-observation policy, Stage 0c branching).
> 3. `PREFLIGHT_LOG.md` (this directory) recording one Stage 0a/0b
>    run with redacted verdicts.
> 4. `FEASIBILITY_FINDING.md` (this directory) when Stage 0c branches
>    to `CONFIG-MISSING` or `INFEASIBLE-AS-SPEC'D`.
> 5. `runs/.gitkeep` and `results/native-spillover-comparison/.gitkeep`
>    placeholders for any later approved smoke/full run.
>
> **No Azure resources were mutated. No Stage 1 smoke was run. No full
> comparison was run. No `spilloverDeploymentName` was set. No PTU
> deployments were created.**

## Sources (last accessed 2026-06-02)

- Azure spillover doc — <https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/spillover-traffic-management> — sole source for: `spilloverDeploymentName` deployment property, `x-ms-spillover-deployment` per-request header, observability headers (`x-ms-spillover-from-deployment`, `x-ms-deployment-name`, `x-ms-spillover-error`), the non-200 trigger behavior, and the PTU primary → standard target topology.
- Azure Responses API doc — <https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses> — cited only for API path / request-response context (NOT for any native-spillover semantics).

## What this benchmark will (eventually) measure

If and only if Stage 0 passes (`READY_FOR_SMOKE_PROOF` + `SAME-API-OK`)
**and** Stage 1 spillover-fire proof smoke verdict is `SPILLOVER_PROVEN`,
this benchmark will run the same workload as Task 013 under native
server-side spillover and produce a head-to-head:

- Cache hit ratio under native spillover vs Task 013's Python reactive baseline.
- p50 / p95 TTFT.
- Per-deployment traffic share (native: derived from `x-ms-deployment-name`; Task 013: from `endpoint_hit`).
- Operational complexity assessment (LOC, configurability, observability).

The pinned Task 013 baseline that this comparison would consume is:

- JSONL: `benchmarks/05-dual-spillover/runs/20260528T135034Z_exp005_dual_spillover_reactive_reactive.jsonl`
- Summary: `benchmarks/05-dual-spillover/runs/20260528T135034Z_exp005_dual_spillover_reactive_reactive.jsonl.summary.json`

### Verbatim baseline caveat (required by spec)

The Task 013 reactive baseline summary records:

- `primary_real_429_count = 0`
- `spillover_real_429_count = 0`
- `spillover_request_fraction ≈ 0.988764`

Almost all traffic landed on the spillover deployment via the Python
router's timeout/health/proactive-leaning heuristics, not via real
429s. Therefore the eventual head-to-head is explicitly:

> **Azure native non-200-triggered server-side spillover** vs
> **Task 013 Python timeout/health-driven reactive router**, under a
> workload where the primary historically returned zero real 429s.

Any future `analysis.md` in this directory MUST state this verbatim.

## Stage 0 — feasibility gate (this commit)

### Stage 0a — Read-only Azure CLI verification

`scripts/preflight_native_spillover.py::run_stage_0a` reads three env
vars (`AZURE_OPENAI_RESOURCE_GROUP`, `AZURE_OPENAI_ACCOUNT_NAME`,
`AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED`) and, if all are present,
invokes:

```
az cognitiveservices account deployment show \
    --resource-group <redacted> --name <redacted> \
    --deployment-name <redacted> -o json --only-show-errors
```

Mutating verbs (`create`, `update`, `set`, `delete`, `add`, `remove`,
`replace`, `patch`) in any az argv are unconditionally refused by
`run_az_readonly()`. Only two derived facts are extracted from the
parsed JSON before it is dropped on the floor:

- a coarse SKU alias (`PAYG/GlobalStandard`, `PAYG/Standard`,
  `PTU/ProvisionedManaged`, `PTU/GlobalProvisionedManaged`, `OTHER`,
  `UNKNOWN`), and
- a boolean for the presence of `spilloverDeploymentName` on the
  deployment resource.

Verdicts: `READY_FOR_SMOKE_PROOF` / `CONFIG-MISSING` / `INFEASIBLE-AS-SPEC'D`.

### Stage 0b — Capped same-API Responses/Foundry v1 preflight

`scripts/preflight_native_spillover.py::run_stage_0b` issues exactly
one small, ordinary, **non-spillover** Responses API call against the
primary deployment via the same code path the eventual smoke would
use. The request MUST NOT set `x-ms-spillover-deployment`. A pessimistic
dry-run cost estimate is compared against `preflight_hard_ceiling_usd:
0.10`; if it exceeds the ceiling, the call is aborted before any
network I/O.

**Header observation policy (re-audit fix, verbatim from spec §Stage 0b).** The spillover doc states `x-ms-spillover-from-deployment` is populated only on responses to requests that *actually spilled over*. A non-spillover preflight typically will NOT carry it. **Absence of `x-ms-spillover-from-deployment` on this preflight is expected and MUST NOT be recorded as `HEADERS-UNSUPPORTED`.** The script records header-name presence as observational data only.

Verdicts: `SAME-API-OK` / `SAME-API-FAIL`.

### Stage 0c — Branching

| Stage 0a | Stage 0b | Next action |
| --- | --- | --- |
| `READY_FOR_SMOKE_PROOF` | `SAME-API-OK` | Proceed to Stage 1 (out of scope for this commit) |
| `CONFIG-MISSING` | `SAME-API-OK` | Produce `FEASIBILITY_FINDING.md` (this commit, if reached) |
| `CONFIG-MISSING` | `SAME-API-FAIL` | Produce `FEASIBILITY_FINDING.md`; record same-API issue for later rerun |
| `INFEASIBLE-AS-SPEC'D` | any | Produce `FEASIBILITY_FINDING.md` |
| `READY_FOR_SMOKE_PROOF` | `SAME-API-FAIL` | Fix and rerun Stage 0b (no finding produced from same-API failure alone) |

## Anonymization invariant

Committed artifacts in this directory MUST NOT contain endpoint
hostnames, tenant/subscription IDs, resource group names, resource
IDs, principal IDs, authorization headers, bearer tokens, API keys,
raw `az` CLI JSON, or any environment-variable values. Only env var
**names**, derived booleans, SKU aliases, and header-name presence
are emitted. `scripts/preflight_native_spillover.py::assert_no_secrets`
runs over the full PREFLIGHT_LOG.md / FEASIBILITY_FINDING.md content
before each write; a regex match aborts the write cleanly with a
non-zero exit.

## Running the preflight locally

```
python3 -m scripts.preflight_native_spillover --dry-run
# or, for a real Stage 0b call (≤ $0.10 cap enforced):
python3 -m scripts.preflight_native_spillover
```

The Stage 0a step is read-only and the Stage 0b dry-run skips the
network call entirely; both modes append a section to
`PREFLIGHT_LOG.md` and, if Stage 0c branches accordingly, write
`FEASIBILITY_FINDING.md`.

## Testing

```
python3 -m pytest tests/test_preflight_native_spillover.py -v
```

Covers: mutation refusal, anonymization regex coverage, env-missing
SAME-API-FAIL without value leaks, dry-run cost ceiling enforcement,
header-absence-is-expected, Stage 0c branching matrix, append-only
log writer, anonymization-violation refusal.

## Out of scope for this commit

- `scripts/measure_native_spillover.py` (deferred — will land only after Stage 1 == `SPILLOVER_PROVEN`).
- `experiments/exp008_native_spillover.yaml` (same deferral).
- `analysis.md` (same deferral).
- Workload corpus + prompt copies (deferred until Stage 1 unblocks).
- Any modification to `scripts/measure_dual_spillover.py`.
- Any `prompt_cache_key` / `max_output_tokens` / `retry-after-ms` work (Tasks 018 / 019 / 020).
