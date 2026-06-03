# Smoke Report — Benchmark 02 Multi-Step Reasoning, Phase-1

## Verdict

**GO** — full Task 009 runs (gpt-4o + gpt-5.2) may proceed.

All 6 spec-required smoke cells executed end-to-end against the real Foundry v1
endpoint, exited 0, and produced JSONs that satisfy every Task 009 invariant.
No integration defects surfaced; the runner code path proved against the
harder benchmark-02 input shape works without modification.

## Summary

- Date (UTC): `2026-05-21T09:18:28Z` … `2026-05-21T09:19:24Z` (~56 s)
- Branch: `feature/benchmark-02`. HEAD at smoke start: `00c8e58` (gpt-4o smoke),
  `4f8adf8` (gpt-5.2 smoke after committing gpt-4o cells)
- Endpoint: `AZURE_OPENAI_FOUNDRY_ENDPOINT`
  (`https://<resource>.services.ai.azure.com/api/projects/<project>`,
  Foundry v1 project-scoped)
- Auth mode: `entra` (DefaultAzureCredential, `https://ai.azure.com/.default`
  audience — no API key on disk)
- Cells run / target: **6 / 6**. Per-`experiment_id` partition:
  `exp_smoke_02_gpt4o = 2`, `exp_smoke_02 = 4`
- 429s: **0**. Retries: **0**. Hard-ceiling trips: **0**. Combined spend:
  **$0.0060** (well under the $0.50 spec ceiling for the smoke phase)
- USD breakdown:
  - `exp_smoke_02_gpt4o`: $0.0016 (cells: 000=$0.000812, 001=$0.000788)
  - `exp_smoke_02`: $0.0044 (low: $0.000606 + $0.000623; high: $0.001379 + $0.001754)

## 6-cell evidence table

| experiment_id        | sample | model    | effort | in  | out | total | reasoning_tok | cached_tok | latency_ms | response | expected | correct |
|----------------------|--------|----------|--------|----:|----:|------:|--------------:|-----------:|-----------:|----------|----------|:-------:|
| exp_smoke_02_gpt4o   | mr_01  | gpt-4o   | —      | 317 |   2 |   319 |             0 |          0 |     3021.7 | `576`    | `672`    | ✗       |
| exp_smoke_02_gpt4o   | mr_02  | gpt-4o   | —      | 307 |   2 |   309 |             0 |          0 |     3082.2 | `155`    | `175`    | ✗       |
| exp_smoke_02         | mr_01  | gpt-5.2  | low    | 316 |   5 |   321 |             0 |          0 |     2642.0 | `672`    | `672`    | ✓       |
| exp_smoke_02         | mr_02  | gpt-5.2  | low    | 306 |   5 |   311 |             0 |          0 |     2354.1 | `175`    | `175`    | ✓       |
| exp_smoke_02         | mr_01  | gpt-5.2  | high   | 316 |  33 |   349 |            26 |          0 |     2766.7 | `672`    | `672`    | ✓       |
| exp_smoke_02         | mr_02  | gpt-5.2  | high   | 306 |  47 |   353 |            40 |          0 |     2959.4 | `175`    | `175`    | ✓       |

## Schema validation

- All 6 JSONs: `dry_run == false`, `dirty == false`, `response_text` non-empty,
  `input_tokens > 0`, `output_tokens > 0` ✅
- All 6 JSONs: `usage.input_tokens_details.cached_tokens` key present
  (value 0 — single-call cold session) ✅
- All 6 JSONs: `usage.output_tokens_details.reasoning_tokens` key present
  (value 0 for gpt-4o + gpt-5.2 low; **non-zero (26 / 40)** at gpt-5.2 high —
  expected on benchmark 02 because the prompts demand multi-step inference) ✅
- gpt-4o JSONs: `effort: null`, no `reasoning` param sent on the wire, and
  `reasoning_tokens == 0` ✅
