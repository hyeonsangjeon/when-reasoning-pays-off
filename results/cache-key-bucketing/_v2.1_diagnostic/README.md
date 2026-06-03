# v2.1 diagnostic charts — DO NOT CITE

These PNG charts (`cache_hit_ratio_vs_cardinality.png`,
`ttft_p95_vs_cardinality.png`) were generated from the **Task 018 v2.1**
Stage 1 smoke artifacts at
`benchmarks/06-cache-key-bucketing/runs/_v2.1_diagnostic/` and are kept
here only for traceability of the v2.1 → v2.2 → v2.3 hotfix loop.

**Superseded by Task 018 v2.3.** Do not cite these charts in any final
analysis. Canonical v2.3 charts (when produced) live at
`results/cache-key-bucketing/cache_hit_ratio_vs_cardinality.png` and
`results/cache-key-bucketing/ttft_p95_vs_cardinality.png` (no
`_v2.1_diagnostic/` prefix).

The v2.3 hotfix banner in the internal Task 018 specification (private
working tree) explains why the
v2.1 pinned controls (`runtime.concurrency = 1`, `runtime.sustain_tps = 1.0`,
30K-token Task 012 corpus, scheduled-time-only dispatch telemetry) could
not produce evidence of the docs-stated ~15 req/min overflow threshold.
