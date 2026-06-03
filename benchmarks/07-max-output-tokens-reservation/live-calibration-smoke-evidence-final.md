# Task 019 v2.3 — Live Calibration → Smoke → Evidence Final Report

> **PAYG, not PTU.** All measurements summarized here were collected against
> the throttled PAYG GlobalStandard deployment `ptu-deploy-throttled` (60 K TPM /
> 600 RPM, `ptu_evidence: false`). This document is a *proxy* artifact for
> Hypothesis I from `docs/04-hypotheses.md`; it does **not** observe an Azure
> PTU reservation pool directly and must not be re-cited as PTU evidence.

> **Terminal outcome.** This run completed Stage 0.5 (calibration) with
> `outcome = "selected"` at `selected_peak_tps ≈ 0.4747`, then **terminated
> at the smoke stage's TPM-feasibility preflight gate (exit `1`)** before
> any smoke HTTP call was dispatched. Per Task 019 v2.3 §"Limits & honesty"
> item 6, a non-promotable gate verdict is a *first-class* result: this
> document records it honestly without override, weakening, or invented
> data. Stage 2 (evidence) was therefore never invoked; the analyzer
> (`scripts/analyze_max_output_tokens_sweep.py`) was not run because it
> requires a smoke- or evidence-stage `*.summary.json` (the calibration
> result file is not its consumable input).

---

## 1. Run timeline

| Stage | Outcome | Started (UTC) | Completed (UTC) | Cost (USD) |
|---|---|---|---|---:|
| Stage 0.5 — Calibration (Phase A + bracket, depth 3) | `selected` | 2026-05-30T13:51:25Z | 2026-05-30T14:46:43Z | `$3.0619` realized / `$5.823` committed¹ |
| Stage 1 — Smoke (preflight only) | `TPM_FEASIBILITY_ABORT` (exit `1`) | 2026-05-31T00:35:13Z | 2026-05-31T00:35:13Z | `$0` (no HTTP dispatched) |
| Stage 2 — Evidence | NOT INVOKED (gated by Stage 1) | n/a | n/a | `$0` |
| **Task 019 v2.3 live spend total** | | | | **`$3.0619`** (well below `$400` total cap) |

¹ "committed" is the spec's pessimistic preflight reservation (the
deterministic-conservative estimator's per-probe USD cap before each
probe started). Realized spend is the source-of-truth dollar number;
committed is retained for auditor traceability only.

---

## 2. Calibration boundary (`outcome = "selected"`)

The two-phase Stage 0.5 calibration terminated with `outcome: "selected"`
via the Phase-A-rooted bracket search at depth 3, binding
`selected_peak_tps = 0.47469318448182934`. Phase B was **not** entered
(Phase A's `0.5` candidate produced the contrast trigger before grid
exhaustion, so the runner proceeded directly into the v2.3 bracket
search between `T_low = 0.406201920231798` and `T_high = 0.5`).

### Bracket trace (geometric midpoints, depth 3 max, same-phase only)

| Depth | `t_low` | `t_high` | `t_bracket` (= √(low×high)) | Largest 16 384 cell 429s | Smallest 256 control 429s | Outcome |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 0.330000 | 0.500000 | **0.406202** | 0 | n/a (largest not yet 429-positive) | `recurse_upper` |
| 2 | 0.406202 | 0.500000 | **0.450667** | 0 | n/a (largest not yet 429-positive) | `recurse_upper` |
| 3 | 0.450667 | 0.500000 | **0.474693** | **1** | **0** | **`selected`** |

### Linkage hashes (durable inter-stage references)

| Field | Value |
|---|---|
| `calibration_result_path` | `benchmarks/07-max-output-tokens-reservation/runs/20260530T135125Z_exp007_max_output_tokens_sweep_calibration.result.json` |
| `calibration_result_sha256` | `92126b46ab4320ba38566229292b3b89922d7d58e42a97c43224d67e6a75db81` |
| `calibration_run_id_short` | `1bf76b2b` |
| `calibration_outcome` | `selected` |
| `selected_via` | `bracket_search` |
| `selected_at_phase` | `bracket` |
| `selected_bracket_root_phase` | `A` |
| `selected_at_bracket_depth` | `3` |
| `selected_peak_tps` | `0.47469318448182934` |
| Schema version | `task019.v2.3.calibration_result` |

