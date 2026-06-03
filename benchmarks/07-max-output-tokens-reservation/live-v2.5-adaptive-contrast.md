# Live v2.5 adaptive contrast — Task 019 v2.5 run log

> **PAYG, not PTU.** Every section in this file records a PAYG proxy
> measurement against the `ptu-deploy-throttled` GlobalStandard deployment.
> No section may frame an observation as direct PTU data. The §0.10
> PAYG-proxy wording lint (`scripts/task019_v25_adaptive.lint_payg_proxy_wording`,
> exercised by `tests/test_measure_max_output_tokens_sweep.py`
> §11.54) rejects forbidden phrases (enumerated in
> `FORBIDDEN_PAYG_PROXY_PHRASES`) outside quoted counter-example
> blockquote lines beginning with `> COUNTER-EXAMPLE:`. Example:

> COUNTER-EXAMPLE: do not write "PTU evidence" or "evidence of PTU" or "demonstrates PTU behaviour" outside a counter-example blockquote.

This file is the append-only live-run journal required by Task 019 v2.5
§0.5. Every v2.5 calibration attempt — terminal, partial, or
preflight-blocked — MUST land here as a `## ` section, newest at top,
together with a matching entry under `### Live run — Task 019 v2.5` or
`### Blocked run — Task 019 v2.5` in the repository `CHANGELOG.md`.

Each section MUST include the §0.5 required fields:

- `Run prefix:` — the `runs/<ts>_<exp>_calibration*` prefix path.
- `Terminal artifact sha256:` — the calibration result sha256 (or the
  preflight-blocked diagnostic identifier when no artifact was written).
- `Outcome:` — the v2.5 calibration outcome (or blocked-reason).
- `Total spend:` — USD spend, or `$0.00` for preflight-blocked.
- `Pinned §10 RFC assumptions:` — the v2.5 PINNED RFC table values
  in effect at dispatch.
- `Measurements:` — per-role onset intervals, C1/C2/C3 evaluation trace,
  §4.4 caps state.
- `Fixes attempted:` — every fix attempted during the attempt
  (chronological).
- `Blockers:` — every blocker encountered.
- `Why execution cannot move forward:` OR
  `What the next attempt will change:` — explicit forward-looking
  statement.

The §11.50 CI lint (`scripts/task019_v25_adaptive.check_v25_live_artifacts`)
enforces this discipline; runs missing either artifact fail CI.

---

## Blocked run — 2026-06-02 — Fresh4 v2.7 clean live calibration; no promotable contrast (PAYG, not PTU)

Run prefix: `benchmarks/07-max-output-tokens-reservation/runs/20260602T022643Z_exp007_max_output_tokens_sweep_calibration`

Terminal artifact sha256:
`11b7954f21cbe93f02915f1cc571f55cfd51341de8381e79fb8b6962af988343`
(calibration `result.json`; companion summary
`5a877b60e9159b1763cf2ed9fa1c445d1eda49054f7148df90fe715309c57a09`
asserts the result sha256 via its `calibration_result_sha256` field;
jsonl per-probe records
`050a84e77b3ed519e0b86842ab012f445be06dc718de2a4c894df87b93589867`.)

Outcome: calibration
`outcome=no_promotable_contrast_at_this_prompt_deployment`,
`inconclusive_reason_detail=bracket_exhausted_at_depth_3`,
`selected_peak_tps=null`, `selected_via=null`,
`selected_at_phase=null`, `n_probes=6`,
`n_bracket_points_evaluated=3`. Adaptive Stage 0.5.C was entered
(`ADAPTIVE_STAGE_0_5_C_ENTER v24_outcome=no_usable_contrast_at_this_prompt_deployment
reason=adaptive_trigger_matched`) on this Fresh4 retry; the v2.7
cache-key sanitization shipped in commit
`e9faa9c56b2f02c1e6d25f674b5fd2955e4f39f3`
(`fix: sanitize Task019 adaptive cache key for Azure charset`) was
live-verified: **0 BadRequestError / 0 HTTP 400** across 427 HTTP 200
responses, 2 HTTP 429s, and 2 transient
`transport_exception:APIConnectionError` retries (430 JSONL records
persisted, 4 failed of which 2 = `rate_limited_observed` and 2 = the
transient transport retries). The Fresh3 failure mode (133× HTTP 400
from punctuated adaptive `prompt_cache_key` values) is fixed.

Total spend: $3.067124 (calibration; v2.4 calibration cap $4.95
unaffected; v2.5/v2.6 adaptive $25 Stage 0.5.C envelope unaffected;
no smoke / evidence cells dispatched because outcome is non-`selected`).

