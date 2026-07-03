# Supplementary statistics — benchmark 01 (short-factual)

Slice: **`01-short-factual`** — the short-factual benchmark (single-shot factual recall).
Cohort: gpt-4o baseline + gpt-5.2 across efforts `minimal | low | medium | high | xhigh`.
The canonical `gpt-5.2/none` cell is empty (`n_used=0`) and is recorded in each script's `skipped_cells`.
Authored sample design is **N=20 samples × R=3 repeats** per non-empty cell; the supplementary
statistics here are **descriptive** (per revision D-003), not universal inference claims.

Source artifacts (this directory):

- `bootstrap_ci.json` — `scripts.stats.bootstrap_ci`
- `cohens_d.json` — `scripts.stats.cohens_d`
- `inter_rater.json` — `scripts.stats.inter_rater`

Upstream inputs: `benchmarks/01-short-factual/runs/*.json` + `benchmarks/01-short-factual/judge_runs/*.json`
(experiment prefix `exp008_short-factual_fixture`, 354 used judge rows of 360 parsed).
Cost means are derived per-row and validated byte-for-byte against the committed `analysis.json`
(billing mode `reasoning_billed_separately`; see `cost_provenance` in each JSON).

## 1. Bootstrap 95% CI availability

Percentile bootstrap, 10,000 resamples, 95% CI, per non-empty cell × metric.
**24 CIs computed (status `ok`); 4 not computed** — all four belong to the empty
`gpt-5.2/none` cell (`empty_canonical_cell`).

| Metric | CIs available | Notes |
| --- | --- | --- |
| cost | 6 / 7 cells | reproduces committed `analysis.json` means (`cost_validation.status=match`) |
| quality | 6 / 7 cells | ordinal judge score (0/1/2); CI is descriptive |
| latency | 6 / 7 cells | wall-clock ms |
| reasoning_tokens | 6 / 7 cells | 0 for gpt-4o baseline by construction |

No warnings outside the empty-cell skips.

## 2. Effect sizes (method choice)

Pairwise comparisons: **60 rows** (15 cell pairs × 4 metrics). Every row uses
**Cliff's δ** (`method="cliffs_delta"`). Rationale recorded in each row's
`method_selection`: effective independent **N=20 < 30**, so the documented selection rule
takes the nonparametric branch; `quality` is additionally ordinal. Cohen's d is *implemented*
but not selected for any benchmark-01 cell. **No effect size here should be read as a
population-level claim** — magnitudes are Cliff's-δ thresholds (negligible/small/medium/large),
direction follows canonical cell order (positive ⇒ cell B larger).

Magnitude distribution: 45 large, 6 small, 9 negligible.

gpt-4o baseline → gpt-5.2 (per metric, Cliff's δ):

| Metric | minimal | low | medium | high | xhigh |
| --- | --- | --- | --- | --- | --- |
| cost | −0.80 large | +1.00 large | +1.00 large | +1.00 large | +1.00 large |
| latency | +0.85 large | +1.00 large | +1.00 large | +1.00 large | +1.00 large |
| reasoning_tokens | +1.00 large | +1.00 large | +1.00 large | +1.00 large | +1.00 large |
| quality | +0.01 negligible | −0.22 small | −0.22 small | +0.01 negligible | −0.13 negligible |

Read honestly: on this slice reasoning effort moves **cost / latency / reasoning-token**
usage strongly and monotonically, while **quality effect sizes are negligible-to-small** —
i.e. no clear quality gain from added effort on short-factual questions. (Full pairwise
matrix including gpt-5.2 effort-vs-effort pairs is in `cohens_d.json`.)

## 3. Inter-rater agreement

**Status: `manual_scores_missing` — not computable yet.** This is a methodology gap, not a result.

- Rubric is integer ordinal `0|1|2`; selected statistic is **percent agreement + Cohen's κ**
  (unweighted primary, linear-weighted supplementary). **ICC = `null`** (no continuous graded rubric).
- Judge scores available: **354**. Manual reviewer scores found: **0** (`n_overlap=0`).
- `percent_agreement`, `cohens_kappa`, `linear_weighted_kappa`, `icc` are all `null`.
- Expected ≥ **36** manual scores (10% per non-empty cell). A deterministic 36-row
  `manual_review_queue` (`reviewer_score=null`) is emitted for a reviewer to fill.
- Accepted manual-score paths checked (none present):
  `benchmarks/01-short-factual/manual_spot_checks.json`,
  `benchmarks/01-short-factual/manual_spot_checks/*.json`,
  `results/supplementary/01-short-factual/manual_spot_checks.json`.

**Caveat (T-023 gap):** manual spot-check scores have not been committed, so judge-vs-human
agreement cannot be reported. The κ/confusion-matrix path is implemented and will populate
automatically once manual scores land at one of the accepted paths. No manual scores were
fabricated to fill this gap.

---

Regeneration is deterministic (sorted keys, no timestamps); re-running the three scripts on
the committed benchmark-01 data reproduces these JSONs byte-for-byte.
