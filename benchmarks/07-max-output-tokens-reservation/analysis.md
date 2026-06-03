# Analysis — Benchmark 07 / Task 019 v2.3

> **PAYG, not PTU.** All evidence summarized here was collected against the
> *throttled* PAYG GlobalStandard deployment `ptu-deploy-throttled` (60 K TPM,
> `ptu_evidence=false`). This is a *proxy* experiment for Hypothesis I from
> `docs/04-hypotheses.md` — it does *not* observe Azure's PTU reservation
> pool directly. Treat results as behavioural evidence about the *throttled
> PAYG admission layer's* response to `max_output_tokens`, and do not
> reframe as PTU evidence under any circumstance.

## Protocol (v2.3)

v2.3 supersedes v2.2.1's single-grid calibration with a **two-phase
escalate-until-429** Stage 0.5 — Phase A (the v2.2.1 safe-ramp grid
preserved verbatim) followed (only when needed) by Phase B
(`candidate_tps_grid_phase_b: [5.0, 8.0, 12.0, 16.0, 24.0, 32.0]`,
the EXACT pinned full grid, with `runtime.concurrency_phase_b: 512`
scoped to Phase B + Phase-B-rooted bracket probes only). The
v2.2.1 terminal outcome `no_largest_cell_429_at_any_candidate_tps` is
**RETIRED** as a final calibration verdict; under v2.3 it persists ONLY
as an intra-calibration signal meaning "Phase A grid exhausted with
admitted-pressure validated → enter Phase B". The runtime `peak_ramp_tps`
is therefore selected from EITHER Phase A's grid, OR Phase B's grid, OR
a bracket midpoint, by a calibration probe that walks the grids
ascending and binds the first TPS at which the largest cell saturates
(≥ 1 real 429) AND the smallest-cell control probe at the same TPS
observes zero 429s AND the **admitted-pressure validation gate** (0.70
floor over the last 30 s of admitted-dispatch timestamps) is satisfied.
A non-`selected` calibration outcome (any of the eight terminal failure
members of the new **9-member enum**) is a first-class terminal failure
(exit `8`) and the pipeline stops — analysis records the failure
honestly rather than weakening the predeclared grids or raising the
conservative-but-useful caps.

The end-to-end flow is `calibration → smoke → evidence`, with each stage
hash-linked to its predecessor via `calibration_result_sha256` (stored in
the sibling `*.calibration.summary.json`, not inside the result file
itself, so the result-file bytes have a clean round-trip hash) and the
sidecar `.sha256` text file emitted alongside every smoke summary.
Auto-discovery of "the most recent" calibration / smoke artifact is
forbidden — the operator passes absolute paths via
`--calibration-result` and `--smoke-summary` explicitly. The
`--peak-ramp-tps` CLI override is forbidden (exit `9`).

### v2.3 9-member calibration outcome enum (pinned)

The procedure can terminate in exactly one of these nine members:

1. `selected`
2. `no_usable_contrast_at_this_prompt_deployment`
3. `smallest_cell_control_probe_inconclusive_cap_hit`
4. `calibration_total_usd_exhausted`
5. `calibration_probe_inconclusive_cache_not_warm`
6. `calibration_probe_inconclusive_backlog_excessive`
7. `calibration_probe_inconclusive_admitted_pressure_insufficient` (v2.3 NEW)
8. `no_largest_cell_429_at_any_phase_b_candidate_tps_endpoint_not_throttling` (v2.3 NEW)
9. `no_largest_cell_429_at_any_phase_b_candidate_tps_driver_pressure_insufficient` (v2.3 NEW)

The retired `no_largest_cell_429_at_any_candidate_tps` is NOT a member
of the terminal enum under v2.3.

## Status — pending live calibration run

This analysis document is currently a template; the headline tables
below populate after a successful `selected` calibration outcome plus a
PASS smoke gate plus the Stage 2 evidence run. The smoke / evidence
runners will both refuse to start without the calibration-result path
(exit `9`, `reason=calibration_result_missing`), and evidence
additionally refuses without `--smoke-summary` (`smoke_summary_missing`).

## ⚠ DIAGNOSTIC ONLY — v2.1 status block (2026-05-30, preserved for audit)

**The block below documents the v2.1 protocol run that failed the smoke
gate. Under v2.3 it is retained verbatim for audit reference only and
MUST NOT be interpreted as evidence of Hypothesis I.** The associated
artifacts (originally under `runs/20260529T160517Z_*` and
`runs/20260529T165523Z_*_evidence.partial.*`) have been moved to the
quarantine directory `runs/_quarantined_pre_v23_microfix/` (operator
forensic retention, excluded from git scope by `.gitignore`) — they are
kept on disk so that future investigators can reproduce the failure
that motivated the v2.2.1 → v2.3 calibration design without those
bytes entering any Task 019 PR.

