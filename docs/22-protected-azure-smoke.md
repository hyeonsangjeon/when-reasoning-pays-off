# Protected Azure live-provider smoke

This health check is intentionally separate from public PR CI and the nightly
offline campaign. It issues one fixed, non-sensitive Azure OpenAI Responses
request from an approved Azure-hosted self-hosted runner. It never runs on
`pull_request`, a fork, or an ordinary CI runner.

## Contract and threat model

The workflow accepts only `schedule` and `workflow_dispatch`, requires the
repository's `main` ref and exact checkout commit, waits for the
`protected-azure-smoke` GitHub environment, and uses these runner labels:
`self-hosted`, `linux`, `x64`, `azure`, `managed-identity`, and
`protected-smoke`. Repository permissions are `contents: read`; no repository
write or GitHub OIDC token permission is granted.

The entrypoint distrusts workflow input. Before endpoint or identity work it
checks GitHub-owned event/repository/ref/workflow/runner markers, verifies
`HEAD == GITHUB_SHA`, requires the approved endpoint variable name, rejects API
keys and service-principal secret/certificate inputs, validates the Azure
endpoint shape, and confines output to
`RUNNER_TEMP/protected-azure-smoke` outside the checkout. Existing output,
symlinks, wrong paths, wrong events, wrong repositories, and wrong workflow
identity fail closed.

The model call uses `DefaultAzureCredential` configured so its realized chain
contains only `ManagedIdentityCredential`, then uses
`get_bearer_token_provider` for `https://ai.azure.com/.default`. A token probe
must succeed before the request. The ordinary `reasoning-payoff sample run`
Azure provider retains its unconditional generic-CI refusal; only this
entrypoint removes `CI` from the provider's private environment after all
protected checks and the identity proof succeed.

The fixed request is `Reply with exactly OK.` with one sample, one repeat,
concurrency one, `capture_io: false`, `store=false`, `max_retries=0`, a
30-second timeout, 32 output tokens, a fresh canonical pricing snapshot, both
cost confirmations, and a `$0.001` conservative estimator ceiling. Raw
immutable run files live only in protected temporary storage and are deleted
before publication.

## One-time operator setup

1. Create the GitHub environment named `protected-azure-smoke`. Require
   reviewers, prevent self-review where available, and restrict deployment
   branches to `main`.
2. Register an ephemeral or otherwise tightly administered self-hosted runner
   on an Azure compute resource. Apply all six labels listed above. Keep the
   runner out of public repository groups unless the group policy limits it to
   this repository.
3. Attach a managed identity and grant only the inference permission required
   by the selected Azure AI resource. `Cognitive Services OpenAI User` at the
   narrow resource scope is the expected role; do not grant subscription-wide
   Contributor or Owner.
4. Add environment variables
   `PROTECTED_AZURE_SMOKE_ENVIRONMENT=protected-azure-smoke` and
   `PROTECTED_AZURE_SMOKE_RUNNER_CLASS=azure-managed-identity`.
5. Add environment-scoped values named
   `AZURE_OPENAI_FOUNDRY_ENDPOINT`,
   `PROTECTED_AZURE_SMOKE_DEPLOYMENT`, and
   `AZURE_MANAGED_IDENTITY_CLIENT_ID`. The workflow maps the last value to
   `AZURE_CLIENT_ID`. Use placeholders during review; never commit real
   endpoint, deployment, client, tenant, subscription, or resource identifiers.
6. Approve the first manual run only after reviewing the workflow commit on
   `main`. Do not provision or mutate Azure resources from this repository
   workflow.

GitHub OIDC is not used by this workflow. If a separate control plane later
uses OIDC only to start or stop the Azure runner, that trust must remain
separate: the model request must still authenticate with the runner's managed
identity.

## Sanitized health artifact

Only `health.json`, validated against
`schemas/protected_azure_smoke_health.v1.schema.json`, is uploaded for three
days. It contains:

- status and UTC observation time;
- commit, package, dependency-set, runtime, and architecture-safe fingerprints;
- declared model family/version, deployment type, and region class;
- pricing snapshot ID and SHA-256;
- latency and aggregate token counts;
- the fixed store/retry/capture/timeout/output/cost settings; and
- one typed failure class: protected context, managed identity, pricing,
  cost guard, authentication, quota, deployment, API compatibility, timeout,
  provider, response, runtime dependency, or internal.

The schema and a second privacy scanner reject prompt/response content,
endpoint hostnames, deployment aliases, request/trace IDs, access tokens,
subscription/tenant/resource IDs, usernames, absolute paths, environment
contents, URL values, bearer/API-key/secret shapes, and unexpected fields. Logs
print only the overall status or a Python exception class, never exception
details.

## Public CI fake

`python -m scripts.run_protected_azure_smoke --offline-fake --output DIR`
runs the same ledger, pricing, cost, runner, and Azure-provider request assembly
with an in-process fake client. It asserts exactly one client call and the exact
timeout, retry, store, prompt, and output-token contract. PR CI runs it inside a
network namespace with provider credentials blank. Fake mode does not call the
live context validator or issue a managed-identity token, while live mode has no
flag or environment shortcut around those checks. To keep this integrity test
hermetic after the snapshot ages, fake mode runs the live-freshness verifier at
the immutable snapshot's own access date; only protected live mode evaluates
freshness against the actual date.

## Official references

- `OFFICIAL_SPEC` (Tier 1): Microsoft, *Authenticate to Azure OpenAI with
  Microsoft Entra ID*, managed identities and Azure RBAC,
  <https://learn.microsoft.com/azure/ai-foundry/openai/how-to/managed-identity>,
  accessed 2026-08-30.
- `OFFICIAL_SPEC` (Tier 1): Microsoft, *Azure Identity client library for
  Python*, `DefaultAzureCredential`,
  <https://learn.microsoft.com/python/api/overview/azure/identity-readme>,
  accessed 2026-08-30.
- `OFFICIAL_SPEC` (Tier 1): Microsoft, *Azure OpenAI Responses API*,
  stateless `store=false` requests,
  <https://learn.microsoft.com/azure/ai-foundry/openai/how-to/responses>,
  accessed 2026-08-30.
- GitHub, *Deployments and environments*, protection rules and environment
  secrets,
  <https://docs.github.com/actions/reference/workflows-and-actions/deployments-and-environments>,
  accessed 2026-08-30.
