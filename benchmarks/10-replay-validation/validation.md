# Task 024 — Replay validation report
This report is an approximate replay of legacy source runs against the Guide §0 admission-reservation formula and a fitted continuous leak constant. It is a candidate input for capacity planning with source-run caveats. It is not a generalized PTU predictor across deployments without re-calibration.
## Source caveats
- `task013_dual_spillover` runs use Azure PAYG deployments shaped to expose a PTU-like saturation pattern; they are not customer-attributed native PTU evidence.
- `task019_max_output_tokens_proxy` runs are PAYG-throttled-quota proxy evidence, not direct PTU validation.
- Inputs are normalized through legacy adapters; Task 028 canonical schema is not used here.
- Guide §3 model-specific output-token weighting ratios are not modeled in v1.
- Divergences may be consistent with burst tolerance, the reserve/release completion approximation, missing source fields, or proxy-source mismatch. Those are not causal conclusions.
## Fitted leak constant

- `k_leak_tokens_per_ptu_per_second = 3.91028`
- Optimizer settings: `{"fit_status": "ok", "fit_value_sum_sq_seconds": 2497803362.0, "golden_section_refine_iters": 24, "grid": [0.125, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 512.0, 1024.0, 2048.0, 4096.0], "grid_values": [120644935532.0, 115521906647.0, 97072141692.0, 5264373737.0, 5518504715.0, 2511115287.0, null, null, null, null, null, null, null, null, null, null], "pairing_rule": "per_source_run_per_cell_then_event_order"}`

## In-sample calibration residuals
- Confusion matrix: `{"fn": 107, "fp": 5, "tn": 4267, "tp": 60}`
- Onset timestamp residuals (paired, abs seconds): p50=24695.5, p95=26248.75, n_paired=4
- Retry-after-ms residuals (paired, abs ms): p50=74245.34968697734, p95=264626.91306269186, n_paired=60

## Out-of-sample residuals
Holdout is not feasible (fewer than two eligible source runs). Residuals above are in-sample calibration residuals only.

## Source diagnostics
- Empty files skipped: 0
- `20260528T135034Z_exp005_dual_spillover_reactive_reactive.jsonl` (task013_dual_spillover, capacity_source=payg_quota_proxy, effective_ptu_count=17.647058823529413): n_records=2136, observed_429=0, observed_accepted=2136, excluded=0
- `20260528T183310Z_exp005_dual_spillover_proactive_proactive.jsonl` (task013_dual_spillover, capacity_source=payg_quota_proxy, effective_ptu_count=17.647058823529413): n_records=2303, observed_429=167, observed_accepted=2136, excluded=0

## Methodology notes
- Predicted/observed 429 events are paired per `(source_run_id, cell_key)` in event order. Unpaired predicted/observed events surface as confusion-matrix false positives / false negatives.
- The simulator uses a single token-denominated bucket: `util_tokens`, `capacity_tokens`, token leak, token overshoot, and token-derived `predicted_retry_after_ms`. Capacity comes from the Guide §3 Input TPM / PTU table; only `k_leak_tokens_per_ptu_per_second` is fitted.
- Default validation mode does not mutate simulator state on observed-429 source records.
