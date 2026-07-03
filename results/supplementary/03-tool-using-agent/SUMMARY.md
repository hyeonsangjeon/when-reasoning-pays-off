# Supplementary statistics — benchmark 03 (tool-using-agent)

Slice: **`03-tool-using-agent`** — the tool-using agent benchmark (function-calling / tool-orchestration tasks).
Cohort: gpt-4o baseline + gpt-5.2 across efforts `none | low | medium | high | xhigh`.
The canonical `gpt-5.2/minimal` cell is empty (`n_used=0`) and is recorded in each script's `skipped_cells`.
Authored sample design is **N=20 samples × R=3 repeats** per non-empty cell (60 rows each); the supplementary
statistics here are **descriptive** (per revision D-003), not universal inference claims.

Source artifacts (this directory):

- `bootstrap_ci.json` — `scripts.stats.bootstrap_ci`
- `cohens_d.json` — `scripts.stats.cohens_d`
- `inter_rater.json` — `scripts.stats.inter_rater`

Upstream inputs: `benchmarks/03-tool-using-agent/runs/*.json` + `benchmarks/03-tool-using-agent/judge_runs/*.json`
(experiment prefix `exp003`, **359 used judge rows of 360 parsed**; 1 outlier row excluded, 6 sibling
non-cohort run files skipped). Cost means are derived per-row and validated byte-for-byte against the
committed `analysis.json` (billing mode `output_only`; see `cost_provenance` in each JSON).

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

The `gpt-5.2/low` cell resamples `n_used=59` (one outlier row excluded); all other non-empty
cells use `n_used=60`. No warnings outside the empty-cell skips.

## 2. Effect sizes (method choice)

Pairwise comparisons: **60 rows** (15 cell pairs × 4 metrics). Every row uses
**Cliff's δ** (`method="cliffs_delta"`). Rationale recorded in each row's
`method_selection`: effective independent **N=20 < 30**, so the documented selection rule
takes the nonparametric branch; `quality` is additionally ordinal. Cohen's d is *implemented*
but not selected for any benchmark-03 cell. **No effect size here should be read as a
population-level claim** — magnitudes are Cliff's-δ thresholds (negligible/small/medium/large),
direction follows canonical cell order (positive ⇒ cell B larger).

Magnitude distribution: **40 negligible, 15 small, 3 medium, 2 large** — i.e. effects on this
slice are predominantly negligible-to-small, unlike the strong cost/latency separations seen on
benchmarks 01–02.

gpt-4o baseline → gpt-5.2 (per metric, Cliff's δ):

| Metric | none | low | medium | high | xhigh |
| --- | --- | --- | --- | --- | --- |
| cost | −0.295 small | −0.275 small | −0.265 small | −0.250 small | −0.180 small |
| latency | −0.155 small | −0.080 negligible | −0.035 negligible | +0.060 negligible | +0.045 negligible |
| reasoning_tokens | +0.000 negligible | +0.200 small | +0.250 small | +0.400 medium | +0.500 large |
| quality | +0.055 negligible | +0.100 negligible | +0.055 negligible | +0.055 negligible | +0.100 negligible |

Read honestly:

- **reasoning_tokens** is the only metric with a clear monotone rise with effort, growing from
  negligible (`none`) to **large at `xhigh` (+0.50)** — added effort buys more reasoning tokens.
- **cost** dominance is a uniformly **small negative** δ (gpt-5.2 medians sit below gpt-4o).
  Note the *means* tell a complementary story: within gpt-5.2, cost rises monotonically with
  effort (none `$0.00276` → xhigh `$0.00350`) and the `xhigh` mean (`$0.00350`) **exceeds** the
  gpt-4o baseline mean (`$0.00329`), even though the median-based Cliff's δ stays small-negative
  (−0.18). This is a mixed mean-vs-dominance picture, not a single threshold.
- **latency** is negligible across efforts (small-negative only at `none`); effort does not
  move wall-clock time on this slice.
- **quality is negligible across every effort (+0.06 to +0.10)** — this is a **ceiling / plateau**,
  not a quality improvement. gpt-4o is already at mean quality **1.85** and the gpt-5.2 cells sit at
  **1.97 / 2.00 / 1.98 / 1.97 / 2.00** (none / low / medium / high / xhigh). The gpt-5.2 means are
  **non-monotonic** (they oscillate just under the 2.0 ceiling), so **no monotonic quality gain from
  added effort is claimed** — the directional edge is a small, flat near-ceiling offset only.

(Full pairwise matrix including gpt-5.2 effort-vs-effort pairs is in `cohens_d.json`.)

## 3. Inter-rater agreement

**Status: `manual_scores_missing` — not computable yet.** This is a methodology gap, not a result.

- Rubric is integer ordinal `0|1|2`; selected statistic is **percent agreement + Cohen's κ**
  (unweighted primary, linear-weighted supplementary). **ICC = `null`** (no continuous graded rubric).
- Judge scores available: **359**. Manual reviewer scores found: **0** (`n_overlap=0`).
- `percent_agreement`, `cohens_kappa`, `linear_weighted_kappa`, `icc` are all `null`.
- Expected ≥ **36** manual scores (10% per non-empty cell). A deterministic 36-row
  `manual_review_queue` (`reviewer_score=null`) is emitted for a reviewer to fill.
- Accepted manual-score paths checked (none present):
  `benchmarks/03-tool-using-agent/manual_spot_checks.json`,
  `benchmarks/03-tool-using-agent/manual_spot_checks/*.json`,
  `results/supplementary/03-tool-using-agent/manual_spot_checks.json`.

**Caveat (T-023 gap):** manual spot-check scores have not been committed, so judge-vs-human
agreement cannot be reported. The κ/confusion-matrix path is implemented and will populate
automatically once manual scores land at one of the accepted paths. No manual scores were
fabricated to fill this gap.

---

Regeneration is deterministic (sorted keys, no timestamps); re-running the three scripts on
the committed benchmark-03 data reproduces these JSONs byte-for-byte.
