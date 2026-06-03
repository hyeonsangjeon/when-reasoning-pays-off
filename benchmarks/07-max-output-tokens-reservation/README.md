# Benchmark 07 — `max_output_tokens` Reservation Sweep (Task 019 v2.4 + v2.5)

> **PAYG, not PTU.** This benchmark provides indirect, behavioral evidence about
> how a PAYG GlobalStandard deployment's *throttled* TPM quota interacts with
> the `max_output_tokens` parameter — the conjecture under test is whether
> Azure's admission layer reserves quota up to the *cap* rather than the
> realized output count. **All evidence here is collected against a PAYG
> GlobalStandard deployment (`ptu-deploy-throttled`, 60 K TPM, `ptu_evidence=false`).**
> It does **not** instantiate or bill against a PTU. The hypothesis being
> proxied — Hypothesis I from `docs/04-hypotheses.md` — concerns *reservation*
> semantics that could not be cleanly tested on the PAYG deployment without
> first ruling out alternative explanations (such as PTU semantics). This is a
> *proxy* experiment in the sense documented in
> `docs/05-methodology.md` §6.6 and in the internal task spec
> (Task 019 max-output-tokens-reservation, private working tree, v2.3
> §"Hypothesis"
> and §"Limits & honesty"). The PAYG-not-PTU caveat is enforced at YAML load
> via `metadata.ptu_evidence: false` and echoed verbatim into every
> `runs/*.summary.json`.

---

## Protocol v2.4 — Empirical-calibration-aware smoke/evidence promotion

> v2.4 is an **additive** layer on top of v2.3: every v2.3 pin (TPM
> 0.85/1.25 thresholds, `selected_peak_tps` non-overridability,
> Phase A/B grids, bracket-search semantics, calibration max age = 24 h)
> is **preserved verbatim**. v2.4 introduces a §6 chain step 5
> empirical-promotion gate that runs **before** the v2.1 cold-cache
> TPM-feasibility preflight; when every §3.1 invariant holds, the
> gate replaces the cold-cache projection's smallest-cell input with
> the calibration's already-paid-for warm-cache observation. This
> rescues smoke/evidence runs whose cold-cache projection would
> otherwise abort even though the live calibration already proved the
> deployment passes the contrast contract at the selected TPS.

## Protocol v2.5 — Adaptive Stage 0.5.C contrast calibration (PAYG, not PTU)

> v2.5 is a forward-only protocol revision layered on top of v2.4: every
> v2.4 pin (cache-hit floors `0.80 / 0.80`, `0.85 / 1.25` TPM thresholds,
> 24-hour calibration freshness window, prompt-identity contract,
> run-lock, `max_retries=0`, the literal guardrail string, Phase A
> grid, Phase B grid, `selected_peak_tps` non-override invariant, the
> cold-cache and warm-projection formulae, and the mini-probe path) is
> **preserved verbatim**. v2.5 adds an **adaptive Stage 0.5.C** that
> runs IFF (i) 0.5.B terminated `no_usable_contrast_at_this_prompt_deployment`
> (or another v2.4 inconclusive outcome the spec admits for adaptive
> entry), (ii) `runtime.adaptive_calibration.enabled: true`, AND (iii)
> the YAML carries an `adaptive_calibration_auditor_approval.comment`
> matching `^methodology-auditor approved v2\.5 adaptive — [a-z0-9-]+ —
> \d{4}-\d{2}-\d{2}$`. Default `enabled: false`.

0.5.C role-separately estimates each role's onset interval (§4.1),
runs at most two §4.2 expansion probes and three §4.3 bracket probes
per role under hard caps (`adaptive_tps_hard_max = 1.20 × max(phase_b_grid_tps)`,
`adaptive_tps_hard_min = 0.5 × min(phase_a_grid_tps)`), and evaluates
the contrast criteria in order:

1. **C1 — strict separating TPS** (§5.1). The v2.4 admission rule
   restated against §0.8 aggregated same-`(role, t*)` observations
   from eligible probes only (§0.2). When multiple TPS values
   qualify, §0.7 deterministically picks the **lowest** qualifying
   TPS. `selected_via = "adaptive_strict_separating_tps"`.
2. **C2 — replicate-gated onset separation** (§5.2, microfix #2 PINNED
   margin `0.05` TPS). Requires both roles bracketed,
   `onset_lower[smallest_control] − onset_upper[largest] ≥ 0.05`, and
   one replicate per role at the geometric-mean candidate `t*` that
   passes C1's strict predicates. The C2 replicate is budgeted under
   the **separate** `adaptive_c2_replicates_max_per_role = 1` cap
   (§0.4) — it does **not** consume bracket-depth headroom; the FIRST
   replicate is binding (no best-of-N).
3. **C3 — first-class `no_promotable_contrast_at_this_prompt_deployment`**
   (§5.3, §8.1). Emitted IFF the search ran to completion within all
   §4.4 caps AND neither C1 nor C2 admitted. A §4.4 hard-cap halt
   **never** surfaces as C3 (§0.3); it surfaces as
   `adaptive_calibration_budget_exhausted` /
   `_wall_time_exhausted` / `_api_connection_unstable`.

Schema bumps (§9): `task019.v2.5.calibration_result`,
`task019.v2.5.adaptive_calibration_summary` (new file), bumps to
`task019.v2.5.smoke_summary` and `task019.v2.5.evidence_summary` that
add `calibration_selected_via`, `calibration_adaptive_summary_path`,
and `calibration_adaptive_summary_sha256` linkage fields.