**Stage 0 dry-run (v2.1):** complete — see *Reviewer reproduction* in
[README.md](README.md). The deterministic test suite passes in <2 s.

**Stage 1 smoke (v2.1) — DIAGNOSTIC ONLY under v2.3:** completed 7/7
cells, warm criterion + backlog OK, total USD ≈ $1.71 — but **observed
ZERO real 429s in every cell**, including the largest cell
(`max_output_tokens=16384`) at the v2.1-pinned `peak_ramp_tps = 0.33`.
Spec §"Stage 1 acceptance" requires the largest cell to observe ≥1 real
429 AND the smallest cell to observe zero 429s; this contrast was NOT
obtained. **GATE_VERDICT = FAIL, reason = `no_429_in_largest_cell`.**

Reproducible verdict (DIAGNOSTIC ONLY):

```
$ python3 -m scripts.analyze_max_output_tokens_sweep \
    --summary benchmarks/07-max-output-tokens-reservation/runs/_quarantined_pre_v23_microfix/20260529T160517Z_exp007_max_output_tokens_sweep_smoke.jsonl.summary.json \
    --require-gate-pass
# exit 3, stderr: GATE_VERDICT=FAIL reason='no_429_in_largest_cell' — refusing to certify promotion to Stage 2.
```

**Stage 2 evidence (v2.1) — DIAGNOSTIC ONLY under v2.3:** NOT validly
promoted. An accidental evidence run was launched before the gate
verdict had been confirmed; the orchestrator killed it after cells 256
and 512 completed (253 records total, also 0 429s). The partial JSONL
is preserved at
`runs/_quarantined_pre_v23_microfix/20260529T165523Z_..._evidence.jsonl`
(DIAGNOSTIC ONLY) and labelled diagnostic-only via the sidecar manifest
`*..._evidence.partial.summary.json` with
`partial: true, reason: "smoke_gate_failed_stage2_aborted"`. **It MUST
NOT be analyzed as a completed Stage-2 evidence run.**

### What the v2.1 smoke observation does (and does not) tell us

* **Does not tell us:** Whether Hypothesis I is true or false. The v2.1
  smoke result is, on its own, a *measurement-protocol failure* — the
  experiment did not exercise the throttled-quota regime it was
  designed to probe at `peak_ramp_tps = 0.33`.
* **Possible causes that v2.3 calibration is designed to distinguish:**
  1. `peak_ramp_tps=0.33` is too low for this deployment / for the
     observed arrival cadence — under v2.3 the calibration probe walks
     Phase A `[0.33 … 3.0]` AND, only on Phase-A exhaustion with
     admitted-pressure validated, Phase B `[5.0, 8.0, 12.0, 16.0, 24.0,
     32.0]` (up to ~97× the v2.1 pin) before declaring failure.
  2. The deployment's *actual* throttled cap is higher than the 60 K
     TPM documented in the pinned spec — Phase B's escalate-until-429
     coverage now reaches this regime cleanly.
  3. The admission layer charges *realized* output tokens rather than
     the `max_output_tokens` cap — which would itself be evidence
     *against* Hypothesis I on this deployment (interesting null
     result, not a measurement bug). v2.3 distinguishes #3 from #1/#2
     via the 9-member calibration outcome enum:
     `no_largest_cell_429_at_any_phase_b_candidate_tps_endpoint_not_throttling`
     cleanly identifies #3 after the FULL Phase A+B grid sweep with
     admitted-pressure validated, while `selected` proceeding to a FAIL
     smoke gate would identify a true reservation-non-monotonicity.
  4. The driver could not generate enough admitted pressure to validate
     a candidate TPS — v2.3 NEW outcomes
     `calibration_probe_inconclusive_admitted_pressure_insufficient` and
     `no_largest_cell_429_at_any_phase_b_candidate_tps_driver_pressure_insufficient`
     surface this as a host-capacity finding, NOT a Hypothesis I
     verdict.

### Partial v2.1 evidence sidecar — DIAGNOSTIC ONLY

| cell `max_output_tokens` | n_records | n_429 | phase |
|---:|---:|---:|---|
| 256  | 126 (12 prewarm + 114 admitted) | 0 | complete |
| 512  | 127 (12 prewarm + 115 admitted) | 0 | complete |
| 1024 | — | — | never started |
| 2048 | — | — | never started |
| 4096 | — | — | never started |
| 8192 | — | — | never started |
| 16384 | — | — | never started |

(See `runs/_quarantined_pre_v23_microfix/20260529T165523Z_..._evidence.partial.summary.json`
for the full manifest, including the launch-time smoke-gate verdict
that should have prevented this run from being started.) DIAGNOSTIC
ONLY.

