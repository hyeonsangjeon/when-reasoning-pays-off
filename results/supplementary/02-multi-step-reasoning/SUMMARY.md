# Supplementary statistics — benchmark 02 (multi-step-reasoning)

Slice: **`02-multi-step-reasoning`** — the multi-step reasoning benchmark (multi-hop / chained-step problems).
Cohort: gpt-4o baseline + gpt-5.2 across efforts `none | low | medium | high | xhigh`.
The canonical `gpt-5.2/minimal` cell is empty (`n_used=0`) and is recorded in each script's `skipped_cells`.
Authored sample design is **N=20 samples × R=3 repeats** per non-empty cell (60 rows each); the supplementary
statistics here are **descriptive** (per revision D-003), not universal inference claims.

Source artifacts (this directory):

- `bootstrap_ci.json` — `scripts.stats.bootstrap_ci`
- `cohens_d.json` — `scripts.stats.cohens_d`
- `inter_rater.json` — `scripts.stats.inter_rater`

Upstream inputs: `benchmarks/02-multi-step-reasoning/runs/*.json` + `benchmarks/02-multi-step-reasoning/judge_runs/*.json`
(experiment prefix `exp002`, **360 used judge rows of 360 parsed**; 6 sibling non-cohort run files skipped).
Cost means are derived per-row and validated byte-for-byte against the committed `analysis.json`
(billing mode `output_only`; see `cost_provenance` in each JSON).

## 1. Bootstrap 95% CI availability

Percentile bootstrap, 10,000 resamples, 95% CI, per non-empty cell × metric.
**24 CIs computed (status `ok`); 4 not computed** — all four belong to the empty
`gpt-5.2/minimal` cell (`empty_canonical_cell`).

| Metric | CIs available | Notes |
| --- | --- | --- |
| cost | 6 / 7 cells | reproduces committed `analysis.json` means (`cost_validation.status=match`) |
| quality | 6 / 7 cells | ordinal judge score (0/1/2); CI is descriptive |
| latency | 6 / 7 cells | wall-clock ms |
| reasoning_tokens | 6 / 7 cells | 0 for gpt-4o baseline and gpt-5.2/none by construction |

No warnings outside the empty-cell skips.

## 2. Effect sizes (method choice)

Pairwise comparisons: **60 rows** (15 cell pairs × 4 metrics). Every row uses
**Cliff's δ** (`method="cliffs_delta"`). Rationale recorded in each row's
`method_selection`: effective independent **N=20 < 30**, so the documented selection rule
takes the nonparametric branch; `quality` is additionally ordinal. Cohen's d is *implemented*
but not selected for any benchmark-02 cell. **No effect size here should be read as a
population-level claim** — magnitudes are Cliff's-δ thresholds (negligible/small/medium/large),
direction follows canonical cell order (positive ⇒ cell B larger).

Magnitude distribution: 30 large, 7 medium, 12 small, 11 negligible.

gpt-4o baseline → gpt-5.2 (per metric, Cliff's δ):

| Metric | none | low | medium | high | xhigh |
| --- | --- | --- | --- | --- | --- |
| cost | −0.99 large | −0.36 medium | +0.20 small | +0.60 large | +0.99 large |
| latency | +0.48 large | +0.61 large | +0.72 large | +0.86 large | +0.97 large |
| reasoning_tokens | +0.00 negligible | +0.35 medium | +0.60 large | +0.80 large | +1.00 large |
| quality | +0.25 small | +0.17 small | +0.25 small | +0.25 small | +0.25 small |

Read honestly: on this slice **latency and reasoning-token** usage rise strongly and
monotonically with effort. **Cost is mixed rather than a single threshold** — under
`output_only` billing `none` is lower than gpt-4o on both mean and dominance; `low` has a
slightly higher mean but lower median/dominance (Cliff's δ = −0.365); `medium` turns positive
but small (+0.20), and `high`/`xhigh` are large positive.
**Quality shows a small but consistently positive effect** (≈ +0.25 Cliff's δ): gpt-4o's judge
scores include 15/60 fails (mean ≈ 1.5) whereas gpt-5.2 cells are near-perfect (all pass except
2/60 fails at `low`). This is a modest, directionally-clear quality edge for the reasoning model —
**not** a significance claim, and it does **not** grow with added effort beyond `none`. (Full
pairwise matrix including gpt-5.2 effort-vs-effort pairs is in `cohens_d.json`.)

## 3. Inter-rater agreement

**Status: `manual_scores_missing` — not computable yet.** This is a methodology gap, not a result.

- Rubric is integer ordinal `0|1|2`; selected statistic is **percent agreement + Cohen's κ**
  (unweighted primary, linear-weighted supplementary). **ICC = `null`** (no continuous graded rubric).
- Judge scores available: **360**. Manual reviewer scores found: **0** (`n_overlap=0`).
- `percent_agreement`, `cohens_kappa`, `linear_weighted_kappa`, `icc` are all `null`.
- Expected ≥ **36** manual scores (10% per non-empty cell). A deterministic 36-row
  `manual_review_queue` (`reviewer_score=null`) is emitted for a reviewer to fill.
- Accepted manual-score paths checked (none present):
  `benchmarks/02-multi-step-reasoning/manual_spot_checks.json`,
  `benchmarks/02-multi-step-reasoning/manual_spot_checks/*.json`,
  `results/supplementary/02-multi-step-reasoning/manual_spot_checks.json`.

**Caveat (T-023 gap):** manual spot-check scores have not been committed, so judge-vs-human
agreement cannot be reported. The κ/confusion-matrix path is implemented and will populate
automatically once manual scores land at one of the accepted paths. No manual scores were
fabricated to fill this gap.

---

Regeneration is deterministic (sorted keys, no timestamps); re-running the three scripts on
the committed benchmark-02 data reproduces these JSONs byte-for-byte.
