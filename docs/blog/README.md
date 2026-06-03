# `docs/blog/` — Blogging & Articles Data Inventory

Planning workspace for the upcoming GitHub Pages **blogging / articles**
section. **No articles are published from here yet.** This directory is a
topic-by-topic map of the public, sanitized data and the visualization
candidates that future articles can draw on.

## Contents

| File | Purpose |
| --- | --- |
| [`editorial-data-inventory.md`](./editorial-data-inventory.md) | Human-readable, topic-by-topic data & chart inventory with observed patterns and operator takeaways. |
| [`chart-candidates.json`](./chart-candidates.json) | Machine-readable manifest of topics + reusable chart families for later article rendering. |
| [`data/reasoning_effort_summary.csv`](./data/reasoning_effort_summary.csv) | Derived, aggregate-safe stub: an exact (unrounded) re-projection of fields from `benchmarks/0{1,2,3}/analysis.json` (cost, quality, latency, reasoning tokens, throughput gain per effort cell). Metric columns are blank where `n_used=0`. |

## Ground rules (public-safety)

- **Public mirror only.** Sources are limited to tracked files under
  `benchmarks/`, `results/`, `docs/`, `scripts/`, `pricing/`, `schemas/`,
  `release/`.
- **No private data.** No `RAW_PRIVATE` artifacts, no `.internal/` files, no
  secrets, endpoint names, or private identifiers.
- **No claim drift.** Measured numbers are quoted verbatim with their exact
  source path. The only derived file is the clearly labelled CSV stub above,
  which re-projects values already present in public `analysis.json` files.
- **Consumption-model honesty.** PAYG-vs-PTU caveats are preserved: benchmarks
  06/07 are PAYG (not PTU) evidence; benchmark 08 is descriptive single-tenant;
  benchmark 09 is a calibration proxy, not direct PTU-pool observation.

## Topics at a glance

T01 short-factual (reasoning doesn't pay) · T02 multi-step (reasoning pays) ·
T03 tool-agent · T04 reasoning-token tax · T05 PTU/PAYG crossover ·
T06 cache-key bucketing · T07 retry-after · T08 spillover recovery ·
T09 admission replay · T10 `max_output_tokens` boundary ·
T11 evidence-gate / release tiers · T12 i18n site metadata.

See `editorial-data-inventory.md` for the full breakdown and readiness flags.