Pinned §10 RFC assumptions: unchanged from the header section below
(v2.5 microfix #1 + microfix #2 PINs). Dispatch-time PIN state is
preserved by the v2.7 fix (the cache-key change is purely a
post-composition charset sanitization and does not perturb any §10 RFC
value, the v2.4 prompt-identity bytes, or any selection threshold).

Measurements:

- Bracket trace — every depth converged in the upper half because the
  largest role admitted 0 × 429 across the explored TPS envelope:

  | depth | t_low    | t_bracket | t_high | largest_n_429 | outcome        |
  |------:|---------:|----------:|-------:|--------------:|----------------|
  | 1     | 0.330000 | 0.406202  | 0.500  | 0             | recurse_upper  |
  | 2     | 0.406202 | 0.450667  | 0.500  | 0             | recurse_upper  |
  | 3     | 0.450667 | 0.474693  | 0.500  | 0             | recurse_upper  |

- Cache-key sanitization PASS: every `prompt_cache_key_used` value
  persisted to JSONL conforms to the sanitized scheme
  (`^[A-Za-z0-9_-]+$`), e.g.
  `task019_calib_f29c7714_cell16384_tps0500`,
  `task019_calib_f29c7714_cell16384_tps0406_bracket1`,
  `task019_calib_f29c7714_cell16384_tps0475_bracket3`. No `::`, no
  `=`, no decimal points.
- PAYG-proxy interpretation (PAYG, not PTU): across the explored TPS
  envelope (0.33–0.50 rps) at `max_output_tokens=16384`, the
  `ptu-deploy-throttled` GlobalStandard deployment never exhibited
  rate-limiting pressure on the largest role, so the v2.4 bracket
  search could not isolate a promotable contrast point. The Stage
  0.5.C adaptive trigger fired on the no-contrast outcome but the
  terminal calibration result remains
  `no_promotable_contrast_at_this_prompt_deployment` —
  methodology-correct: the bracket envelope did not cross the
  deployment's PAYG 429 threshold for this prompt+`max_output_tokens`
  cell. This is a **negative finding** for Task 019 against this
  prompt+deployment combination, not a wiring bug.

Fixes attempted:

1. v2.7 cache-key sanitization (commit `e9faa9c`) shipped before
   dispatch; live-verified by the run (0 × HTTP 400, see above).
2. None during this attempt — the calibration completed cleanly and
   the no-promotable-contrast terminal outcome is a methodology-valid
   v2.5 C3 result, not a transport or wiring fault.

Secondary defect observed (NEW in v2.7 path — non-fatal to primary
artifacts; reported as a follow-up, NOT as a blocker for this run):

```
2026-06-02T12:13:11Z ERROR scripts.measure_max_output_tokens_sweep
  ADAPTIVE_SUMMARY_WRITE_FAILED AttributeError:
  '_DeploymentBlock' object has no attribute 'model'
```

Root cause: `_write_adaptive_calibration_summary` in
`scripts/measure_max_output_tokens_sweep.py` read `cfg.deployment.model`,
but `_DeploymentBlock` exposes `deployment_name` / `family` (no
`model` attribute). The optional adaptive-summary sidecar file was
NOT written; the authoritative `.result.json` / `.summary.json` /
`.jsonl` artifacts above WERE written and verified by sha256. No
measurement data was lost. Fix: swap the single buggy reference to
`cfg.deployment.family` (mirroring the convention used by every other
"model"-keyed result payload in the module, e.g. v2.4 result writer at
the calibration result path) and pin both the dataclass surface and
the writer's source via focused regression tests in
`tests/test_measure_max_output_tokens_sweep.py`
(`TestV27DeploymentBlockHasNoModelAttribute_v27`,
`TestV27AdaptiveSummaryWriterUsesFamilyNotModel_v27`).

Blockers:

1. Smoke / evidence promotion is BLOCKED upstream by the
   methodology-valid no-promotable-contrast terminal outcome
   (`selected_peak_tps=null`, `selected_via=null`). Per the v2.4 / v2.5
   contract, smoke promotion requires `outcome=selected` with a
   non-null `selected_peak_tps`. Neither holds here. **No promotion
   was attempted, no promotion artifacts were produced.**
2. The bracket TPS envelope (Phase A 0.33–0.50, Phase B extending
   downward) did not cross the deployment's PAYG 429 admission ceiling
   at `max_output_tokens=16384` for this prompt. Future calibrations
   against this prompt+deployment combination require either (a) a
   wider Phase A grid that reaches into the deployment's actual PAYG
   429 envelope, (b) per-cell concurrency / ramp-duration tuning so
   admission pressure is realised within the existing grid, or
   (c) accepting `no_promotable_contrast` as the terminal Task 019
   finding for this combination (PAYG measurement could not isolate a
   contrast; no PTU-specific causal claim is made or implied).

What the next attempt will change: the next live calibration retry,
if any, should adopt one of the three strategy options above under
methodology audit. This run's role in the journal is the
clean-cache-key-path negative finding: v2.7's cache-key sanitization
is empirically confirmed; the no-contrast outcome is a property of
the deployment envelope at this prompt+`max_output_tokens` cell, not
of the tooling. No mid-run code change is appropriate. The
adaptive-summary writer fix (above) is a separate narrow patch that
ships under its own commit on `feature/task019-max-output-tokens-reservation`
and does not require a live retry to validate (covered by unit tests).

PAYG, not PTU. All observations above are PAYG proxy measurements
against the `ptu-deploy-throttled` GlobalStandard deployment. No section
above frames an observation as direct PTU data; PTU-specific
phenomena (slot routing, spillover thrashing, capacity-correlated
cache effects) remain hypotheses that this repo's PAYG measurements
cannot directly support or refute.

---

## Blocked run — 2026-06-02 — live v2.6 calibration selected; smoke promotion gate denied

Run prefix: `benchmarks/07-max-output-tokens-reservation/runs/20260601T223532Z_exp007_max_output_tokens_sweep_calibration`

Terminal artifact sha256: `88a2afb41418f4bdbe2636ed5e5ea07ffb8eb49d8773f12a5a3c9c5e8ac05805`
(calibration `result.json`; companion summary
`ebc737d69fd9264acc2c553f0d2378cbdda70afce5c34f8b4a40c693f5fe9afa`
asserts the result sha256 via its `calibration_result_sha256` field;
jsonl per-probe records
`c34c04643653cb6c4f5e95e4a73076d0664deed7000f2400cf51f56df2f630d4`;
smoke v2.4 abort-envelope sidecar
`benchmarks/exp007_max_output_tokens_sweep/runs/20260601T233416Z_exp007_max_output_tokens_sweep_smoke.summary.json`
→ `9ca5ab170242b18f969dd9a351eed28aaf2442325acf2e454485571f36a55c29`.)

Outcome: calibration `outcome=selected` via `selected_via=bracket_search`
at `selected_at_phase=bracket`, `selected_peak_tps=0.47469318448182934`,
`selected_bracket_root_phase=A`, `n_probes=7`,
`n_bracket_points_evaluated=3`. Downstream smoke preflight blocked with
`empirical_promotion_denied_reason=empirical_promotion_disabled_cache_hit_below_floor`
and `exit_reason=TPM_FEASIBILITY_ABORT`
(schema `task019.v2.4.abort_envelope`). The temporary dispatch YAML
for this live run had `runtime.adaptive_calibration.enabled: true`
installed under an in-file methodology-auditor APPROVE phrase, so the
v2.5/v2.6 adaptive Stage 0.5.C path was *armed* at dispatch. However,
the §3.2 adaptive Stage 0.5.C runtime trigger predicate did not match
the v2.4 `bracket_search` selected outcome — the operator log recorded
`adaptive_trigger_not_matched_outcome_not_in_predicate_set` — so no
adaptive Stage 0.5.C dispatch fired, no Step 1/2/3 probes were
executed, and no `task019.v2.5.adaptive_calibration_summary` /
`task019.v2.6.*` records were written. The terminal calibration
artifact therefore remains a v2.3/v2.4 `calibration_result` selected
via `bracket_search`, and the downstream smoke / evidence promotion
path that was then evaluated is the v2.4 §10 empirical-promotion gate
(not a v2.5 adaptive path). This entry is journaled here per §0.5 as
the live-run log of record for any Task 019 calibration attempt
regardless of which Stage 0.5.C branch actually fires.

Total spend: $3.057047 (calibration; `total_committed_usd = $5.823`).
Smoke incremental spend: $0.00 — preflight aborted before any smoke
cell dispatched. Both figures remain within the v2.4 calibration cap
and within the separate v2.5/v2.6 adaptive `$25` 0.5.C envelope.

Pinned §10 RFC assumptions: unchanged from the header section below
(v2.5 microfix #1 + microfix #2 PINs). The v2.5/v2.6 adaptive Stage
0.5.C path was armed at dispatch (temp YAML
`runtime.adaptive_calibration.enabled: true` under approved live
phrase) but did not fire (trigger predicate not matched against the
v2.4 `bracket_search` selected outcome); the PIN table is recorded as
the dispatch-time state for audit completeness.

Measurements:

- Calibration phase A bracket search converged at root_phase=A with
  `selected_peak_tps ≈ 0.4747` over 3 bracket points / 7 probes.
- At the selected bracket point (`bracket_depth=3`,
  `candidate_tps=0.47469318448182934`):
  - Largest probe: `first_429_arrival_rpm=29`, `n_429_records=1`,
    `halt_reason=first_429_observed`, cache-hit ratio at steady state
    `≈ 0.8829` (above the v2.4 PIN
    `cache_hit_floor_largest = 0.80`).
  - Smallest control probe: `n_429_records=0` (no 429 pressure
    admitted), cache-hit ratio at steady state `≈ 0.6544` —
    **below** the v2.4 PIN
    `cache_hit_floor_smallest_control = 0.80`.
- Smoke preflight TPM projector (deterministic-conservative) at the
  selected TPS: `smallest_tpm = 68_754.6`,
  `largest_tpm = 528_105.7`, deployment TPM cap `60_000`
  (`ptu-deploy-throttled` GlobalStandard) →
  `failure_reason=smallest_overshoots_lower_threshold`.
- Cold-cache strict fallback path therefore cannot rescue this
  calibration: the smallest cell's projected TPM already exceeds the
  lower TPM corridor by construction.

Fixes attempted: none in this attempt — the calibration completed
cleanly and the smoke block is a methodology-correct denial of the
selected calibration's admission to evidence, not a wiring bug. The
mini-probe revalidation path (`runtime.empirical_promotion.mini_probe_enabled: true`)
was considered as the narrowest forward step but is
**not authorized** for this denial class (see Blockers).

Blockers:

1. v2.4 §10 empirical-promotion gate denial
   `empirical_promotion_disabled_cache_hit_below_floor` at the
   smallest-control probe of the selected bracket point. Smallest
   control did not warm to floor within the calibration steady-state
   window at the selected TPS.
2. Cold-cache strict fallback denial
   `smallest_overshoots_lower_threshold` — the smallest cell's
   projected TPM already exceeds the deployment's lower TPM corridor
   at the selected TPS, so the strict fallback cannot admit smoke
   either.
3. Mini-probe revalidation is **not** an authorized recovery for
   this denial class. The methodology-auditor's review of a
   mini-probe retry proposal returned `REQUEST-CHANGES`: v2.4 §7 /
   §3.1 restrict `mini_probe_revalidated` to attempts where
   invariants 1–11 pass and invariant 12 (freshness) is the **sole**
   denial. Invariant 5 (cache-hit floor) is the active denial here,
   so a mini-probe retry against this same calibration would
   constitute methodology drift and is rejected before dispatch.

What the next attempt will change: not a mini-probe retry against
this calibration. The next attempt will be **either** (a) a fresh
Stage 0.5 calibration attempt against the same selected-TPS region
with a longer steady-state window (or a smaller bracket step) tuned
to allow the smallest-control cache to warm to the
`cache_hit_floor_smallest_control = 0.80` PIN before steady-state
close — re-spending the calibration budget under the v2.4 cap — **or**
(b) a separately auditor-approved methodology/code change that lands
a new revalidation path under a fresh spec revision (no PIN
movement, no in-place gate loosening). Either branch will land its
new attempt as a fresh `## ` section above this one, with a matching
`### Blocked run — Task 019 v2.5` (or `### Live run — Task 019
v2.5`) entry in `CHANGELOG.md`.

PAYG-not-PTU framing: this attempt was dispatched against the
`ptu-deploy-throttled` GlobalStandard PAYG deployment. The cache-hit
floor denial and the TPM corridor denial are PAYG proxy observations
relevant to the Task 019 hypothesis I (PTU admission-time reservation
under `max_output_tokens`); they do not directly measure PTU
admission-slot behaviour and are not a substitute for PTU
measurement.

---

## Header — v2.5 spec PIN date 2026-05-31 (no live attempts yet)

Run prefix: _none — v2.5 implementation landed; first live attempt
pending operator dispatch._

Terminal artifact sha256: _n/a_

Outcome: _n/a — header section only._

Total spend: $0.00

Pinned §10 RFC assumptions (v2.5 microfix #1 + microfix #2):

| RFC | PINNED value |
|---|---:|
| `adaptive_expansion_factor` | `1.5` |
| `adaptive_expansion_probes_max_per_role` | `2` |
| `adaptive_bracket_depth_max_per_role` | `3` |
| `adaptive_c2_replicates_max_per_role` | `1` |
| `c2_onset_separation_margin_tps` | `0.05` |
| `adaptive_calibration_max_usd` | `$25` |
| `adaptive_calibration_wall_time_max_minutes` | `45` |
| `adaptive_apiconnectionerror_consecutive_max` | `3` |
| `min_remaining_usd_for_adaptive_entry` | `$8` |
| `min_remaining_usd_for_expansion` | `$3` |

Measurements: _none — header section only._

Fixes attempted: _none — header section only._

Blockers: _none — header section only._

What the next attempt will change: The first live v2.5 attempt will
dispatch the full `experiments/exp007_max_output_tokens_sweep.yaml`
calibration with `runtime.adaptive_calibration.enabled: true` and the
methodology-auditor APPROVE comment installed inline. Until then,
`enabled` stays `false` and the runner ignores the v2.5 block entirely.

---

## Blocked entry — 2026-05-31 reviewer fix-loop #1 (preflight wiring hardened; live dispatcher deferred)

Run prefix: _none — no live calibration dispatched; entry recorded per
§0.5 ("preflight-blocked / partial / terminal runs are analysis
evidence")._

Terminal artifact sha256: _n/a — no calibration result written._

Outcome: `implementation_blocked_live_dispatcher_not_wired`

Total spend: $0.00

Pinned §10 RFC assumptions (v2.5 microfix #1 + microfix #2): unchanged
from the header section above.

Measurements: _n/a — no probes dispatched._

Fixes attempted:

1. `scripts/measure_max_output_tokens_sweep.load_experiment` now
   re-raises `AdaptiveCalibrationYAMLPreflightError` as
   `LinkageValidationError` so the existing exit-9 branch in `main()`
   fires deterministically (`LINKAGE_VALIDATION_FAILED reason=...`)
   instead of leaking a Python traceback when an operator flips
   `runtime.adaptive_calibration.enabled: true` with an invalid
   `prior_calibrations_disclosure_path` or auditor comment. The
   re-raise preserves the enumerated `.reason` slot (one of
   `adaptive_calibration_prior_disclosure_path_required` /
   `adaptive_calibration_auditor_approval_missing_or_invalid`).
2. The previous silent `ImportError → no-op` degradation of the v2.5
   helper import has been replaced with a fail-closed
   `LinkageValidationError` carrying the new reason
   `adaptive_calibration_helper_import_failed`. The helper module
   lives in the same repository with no optional dependency; the
   ImportError branch is a refactor-breakage guard, NOT an advertised
   disable switch.
3. New `main()`-level regression tests (`tests/test_measure_max_output_tokens_sweep.py::TestV25YAMLPreflightWiringInMain_FixLoop1`)
   assert exit code + stderr token for the bad disclosure-path branch
   and the bad auditor-comment branch (one invokes `main()`
   end-to-end, not just the pure validator). Full sweep suite remains
   green: `478 passed, 3 skipped` (+2 over previous 476-pass
   baseline).

Blockers:

- Live HTTP dispatch for v2.5 Stage 0.5.C Steps 1–3 is NOT wired into
  the production calibration loop in
  `scripts/measure_max_output_tokens_sweep.run_calibration` /
  `_run_calibration_async`. The pure planner / evaluator / validator /
  lint surface is present and fully tested; the orchestration that
  (i) checks the §3.2 trigger predicate at runtime, (ii) calls
  `compute_role_onset_interval` per role, (iii) dispatches the planned
  Step 2 / Step 3 probes via the existing v2.4 calibration dispatcher
  with the §0.12-suffixed `prompt_cache_key`, (iv) runs C1 → C2 → C3
  in order, and (v) writes the
  `task019.v2.5.adaptive_calibration_summary` alongside the bumped
  `task019.v2.5.calibration_result` is NOT wired.

Why execution cannot move forward: the worker context disallows live
Azure calls (session directive); the dispatcher wiring is a
measurement-bearing change whose only meaningful regression evidence
is a live calibration attempt (planner-step alignment with real 429
timings, §0.12 cache-bucket isolation against real prefix-cache
behaviour, §4.4 cap-halt timing against real wall-time). Landing
untested dispatcher code without a live attempt would violate
Engineering Principle #2 (Silent Failure Is the Enemy) and the spec's
own §16 DoD which gates v2.5 on a final live smoke + evidence run.

What the next attempt will change: a follow-up commit guarded by a
fresh `methodology-auditor` approval will wire the dispatcher inside
`_run_calibration_async`, gated by the runtime equivalent of the §3.2
trigger predicate (the YAML preflight already enforces the
pre-dispatch half), preserving v2.4 default behaviour when
`enabled=false`. That commit will land its first live attempt under a
new `## ` section above this one and a matching `### Live run — Task
019 v2.5` entry in the repository `CHANGELOG.md`.