## Headline numbers (Stage 2 evidence run pending v2.3 live runs)

### 1. `visible_output_tokens` — does it scale with the cap?

| `max_output_tokens` (cell) | n (post-prewarm successes) | p50 visible | p95 visible | max visible |
|---:|---:|---:|---:|---:|
| 256   | TBD | TBD | TBD | TBD |
| 512   | TBD | TBD | TBD | TBD |
| 1024  | TBD | TBD | TBD | TBD |
| 2048  | TBD | TBD | TBD | TBD |
| 4096  | TBD | TBD | TBD | TBD |
| 8192  | TBD | TBD | TBD | TBD |
| 16384 | TBD | TBD | TBD | TBD |

**Expected**: roughly flat (±20 %) across cells — `reasoning.effort=low`
bounds the realized output, so a higher *cap* should not bump
`visible_output_tokens`. Annotate any monotone trend.

### 2. `first_429_arrival_rpm` — does it shrink as the cap grows?

| `max_output_tokens` | first-429 arrival RPM | cache-warm? | backlog-excessive? | admitted-pressure-PASS? |
|---:|---:|---|---|---|
| 256   | TBD | TBD | TBD | TBD |
| 512   | TBD | TBD | TBD | TBD |
| 1024  | TBD | TBD | TBD | TBD |
| 2048  | TBD | TBD | TBD | TBD |
| 4096  | TBD | TBD | TBD | TBD |
| 8192  | TBD | TBD | TBD | TBD |
| 16384 | TBD | TBD | TBD | TBD |

**Expected (Hypothesis I positive)**: monotone decrease — the admission
layer reserves quota at the cap, so larger caps consume reservation
faster per request and 429 onset arrives at *lower* admitted RPM.
Cells with `cache_not_warm=True`, `backlog_excessive=True`, or
admitted-pressure-FAIL are excluded from this trend (their JSONL
records remain available for audit; admitted-pressure-FAIL records use
the v2.3 NEW `_retry1_admp` cache-key suffix on the bounded retry).

### 3. Cache-hit ratio per cell

| `max_output_tokens` | cache_hit_ratio (cached_tokens > 0 / successes) |
|---:|---:|
| 256   | TBD |
| 512   | TBD |
| 1024  | TBD |
| 2048  | TBD |
| 4096  | TBD |
| 8192  | TBD |
| 16384 | TBD |

Each cell has its own `prompt_cache_key_used`
(`task019_card1_<run_id_short>_cell<NNNNN>`), so cache state is *per cell ×
per run*; ratios should rise sharply after the 12-call prewarm phase if
prompt caching is engaged for that key.

### 4. Calibration linkage (v2.3, populate after `selected`)

| field | value |
|---|---|
| `selected_peak_tps` | TBD |
| `selected_phase` (`A` / `B` / `bracket`) | TBD |
| `bracket_depth_at_selection` (null when phase ≠ `bracket`) | TBD |
| `calibration_outcome` | TBD (one of 9 members) |
| `calibration_run_id_short` | TBD |
| `calibration_result_sha256` | TBD |
| `calibration_total_usd` | TBD |
| `n_probes_attempted_phase_a` / `_phase_b` / `_bracket` | TBD / TBD / TBD |
| `admitted_peak_rpm_observed_last_30s` (largest-cell at `selected_peak_tps`) | TBD |
| `first_429_metadata.candidate_tps` / `.phase` / `.bracket_depth` | TBD / TBD / TBD |

## Interpretation guide

- **Monotone signal (cell-RPM ↓, visible flat):** consistent with a
  *reservation-at-cap* interpretation. State this as proxy evidence only.
- **Flat signal (cell-RPM ≈ flat as cap grows, visible flat):** the
  throttled PAYG admission layer is *not* using the cap as the reservation
  size; it is computing reservation from realized tokens. This refutes the
  proxy.
- **Non-monotone:** likely a measurement artefact (warm criterion failure,
  backlog excess, admitted-pressure-FAIL, or 429 onset outside the ramp
  budget). Re-run if the artefact is fixable; otherwise report honestly.
- **Calibration says
  `no_largest_cell_429_at_any_phase_b_candidate_tps_endpoint_not_throttling`
  (v2.3 NEW):** the deployment did not throttle anywhere on Phase A
  `[0.33 … 3.0]` OR Phase B `[5.0, 8.0, 12.0, 16.0, 24.0, 32.0]` with
  admitted-pressure validated on every probe — this is itself a
  meaningful null observation about the throttling layer at the PROXY
  level.
