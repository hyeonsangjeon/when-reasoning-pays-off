# Benchmark 01-short-factual -- Full Run Report

**Task:** 007-full-benchmark-01
**Branch:** `feature/full-benchmark-01`
**Pricing snapshot:** `pricing/azure-openai-payg-2026-05.yaml` (accessed_date 2026-05-19; source: https://azure.microsoft.com/en-us/pricing/details/azure-openai/)

This document is **facts only**. No analysis claims, no quality interpretation. Per the
spec, quality scoring and chart generation are Task 008's surface; this report establishes
the data is clean.

---

## 1. Pre/post manifest

| field | value |
|---|---|
| pre-run manifest file | `benchmarks/01-short-factual/.pre_run_manifest.txt` (gitignored) |
| pre-run manifest SHA-256 | `a49546d21c0b740e0587de055e74a77d1e7f709f200d966279c58930e5dd5e18` |
| pre-run JSON count | 8 (smoke run leftovers from Task 006; unchanged) |
| post-run JSON count | 368 |
| **delta (new files)** | **360** |

### Delta partition by `experiment_id`

| experiment_id | new files |
|---|---|
| `exp001_short-factual_baseline_gpt4o` | 60 |
| `exp001_short-factual_baseline` | 300 |
| **total** | **360** |

Assertion `60 + 300 == 360` partitioned by JSON `experiment_id` field: **PASS**.

---

## 2. Run window and runtime metadata

| field | value |
|---|---|
| earliest cell timestamp_utc | `2026-05-20T20:56:25Z` |
| latest cell timestamp_utc | `2026-05-20T20:59:54Z` |
| total wall window (approx.) | ~3.5 minutes for the 300-call gpt-5.2 segment; ~33 s for the 60-call gpt-4o segment |
| concurrency | 5 (runner default) |
| `git_commit` values seen | `a9c9cce`, `b0ee8ff` -- one per run (gpt-4o run started at one HEAD, gpt-5.2 run after committing gpt-4o JSONs at the next HEAD) |
| `endpoint` | `https://<resource>.services.ai.azure.com/api/projects/<project>` |
| `api_version` | `preview` |
| `auth_mode` | `entra` |
| `dry_run` flag | `False` on all 360 cells |
| `dirty` flag | `False` on all 360 cells |
| `pricing_snapshot_path` reference | `pricing/azure-openai-payg-2026-05.yaml` |

---

## 3. Per-cell aggregates

Each row aggregates **N=20 samples x R=3 repeats = 60 calls**. `mean` is mean,
`sd` is sample stdev (N-1). USD/call computed via `scripts.cost_calculator.payg_cost_per_call`
against the pricing snapshot listed above. `cache_hit` counts cells with
`usage.input_tokens_details.cached_tokens > 0`. `cold_start` counts cells where
the runner's `cold_start` flag was set (no warm cache prefix seen within window).

| model | effort | n | input mean (sd) | cached mean | output mean (sd) | reasoning mean (sd) | total mean (sd) | latency_ms mean (sd) | USD/call mean | USD cell-total | cache_hit | cold_start |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| gpt-4o | -- | 60 | 224.5 (12.6) | 0 | 10.4 (7.1) | 0 (0.0) | 234.9 (17.0) | 1494 (753) | $0.000665 | $0.0399 | 0/60 | 5/60 |
| gpt-5.2 | none | 60 | 223.5 (12.6) | 0 | 14.0 (7.9) | 0 (0.0) | 237.5 (18.1) | 1657 (700) | $0.000587 | $0.0352 | 0/60 | 3/60 |
| gpt-5.2 | low | 60 | 223.5 (12.6) | 0 | 14.1 (8.1) | 0 (0.0) | 237.6 (18.2) | 1713 (677) | $0.000589 | $0.0353 | 0/60 | 2/60 |
| gpt-5.2 | medium | 60 | 223.5 (12.6) | 0 | 14.2 (8.0) | 0 (0.0) | 237.7 (18.2) | 1410 (293) | $0.000589 | $0.0354 | 0/60 | 0/60 |
| gpt-5.2 | high | 60 | 223.5 (12.6) | 0 | 15.5 (10.0) | 1.4 (5.9) | 239.0 (18.0) | 1580 (416) | $0.000627 | $0.0376 | 0/60 | 0/60 |
| gpt-5.2 | xhigh | 60 | 223.5 (12.6) | 0 | 14.8 (8.8) | 0.6 (4.8) | 238.3 (19.2) | 1441 (305) | $0.000607 | $0.0364 | 0/60 | 0/60 |

**Totals** -- sum of `USD cell-total` across all 6 rows:
- gpt-4o: **$0.0399**
- gpt-5.2: **$0.1799**
- **Combined: $0.2198** (vs. combined hard ceiling $30; vs. per-YAML hard ceilings $5 / $25)

---

## 4. Operational tallies

| metric | value |
|---|---|
| 429 (rate-limit) responses | 0 (no retry-driven cells; `retry_count==0` everywhere) |
| total retry events across 360 cells | 0 |
| cold-start cells (per-runner `cold_start` flag) | 10 / 360 |
| cells with cache hit (`cached_tokens > 0`) | 0 / 360 (short-factual prompts; no shared prefix beyond the system prompt that meets Foundry's cache threshold) |
| HTTP / API errors | 0 (all 360 cells captured a full `usage` block) |

---

## 5. Byte-identical prompt audit (methodology section 2 invariant)

The runner stamps each call's `call_metadata` with `system_prompt_sha256` and
`user_input_sha256`. Audit ran across the 360 new cells:

| audit | observed | expected | verdict |
|---|---|---|---|
| `system_prompt_sha256` unique values | 1 | 1 (byte-identical across all 360 cells) | **PASS** |
| `user_input_sha256` unique values | 20 | 20 (one per dataset sample, repeated 18x under gpt-5.2 sweep + 3x under gpt-4o) | **PASS** |
| `system_prompt_sha256` value | `95d5b8d04f312d6faabd99c4a2584fd686c6d1454050ed9a6661a7d2b8f7fe64` | -- | -- |

### Union SHA-256 audit

A SHA-256 was computed over the sorted concatenation of each new file's content hash:

```
union_sha256_of_hashes = 8847580b7523526c871c878a35b97d94125ff40be1d6c1951853618c356f5791
```

This is a single fingerprint that any future re-run or reviewer can recompute to
prove the on-disk 360-file set hasn't been mutated.

---

## 6. Methodology invariants -- full checklist

| invariant | verdict | evidence |
|---|---|---|
| Pre/post manifest delta == 360 | **PASS** | pre=8, post=368, new=360 |
| gpt-4o partition == 60 (experiment_id=exp001_short-factual_baseline_gpt4o) | **PASS** | observed=60 |
| gpt-5.2 partition == 300 (experiment_id=exp001_short-factual_baseline) | **PASS** | observed=300 |
| Combined delta == 360 | **PASS** | observed=360 |
| system_prompt_sha256 single unique value (byte-identical system prompt) | **PASS** | sha256=95d5b8d04f312d6faabd99c4a2584fd686c6d1454050ed9a6661a7d2b8f7fe64 |
| user_input_sha256 == 20 unique values (one per sample, repeated 18x gpt-5.2 + 3x gpt-4o) | **PASS** | unique=20 |
| gpt-5.2 reasoning_tokens present (>=0) in all cells | **PASS** | 300/300 |
| gpt-4o reasoning_tokens absent or 0 in all cells | **PASS** | 60/60 |
| cached_tokens field present (possibly 0) in all cells | **PASS** | 360/360 |
| Per-YAML hard ceiling: gpt-4o spend <= $5 | **PASS** | actual=$0.0399 |
| Per-YAML hard ceiling: gpt-5.2 spend <= $25 | **PASS** | actual=$0.1799 |
| Combined ceiling <= $30 | **PASS** | actual=$0.2198 |
| No raw JSON overwritten (append-only growth from pre to post) | **PASS** | delta = post - pre (no replacements) |
| No env-var value leaked outside `endpoint` field | **PASS** | 0 files with services.ai mention outside endpoint |
| No api_key / bearer / sk- string in any new JSON | **PASS** | 0 matches |
| All cells captured at api_version=preview, auth_mode=entra | **PASS** | versions=['preview'], auth=['entra'] |
| Foundry v1 endpoint (services.ai.azure.com/api/projects) | **PASS** | endpoints=['https://<resource>.services.ai.azure.com/api/projects/<project>'] |
| Single pricing snapshot referenced | **PASS** | path=['pricing/azure-openai-payg-2026-05.yaml'] |
| No dry-run cells (all dry_run=False) | **PASS** | dry_run flags observed=[False] |
| No dirty-tree cells (all dirty=False -- clean working tree at run-start) | **PASS** | dirty flags observed=[False] |


---

## 7. Sampled raw JSONs (spot-check pointers)

These three files are committed at the indicated SHA-256 content hashes. Anyone
reviewing this report can verify by recomputing `shasum -a 256 <path>`.

| path | content sha256 | what |
|---|---|---|
| `benchmarks/01-short-factual/runs/20260520T205625Z_exp001_short-factual_baseline_gpt4o_000_gpt-4o_null_r0.json` | `c487aae56b9d07e13088e68ba02f291fca2305f0f42b763244ca794d500e595d` | gpt-4o, sample 000, repeat 0 (first of gpt-4o run) |
| `benchmarks/01-short-factual/runs/20260520T205858Z_exp001_short-factual_baseline_008_gpt-5.2_none_r0.json` | `00ad7d6dc4df05af0b49311b73b8f3ad2f27d962bca593ad4cccd66bea8d2673` | gpt-5.2, effort=none, sample 008, repeat 0 (middle of gpt-5.2 run) |
| `benchmarks/01-short-factual/runs/20260520T205954Z_exp001_short-factual_baseline_019_gpt-5.2_xhigh_r2.json` | `5d6b6116a0aec3b768d413f4ec33706cfb38d140bc2b952be5de95a7dd99a38f` | gpt-5.2, effort=xhigh, sample 019, repeat 2 (last of gpt-5.2 run) |


---

## 8. Cost-calculator invocation note

The spec's verification step calls for
`python -m scripts.cost_calculator payg benchmarks/01-short-factual/analysis.json --pricing-dir pricing`.
**That command depends on `analysis.json`**, which is the deliverable of Task 008
(aggregation pipeline) -- `analysis.json` does not exist in this repo state and
will not be produced by Task 007. For this report, USD figures were derived by
calling `scripts.cost_calculator.payg_cost_per_call(...)` directly against each
of the 360 raw JSONs and the same pricing snapshot path the runner stamped into
every cell (`pricing/azure-openai-payg-2026-05.yaml`, accessed 2026-05-19). When
Task 008 produces `analysis.json`, re-running the CLI command must yield a
combined PAYG total within rounding of **$0.2198**; any larger
deviation indicates aggregation drift.

---

## 9. Reproducibility manifest

| key | value |
|---|---|
| dataset | `benchmarks/01-short-factual/dataset.json` (pinned via `metadata.dataset_sha256` in each experiment YAML) |
| system prompt | `prompts/01-short-factual/system.md` (`system_prompt_sha256 = 95d5b8d04f312d6f...`) |
| gpt-4o experiment YAML | `experiments/exp001_short-factual_baseline_gpt4o.yaml` |
| gpt-5.2 experiment YAML | `experiments/exp001_short-factual_baseline.yaml` |
| pricing snapshot | `pricing/azure-openai-payg-2026-05.yaml` |
| commit landing 60 gpt-4o cells | `b0ee8ff` |
| commit landing 300 gpt-5.2 cells | `4d142f4` |
| commit landing this report | (set at commit time) |

---

## 10. Definition of Done -- Task 007 status

| criterion | status |
|---|---|
| Two experiment YAMLs updated and committed before run | PASS (commit `66b1fd1`) |
| 60 gpt-4o cells landed in single exp commit | PASS (commit `b0ee8ff`) |
| 300 gpt-5.2 cells landed in single exp commit | PASS (commit `4d142f4`) |
| Pre/post manifest captured, delta = 360, partition 60/300 | PASS (section 1 above) |
| Byte-identical prompt audit clean (1 system hash, 20 user hashes) | PASS (section 5 above) |
| Reasoning token presence per family rule | PASS (sections 3 and 6 above) |
| Cached tokens captured (may be 0) | PASS (sections 3 and 6 above) |
| Per-YAML hard-ceiling check (gpt-4o <= $5, gpt-5.2 <= $25) | PASS (sections 3 and 6 above) |
| 429 / retry tally logged | PASS (section 4 above; both zero) |
| `RUN_REPORT.md` committed under `benchmarks/01-short-factual/` | PASS (this file) |
| No raw JSON overwritten | PASS (append-only delta) |
| No env-var value leaked outside `endpoint` field | PASS (section 6 above) |
| Quality eval deferred to Task 008 | PASS (response_text preserved verbatim, no judging here) |

**Verdict: data is clean. Task 007 is complete. Task 008 is unblocked.**
