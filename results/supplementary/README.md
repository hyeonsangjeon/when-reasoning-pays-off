# Supplementary statistics

This directory contains descriptive supplementary statistics for the first
three benchmark slices:

- `01-short-factual`
- `02-multi-step-reasoning`
- `03-tool-using-agent`

Each slice has the same artifact set:

| File | Meaning |
| --- | --- |
| `bootstrap_ci.json` | Percentile bootstrap summaries for cost, quality, latency, and reasoning tokens. |
| `cohens_d.json` | Pairwise descriptive effect sizes. The selection rule chooses Cliff's delta for these public slices. |
| `inter_rater.json` | Judge-vs-human agreement scaffold and deterministic manual review queue. Current public status is `manual_scores_missing`. |
| `SUMMARY.md` | Human-readable per-slice interpretation of the JSON artifacts. |

The cross-slice synthesis is `SUMMARY.md` in this directory.

## Inputs and sample units

The stats scripts regenerate from public benchmark run captures and judge rows:

```text
benchmarks/01-short-factual/runs/*.json
benchmarks/02-multi-step-reasoning/runs/*.json
benchmarks/03-tool-using-agent/runs/*.json
benchmarks/*/judge_runs/*.json
pricing/azure-openai-payg-2026-05.yaml
```

The raw unit is a per-call Responses API record joined to a judge row. The
analysis unit is a benchmark/model/effort cell over `N=20` authored samples and
`R=3` repeats, excluding rows whose canonical `analysis.json` marks as outliers.

These files are descriptive summaries for the committed slices. They are not
population-level significance tests and do not replace the frozen methodology in
`docs/05-methodology.md`.

## Artifact contracts

`bootstrap_ci.json`:

- deterministic percentile bootstrap with `resamples = 10000`
- `seed = 20260605` plus per-cell seed derivation
- metrics: `cost`, `quality`, `latency`, `reasoning_tokens`
- records empty canonical cells in `skipped_cells`
- validates cost means against committed `analysis.json`

`cohens_d.json`:

- emits pairwise cell comparisons for the same four metrics
- records the method selected per comparison
- uses Cliff's delta for the current public slices because effective independent
  `N < 30` and quality is ordinal
- direction follows canonical cell order

`inter_rater.json`:

- uses join key `(sample_id, model, effort, repeat)`
- reports percent agreement, Cohen's kappa, and linear-weighted kappa when
  committed manual scores exist
- currently reports `manual_scores_missing` and emits a deterministic review
  queue; no human scores are synthesized

## Regeneration and checks

```bash
python3 scripts/stats/check_repro.py
```

`check_repro.py` regenerates the supplementary JSON into a temporary directory
and byte-diffs it against the committed files. It should be part of any review
that changes benchmark run captures, judge rows, pricing snapshots, or stats
code.