- All 6 JSONs: `endpoint` field matches
  `https://<resource>.services.ai.azure.com/api/projects/<project>` ✅
- All 6 JSONs: `git_commit` is a real SHA (`00c8e58…` or `4f8adf8…`),
  `dirty: false`, `api_version: preview`, `auth_mode: entra` ✅
- Secret-leak grep — `api_key|sk-[A-Za-z0-9]|bearer |AZURE_OPENAI_API_KEY` —
  **CLEAN** (no matches) ✅

## Reasoning-token finding contrasts with benchmark 01

On benchmark 01 (short-factual) the warm-probe smoke established that
`reasoning_tokens` is **zero at effort=high** because the null-case prompts do
not trigger reasoning expansion. On benchmark 02 (multi-step) the same
`effort=high` produces **non-zero reasoning_tokens (26 and 40)** on the two
smoke samples — exactly the workload-shape effect benchmark 02 exists to
quantify. The smoke therefore confirms the reasoning surface activates on the
new benchmark inputs without any runner modification.

## Quality signal already visible

At N=2, R=1, no statistical claim is possible. The descriptive observation:
gpt-4o failed both arithmetic-word smoke samples (mr_01 and mr_02), returning
576 instead of 672 and 155 instead of 175. gpt-5.2 returned the correct answer
at both `low` and `high` effort on both samples. This is the inverse of
benchmark 01 and is the pre-registered hypothesis of benchmark 02. The full
run will give the binarized pass-rate the analysis depends on.

## Foundry v1 surprises

**None.** Every defect surfaced during Task 006 (effort whitelist drift,
temperature rejection, `{input}` JSON render) is already fixed in the runner.
The benchmark-02 prompt shapes (multi-line code blocks, multi-sentence word
problems) exercise the runner's prompt-rendering paths without producing any
new surprise.

## Known deferred limitations

- **Cache hit rate untested under warm conditions.** All 6 calls returned
  `cached_tokens = 0` — same as benchmark 01's smoke. Either the deployment
  was effectively cold across the 56-second window, or Foundry v1 prompt
  caching has a higher activation threshold than documented. The full run's
  warm batch is the right place to retest.
- **Reasoning-token magnitude is sample-specific.** mr_01 → 26 tokens at
  high, mr_02 → 40 tokens at high. The full run will produce per-effort
  distributions across all 20 samples.

## Task 009 full-run handoff gate

- [x] **GO** — gpt-4o full run (60 cells) and gpt-5.2 full run (300 cells) may
      proceed.
- [ ] NO-GO

Preconditions for the full run confirmed:
1. 6/6 cells succeeded against the real Foundry v1 endpoint; runner exit 0 on
   every invocation.
2. JSON schema invariants hold on every file (cached_tokens +
   reasoning_tokens key presence; gpt-4o `effort: null` with
   `reasoning_tokens=0`; no secret leak).
3. No 429 retry exhaustion; no budget-ceiling trip; total spend $0.0060 across
   6 calls — well under $0.50 spec ceiling.
4. Reasoning-token emission at gpt-5.2 high is non-zero, confirming the
   reasoning surface activates on benchmark-02 inputs.
5. Pre-registered hypothesis (gpt-4o fails, gpt-5.2 succeeds on multi-step
   arithmetic) holds at the smoke scale.

## Carry-forward for the full run

- Hard-ceiling budgets per the spec:
  - `exp002_benchmark02_gpt4o.yaml`: estimated $5, ceiling $5 (dry-run pre-run
    estimate is $0.20 — comfortable margin)
  - `exp002_benchmark02_gpt5_2.yaml`: estimated $20, ceiling $25 (dry-run
    pre-run estimate is $8.24 — comfortable margin)
  - Combined estimated ≤ $25; combined ceiling ≤ $30
- Run order: gpt-4o first (cheaper, lower variance), then commit, then
  gpt-5.2.
- Judge pass follows the full run (~360 judge calls under
  `scripts.run_judge --confirm`).
