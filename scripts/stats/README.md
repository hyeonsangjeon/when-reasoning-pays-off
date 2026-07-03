# Supplementary stats scripts

These scripts regenerate the descriptive supplementary statistics committed
under `results/supplementary/`.

## Scripts

| Script | Work | Output |
| --- | --- | --- |
| `bootstrap_ci.py` | Percentile bootstrap means for cost, quality, latency, and reasoning tokens. | `bootstrap_ci.json` |
| `cohens_d.py` | Pairwise effect-size summaries with a fixed method-selection rule. | `cohens_d.json` |
| `inter_rater.py` | Judge-vs-human agreement report and manual review queue. | `inter_rater.json` |
| `common.py` | Shared CLI helpers for benchmark discovery and deterministic JSON writing. | Imported by the three stats CLIs. |
| `check_repro.py` | Regenerates all three stats files for all public slices into a temporary directory and byte-diffs committed output. | No committed output; exits non-zero on drift. |

## Inputs

The scripts read public benchmark runs, public judge rows, the committed
benchmark analyses, and the PAYG pricing snapshot. They do not call Azure
OpenAI and do not read private raw archives.

```text
benchmarks/<slice>/runs/*.json
benchmarks/<slice>/judge_runs/*.json
benchmarks/<slice>/analysis.json
pricing/azure-openai-payg-2026-05.yaml
```

## Sample and result unit

The record unit is one public per-call run capture joined to one judge result.
The descriptive statistics are grouped by benchmark, model, effort, authored
sample, and repeat. The intended design is `N=20` authored samples and `R=3`
repeats per non-empty benchmark/model/effort cell; skipped or outlier rows
follow the canonical `analysis.json` population.

## Public interpretation boundary

These scripts support the article evidence trail by exposing how the descriptive
checks were produced. They do not create the main benchmark measurements. They
also do not upgrade the findings into population-level inference: bootstrap CIs
and effect sizes are supplementary descriptions of these committed slices only.
