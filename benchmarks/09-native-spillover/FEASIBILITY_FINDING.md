# Task 021 v2.1 — Feasibility Finding (INFEASIBLE-AS-SPEC'D)

**Timestamp (UTC):** 2026-06-02T07:57:14Z
**Git commit:** `a3764d95f9db`
**Outcome class:** INFEASIBLE-AS-SPEC'D
**Task 021 status:** closed at Stage 0 (feasibility-closed DoD per spec §"Definition of Done" option A).

## Stage 0a (read-only)

- Verdict: **INFEASIBLE-AS-SPEC'D**
- SKU alias: `PAYG/GlobalStandard`
- `spilloverDeploymentName` present: `False`
- Notes: primary SKU alias=PAYG/GlobalStandard is not PTU and spilloverDeploymentName is not configured; per the spillover doc (accessed 2026-06-02) native spillover requires PTU primary → standard target. No owner OPTIN was granted to provision a PTU primary. (account/RG resolved via read-only az discovery; values redacted)

## Stage 0b (capped same-API preflight)

- Verdict: **SAME-API-OK**
- Network call attempted: `True`
- Failure reason: <none>

## Configuration modes (per spillover doc)

Per the Azure spillover doc (accessed 2026-06-02), native spillover
fires when one of:

- **Mode A** — the target deployment has `spilloverDeploymentName` set
  on the deployment resource (deployment-level default), OR
- **Mode B** — the request explicitly sets the
  `x-ms-spillover-deployment` header to a valid sibling deployment
  alias, OR
- **Mode C** — owner-approved mutation provisioning Mode A for the
  experiment.

Status against the current deployment, derived from Stage 0a evidence
above and Stage 0b scope:

- **Mode A** — observed **absent**: `spilloverDeploymentName` is not set on the inspected deployment resource, and the primary SKU alias (`PAYG/GlobalStandard`) is not a PTU primary as required by the spillover doc.
- **Mode B** — **not exercised / not proven** under this feasibility gate: Stage 0b issues exactly one ordinary non-spillover Responses API call and MUST NOT set `x-ms-spillover-deployment`; Stage 1 spillover-fire proof smoke and the full head-to-head comparison were not executed. Absence here is a scope statement, not a claim that Mode B is impossible.
- **Mode C** — **not granted**: no owner opt-in has been granted to provision a PTU primary or to set `spilloverDeploymentName`; no Azure resources were mutated by this preflight.

## Sources (last accessed 2026-06-02)

- Azure spillover doc — <https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/spillover-traffic-management>
- Azure Responses API doc — <https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses> (cited only for API path/context, not for native-spillover semantics)

## Closure

Task 021 v2.1 closes at this finding. Task 022 may cite this file
instead of a head-to-head comparison.
