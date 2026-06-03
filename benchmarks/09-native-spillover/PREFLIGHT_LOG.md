# benchmarks/09-native-spillover/PREFLIGHT_LOG.md

> Task 021 v2.1 — Stage 0 pre-flight log. **Append-only.** Each Stage 0
> run appends one timestamped section. No mutation of Azure resources is
> performed by this log or the script that writes it. No endpoint
> hostnames, tenant/subscription IDs, resource group names, resource IDs,
> auth tokens, bearer tokens, API keys, raw `az` CLI JSON, or
> environment-variable values are recorded — only derived booleans, SKU
> aliases, and header-name presence.

> Sources (last accessed 2026-06-02):
> - Azure spillover doc — <https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/spillover-traffic-management>
> - Azure Responses API doc — <https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses>

## Stage 0 run — 2026-06-02T07:47:02Z

- git_commit: `a3764d95f9db`
- preflight_hard_ceiling_usd: `$0.10`

### Stage 0a — read-only az CLI verification

- verdict: **CONFIG-MISSING**
- sku_alias: `UNKNOWN`
- spillover_deployment_name_present: `None`
- mode_a_property_configured: `False`
- notes: missing env vars (names only): AZURE_OPENAI_ACCOUNT_NAME,AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED,AZURE_OPENAI_RESOURCE_GROUP; cannot run read-only az cognitiveservices account deployment show; Modes A/B/C unverifiable from this environment.

### Stage 0b — capped same-API Responses/Foundry v1 preflight

- verdict: **SAME-API-FAIL**
- attempted: `False`
- dry_run_cost_estimate_usd: `$0.0000` (ceiling `$0.10`)
- observed_relevant_header_names: `<none>`
- x-ms-spillover-from-deployment: absent (EXPECTED on a non-spillover preflight; NOT a HEADERS-UNSUPPORTED finding)
- failure_reason: required env vars absent (names only): AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED,AZURE_OPENAI_FOUNDRY_ENDPOINT; no network call attempted; no identifier values leaked.

### Stage 0c — branching verdict

- next_action: **PRODUCE_FEASIBILITY_FINDING**
- feasibility_finding_kind: `CONFIG-MISSING`

## Stage 0 run — 2026-06-02T07:56:45Z

- git_commit: `a3764d95f9db`
- preflight_hard_ceiling_usd: `$0.10`

### Stage 0a — read-only az CLI verification

- verdict: **INFEASIBLE-AS-SPEC'D**
- sku_alias: `PAYG/GlobalStandard`
- spillover_deployment_name_present: `False`
- mode_a_property_configured: `False`
- notes: primary SKU alias=PAYG/GlobalStandard is not PTU and spilloverDeploymentName not configured; per the spillover doc (accessed 2026-06-02) native spillover requires PTU primary → standard target. No owner OPTIN to provision a PTU primary. (account/RG resolved via read-only az discovery; values redacted)

### Stage 0b — capped same-API Responses/Foundry v1 preflight

- verdict: **SAME-API-OK**
- attempted: `True`
- dry_run_cost_estimate_usd: `$0.0002` (ceiling `$0.10`)
- observed_relevant_header_names: `<none>`
- x-ms-spillover-from-deployment: absent (EXPECTED on a non-spillover preflight; NOT a HEADERS-UNSUPPORTED finding)
- failure_reason: <none>

### Stage 0c — branching verdict

- next_action: **PRODUCE_FEASIBILITY_FINDING**
- feasibility_finding_kind: `INFEASIBLE-AS-SPEC'D`

## Stage 0 run — 2026-06-02T07:57:14Z

- git_commit: `a3764d95f9db`
- preflight_hard_ceiling_usd: `$0.10`

### Stage 0a — read-only az CLI verification

- verdict: **INFEASIBLE-AS-SPEC'D**
- sku_alias: `PAYG/GlobalStandard`
- spillover_deployment_name_present: `False`
- mode_a_property_configured: `False`
- notes: primary SKU alias=PAYG/GlobalStandard is not PTU and spilloverDeploymentName not configured; per the spillover doc (accessed 2026-06-02) native spillover requires PTU primary → standard target. No owner OPTIN to provision a PTU primary. (account/RG resolved via read-only az discovery; values redacted)

### Stage 0b — capped same-API Responses/Foundry v1 preflight

- verdict: **SAME-API-OK**
- attempted: `True`
- dry_run_cost_estimate_usd: `$0.0002` (ceiling `$0.10`)
- observed_relevant_header_names: `<none>`
- x-ms-spillover-from-deployment: absent (EXPECTED on a non-spillover preflight; NOT a HEADERS-UNSUPPORTED finding)
- failure_reason: <none>

### Stage 0c — branching verdict

- next_action: **PRODUCE_FEASIBILITY_FINDING**
- feasibility_finding_kind: `INFEASIBLE-AS-SPEC'D`