### Per-probe artifact trail (7 probes; all warm + backlog OK + admitted-pressure PASS)

| Phase / depth | Role | `candidate_tps` | n records (incl. 12 prewarm) | n 429 | Cache-hit ratio (steady-state) | Mean visible output | `prompt_cache_key` |
|---|---|---:|---:|---:|---:|---:|---|
| A | largest (16384) | 0.33000 | 71 | 0 | 0.697 | 459 | `task019_calib_1bf76b2b_cell16384_tps0330` |
| A | largest (16384) | 0.50000 | 42 | **1** (first-429 ⇒ early stop) | 0.822 | 411 | `task019_calib_1bf76b2b_cell16384_tps0500` |
| A | smallest_control (256) | 0.50000 | 42 | **1** (first-429 ⇒ early stop) | 0.761 | 211 | `task019_calib_1bf76b2b_cell00256_tps0500` |
| bracket d=1 | largest (16384) | 0.40620 | 85 | 0 | 0.846 | 441 | `…cell16384_tps0406_bracket1` |
| bracket d=2 | largest (16384) | 0.45067 | 93 | 0 | 0.883 | 462 | `…cell16384_tps0451_bracket2` |
| bracket d=3 | largest (16384) | 0.47469 | 71 | **1** (first-429 ⇒ early stop) | 0.883 | 440 | `…cell16384_tps0475_bracket3` |
| bracket d=3 | smallest_control (256) | 0.47469 | 97 | **0** | 0.852 | 226 | `…cell00256_tps0475_bracket3` |

> Cache-hit ratios and visible-output means in the four bracket rows above
> are taken directly from
> `runs/20260530T135125Z_exp007_max_output_tokens_sweep_calibration.result.json`
> `probes[].cache_hit_ratio_steady_state` and
> `probes[].visible_output_mean_per_probe` (the canonical per-probe
> fields written by the runner at probe completion).

### 429 observations (3 real 429s across 501 calibration records)

| `phase` / depth | `candidate_tps` | `cell_max_output_tokens` | `admitted_peak_rpm_observed_last_30s` | `dispatch_backlog_ms_at_first_429` | `retry_after_ms` | `cache_hit_ratio_at_first_429` | `visible_output_tokens_of_preceding_success` |
|---|---:|---:|---:|---:|---:|---:|---:|
| A | 0.50000 | 16384 (largest) | 28.0 | 0 | 3 | 0.822 | 287 |
| A | 0.50000 | 256 (smallest control) | 28.0 | 0 | 3 | 0.761 | 256 |
| bracket d=3 | 0.47469 | 16384 (largest) | 28.0 | 0 | 2 | 0.883 | 654 |

The bracket-depth-3 largest-cell 429 is the v2.3 contrast trigger: at
`candidate_tps = 0.47469` the largest cell saturated while the same-TPS
smallest-cell control probe observed **0 / 97** 429s with a 0.852
steady-state cache-hit ratio — the cleanest available signal-vs-control
separation under the v2.3 grid + bracket bounds.

---

## 3. Admitted-pressure validation (v2.3 NEW — blocking gate)