- **Calibration says
  `no_largest_cell_429_at_any_phase_b_candidate_tps_driver_pressure_insufficient`
  (v2.3 NEW):** Phase B exhausted with the admitted-pressure gate
  failing on at least one Phase B candidate's bounded retry — a driver
  / host-capacity finding, NOT a Hypothesis I verdict. Investigate the
  dispatcher / host concurrency budget before re-running.
- **Calibration says
  `calibration_probe_inconclusive_admitted_pressure_insufficient`
  (v2.3 NEW):** a single probe (either initial OR `_retry1_admp`)
  ended with zero 429s AND `admitted_peak_rpm < 0.70 × candidate_tps
  × 60`. The driver could not generate enough admitted pressure to
  validate the candidate TPS at this point in the grid; do NOT escalate
  further (escalating with a known-insufficient driver would burn
  shared budget without scientific value).
- **Calibration says `no_usable_contrast_at_this_prompt_deployment`:**
  every TPS that saturated the largest cell — INCLUDING up to three
  bounded bracket-search midpoints between the most recent
  zero-largest-429 TPS and the failing TPS — ALSO saturated the
  smallest control cell. The throttling layer is not behaving as a
  per-cap reservation calculator on this prompt + deployment
  combination, which is itself proxy evidence *against* Hypothesis I.
  The `bracket_trace` array in the calibration result captures every
  bracket midpoint attempted (up to `bracket_max_depth: 3`).

## What this analysis cannot conclude

1. Whether **PTU** deployments behave the same way. They are administered
   separately, billed separately, and may have an entirely different
   reservation calculator.
2. Whether the throttled PAYG admission layer's behaviour generalises to
   non-throttled (canonical-quota) PAYG deployments.
3. Whether the observed reservation size matches Azure's documented
   formula. The runner only measures *behaviour*, not formula identity.
4. **PTU-specific gating** (slot routing, expected-utilization tiering,
   capacity-correlated cache effects) is NOT measured here — it remains
   a hypothesis for PTU readers and is explicitly out of scope.

## Spend reconciliation (populate post-Stage-2)

- Pre-run USD projection (deterministic conservative estimator, no
  429-no-bill discount): calibration two-phase exhaustive worst-case
  ≈ $187, smoke @ `peak_tps=3.0` ≈ $12.29 (≤ $33 at `peak_tps=8.0`,
  ABORTS at preflight if projection ≥ $45), evidence @ `peak_tps=3.0`
  ≈ $58.40 (ABORTS at preflight if projection ≥ $90).
- Realized USD per stage (from each `summary.json` → `total_usd`): **TBD**.
- Hard ceilings (v2.3 conservative-but-useful Task 019 cap): calibration
  $220 (per-probe $60), smoke $50, evidence $100, contingency $30, task
  total $400.
- **Do not exhaust shared budget; cap is an accounting guardrail, not
  a spend target.** The shared tenant has > $1,600 remaining of its
  ~$2,500/month — that figure is context for auditor traceability ONLY
  and is NOT a Task 019 spend target. Other in-flight measurements
  (Tasks 018, 020, 021, 022) draw from the same shared balance.

## Artefacts

- Calibration:
  `runs/<TS>_exp007-max-output-tokens-sweep_calibration.result.json` plus
  sibling `runs/<TS>_exp007-max-output-tokens-sweep_calibration.summary.json`.
  v2.3 result additions: top-level `candidate_tps_grid_phase_b` echo,
  top-level `calibration_probe_max_calls_phase_a` /
  `calibration_probe_max_calls_phase_b`, per-probe `halt_reason`, per-probe
  `first_429_metadata` (on every probe with `n_429 ≥ 1`),
  `bracket_trace` (on bracket-search executions),
  `inconclusive_probe_role` / `inconclusive_at_candidate_tps` /
  `inconclusive_reason_detail` (on any `*_inconclusive_*` outcome).
- Smoke JSONL: `runs/<TS>_exp007-max-output-tokens-sweep_smoke.jsonl`
  plus `*.summary.json` plus sidecar `*.summary.json.sha256`.
- Evidence JSONL: `runs/<TS>_exp007-max-output-tokens-sweep_evidence.jsonl`
  plus `*.summary.json` plus sidecar `*.summary.json.sha256`.
- Partial-run summary (if mid-run halt): `*.partial.summary.json`.
- Charts: `runs/figures/` (populated after Stage 2; expect at least three:
  visible_output_p50_vs_cap, first_429_arrival_rpm_vs_cap,
  cache_hit_ratio_vs_cap).
- DIAGNOSTIC ONLY pre-v2.3 forensic retention:
  `runs/_quarantined_pre_v23_microfix/` (operator on-disk forensic
  data — excluded from git scope by `.gitignore`;
  `benchmarks/*/runs/_quarantined_*/`).