Live-run / blocked-run discipline (§0.5): every v2.5 attempt MUST
append a section to
`benchmarks/07-max-output-tokens-reservation/live-v2.5-adaptive-contrast.md`
and a matching entry to `CHANGELOG.md` under `### Live run — Task 019
v2.5` or `### Blocked run — Task 019 v2.5`. Blocked runs are analysis
data, not disposable logs. The §11.50 lint (`scripts/task019_v25_adaptive.check_v25_live_artifacts`)
fails CI when either artifact is missing.

PAYG-not-PTU caveat (§0.10): every v2.5 artefact and the v2.5
live-report markdown frame every PAYG observation as **"PAYG proxy
evidence for a PTU hypothesis"**, never as "PTU evidence". The §11.54
lint (`scripts/task019_v25_adaptive.lint_payg_proxy_wording`) rejects
the forbidden phrases outside explicitly quoted
`> COUNTER-EXAMPLE:` blockquote lines.

Full v2.5 spec: internal Task 019 v2.5 adaptive-contrast specification
(private working tree).

### Three promotion paths (§3, §8)

1. `cold_cache_strict` — v2.1 baseline. Used when ANY §3.1 invariant
   denies AND v2.1 cold-cache feasibility admits.
2. `empirical_calibration_aware` — used when every §3.1 invariant
   holds. Smallest-cell projection uses
   `60 × selected_peak_tps × (effective_uncached_prompt_tokens + max_output_tokens)`
   where
   `effective_uncached_prompt_tokens = max(base × (1 − cache_hit_ratio_steady_state), 100)`.
   Largest cell uses the warm formula too (`v2.4_warm_projection`).
3. `mini_probe_revalidated` — only available when
   `runtime.empirical_promotion.mini_probe_enabled: true` (opt-in,
   requires `# auditor-approved-YYYY-MM-DD: <handle>` comment per §7),
   and only when invariant 12 (freshness) is the **sole** denial.
   Smallest cell uses the warm formula with the mini-probe's measured
   cache-hit ratio; largest cell uses the v2.1 cold-cache formula
   (`v2.1_cold_cache_strict`) because the calibration's
   `cache_hit_ratio_steady_state_largest` is no longer trustworthy.

### Pinned §10 values (single source of truth in
`scripts/measure_max_output_tokens_sweep.py`)

| Key | Pinned value |
| --- | --- |
| `cache_hit_floor_smallest_control` | 0.80 |
| `cache_hit_floor_largest` | 0.80 |
| `calibration_max_age_hours` | 24 |
| `minimum_records_at_selected_tps` | 30 |
| `mini_probe_enabled` (default) | `false` |
| `mini_probe_max_usd` | $1.00 |
| `mini_probe_max_attempts_per_run` | 1 |

The YAML loader enforces equality against these constants — any
carried value MUST equal the PIN; per-run loosening is forbidden.

### v2.3 fixture expected outcomes (§11.18 frozen-clock tests)

The v2.3 calibration result at
`runs/20260530T135125Z_exp007_max_output_tokens_sweep_calibration.result.json`
(sha256 `92126b46…b81`) is the canonical §11.18 fixture:

- **18(a) fresh clock** (frozen at `completed_at_iso + 1h`):
  `promotion_path = empirical_calibration_aware`,
  `largest_cell_projection_formula = v2.4_warm_projection`,
  projected smallest TPM ≈ 16,406 (well under 0.85 × 60,000 = 51,000),
  projected largest TPM ≈ 473,840 (well over 1.25 × 60,000 = 75,000).
- **18(b) stale clock without mini-probe** (frozen at
  `completed_at_iso + 36h`): gate denies with
  `empirical_promotion_denied_reason = empirical_promotion_disabled_calibration_stale_and_mini_probe_disabled`;
  cold-cache fallback also denies at `selected_peak_tps = 0.47469`
  (smallest TPM = 68,755 > 51,000) → `task019.v2.4.abort_envelope` with
  `exit_reason = TPM_FEASIBILITY_ABORT`.

### Abort envelope vs admitted summary (§9.4)

The abort envelope `task019.v2.4.abort_envelope` is **mutually
exclusive** with admitted smoke/evidence summaries. Forbidden fields
include `tpm_feasibility_promotion_inputs`,
`empirical_warm_projection_inputs`, `mini_probe_result`,
`smoke_summary_reference`, etc. (§9.4 enforcement list).

Microfix #5 / #6 stable-identifier discipline (audit-critical):

- `exit_reason` is the v2.1-PRESERVED `TPM_FEASIBILITY_ABORT` for the
  empirical-denied-then-cold-cache-denied terminal case. The §8
  stable `empirical_promotion_disabled_*` identifier surfaces in
  `empirical_promotion_denied_reason`, NEVER in `exit_reason`.
- Raw `mini_probe_failed_*` strings NEVER appear in the abort
  envelope; the composite identifier
  `empirical_promotion_disabled_mini_probe_failed_and_cold_cache_fails`
  is used instead.

### PAYG-not-PTU caveat (v2.4 §3.1 invariant 11 backward-compat)

The v2.3 calibration fixture pre-dates the
`metadata.ptu_evidence: false` field added in v2.4. When that field
is absent, the gate admits the calibration as PAYG-not-PTU **only if
ALL FIVE** of the following hold:

1. `deployment_used == "ptu-deploy-throttled"`
2. `deployment_env == "AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED"`
3. `experiment_id == "exp007_max_output_tokens_sweep"`
4. `pricing_snapshot_path` resolves to an existing committed PAYG
   snapshot under `pricing/` whose top-level `source_url`
   (microfix #4 — **not** `source`) is a non-empty HTTPS URL AND
   `pricing_accessed_date` is present and non-empty
5. The smoke YAML's `metadata.ptu_evidence == false` AND the
   committed v2.3 terminal report (`live-calibration-smoke-evidence-final.md`)
   enumerates the calibration's sha256 with explicit PAYG-not-PTU
   classification

The dictionary of basis booleans is echoed verbatim into the smoke
admitted summary's `ptu_evidence_inference_basis` block for audit
reproducibility.

Spec: internal Task 019 v2.4 empirical-calibration-aware-promotion
specification (private working tree).

---

## Protocol v2.3 — Two-phase Stage 0.5 calibration (Phase A safe ramp → Phase B escalate-until-429)

v2.3 supersedes v2.2.1's single-grid calibration with a **two-phase**
adaptive TPS calibration that escalates until a real 429 is observed (or
one of three honest gates fires). The downstream pipeline is still a
strict three-stage chain glued together by sha256 file hashes:

1. **Stage 0.5 — Calibration (two-phase).**
   - **Phase A — safe ramp (v2.2.1 grid preserved verbatim).** Walks
     `candidate_tps_grid: [0.33, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]`
     ascending. At each candidate TPS, runs a 3-minute constant-rate probe
     on the *largest* cell (`max_output_tokens = 16 384`) with the same
     12-call prewarm + same `prompt_cache_key` namespacing as v2.2.1,
     `sdk_max_retries = 0`, no spillover. The first candidate that
     produces **≥ 1 real 429** in the largest-cell probe is followed up
     with a *control* probe on the *smallest* cell
     (`max_output_tokens = 256`) at the **same TPS** — if the smallest
     cell observes **zero 429s** the candidate is *selected* and becomes
     the binding `peak_ramp_tps` for the rest of the run.
   - **Phase B — escalate-until-429 (v2.3 NEW).** Entered IFF Phase A
     iterated the full grid without observing a largest-cell 429 **AND**
     every Phase A largest-cell probe passed the new admitted-pressure
     validation gate. Iterates the **EXACT pinned six-member grid**
     `candidate_tps_grid_phase_b: [5.0, 8.0, 12.0, 16.0, 24.0, 32.0]` in
     ascending order, same per-probe shape and same eligibility gates as
     Phase A, **but at the dedicated** `runtime.concurrency_phase_b: 512`
     (scoped to Phase B and Phase-B-rooted bracket probes only — Phase A,
     smoke, and evidence keep v2.2.1's `runtime.concurrency: 96`). The
     v2.2.1 terminal outcome `no_largest_cell_429_at_any_candidate_tps`
     is **RETIRED** as a final calibration verdict; under v2.3 it persists
     ONLY as an intra-calibration signal meaning "Phase A grid exhausted
     with admitted-pressure validated → enter Phase B".
2. **Stage 1 — Smoke.** Required to receive
   `--calibration-result <path>` and refuses to start without it
   (exit `9`, `reason=calibration_result_missing`). Writes its own
   `*.summary.json` plus a sibling `.sha256` sidecar so the next stage
   can hash-link to it.
3. **Stage 2 — Evidence.** Required to receive *both*
   `--calibration-result` *and* `--smoke-summary` (exit `9` if either is
   missing). Validates the sidecar sha256 + the calibration → smoke
   linkage triple match (`selected_peak_tps`, `calibration_result_path`,
   `calibration_result_sha256`) + smoke gate `passed = true` before
   dispatching any HTTP call.

**No flag auto-discovery.** The CLI deliberately refuses to walk
`runs/` looking for "the most recent" calibration / smoke artifact. The
operator MUST pass the absolute paths explicitly. The CLI ALSO refuses
any `--peak-ramp-tps` override (exit `9`,
`reason=peak_ramp_tps_override_forbidden_use_calibration_result`); the
TPS value is owned by calibration end-to-end.

### Phase B grid — exact-full-grid contract (no subsets, no ad-hoc values)

The YAML validator REQUIRES `calibration.candidate_tps_grid_phase_b`
to EQUAL the pinned six-member list `[5.0, 8.0, 12.0, 16.0, 24.0, 32.0]`
in EXACT ascending order. Four mutations each abort at YAML-load time
(exit code `9`) with a distinct `LinkageValidationError.reason`,
evaluated in this order (so a grid like
`[5.0, 5.0, 8.0, 12.0, 16.0, 24.0, 32.0]` reports the duplicate, not a
misleading missing-member error):

| Order | `reason` | What triggers it |
|---:|---|---|
| 1 | `candidate_tps_grid_phase_b_contains_duplicate_value` | any repeated member |
| 2 | `candidate_tps_grid_phase_b_contains_ad_hoc_value` | any value outside the pinned six (e.g. `7.5`) |
| 3 | `candidate_tps_grid_phase_b_member_missing` | omission of any of the six |
| 4 | `candidate_tps_grid_phase_b_not_sorted_ascending` | re-ordered list |

The runner has NO CLI flag, NO environment-variable override, and NO
alternative-grid YAML path that admits these mutations. **Changing or
shortening the Phase B grid requires a NEW SPEC REVISION (a re-audited
v2.4 banner item), NOT a runtime YAML subset.**

### Admitted-pressure validation gate (v2.3 NEW — blocking on every probe)

Every calibration probe — Phase A and Phase B, largest-cell and
smallest-cell control — computes its actual ADMITTED RPM from the
per-record `admitted_dispatch_iso` timestamps (NOT from response
completions; NOT from the scheduled dispatch list; NOT from the
request-completion latency). The gate requires
`admitted_peak_rpm_observed_last_30s ≥ 0.70 × (candidate_tps × 60)`
(pinned floor ratio `admitted_pressure_floor_ratio: 0.70`,
window `admitted_pressure_window_s: 30`).

- If a probe ends with ZERO real 429s observed AND
  admitted-RPM < threshold → the probe is INELIGIBLE under the
  admitted-pressure gate. Bounded retry ONCE at the same `candidate_tps`
  with cache-key suffix `_retry1_admp` (distinct from the v2.2.1
  warm/backlog `_retry1` suffix so the per-probe artifact trail is
  unambiguous about which gate triggered the retry); if the retry's
  admitted-RPM ALSO fails the threshold AND still observes zero 429s →
  terminal `calibration_probe_inconclusive_admitted_pressure_insufficient`
  (v2.3 NEW outcome), exit code `8`.
- **The admitted-pressure gate is SKIPPED on any probe that observes
  ≥ 1 real 429 in its window** — the 429 itself is the proof that the
  deployment's admission ceiling was crossed.

### Bracket search before terminal no-contrast (v2.3 NEW — bounded depth)

Under v2.2.1, a smallest-cell control probe observing ≥ 1 real 429 at
`candidate_tps = T` triggered IMMEDIATE terminal
`no_usable_contrast_at_this_prompt_deployment`. Under v2.3, BEFORE
accepting the no-contrast verdict, the runner attempts a bounded
**bracket search** IFF there exists a `T_low` (the most-recently-iterated
candidate in the SAME phase at which the largest-cell probe passed all
eligibility gates AND observed zero 429s). The bracket point is the
**geometric midpoint** `T_bracket = sqrt(T_low × T)`. At every bracket
point both cells are re-probed (same eligibility gates, cache-key suffix
`_bracketN` where N ∈ {1, 2, 3}). The bracket recurses to
`bracket_max_depth: 3` (PINNED). On exhaustion, terminal
`no_usable_contrast_at_this_prompt_deployment` is emitted with a
structured `bracket_trace` recorded in `calibration_result.json`. The
bracket NEVER spans Phase A → Phase B; the `T_low` must be in the same
phase as the failing T.

### 9-member calibration outcome enum (v2.3 pinned — v2.2.1's 7 minus one retired plus three NEW)

| outcome | meaning |
|---|---|
| `selected` | candidate TPS produced ≥1 real 429 in the largest cell and zero 429 in the smallest control cell at the same TPS → bound as `selected_peak_tps`. |
| `no_usable_contrast_at_this_prompt_deployment` | every candidate (including bracket points) that saturated the largest cell ALSO saturated the smallest-cell control — no TPS in the grid cleanly separates "saturated" from "headroom". Exit `8`. |
| `smallest_cell_control_probe_inconclusive_cap_hit` | smallest-cell control probe halted on its per-probe USD cap before reaching its 3-minute duration; cannot rule out late 429s in control. Exit `8`. |
| `calibration_total_usd_exhausted` | cumulative probe spend crossed the conservative-but-useful $220 calibration ceiling before a `selected` verdict (honest stop-and-report; do NOT raise the cap). Exit `8`. |
| `calibration_probe_inconclusive_cache_not_warm` | a probe failed the warm criterion (`≥ 3 of last 6 prewarm with cached_tokens > 0`) on both the initial attempt AND the single bounded `_retry1` re-run. Exit `8`. |
| `calibration_probe_inconclusive_backlog_excessive` | a probe satisfied warm but its dispatch backlog exceeded the spec backlog limits (p95 > 1 500 ms, max > 5 000 ms) on both attempts. Exit `8`. |
| `calibration_probe_inconclusive_admitted_pressure_insufficient` **(v2.3 NEW)** | a probe (either initial OR `_retry1_admp`) ended with zero 429s AND `admitted_peak_rpm_observed_last_30s < 0.70 × candidate_tps × 60`. The driver could not generate enough admitted pressure to validate the candidate TPS; escalating further would burn shared budget without scientific value. Exit `8`. |
| `no_largest_cell_429_at_any_phase_b_candidate_tps_endpoint_not_throttling` **(v2.3 NEW)** | Phase B grid exhausted with admitted-pressure validated on every Phase B candidate AND zero 429s observed — a meaningful negative finding for Hypothesis I at this proxy + prompt identity. Exit `8`. |
| `no_largest_cell_429_at_any_phase_b_candidate_tps_driver_pressure_insufficient` **(v2.3 NEW)** | Phase B grid exhausted with the admitted-pressure gate failing on at least one Phase B candidate's bounded retry — a driver / host-capacity finding, NOT a Hypothesis I verdict. Exit `8`. |

The retired `no_largest_cell_429_at_any_candidate_tps` is **NOT** a
member of the v2.3 9-member terminal enum; the runner uses it only as
an internal symbolic signal that Phase A exhausted with admitted-pressure
validated → enter Phase B.

A `selected` outcome writes `*.calibration.result.json` plus the sibling
`*.calibration.summary.json` (the latter containing the
`calibration_result_sha256` field that downstream stages validate
against). Any non-`selected` outcome MUST stop the pipeline immediately.

### Three dispatch-time ISO-8601 timestamps (v2.3 NEW per-record)

Every calibration record now carries THREE distinct dispatch timestamps
so post-hoc auditors can tell pacer lag from concurrency saturation:

- `intended_dispatch_iso` — the deterministic schedule's intended
  dispatch time (computed at cell start from the seeded inverse-CDF
  schedule). New in v2.3.
- `scheduled_dispatch_iso` — the time the record was released by the
  dispatch pacer (post-pacer, pre-semaphore). Preserved from v2.2.1.
- `admitted_dispatch_iso` — the time the record crossed the concurrency
  semaphore (post-semaphore, pre-HTTP-send). Preserved from v2.2.1; this
  is the timestamp the admitted-pressure gate computes against.

### Concurrent-dispatch invariant (v2.3 microfix B — runtime requirement)

The Phase A / Phase B / bracket dispatcher MUST schedule
`asyncio.create_task` per scheduled time concurrently (non-blocking),
even when `probe_max_usd` / `total_max_usd` / `probe_max_calls` caps are
active. A sequential-await pattern that collapses nominal TPS below the
admitted-pressure floor is FORBIDDEN. Cap enforcement uses one of three
non-blocking mechanisms: (a) an `asyncio.Event` `total_max_usd` stop
sentinel set at `0.85 × $220 = $187` cumulative; (b) an O(1) advisory
admission check (`projected + 1_call_cost > cap` → break BEFORE
`asyncio.create_task`); (c) a probe-boundary cumulative-spend
re-evaluation. The deterministic pytest
`test_calibration_concurrent_dispatch_invariant` MUST FAIL if the
implementation regresses (this is enforced in
`tests/test_measure_max_output_tokens_sweep.py` and includes a
deliberate-regression sub-test as proof-of-coverage).

### `first_429_metadata` block (v2.3 NEW — single source of truth on every 429-producing probe)

Whenever a calibration probe records `n_429 ≥ 1`, the per-probe summary
populates a `first_429_metadata` block carrying:
`target_tps`, `admitted_peak_rpm_observed_last_30s`,
`admitted_steady_state_rpm_observed_last_30s`, `scheduled_dispatch_iso`,
`admitted_dispatch_iso`, `dispatch_backlog_ms_at_first_429`,
`retry_after_ms`, `retry_after`, `backlog_p50_ms_at_first_429`,
`backlog_p95_ms_at_first_429`, `cache_hit_ratio_at_first_429`,
`visible_output_tokens_of_preceding_success`, `prompt_cache_key_used`,
`source_corpus_sha256`, `system_prompt_sha256`,
`user_prompts_source_sha256`, `run_id_short`, `candidate_tps`,
`probe_phase`, `phase ∈ {"A", "B", "bracket"}`, `bracket_depth`
(null when phase ≠ "bracket"). This block is the durable single source
of truth Task 020 (retry-after-ms characterization) consumes with zero
new spend and Task 022 (customer 1-pager) cites with the PAYG-not-PTU
caveat.

---

## ⚠ DIAGNOSTIC ONLY — v2.1 status block (2026-05-30, preserved for audit)

> The block below documents the v2.1 protocol run that failed the smoke
> gate. Under v2.3 it is retained verbatim **for audit reference only**
> and **MUST NOT** be interpreted as evidence of Hypothesis I. All
> v2.1-stage artifacts (originally under
> `runs/20260529T160517Z_*` and
> `runs/20260529T165523Z_*_evidence.partial.*`) have been moved to the
> quarantine directory
> `runs/_quarantined_pre_v23_microfix/` (operator forensic retention,
> excluded from git scope by `.gitignore`) — they are kept on disk so
> that future investigators can reproduce the failure that motivated
> the v2.2.1 → v2.3 calibration design without those bytes entering
> any Task 019 PR.

> * **Stage 0 dry-run:** PASSED. 0 network calls, JSONL + summary written, schema complete.
> * **Stage 1 smoke
>   (`_quarantined_pre_v23_microfix/20260529T160517Z_..._smoke.jsonl[.summary.json]`)
>   — DIAGNOSTIC ONLY under v2.3:** completed 7/7 cells, warm criterion +
>   backlog OK in every cell, total USD ≈ $1.71. **But: zero real 429s
>   observed in every cell — including `max_output_tokens=16384`.** Spec
>   §"Stage 1 acceptance" requires the largest cell to observe ≥1 real
>   429 AND the smallest cell to observe zero 429s; this contrast was NOT
>   obtained at the v2.1-pinned `peak_ramp_tps = 0.33`. **GATE_VERDICT =
>   FAIL, reason = `no_429_in_largest_cell`.** See
>   `scripts/analyze_max_output_tokens_sweep.py --require-gate-pass` for
>   the reproducible verdict (exit 3).
> * **Stage 2 evidence — DIAGNOSTIC ONLY under v2.3:** NOT validly
>   promoted. An accidental evidence run was launched before the gate
>   verdict was confirmed; the orchestrator killed it after cells 256 and
>   512 completed (253 records, 0 429s). The partial JSONL is preserved
>   at
>   `runs/_quarantined_pre_v23_microfix/20260529T165523Z_..._evidence.jsonl`
>   (DIAGNOSTIC ONLY) and labelled diagnostic-only via the sidecar
>   manifest `*..._evidence.partial.summary.json`
>   (`partial: true, reason: "smoke_gate_failed_stage2_aborted"`). It
>   MUST NOT be analyzed as a completed Stage-2 evidence run.
> * **Implication (under v2.1):** Hypothesis I (the PTU reservation
>   conjecture proxied here) could not be evaluated from the v2.1 smoke
>   run because the largest cell never observed a single real 429 at
>   `peak_ramp_tps = 0.33`. v2.2.1 replaced the single fixed-TPS choice
>   with a calibration probe that walks `[0.33, 0.5, 0.75, 1.0, 1.5, 2.0,
>   3.0]`; v2.3 extends that with **Phase B
>   `[5.0, 8.0, 12.0, 16.0, 24.0, 32.0]`** to escalate-until-429 within
>   the conservative-but-useful $220 calibration cap, plus the
>   admitted-pressure gate so a no-429 outcome is informative ONLY when
>   the dispatcher actually pushed at the scheduled rate.

---

## What this run measures

For a fixed system prompt (8 233 chars, ~2 158 input tokens), fixed
user-prompt subset (10 of 30 entries — pinned indices), and fixed
reasoning effort (`low`), the runner sweeps `max_output_tokens` across
the cells `{256, 512, 1024, 2048, 4096, 8192, 16384}` against the
throttled deployment and records:

- realized `visible_output_tokens` (= `output_tokens − reasoning_tokens`),
- `cached_tokens`, `reasoning_tokens`, `cached_input_tokens`,
- `arrival_rpm_at_request_time` *at the moment the first 429 was observed for
  each cell* (`first_429_arrival_rpm_per_cell`),
- `cache_hit_ratio` per cell (non-prewarm successes only),
- per-call dispatch backlog (`scheduled_dispatch_cell_elapsed_ms` vs
  `admitted_dispatch_cell_elapsed_ms`),
- `prompt_cache_key_used` (one key per cell × run, namespaced).

The expected signal: **if Azure reserves quota at the cap**, the 429-onset RPM
should *fall* monotonically as `max_output_tokens` rises, while
`visible_output_tokens` should remain roughly flat (the actual reasoning
budget is bounded by `reasoning.effort=low`, not by the cap).

## Pinned controls (do not mutate without amending the spec)

| Field | Pin |
|---|---|
| Deployment | `ptu-deploy-throttled` (PAYG GlobalStandard, 60 K TPM) |
| `reasoning.effort` | `low` |
| `max_output_tokens` (sweep) | `[256, 512, 1024, 2048, 4096, 8192, 16384]` |
| Phase A concurrency (smoke + evidence too) | `runtime.concurrency: 96` |
| Phase B concurrency (Phase B + Phase-B-rooted bracket only) | `runtime.concurrency_phase_b: 512` |
| Peak ramp TPS | **calibration-selected** (Phase A grid value, OR Phase B grid value, OR bracket point) |
| Phase A candidate TPS grid | `[0.33, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]` (sorted ascending, ad-hoc values rejected at YAML load) |
| Phase B candidate TPS grid | `[5.0, 8.0, 12.0, 16.0, 24.0, 32.0]` (EXACT full grid; subset / ad-hoc / duplicate / re-ordered all rejected at YAML load) |
| Admitted-pressure floor ratio / window | `0.70` / `last 30 s` |
| Bracket max depth (geometric midpoint, same-phase only) | `3` |
| Prewarm TPS | `0.05` |
| Prewarm count | `12` per cell / per probe |
| Calls per cell (ramp) | 100 evidence / probe budget for calibration (Phase A `probe_max_calls: 600`; Phase B `probe_max_calls_phase_b: 6624`) |
| SDK `max_retries` | **0** (no retry budget — 429s captured verbatim) |
| `ptu_evidence` | `false` (PAYG, not PTU) |
| Dispatcher | `async_scheduled` (inverse-CDF arrival schedule, concurrent `asyncio.create_task`) |
| Pricing snapshot | `pricing/azure-openai-payg-2026-05.yaml` |
| Run-lock | `benchmarks/07-max-output-tokens-reservation/.runlock` (runtime-only artifact, gitignored under `benchmarks/*/.runlock`) |

## Spend ceilings (v2.3 — conservative-but-useful Task 019 cap)

| Stage | Per-probe cap | Stage hard ceiling | Preflight abort (0.9×) | Mid-run halt (0.85×) |
|---|---:|---:|---:|---:|
| Calibration (Phase A + Phase B + bracket) | $60 | $220 | $198 | $187 |
| Smoke | n/a | $50 | $45 | $42.50 |
| Evidence | n/a | $100 | $90 | $85 |
| Contingency (single stage rerun within lifecycle) | n/a | $30 | n/a | n/a |
| **Task 019 v2.3 total live cap** | | **$400** | | |

**Do not exhaust shared budget; cap is an accounting guardrail, not a
spend target.** The shared tenant has > $1,600 remaining of its
~$2,500/month — that figure is recorded for auditor traceability ONLY
and is NOT a Task 019 spend target. Other in-flight measurements
(Tasks 018, 020, 021, 022) draw from the same shared balance.

The deterministic conservative cost estimator (every dispatched call
billed at the per-call rate; NO 429-no-bill discount) projects:
- calibration two-phase exhaustive worst-case ≈ $187 (fits under $220
  with the mid-run stop_event firing at $187),
- smoke @ `peak_tps=3.0` ≈ $12.29; @ `peak_tps=8.0` ≈ $33 (fits under
  $45 preflight); @ `peak_tps=12.0` ≈ $49 ABORTS at preflight with
  `smoke_preflight_over_ceiling_narrow_sweep_or_file_new_spec`,
- evidence @ `peak_tps=3.0` ≈ $58.40 (fits under $90 preflight);
  @ `peak_tps=5.0` ≈ $97 ABORTS at preflight with
  `evidence_preflight_over_ceiling_narrow_sweep_or_file_new_spec`.

The estimator is the SOURCE OF TRUTH for preflight + mid-run-halt
projections; it actively ENFORCES the calibration cap and is NOT
permitted to silently waive it so Phase B can complete.

## Source-content SHAs (immutable; reused from Task 018 v2.4)

- `source_corpus_sha256` = `6a8ab5a3cb1ad3dace030a82ec1327496b39e65b77a627714a27c39017ca19e3`
- `user_prompts_source_sha256` = `45f4a95b5cfe208a3555683fe22c6eb74f1b0e46d621723b1ea1480713ab3087`
- `assembled_system_prompt_sha256` = `f8a74528164b22eed27d30a5fa089b1d0fbfb38440cc341b043c2cb24e9289c7`
- Assembled prompt length: **8 233** chars

The runner verifies all three SHAs at start-up and exits with code **7** on
mismatch. **No Task-019-specific corpus or prompt file is written; the
canonical sources under `benchmarks/04-spillover-simulation/` are reused
read-only.**

## Reviewer reproduction (zero spend — Stage 0 dry-run)

```bash
python -m scripts.measure_max_output_tokens_sweep \
  --experiment experiments/exp007_max_output_tokens_sweep.yaml \
  --dry-run --allow-dirty
```

Expected: exit `0`, $0.00 spend, 0 HTTP calls, JSONL with
`7 × (12 + 100) = 784` records (the exact ramp count per cell may be ±1 due
to SHA-seeded jitter), full `summary.json` block, including
`pinned_confounds_echo`, `tpm_feasibility`, `citations`, `run_lock_metadata`,
`first_429_arrival_rpm_per_cell`.

## Live runs (Azure-authenticated)

```bash
# Stage 0.5 — calibration (≤ $220 total, ≤ $60 per probe; two-phase A→B)
python -m scripts.measure_max_output_tokens_sweep \
  --experiment experiments/exp007_max_output_tokens_sweep.yaml \
  --stage calibration

# Stage 1 — smoke (≤ $50) — requires --calibration-result
python -m scripts.measure_max_output_tokens_sweep \
  --experiment experiments/exp007_max_output_tokens_sweep.yaml \
  --smoke \
  --calibration-result benchmarks/07-max-output-tokens-reservation/runs/<TS>_..._calibration.result.json

# Stage 2 — evidence (≤ $100 hard ceiling) — requires BOTH --calibration-result AND --smoke-summary
python -m scripts.measure_max_output_tokens_sweep \
  --experiment experiments/exp007_max_output_tokens_sweep.yaml \
  --calibration-result benchmarks/07-max-output-tokens-reservation/runs/<TS>_..._calibration.result.json \
  --smoke-summary    benchmarks/07-max-output-tokens-reservation/runs/<TS>_..._smoke.jsonl.summary.json
```

Requires `DefaultAzureCredential` resolution + the env vars
`AZURE_OPENAI_FOUNDRY_ENDPOINT` and
`AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED` populated to a throttled
GlobalStandard deployment.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | OK (or clean partial-run halt) |
| 1 | Runtime / TPM-feasibility / dataset / YAML |
| 2 | Auth / endpoint / reachability |
| 3 | Corpus / user-prompts missing |
| 4 | Run-lock already held |
| 5 | Pricing snapshot stale (>90 days) |
| 6 | USD preflight gate (>0.9 × hard ceiling) |
| 7 | SHA mismatch (prompt-identity contract) |
| 8 | Calibration terminal (any non-`selected` outcome in the v2.3 9-member enum) |
| 9 | Linkage validation (missing / mismatched / forbidden flags, including the 4 Phase B grid mutation reasons and the new `experiment_yaml_malformed`) |

## Schema (per-record, JSONL)

Each record in `runs/<timestamp>_<experiment_id>_<stage>.jsonl` carries:

- **Provenance:** `experiment_id`, `git_commit`, `dirty`, `timestamp_utc`,
  `api_version`, `deployment_used`, `auth_mode`, `model`.
- **Pinned-control echo:** `request_concurrency` (96 for Phase A / smoke /
  evidence; 512 for Phase B and Phase-B-rooted bracket probes),
  `request_peak_ramp_tps` (calibration-selected under v2.3),
  `request_prewarm_tps=0.05`, `request_reasoning_effort='low'`,
  `dispatcher_kind='async_scheduled'`, `sdk_max_retries=0`.
- **Cell coordinates:** `cell_idx`, `cell_max_output_tokens`,
  `max_output_tokens_sent`, `arrival_idx_within_cell`, `request_idx`,
  `is_prewarm`, `prompt_cache_key_used`.
- **Cadence (v2.3 — three dispatch ISO timestamps):**
  `intended_dispatch_iso` (v2.3 NEW — the schedule's intended dispatch
  time), `scheduled_dispatch_iso` (v2.2.1 preserved — post-pacer
  pre-semaphore), `admitted_dispatch_iso` (v2.2.1 preserved —
  post-semaphore pre-HTTP-send),
  `scheduled_dispatch_cell_elapsed_ms`,
  `admitted_dispatch_cell_elapsed_ms`, `dispatch_backlog_ms`,
  `in_flight_at_dispatch`, `arrival_rpm_at_request_time`,
  `relative_time_s`.
- **Outcome:** `failed`, `failure_reason`, `429_observed`, `rate_limited`,
  `retry_after_ms`, `retry_after`, `first_token_latency_ms`,
  `total_latency_ms`.
- **Tokens (canonical names):** `canonical_input_tokens`,
  `canonical_output_tokens`, `cached_tokens`, `reasoning_tokens`,
  `visible_output_tokens` (= `output − reasoning`),
  `request_estimated_processed_tokens`.
- **Content SHAs:** `system_prompt_sha256`, `user_prompts_source_sha256`,
  `source_corpus_sha256`.
- **Pricing:** `pricing_snapshot_path`, `dry_run`.
- **Raw upstream usage block:** `usage` (verbatim from the SDK).

`summary.json` adds: `pinned_confounds_echo`, `tpm_feasibility` (per-cell
projected TPM and admit/abort flag), `citations` (PTU doc, rate-limit doc,
prompt-caching doc, pricing snapshot URL), `run_lock_metadata`,
`first_429_arrival_rpm_per_cell`, `cell_summaries` (p50/p95/max
visible_output_tokens, reasoning_tokens, cached_tokens; cache_hit_ratio;
backlog_excessive flag; cache_not_warm flag), and global aggregates
(`backlog_excessive_any`, `cache_not_warm_any`,
`max_in_flight_observed_run`).

**v2.3 calibration result additions** (relative to v2.2.1):
- top-level `candidate_tps_grid_phase_b` echo (the pinned six-member
  list — never the result-file's own copy is treated as authoritative
  for downstream validation; the pinned constant is the source of
  truth),
- top-level `calibration_probe_max_calls_phase_a` and
  `calibration_probe_max_calls_phase_b`,
- per-probe `halt_reason` (e.g. `probe_max_calls_hit`,
  `probe_max_usd_hit`, `total_max_usd_stop_event_set`,
  `first_429_observed`),
- per-probe `first_429_metadata` block (populated on every probe with
  `n_429 ≥ 1`),
- `bracket_trace: [{depth, t_low, t_high, t_bracket, largest_n_429,
  smallest_n_429, eligibility_outcomes, ...}, ...]` (when bracket
  search executed),
- on any `*_inconclusive_*` outcome: `inconclusive_probe_role ∈
  {"largest", "smallest_control"}` + `inconclusive_at_candidate_tps`
  + optional `inconclusive_reason_detail`.

**v2.2.1-preserved smoke / evidence `summary.json` additions:**
`selected_peak_tps`, `calibration_result_path`,
`calibration_result_sha256`, `calibration_run_id_short`, plus
(evidence only) `smoke_summary_path`, `smoke_summary_sha256`. The
smoke summary's own sha256 is stored in a **sibling `.sha256` text
sidecar** (NOT inside the JSON, to preserve a clean hash-of-bytes
round-trip).

## Citations (accessed 2026-05-29)

- Azure OpenAI rate-limit & quota docs:
  <https://learn.microsoft.com/en-us/azure/ai-services/openai/how-to/quota>
- Azure OpenAI **Provisioned Throughput Units (PTU)** concept (referenced
  *only* to mark the absence of PTU evidence in this run):
  <https://learn.microsoft.com/en-us/azure/ai-services/openai/concepts/provisioned-throughput>
- Azure OpenAI prompt caching:
  <https://learn.microsoft.com/en-us/azure/ai-services/openai/how-to/prompt-caching>
- PAYG pricing snapshot: `pricing/azure-openai-payg-2026-05.yaml` (canonical
  Retail Prices API, archived 2026-05-19).

## Limits & honesty

1. **Not PTU evidence.** A PAYG GlobalStandard deployment's throttled-quota
   admission layer is **not** the same as Azure's PTU reservation pool. Any
   reservation-shape observed here is a *proxy* for the PTU hypothesis at
   best.
2. **Per-cell N=100 ramp calls** is sufficient to detect a *monotone*
   reservation signal but not to attribute small RPM-vs-TPM offsets to any
   particular root cause. Confidence intervals on `first_429_arrival_rpm` are
   wide for cells where 429 onset is late (the cell may not reach 429 at all
   within its ramp budget).
3. **Reasoning effort is fixed at `low`** so `visible_output_tokens` should
   *not* track `max_output_tokens` (the cap is a ceiling, not a target). If
   `visible_output` *does* track the cap, that itself is a non-trivial
   finding — annotate, do not silently re-interpret.
4. **Cache-warm criterion** (`≥ 3 of last 6 prewarm with cached_tokens > 0`)
   may fail on a fresh deployment alias. Cells with `cache_not_warm=True`
   are *excluded* from the 429-onset analysis but their JSONL records are
   preserved verbatim for auditing.
5. **Admitted-pressure validation (v2.3 NEW).** A no-429 probe outcome is
   informative ONLY when the dispatcher actually pushed at ≥ 70 % of the
   scheduled rate at the deployment over the last 30 s. Otherwise the
   probe is honestly labelled
   `calibration_probe_inconclusive_admitted_pressure_insufficient` and the
   pipeline halts.
6. **Calibration honesty (v2.3).** A non-`selected` calibration outcome
   (any of the eight terminal failure members of the 9-member enum)
   is a *first-class* result — not a transient flake. Operators MUST stop
   the pipeline (exit `8`) and document the outcome rather than weakening
   the predeclared grids, lowering the warm criterion, raising the
   conservative-but-useful caps, or re-running with ad-hoc parameters.
   The calibration cost estimator deliberately does NOT discount for the
   PAYG 429-no-bill quirk: a $220 calibration budget is treated as a
   *real* $220 of risk.

## Related

- Internal Task 019 max-output-tokens-reservation spec (private working
  tree) v2.3 — read-only
  spec (supersedes v2.2.1 / v2.1 in full).
- `experiments/exp007_max_output_tokens_sweep.yaml` — pinned YAML config
  (now includes the `calibration:` block, the v2.3 Phase B grid, the
  admitted-pressure gate constants, the bracket depth cap, and the
  `concurrency_phase_b: 512` runtime override).
- `scripts/measure_max_output_tokens_sweep.py` — runner (supports
  `--stage calibration`, `--calibration-result`, `--smoke-summary`;
  enforces the 9-member outcome enum, the Phase B exact-full-grid
  contract, the admitted-pressure gate, the bracket search, and the
  concurrent-dispatch invariant).
- `tests/test_measure_max_output_tokens_sweep.py` — deterministic test
  suite (all pure / dry-run / stubbed; covers the v2.3 additions plus
  the v2.2.1 invariants).
- Task 018 v2.4 (`benchmarks/06-cache-key-bucketing/`) — the dispatcher,
  prompt-identity contract, and warm criterion were forked from there.
