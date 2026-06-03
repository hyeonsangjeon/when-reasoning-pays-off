# Pre-flight Verification Log — Benchmark 05 Dual-Endpoint Spillover

This log records the owner-performed manual pre-flight for the two Azure
deployments Phase 2 measurement depends on, plus the implementer-run
reachability check performed at the start of every live run.

The log lives in the working repo so a reviewer can confirm — before any
live spend — that the deployments exist, are scoped to the right
parameters, and are reachable under the runner's Entra ID auth path. It
is updated by the runner on every live invocation; the most recent
record **that is not explicitly labelled `aborted-dirty-attempt /
invalid-for-measurement`** is the source of truth. Rows so labelled were
appended by runner attempts that did not produce measurement-valid data
(e.g., aborted before the workload loop started) and must not be used
as a reachability witness for any analysis or downstream artifact.

## Owner pre-flight (manual, completed 2026-05-21)

| Item                            | Value                                                     |
|---------------------------------|-----------------------------------------------------------|
| Foundry resource                | `<resource>`                                               |
| Foundry project                 | `<project>`                                        |
| Endpoint base                   | `https://<resource>.services.ai.azure.com/api/projects/<project>` |
| Auth mode                       | Entra ID (`DefaultAzureCredential`); no API keys          |
| Primary deployment name         | `ptu-deploy-throttled`                                       |
| Primary deployment created      | 2026-05-21                                                |
| Primary deployment TPM / RPM    | 60,000 TPM / 600 RPM                                      |
| Primary capacity tier           | GlobalStandard PAYG                                       |
| Spillover deployment name       | `gpt-5.2`                                                 |
| Spillover deployment TPM / RPM  | 500,000 TPM / 5,000 RPM (unchanged from benchmark 01)     |
| Spillover capacity tier         | GlobalStandard PAYG                                       |
| Benchmark 01 impact             | None (spillover deployment unchanged from benchmark 01 use) |

The TPM values are workload-shaping parameters chosen so the 60K-TPM
primary throttles under the 22-minute sustained-2-TPS / 30K-token
workload while the 500K-TPM spillover does not. They are deployment
identifiers and capacity-tier numbers, not customer-attributed
references. The deployment names are mechanism / deployment-side
identifiers only.

## Implementer reachability check (runtime — updated on every live run)

The runner (`scripts/measure_dual_spillover.py`) executes a one-request
`responses.create()` per deployment before any policy iteration begins.
On failure the run aborts with exit code 2 and emits the hint to verify
this checklist. On success, the runner atomically appends a new
timestamped row to this file (read existing content, compose new
section in memory, write to a sibling `*.tmp.<pid>` file, then
`os.replace()` into place) using only env-var names from the
experiment YAML and the boolean reachability + `output_tokens` count
returned by each preflight call. No secrets, no endpoint URLs, and no
resolved deployment names are written by the runner.

### Most recent run (stub — replaced by appended rows after first live run)

| Field                            | Value                                                    |
|----------------------------------|----------------------------------------------------------|
| `run_timestamp_utc`              | _(filled by runner on first live run)_                   |
| `git_commit`                     | _(filled by runner on first live run)_                   |
| `experiment_id`                  | _(filled by runner on first live run)_                   |
| `primary_reachable`              | _(filled by runner on first live run)_                   |
| `primary_output_tokens`          | _(filled by runner on first live run)_                   |
| `spillover_reachable`            | _(filled by runner on first live run)_                   |
| `spillover_output_tokens`        | _(filled by runner on first live run)_                   |
| `corpus_sha256_match_phase1`     | _(filled by runner on first live run)_                   |
| `user_prompts_sha256_match_phase1` | _(filled by runner on first live run)_                  |

### Reachability check contract (verbatim)

```python
async def preflight_reachability(primary_deployment: str, spillover_deployment: str) -> None:
    """One short request per deployment. Aborts the entire run if either fails."""
    for deployment in (primary_deployment, spillover_deployment):
        resp = await client.responses.create(
            model=deployment,
            input="ping",
            # Foundry v1 Responses API rejects max_output_tokens < 16
            # (HTTP 400 integer_below_min_value); 16 is the smallest
            # legal value and keeps the preflight cost negligible.
            max_output_tokens=16,
            # gpt-5.2-2025-12-11 rejects effort="minimal" at dispatch
            # (HTTP 400 unsupported_value; supported: none / low /
            # medium / high / xhigh). "low" is the smallest non-zero
            # reasoning effort the production model accepts and matches
            # the effort used by the workload loop.
            reasoning={"effort": "low"},
        )
        assert resp.usage.output_tokens > 0, f"unreachable: {deployment}"
```

The check is mandatory on smoke and full runs. It is skipped only on
`--dry-run` (which makes zero outbound HTTPS calls by construction).

## Anonymization invariant

