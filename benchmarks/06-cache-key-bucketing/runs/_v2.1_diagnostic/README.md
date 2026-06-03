# v2.1 diagnostic artifacts — DO NOT CITE

These JSONL + summary files were produced under **Task 018 v2.1** pinned
controls (`runtime.concurrency = 1`, `runtime.sustain_tps = 1.0`, 30K-token
Task 012 corpus, scheduled-time-only dispatch telemetry) and are kept here
**only for traceability of the v2.1 → v2.2 → v2.3 hotfix loop**.

**Superseded by Task 018 v2.3.** The v2.3 hotfix banner in the internal
Task 018 specification (private working tree) documents the two
blockers (the v2.1 concurrency=1 mode could not reach the >=15 RPM target;
v2.2's then-proposed concurrency=12 + 30K corpus + sustain_tps=1.0 violated
the Azure OpenAI estimated-processed-token sliding-window TPM quota with
~3.6× headroom, and its dispatch timestamps were captured pre-semaphore,
so the per-bucket / common-prefix RPM bookkeeping could lie about the
realized cadence) that these artifacts inherit.

**Do not cite these files in any final analysis.** The canonical v2.3
artifacts live at `benchmarks/06-cache-key-bucketing/runs/*.jsonl` (no
`_v2.1_diagnostic/` prefix) and at `results/cache-key-bucketing/` (no
`_v2.1_diagnostic/` prefix). The v2.3 final write-up in
`benchmarks/06-cache-key-bucketing/analysis.md` references only the
unprefixed paths.

Files preserved:

```
20260529T084412Z_exp006_cache_key_bucketing_inmemory_smoke.jsonl(.summary.json)
20260529T090514Z_exp006_cache_key_bucketing_24h_smoke.jsonl(.summary.json)
20260529T092805Z_exp006_cache_key_bucketing_inmemory_dry-run.jsonl(.summary.json)
20260529T092806Z_exp006_cache_key_bucketing_24h_dry-run.jsonl(.summary.json)
```

These artifacts contain `prompt_cache_key` values shaped
`benchmark06_(inmemory|24h)_card01_<run_id_short>_bucket_000` (and similar
for card=8). The anonymization audit regex used by v2.3
(`^benchmark06_(inmemory|24h)_card\d{2}_[a-f0-9]{4,8}_bucket_\d{3}$`) matches
them, so the diagnostic subdir does not regress the audit recipe.
