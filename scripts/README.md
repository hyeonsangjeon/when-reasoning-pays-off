# Public measurement and article-data scripts

This directory has both the original measurement utilities and the public
article-data publication layer. The table below is the map for the code that
turns committed public evidence into the artifacts used by the repo, Pages, and
review checks.

For the article-topic registry and generator contracts, see
`scripts/article_topics/README.md`. For supplementary statistics, see
`scripts/stats/README.md`.

## Public data pipeline

| Script | Work | Inputs | Sample/result unit | Outputs/checks |
| --- | --- | --- | --- | --- |
| `build_article_topic_data.py` | Topic-oriented entry point. Runs the generator(s) attached to one or more reader-facing article topics. | `scripts/article_topics/registry.py`; then the inputs of the selected generator. | One selected article topic resolves to one or more generator keys. | `python3 scripts/build_article_topic_data.py --all --check` verifies all governed article-data outputs. |
| `promote_chart_data.py` | Converts public benchmark CSV summaries into locale-agnostic chart-data JSON for cost, latency, quality, throughput, and token composition. | `results/cost-curves/benchmark-0{1,2,3}-*.csv`; `results/token-composition/benchmark-0{1,2,3}-tokens.csv`. | One JSON row is one `(model, effort)` cell after the canonical benchmark analysis joins `N=20` authored samples, `R=3` repeats, and judge rows. | Writes/checks `results/public/chart-data/cost-curves-effort/**`, `results/public/chart-data/token-composition/**`, and `release/public_chart_candidates.json`. |
| `promote_ptu_payg_crossover.py` | Builds the modeled PTU/PAYG crossover planning lens from already-governed public chart data plus pinned pricing snapshots. | Public cost/throughput/quality/token chart-data; `pricing/azure-openai-ptu-2026-05.yaml`; `pricing/azure-openai-payg-2026-05.yaml`. | One JSON row is one modeled `(model, effort)` cell. PAYG cost and throughput are exact carry-throughs; `modeled_break_even_rpm` is calculated, not measured PTU throughput. | Writes/checks `results/public/chart-data/ptu-payg-crossover/**` and upserts the shared candidate/public manifests. |
| `sync_pages_chart_data.py` | Mirrors governed chart-data into the static GitHub Pages data directory and validates the page-facing copy. | `release/public_chart_candidates.json`; `results/public/chart-data/**`. | One candidate-listed chart-data file copied into `docs/blog/data/chart-data/**`. | `python3 scripts/sync_pages_chart_data.py --check` verifies 18 candidates, 3 families, stable keys, quality pairing, and byte-stable snapshot metadata. |
| `check_promotion_set.py` | Structured redaction scanner for the promoted public data surface. | Files under `results/public/chart-data/**`, `docs/blog/data/**`, `release/public_chart_candidates.json`, or explicit paths. | One finding is a file/location/category/snippet/tier violation. | Exits non-zero on secrets, endpoints, private paths, request IDs, free-text payload fields, or aggregate-contract violations. |
| `stats/check_repro.py` | Rebuilds supplementary statistics in a temporary directory and byte-diffs them against committed artifacts. | Public `benchmarks/{01,02,03}/runs/*.json`, `judge_runs/*.json`, committed benchmark analyses, pricing snapshots. | One artifact set per benchmark slice: bootstrap CI, effect sizes, inter-rater report. | `python3 scripts/stats/check_repro.py` verifies all 9 supplementary JSON artifacts without writing to `results/`. |

## Shared helpers

| Helper | Why it exists |
| --- | --- |
| `article_topics/registry.py` | The canonical mapping between article topics, benchmark slices, chart families, generator specs, input paths, sample units, and output contracts. |
| `article_topics/publication.py` | Shared publication code for deterministic JSON, candidate-manifest upserts, and `release/public_sanitized_manifest.json` ledger updates. |
| `stats/common.py` | Shared supplementary-stats CLI helpers for benchmark discovery and deterministic JSON writes. |

## Boundaries

- The public article-data generators do not call Azure OpenAI and do not create
  new model responses.
- The chart-data surface is numeric and locale-agnostic; labels and prose live
  in Pages code and locale bundles.
- Public chart data is `SANITIZED_PUBLIC`. The downstream Foundry sample repo
  is a consumer of aggregate/operator-facing forms, not the source of the
  measurement methodology.
- `ptu-payg-crossover` is explicitly a modeled planning lens. It must stay
  paired with the quality series and must not be described as measured live PTU
  throughput.

## Reproducibility reference tools

| Script | Contract |
| --- | --- |
| `measure_cold_mock.py` | Materializes a checkout-equivalent tracked tree, creates a fresh virtual environment, builds and installs the minimal wheel with pip caches disabled, runs help plus Mock init/run, inspects immutable artifacts, and writes the structured Cold Mock timing report. The GitHub reference threshold is 300 seconds; local results are machine-specific. |
| `verify_core_wheel.py` | Builds a non-editable minimal wheel and verifies help, Mock init/run/retry/doctor, experiment list/describe, immutable manifests/checksums, and fail-fast extras without provider network or billing. CI runs it on Ubuntu, macOS, and Windows at CPython 3.11 and 3.13. |
| `run_protected_azure_smoke.py` | Runs exactly one fixed, stateless, no-retry Azure Responses request only after the protected main/Azure-runner/managed-identity guard succeeds. `--offline-fake` executes the same ledger/provider orchestration with an in-process fake and no network for public CI. Only validated `health.json` survives. |

The full research campaign remains POSIX-oriented. Bash release gates,
`fcntl` in `measure_max_output_tokens_sweep.py`, and shell SHA-256 validation
paths are not a native-Windows campaign contract; use WSL/Linux.

## Usual review commands

```bash
python3 scripts/article_topics/manifest.py --check
python3 scripts/build_article_topic_data.py --all --check
python3 scripts/sync_pages_chart_data.py --check
python3 scripts/check_pages_charts.py
python3 scripts/stats/check_repro.py
python3 scripts/sanitize_public_artifacts.py --verify --require-public-manifest
bash scripts/check_public_surface.sh
```