The admitted-pressure gate (`admitted_peak_rpm_observed_last_30s ≥
0.70 × candidate_tps × 60`, window `30 s`) passed on **every** probe
(or was bypassed by a real 429, the spec's documented short-circuit).

| Probe | Target peak RPM | Observed peak RPM (last 30 s) | Floor (0.70×target) | `admitted_pressure_passed` | `skipped_due_to_429` |
|---|---:|---:|---:|---|---|
| A / largest / 0.33 | 19.80 | 18.00 | 13.86 | ✅ | no |
| A / largest / 0.50 | 30.00 | 28.00 | 21.00 | ✅ | yes (real 429 observed) |
| A / smallest_control / 0.50 | 30.00 | 28.00 | 21.00 | ✅ | yes (real 429 observed) |
| bracket d=1 / largest / 0.406 | 24.37 | 24.00 | 17.06 | ✅ | no |
| bracket d=2 / largest / 0.451 | 27.04 | 26.00 | 18.93 | ✅ | no |
| bracket d=3 / largest / 0.475 | 28.48 | 28.00 | 19.94 | ✅ | yes (real 429 observed) |
| bracket d=3 / smallest_control / 0.475 | 28.48 | 28.00 | 19.94 | ✅ | no |

No probe was retried under `_retry1_admp` — the admitted-pressure gate
was satisfied on every initial attempt, so the v2.3 NEW terminal
`calibration_probe_inconclusive_admitted_pressure_insufficient` did not
fire.

---

## 4. Stage 1 terminal verdict — `TPM_FEASIBILITY_ABORT` (exit `1`)

The smoke runner aborted **before any HTTP dispatch** at the v2.1-pinned
TPM-feasibility preflight gate, which the v2.3 protocol preserves
verbatim for smoke and evidence stages.

### Runner output (verbatim)

```
2026-05-31T00:35:13Z INFO scripts.measure_max_output_tokens_sweep
  BUDGET_PREFLIGHT stage=smoke cells=7 calls_per_cell~=43
  projected_usd=2.7393 hard_ceiling=50.0000
  preflight_threshold=45.0000 midrun_threshold=42.5000
  estimator=deterministic_conservative

2026-05-31T00:35:13Z INFO scripts.measure_max_output_tokens_sweep
  TPM_FEASIBILITY_PREFLIGHT smallest_mo=256 smallest_tpm=68754.6
  lower_threshold=51000.0 largest_mo=16384 largest_tpm=528105.7
  upper_threshold=75000.0 quota=60000 peak_tps=0.475 base_tokens=2158

2026-05-31T00:35:13Z ERROR scripts.measure_max_output_tokens_sweep
  TPM_FEASIBILITY_ABORT smallest cell (max_output_tokens=256) projects
  68754.6 TPM > 0.85 × quota=60000 (= 51000.0); the smallest cell would
  429 at peak ramp, eliminating signal contrast. Reduce
  runtime.peak_ramp_tps OR raise the smallest sweep cell. Task 019 v2.1
  pinned peak=0.33.

EXIT=1
```

### Gate arithmetic (canonical formula, pinned by v2.1 blocker #4)

```
projected_tpm(cell, tps) = 60 × tps × (base_prompt_tokens_for_gate + cell.max_output_tokens)
base_prompt_tokens_for_gate = max(assembled_system_prompt_tokens=2058, target_system_prompt_tokens=2000) + 100 = 2158
```

At `peak_tps = 0.47469`:

| Cell `max_output_tokens` | `projected_tpm` | vs. `0.85 × quota` (`51 000`) | vs. `1.25 × quota` (`75 000`) | Gate evaluation |
|---:|---:|---|---|---|
| 256 (smallest) | **68 755** | **> 51 000 by +17 755 (+34.8 %)** | n/a | **smallest-cell fail → ABORT** |
| 16 384 (largest) | 528 106 | n/a | > 75 000 (≈ 7.04× the threshold) | largest-cell PASS (would have 429'd as required) |

The gate is satisfied iff **both** of these hold:

- (a) smallest-cell projected TPM `≤ 0.85 × quota` (so the smallest cell
  has signal *contrast* — must NOT 429 at peak ramp), and
- (b) largest-cell projected TPM `≥ 1.25 × quota` (so the largest cell
  *does* saturate at peak ramp — otherwise no 429-onset to measure).

At `selected_peak_tps = 0.47469`, condition (a) fails by `+17 755 TPM`
(`+34.8 %` over the threshold). This is *projection*-based, not
empirical — see §6 for the calibration's empirical contradiction.

### Spec-correct interpretation

Per Task 019 v2.3 §"Limits & honesty" item 6: a non-`selected`
calibration outcome OR a hard gate abort is **a first-class result, not
a transient flake**. Operators MUST stop and document rather than:

- override `--peak-ramp-tps` (forbidden, exit `9`,
  `peak_ramp_tps_override_forbidden_use_calibration_result`),
- weaken `0.85` or `1.25` thresholds,
- raise the `$220` calibration ceiling, or
- re-run with ad-hoc parameters.

The v2.3 spec did not introduce a TPM-feasibility-gate revision when it
added the bracket search — the gate evaluates the
**calibration-selected** TPS against the v2.1 pinned `0.85` / `1.25`
thresholds verbatim. The terminal exit on this run is the spec
operating as designed.

---

## 5. Cost & admitted-pressure summary (PAYG / PTU dual frame)

| Lens | Calibration realized | Smoke realized | Evidence realized | Total |
|---|---:|---:|---:|---:|
| **PAYG cost (USD, pricing snapshot `pricing/azure-openai-payg-2026-05.yaml`)** | `$3.0619` | `$0.0000` | `$0.0000` | **`$3.0619`** |
| **PAYG hard ceiling** | `$220` (per probe `$60`) | `$50` | `$100` | `$400` (task total) |
| **PAYG headroom remaining** | `$216.94` (98.6 %) | `$50` (100 %) | `$100` (100 %) | `$396.94` (99.2 %) |
| **PTU framing (this proxy run)** | not applicable — `ptu_evidence: false`; PAYG GlobalStandard quota debit (60 K TPM throttled), no PTU slot was instantiated or billed | — | — | — |

**Admitted dispatch pressure delivered** (sum across all calibration
probes): the dispatcher successfully drove the deployment up to a peak
admitted-dispatch rate of **28 RPM** at the bracket-3 selection point,
which the deployment's throttle layer met with a real 429 on the
largest cell. The 28 RPM figure is the canonical artifact field
`probes[5].admitted_pressure.admitted_peak_rpm_observed_last_30s = 28.0`,
which the runner stores **already normalized to requests/minute**
(computed as `count_in_trailing_30s × 60 / 30`, i.e. window-derived
RPM, NOT a raw 30 s count — the underlying 30 s admitted count was 14,
which extrapolates to the same 28 RPM). The earlier wording "28
admitted requests / 30 s window (= 56 RPM)" was wrong on both counts
and has been corrected here. The admitted-pressure floor
(`0.70 × target_peak_rpm`) was met or exceeded on every probe — the
dispatcher is **not** the limiting factor on this host (Mac Mini local,
`hyeons-Mac-mini-4.local`).

---

## 6. Interpretation — the calibration-vs-preflight inconsistency this run surfaces

The v2.3 calibration **empirically validated contrast** at
`peak_tps = 0.47469` against a real Azure throttle on the
`ptu-deploy-throttled` deployment:

- the largest cell (16 384) observed `n_429 = 1` on a 71-record probe
  with cache-hit ratio `0.883`, and
- the smallest cell control (256) observed `n_429 = 0` on a 97-record
  probe with cache-hit ratio `0.852`.

This is a real, prompt-cache-warm, admitted-pressure-validated
saturation contrast — the strongest one available under the v2.3 grid +
bracket bounds.

The smoke-stage TPM-feasibility preflight, however, uses a
**cold-cache** projection that does not consume realized
`cached_tokens`. At `peak_tps = 0.47469`:

- the *projected* smallest-cell load is `68 755 TPM` (`> 51 000`), but
- the *empirically observed* smallest-cell behaviour at the same TPS
  was `0 / 97` 429s with 85.2 % cache hits — i.e. the effective
  uncached input tokens were a small fraction of the cold-cache
  projection, and the deployment had headroom.

So this run's terminal outcome is **not** an experimental refutation of
Hypothesis I — Hypothesis I was never reached at the proxy level
because the gate that ladders calibration into smoke evaluates a
projection that contradicts the calibration's empirical observation.
**This is information about the gate's contract**, not about
`max_output_tokens` reservation semantics.

The cleanest follow-on actions are documented in §8.

---

## 7. Limitations of this run

1. **Not PTU evidence.** `metadata.ptu_evidence: false` is enforced at
   YAML load; this run measures behaviour of a PAYG GlobalStandard
   throttled-quota deployment (60 K TPM, 600 RPM). PTU-specific
   mechanisms (slot routing, expected-utilization tiering,
   capacity-correlated cache effects) are NOT observed here and must
   not be inferred from this artifact (Task 022 cites this benchmark
   only with the PAYG-not-PTU caveat).

2. **No smoke or evidence data exists.** Stage 1 aborted at preflight
   before any HTTP call. No `*_smoke.jsonl`, no `*_smoke.jsonl.summary.json`,
   no `*_smoke.jsonl.summary.json.sha256` sidecar, no
   `*_evidence.*` artifact was created on this run. The analyzer
   (`scripts/analyze_max_output_tokens_sweep.py`) was not invoked
   because its sole consumable input is a smoke/evidence summary.

3. **Hypothesis I is undetermined on this proxy at this prompt
   identity.** The 7-cell `max_output_tokens` reservation sweep was
   never executed against `ptu-deploy-throttled` under the v2.3 protocol
   because the TPM-feasibility preflight rejects every
   `selected_peak_tps > 0.33`. The v2.1 historical run at `0.33`
   completed but observed zero 429s in the largest cell (preserved
   `runs/_quarantined_pre_v23_microfix/` — DIAGNOSTIC ONLY). The v2.3
   bracket search succeeded only at TPS values that the smoke
   preflight rejects.

4. **The TPS-vs-projection inconsistency is a contract finding, not
   measurement evidence.** The smoke gate's cold-cache projection at
   `selected_peak_tps = 0.47469` predicts the smallest cell would
   saturate (`68 755 TPM > 0.85 × 60 K`); the calibration's empirical
   probe at the SAME TPS observed 0 saturations on the smallest cell
   with 85.2 % cache hits. The gate is doing what the spec tells it to
   do; the spec's projection has not been reconciled with the v2.3
   bracket search's empirical contrast outcome. Resolving this is a
   *spec revision* concern, not a runtime override.

5. **Reasoning effort fixed at `low`.** Visible output means observed
   here (~228 tokens at cell=256, ~440-460 tokens at cell≥16384) are
   `reasoning.effort=low` behaviour only. Higher reasoning effort would
   shift both the per-cell visible output curve AND the
   admission-reservation onset; that is not in this run's scope.

6. **N=1 per 429 observation in the bracket.** The v2.3 early-stop-on-
   first-429 contract halts each largest-cell probe at the first real
   429 to preserve shared budget. Per-cell 429-onset RPM at the
   selection point therefore has N=1; confidence intervals on the RPM
   estimate are wide. The cleanest signal would come from a
   smoke/evidence run that did the full ramp at the selected TPS — but
   that run cannot be initiated under the current gate contract.

---

## 8. Recommended next actions (no runtime override of v2.3)

The cleanest follow-on actions, in order of cost and spec-impact:

1. **Spec-revision RFC: reconcile smoke/evidence TPM-feasibility
   preflight with v2.3 bracket calibration outcomes.** Options the
   methodology-auditor / strategy-consultant should weigh:

   - (a) **Relax (i.e. raise)** the smallest-cell smoke/evidence
     TPM-feasibility ceiling from the v2.1-pinned strict
     `≤ 0.85 × quota` to a more permissive bound (e.g. `≤ 1.15 ×
     quota`) *when* the calibration result shows `outcome = "selected"`
     AND the smallest-cell control probe at `selected_peak_tps`
     observed `n_429 = 0` AND its `cache_hit_ratio_steady_state ≥
     <threshold>`. Note the verb: `1.15 × quota > 0.85 × quota`, so
     this is a *raise/relaxation* of the ceiling, not a lowering — the
     earlier "Lower … from `0.85 × quota` to `1.15 × quota`" wording
     was internally inconsistent and is corrected here. At
     `selected_peak_tps = 0.47469` the smallest cell projects
     `68 755 TPM`, which is `> 51 000` (`0.85 × quota`) but `< 69 000`
     (`1.15 × quota`), so this specific relaxation would have unblocked
     this run. The `<threshold>` choice for the cache-hit guard is
     itself an RFC subject and must be justified empirically before
     adoption — it cannot be picked to fit a single run. Concretely,
     this run's smallest-cell control at `selected_peak_tps = 0.47469`
     measured `cache_hit_ratio_steady_state = 0.852` (canonical
     `probes[6].cache_hit_ratio_steady_state =
     0.8517199126857254`), so a `0.90` clause would **still reject
     this run**; only a clause set at `≤ 0.85` would have allowed
     promotion, and no in-repo evidence yet justifies that level. The
     RFC should propose both the ceiling-relaxation level AND the
     cache-hit threshold from a *distribution* of warm
     bracket-selected probes, not a single observation.
   - (b) Make the smoke/evidence preflight consume the calibration
     result's per-cell `cache_hit_ratio_steady_state` and discount
     `cached_tokens` from the projected TPM. This is the "use the
     empirical observation we already paid $3.06 for" path.
   - (c) Tighten the v2.3 calibration probe to *also* enforce
     `peak_tps ≤ smallest-cell-feasible-cold-cache TPS` (`0.33` at
     current pins) — i.e. reject any bracket midpoint above `0.33`.
     This would have caused calibration to terminate with
     `no_largest_cell_429_at_any_phase_b_candidate_tps_endpoint_not_throttling`
     instead of `selected`, which is honest but discards the empirical
     contrast finding.
   - (d) No-op (accept the contract as written and document the
     deployment + prompt identity as not testable for Hypothesis I
     under v2.3 — this is what this report does today).

   Recommendation: option (b) is the most measurement-honest path
   because it lets the preflight learn from a calibration run that
   already paid for the empirical data. Option (a) (relax the ceiling
   from `≤ 0.85 × quota` to a more permissive bound such as `≤ 1.15
   × quota`, gated on a cache-hit floor) is the lowest-spec-churn path
   *in principle*, but it does not unblock this run unless the RFC-
   selected `cache_hit_ratio_steady_state` floor is set `≤ 0.85` (this
   run's smallest-cell control measured `0.852`); a commonly-quoted
   `0.90` floor would still reject this run. Option (c) discards
   genuine signal. Option (d) is what we ship today.

2. **If a spec revision selects (a) or (b), re-run calibration → smoke
   → evidence end-to-end** under the updated v2.4 protocol. Budget
   estimate at `selected_peak_tps ≈ 0.475` and the deterministic
   conservative estimator (`$0.009 / dispatched call`):
   `calibration ≈ $3-6` (this run already paid `$3.06` — fresh run
   needed because of the v2.3 `calibration_max_age_hours: 24` window),
   `smoke ≈ $3-4` (projection at TPS 0.475 ≈ `$2.74`, well below the
   `$45` preflight ceiling on the USD axis), `evidence ≈ $9-15`
   (projection scales as `7 × calls_per_cell × $0.009`). Total
   end-to-end re-run cost: ≈ `$15-25`, comfortably under the
   `$400` task total cap.

3. **Out of scope for this report (Task 020):** the v2.3 calibration
   result this run produced already populates the durable
   `first_429_metadata` block on every 429-producing probe — Task 020
   (`retry-after-ms` characterization) can re-aggregate from
   `runs/20260530T135125Z_..._calibration.jsonl` with **zero new
   spend**. The three 429 records carry `retry_after_ms ∈ {2, 3, 3}`.

---

## 9. Artefact index (read-only outputs of this run)

All four artefacts listed below — the three calibration files written
by the runner on 2026-05-30 and this report — are **committed together
in this same change** (they were untracked on disk prior to this commit;
"durable" in §3 refers to their role as inter-stage linkage anchors, not
to prior commit status).

| Path | Bytes | Schema / format identifier | Note |
|---|---:|---|---|
| `runs/20260530T135125Z_exp007_max_output_tokens_sweep_calibration.result.json` | 17 105 | `schema_version = "task019.v2.3.calibration_result"` (top-level field present in this file) | Stage 0.5 result; `outcome: selected`; `selected_peak_tps: 0.47469318448182934`; sha256 `92126b46…b81` |
| `runs/20260530T135125Z_exp007_max_output_tokens_sweep_calibration.summary.json` | 1 096 | `schema_version = "task019.v2.3.calibration_summary"` (top-level field present in this file) | Sibling sha256-bearing summary (calibration `result_sha256` lives ONLY here, per the no-self-referential-hash rule) |
| `runs/20260530T135125Z_exp007_max_output_tokens_sweep_calibration.jsonl` | 1 138 674 | Runner record format ≈ `task019.v2.3.calibration_record` (per-line records do **not** carry a `schema_version` field; the identifier is a *runner contract label*, not an artifact-level attribute — verified `0/501` records contain `schema_version`) | 501 records (≤ 12 prewarm per probe + admitted ramp). 3 records with `429_observed: true`. |
| Stage 1 / Stage 2 artefacts | — | — | **None created** (Stage 1 aborted at preflight, Stage 2 not invoked) |
| `runs/_quarantined_pre_v23_microfix/` | — | — | v2.1 failed-smoke + killed-evidence artefacts (preserved on disk, gitignored, **DIAGNOSTIC ONLY**, must not be re-cited as v2.3 evidence) |

---

## 10. Pricing, citations, provenance

- Pricing snapshot: `pricing/azure-openai-payg-2026-05.yaml`
  (`accessed_date: 2026-05-19`, well within the 90-day freshness window
  the runner enforces at preflight exit `5`).
- Pricing source URL:
  <https://azure.microsoft.com/en-us/pricing/details/azure-openai/>
- Azure OpenAI rate-limit & quota documentation:
  <https://learn.microsoft.com/en-us/azure/ai-services/openai/how-to/quota>
  (accessed 2026-05-29)
- Azure OpenAI Provisioned Throughput Units (PTU) concept doc, cited
  ONLY to mark the absence of PTU evidence in this run:
  <https://learn.microsoft.com/en-us/azure/ai-services/openai/concepts/provisioned-throughput>
  (accessed 2026-05-29)
- Azure OpenAI prompt caching:
  <https://learn.microsoft.com/en-us/azure/ai-services/openai/how-to/prompt-caching>
  (accessed 2026-05-29)
- Prompt identity (immutable; reused from Task 018 v2.4):
  - `source_corpus_sha256` = `6a8ab5a3cb1ad3dace030a82ec1327496b39e65b77a627714a27c39017ca19e3`
  - `user_prompts_source_sha256` = `45f4a95b5cfe208a3555683fe22c6eb74f1b0e46d621723b1ea1480713ab3087`
  - `assembled_system_prompt_sha256` = `f8a74528164b22eed27d30a5fa089b1d0fbfb38440cc341b043c2cb24e9289c7`
- Deployment: `ptu-deploy-throttled` (PAYG GlobalStandard, 60 K TPM /
  600 RPM), resolved via env vars `AZURE_OPENAI_FOUNDRY_ENDPOINT` and
  `AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED` (values not printed).
- Auth: Entra ID via `DefaultAzureCredential` (Foundry v1 endpoint;
  `api_version: preview`).
- Git branch: `feature/task019-max-output-tokens-reservation`.
- Host: `hyeons-Mac-mini-4.local` (local Mac Mini operator session).
- Run-lock: clean at write time (stale lock from PID `3882` — which was
  no longer running per `ps -p 3882` — was removed before this report's
  smoke attempt; only the `.runlock` file was removed; no other run
  state was touched). **Side observation (out-of-scope for this run,
  potential follow-on bug for the runner):** the smoke runner acquired
  a fresh `.runlock` (PID `38681`) immediately before the
  TPM-feasibility preflight gate fired `EXIT=1` and did NOT release the
  lock on its way out — i.e. preflight-gate aborts leak the lock file.
  Verified `ps -p 38681` empty after exit; the orphan `.runlock` was
  removed during cleanup. The release path appears to be tied to the
  post-HTTP `finally` block rather than the pre-HTTP gate exit path.
  Fixing this is OUT OF SCOPE for the live-run task; surfacing as a
  small runner bug worth a separate PR.
  *Follow-on task to file*: "release `.runlock` on pre-HTTP gate aborts in `scripts/measure_max_output_tokens_sweep.py`" — owner TBD, no spend.

---

## 11. Status for review

- **Calibration:** complete (`selected`), artefacts on disk under
  `runs/20260530T135125Z_…`. Linkable via the sha256 `92126b46…b81`.
- **Smoke:** terminal (`TPM_FEASIBILITY_ABORT`, exit `1`). No artefacts
  produced — by design, the runner refuses to write `*.partial.*` files
  for a preflight gate abort (the abort happens before `runs/*.jsonl`
  opens for writing).
- **Evidence:** not invoked.
- **Analyzer:** not invoked — requires a smoke/evidence summary input.
- **Documentation + artefact commit scope (all in this single commit):**
  - this report
    (`benchmarks/07-max-output-tokens-reservation/live-calibration-smoke-evidence-final.md`)
    added,
  - `CHANGELOG.md` updated with a Task 019 live-run entry under
    `[Unreleased]`,
  - the three calibration artefacts produced by the runner on
    2026-05-30 are **committed in this same change** (they were
    untracked on disk before this commit, not previously versioned):
    - `runs/20260530T135125Z_exp007_max_output_tokens_sweep_calibration.result.json`,
    - `runs/20260530T135125Z_exp007_max_output_tokens_sweep_calibration.summary.json`,
    - `runs/20260530T135125Z_exp007_max_output_tokens_sweep_calibration.jsonl`,
  - `benchmarks/07-max-output-tokens-reservation/analysis.md` left
    unchanged (it documents the protocol; the live-run terminal outcome
    is documented here).
- **Code changes:** none. No runtime, gate, threshold, or spec mutation
  was made on the working tree to enable promotion; all changes are
  documentation-only.

This report is ready for review. **No commit or push has been performed
by this worker; the operator owns the commit step.**
