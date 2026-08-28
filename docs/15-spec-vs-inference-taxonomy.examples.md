# Spec vs inference — worked examples (appendix)

**Task 029 appendix.** Companion to `docs/15-spec-vs-inference-taxonomy.md`.
Each row records a concrete claim made elsewhere in this repo (or by
the Guide) and how it is categorized under the Tier 1 / Tier 2
taxonomy.

This file is a **living index**. New rows may be appended by future
tasks in separate PRs; no row is silently retired. Promotions
(Tier 2 → Tier 1) are recorded with a CHANGELOG entry per §7 of the
main doc.

All identifiers are synthetic. No customer name and no production
deployment identifier appears below.

---

## Index

| # | Claim | Tier | Justification |
|---|-------|------|---------------|
| 1 | `retry-after-ms` is the next acceptable request time | OFFICIAL_SPEC | Cited verbatim from Microsoft Learn — provisioned-throughput concepts (accessed 2026-05-28); URL: https://learn.microsoft.com/azure/ai-services/openai/concepts/provisioned-throughput |
| 2 | 30-second max-wait ceiling in the admission controller is a reasonable cap | OPERATIONAL_INFERENCE | No Learn URL; the 30 s figure is the conservative ceiling chosen in `benchmarks/03-admission-controller/analysis.md §4` based on the empirical `retry-after-ms` p99 distribution from Task 020 |
| 3 | Input tokens per minute (TPM) per provisioned throughput unit (PTU) for `gpt-5.2` = 3,400 (synthetic) | OFFICIAL_SPEC | Cited from Microsoft Learn — provisioned-throughput-onboarding table (accessed 2026-05-28); URL: https://learn.microsoft.com/azure/ai-services/openai/how-to/provisioned-throughput-onboarding |
| 4 | Output:input token ratio for `gpt-5.2` = 8:1 | OPERATIONAL_INFERENCE | Working assumption mirrored from Learn's `gpt-5` ratio; Learn does not list a per-version table for `5.2`, so the ratio is inferred. Rationale in `pricing/ptu-density-2026-05.yaml` header |
| 5 | PTU consumes `(prompt - cached) + max_tokens` at admission | OFFICIAL_SPEC | Cited from Microsoft Learn — provisioned-throughput concepts (accessed 2026-05-28); URL: https://learn.microsoft.com/azure/ai-services/openai/concepts/provisioned-throughput |
| 6 | Leak rate is proportional to deployed PTU count | OFFICIAL_SPEC | Cited from Microsoft Learn — provisioned-throughput concepts (accessed 2026-05-28); URL: https://learn.microsoft.com/azure/ai-services/openai/concepts/provisioned-throughput |
| 7 | Leak rate constant `k_leak_tokens_per_ptu_per_second` (fitted value) | OPERATIONAL_INFERENCE | Numerical constant fitted in Task 024 from this repo's measurements; not a Microsoft-published number. Source: `benchmarks/04-leak-rate-fit/analysis.md §2` |
| 8 | Native spillover (automatic routing of overflow requests from a saturated provisioned deployment to a standard deployment) is reactive (per-request, stateless on caller) | OFFICIAL_SPEC | Cited from Microsoft Learn — spillover traffic management (accessed 2026-05-28); URL: https://learn.microsoft.com/azure/ai-services/openai/how-to/spillover-traffic-management |
| 9 | Slot-claim multi-worker cooldown protocol prevents thundering-herd retry storms | OPERATIONAL_INFERENCE | Repo design described in `docs/11-multi-worker-cooldown.md`; not a Microsoft pattern. Rationale: derived from Task 019 and Task 026 measurements of synchronized-retry behavior |
| 10 | `prompt_cache_key` is combined with the prefix hash to influence routing | OFFICIAL_SPEC | Cited from Microsoft Learn — prompt caching (accessed 2026-05-28); URL: https://learn.microsoft.com/azure/ai-services/openai/how-to/prompt-caching |
| 11 | `target_rpm_per_bucket` default = 10 | OPERATIONAL_INFERENCE | Within the Guide's recommended 8–12 range; the specific midpoint is a choice. Source: `docs/12-prompt-cache-key-policy.md §3` |
| 12 | "Bursts just over 100% may be permitted in short periods" | OFFICIAL_SPEC | Verbatim quote from Microsoft Learn — provisioned-throughput concepts (accessed 2026-05-28); URL: https://learn.microsoft.com/azure/ai-services/openai/concepts/provisioned-throughput |
| 13 | Cache-hit dip near saturation aligns with documented burst-tolerance behavior | OPERATIONAL_INFERENCE | Field observation interpreting Tier 1; the alignment is not asserted by Microsoft. Source: `benchmarks/07-cache-hit-degradation/analysis.md §5` |
| 14 | Azure Monitor PTU metric registry — six canonical metrics | OFFICIAL_SPEC | Cited from Microsoft Learn — monitor-openai (accessed 2026-05-28); URL: https://learn.microsoft.com/azure/ai-services/openai/how-to/monitor-openai; the six names are reproduced verbatim in `batch_runner/observability/azure_monitor_contract.py` |
| 15 | Azure Monitor correlation window heuristic (Task 028) | OPERATIONAL_INFERENCE | The 60-second leading / 300-second trailing window is this repo's choice; Learn does not publish a recommended window for PTU debugging. Source: `docs/14-observability-schema.md §5` |

---

## How to add a row

1. Pick the next integer for `#`.
2. State the claim in one line.
3. Choose tier: `OFFICIAL_SPEC` or `OPERATIONAL_INFERENCE`.
4. For Tier 1: include the public URL AND the ISO access date.
5. For Tier 2: include either an in-repo path (with section anchor)
   OR a rationale of >= 20 characters; the access date is optional.
6. Update `CHANGELOG.md` under `[Unreleased]`.

## Citations

- **Tier 1 (official spec)** — https://learn.microsoft.com/azure/ai-services/openai/concepts/provisioned-throughput — accessed 2026-05-28
- **Tier 1 (official spec)** — https://learn.microsoft.com/azure/ai-services/openai/how-to/spillover-traffic-management — accessed 2026-05-28
- **Tier 1 (official spec)** — https://learn.microsoft.com/azure/ai-services/openai/how-to/prompt-caching — accessed 2026-05-28
- **Tier 1 (official spec)** — https://learn.microsoft.com/azure/ai-services/openai/how-to/monitor-openai — accessed 2026-05-28
- **Tier 2 (operational inference)** — `docs/15-spec-vs-inference-taxonomy.md`
  Rationale: this appendix instantiates the taxonomy published in the main doc; rows beyond the initial 15 are added by future tasks under the convention defined there.
