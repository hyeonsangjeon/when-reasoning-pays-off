# Public chart data

This directory contains locale-agnostic numeric JSON used by the GitHub Pages
dashboard and article charts. Labels, prose, route names, and translations live
outside these files. The data here is the governed public evidence surface for
article charts.

Every file uses the same base contract:

- `schema = "wrpo.chart_data"`
- `schema_semver = "0.1.0"`
- `tier = "SANITIZED_PUBLIC"`
- `benchmark_key` names the benchmark slice (`benchmark-01`, `benchmark-02`,
  `benchmark-03`).
- `family_key` names the chart-data family.
- `metric_key`, `units_key`, `dimension_keys`, and `series_keys` describe how a
  renderer should read `rows`.
- `source_sanitized_sha256` pins the sanitized source artifact used to build the
  JSON.
- `rows` contains numeric, chart-shaped records only.

The generator inventory is canonicalized in
`scripts/article_topics/registry.py` under `GENERATOR_SPECS`.

## Families

| Family | Metrics/files | Generator | Article topic |
| --- | --- | --- | --- |
| `cost-curves-effort` | `cost-per-request.json`, `latency.json`, `quality.json`, `throughput-gain.json` for each benchmark | `effort-evidence` | Overview plus effort/cost/quality evidence topics |
| `token-composition` | `tokens.json` for each benchmark | `effort-evidence` | Invisible reasoning-token topic |
| `ptu-payg-crossover` | `crossover.json` for each benchmark | `ptu-payg-planning` | PTU/PAYG planning topic |

## Inputs and sample units

`cost-curves-effort` and `token-composition` are converted from committed public
CSV summaries under:

```text
results/cost-curves/benchmark-0{1,2,3}-{cost-per-request,latency,quality,throughput-gain}.csv
results/token-composition/benchmark-0{1,2,3}-tokens.csv
```

One output row represents one `(model, effort)` cell after the canonical
benchmark analysis joins `N=20` authored samples, `R=3` repeats, and judge
scores. Empty canonical cells are omitted rather than plotted as zero.

`ptu-payg-crossover` is modeled from already-governed public chart data and
pinned pricing snapshots:

```text
results/public/chart-data/cost-curves-effort/benchmark-*/cost-per-request.json
results/public/chart-data/cost-curves-effort/benchmark-*/throughput-gain.json
results/public/chart-data/cost-curves-effort/benchmark-*/quality.json
results/public/chart-data/token-composition/benchmark-*/tokens.json
pricing/azure-openai-ptu-2026-05.yaml
pricing/azure-openai-payg-2026-05.yaml
```

One crossover row is one modeled `(model, effort)` cell. PAYG cost/request and
throughput-gain values are exact carry-throughs from the public chart-data rows.
`modeled_break_even_rpm` is a planning calculation from the pinned PTU hourly
rate and minimum-PTU snapshot; it is not a live PTU throughput measurement.

## Example rows

Cost-per-request row:

```json
{
  "effort": "na",
  "mean_usd_per_request": 0.000747,
  "model": "gpt-4o",
  "n_used": 58,
  "std_usd_per_request": 0.000063
}
```

Token-composition row:

```json
{
  "effort": "na",
  "mean_cached_tokens": 0.0,
  "mean_input_tokens_noncached": 247.603448,
  "mean_output_tokens": 12.793103,
  "mean_reasoning_tokens": 0.0,
  "model": "gpt-4o",
  "n_used": 58
}
```

Crossover row:

```json
{
  "effort": "na",
  "mean_usd_per_request": 0.000747,
  "min_ptu": 50,
  "model": "gpt-4o",
  "modeled_break_even_rpm": 2231.146809,
  "n_used": 58,
  "ptu_hourly_rate_usd": 2.0,
  "throughput_gain_factor": 1.0
}
```

## Regeneration and checks

```bash
python3 scripts/build_article_topic_data.py --all --check
python3 scripts/promote_chart_data.py --check
python3 scripts/promote_ptu_payg_crossover.py --check
python3 scripts/sync_pages_chart_data.py --check
python3 scripts/check_pages_charts.py
```

Omit `--check` only when intentionally refreshing committed outputs. Any changed
JSON should also update `release/public_chart_candidates.json` and pass the
public-surface checks.