No customer name, product name, app name, or organization-identifying
reference appears in this benchmark's artifacts (this README, the
corpus, the user prompts, the YAMLs, the runner code, the charts, the
summary JSON, or this log). Specific numbers — 60K TPM, 30K-token
prompt, 2 TPS sustain — appear here only as workload-shaping parameters
chosen to expose the mechanism. The deployment names (`ptu-deploy-throttled`,
`gpt-5.2`) are mechanism / deployment-side identifiers only and contain
no customer or organization attribution.

### Run 2026-05-28T01:55:14Z — `exp005_dual_spillover_reactive` — **aborted-dirty-attempt / invalid-for-measurement**

This row was appended by a runner attempt that aborted before the Phase
2 workload loop produced measurement-valid data. The runner's working
tree was dirty relative to `git_commit` recorded below (uncommitted
hotfix edits to `scripts/measure_dual_spillover.py` and the exp005
YAMLs were present at invocation time), so the recorded commit hash
does not faithfully describe the code path that issued the preflight
calls. **Do not treat this row as a reachability witness** for any
downstream analysis or artifact. It is retained only to preserve the
append-only audit trail.

| Field                            | Value                                                    |
|----------------------------------|----------------------------------------------------------|
| `run_timestamp_utc`              | `2026-05-28T01:55:14Z`                                        |
| `git_commit`                     | `d60dbe5de03a5865e62c97ba1b36dfa834a1bb86` _(working tree dirty — does not describe the code path actually executed)_ |
| `experiment_id`                  | `exp005_dual_spillover_reactive`                                        |
| `primary_deployment_env`         | `${AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED}`                               |
| `primary_reachable`              | `True`        |
| `primary_output_tokens`          | `5`    |
| `spillover_deployment_env`       | `${AZURE_OPENAI_DEPLOYMENT_GPT_5_2}`                             |
| `spillover_reachable`            | `True`      |
| `spillover_output_tokens`        | `5`  |
| `status`                         | `aborted-dirty-attempt / invalid-for-measurement`        |

### Run 2026-05-28T01:57:14Z — `exp005_dual_spillover_reactive` — **aborted-dirty-attempt / invalid-for-measurement**

Same status as the previous row: this row was appended by a runner
attempt that aborted before the Phase 2 workload loop produced
measurement-valid data, while the working tree was dirty relative to
the recorded `git_commit`. **Do not treat this row as a reachability
witness** for any downstream analysis or artifact. Retained only for
the append-only audit trail.

| Field                            | Value                                                    |
|----------------------------------|----------------------------------------------------------|
| `run_timestamp_utc`              | `2026-05-28T01:57:14Z`                                        |
| `git_commit`                     | `d60dbe5de03a5865e62c97ba1b36dfa834a1bb86` _(working tree dirty — does not describe the code path actually executed)_ |
| `experiment_id`                  | `exp005_dual_spillover_reactive`                                        |
| `primary_deployment_env`         | `${AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED}`                               |
| `primary_reachable`              | `True`        |
| `primary_output_tokens`          | `5`    |
| `spillover_deployment_env`       | `${AZURE_OPENAI_DEPLOYMENT_GPT_5_2}`                             |
| `spillover_reachable`            | `True`      |
| `spillover_output_tokens`        | `5`  |
| `status`                         | `aborted-dirty-attempt / invalid-for-measurement`        |

### Run 2026-05-28T13:50:44Z — `exp005_dual_spillover_reactive`

| Field                            | Value                                                    |
|----------------------------------|----------------------------------------------------------|
| `run_timestamp_utc`              | `2026-05-28T13:50:44Z`                                        |
| `git_commit`                     | `9a266efec53ca9e1e86c8f9a1b45808808d656d8`                                           |
| `experiment_id`                  | `exp005_dual_spillover_reactive`                                        |
| `primary_deployment_env`         | `${AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED}`                               |
| `primary_reachable`              | `True`        |
| `primary_output_tokens`          | `5`    |
| `spillover_deployment_env`       | `${AZURE_OPENAI_DEPLOYMENT_GPT_5_2}`                             |
| `spillover_reachable`            | `True`      |
| `spillover_output_tokens`        | `5`  |

### Run 2026-05-28T18:33:18Z — `exp005_dual_spillover_proactive`

| Field                            | Value                                                    |
|----------------------------------|----------------------------------------------------------|
| `run_timestamp_utc`              | `2026-05-28T18:33:18Z`                                        |
| `git_commit`                     | `9a266efec53ca9e1e86c8f9a1b45808808d656d8`                                           |
| `experiment_id`                  | `exp005_dual_spillover_proactive`                                        |
| `primary_deployment_env`         | `${AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED}`                               |
| `primary_reachable`              | `True`        |
| `primary_output_tokens`          | `5`    |
| `spillover_deployment_env`       | `${AZURE_OPENAI_DEPLOYMENT_GPT_5_2}`                             |
| `spillover_reachable`            | `True`      |
| `spillover_output_tokens`        | `5`  |
