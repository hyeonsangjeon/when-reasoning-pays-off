# Supplementary statistics — cross-benchmark synthesis

Cross-benchmark roll-up of the Phase 2 supplementary statistics (revision **D-003**)
across the three benchmark slices. Per-benchmark detail lives next to each slice's
JSON artifacts; this file synthesizes, it does **not** restate, those summaries:

- `01-short-factual/SUMMARY.md` — single-shot factual recall
- `02-multi-step-reasoning/SUMMARY.md` — multi-hop / chained-step problems
- `03-tool-using-agent/SUMMARY.md` — function-calling / tool-orchestration tasks

Every slice uses the same cohort shape: gpt-4o baseline + gpt-5.2 across five
efforts, authored design **N=20 samples × R=3 repeats** per non-empty cell, with one
empty canonical cell per slice (`gpt-5.2/none` in 01; `gpt-5.2/minimal` in 02 and 03).
These are **descriptive supplementary statistics for this set of slices**, not
universal inference or significance claims (see *Methodology boundaries* below).

## 1. Artifact inventory by benchmark

Each slice carries the same three deterministic JSON artifacts (sorted keys, no
timestamps; per the per-slice summaries, re-running the scripts reproduces them
byte-for-byte). Producing scripts: `scripts.stats.{bootstrap_ci, cohens_d, inter_rater}`.

| Benchmark | bootstrap_ci.json | cohens_d.json | inter_rater.json | Billing mode | Judge rows used |
| --- | --- | --- | --- | --- | --- |
| 01-short-factual | ✓ | ✓ | ✓ | `reasoning_billed_separately` | 354 / 360 parsed |
| 02-multi-step-reasoning | ✓ | ✓ | ✓ | `output_only` | 360 / 360 parsed |
| 03-tool-using-agent | ✓ | ✓ | ✓ | `output_only` | 359 / 360 parsed |

Cost means in every slice validate byte-for-byte against the committed `analysis.json`
(`cost_validation.status=match`); see `cost_provenance` in each JSON. Benchmark 01 bills
reasoning separately, 02/03 bill output-only — so **cost is not directly comparable
across slices** and is read per-slice only.

## 2. Bootstrap 95% CI availability

Percentile bootstrap, 10,000 resamples, 95% CI, per non-empty cell × metric
(cost / quality / latency / reasoning_tokens). Each slice has 7 canonical cells × 4
metrics = 28 CI slots; the 4 not computed in each slice all belong to that slice's one
empty canonical cell (`empty_canonical_cell`).

| Benchmark | CIs `ok` | CIs not computed | Reason for the 4 skips | Note |
| --- | --- | --- | --- | --- |
| 01-short-factual | 24 | 4 | empty `gpt-5.2/none` cell | — |
| 02-multi-step-reasoning | 24 | 4 | empty `gpt-5.2/minimal` cell | — |
| 03-tool-using-agent | 24 | 4 | empty `gpt-5.2/minimal` cell | `gpt-5.2/low` resamples n=59 (1 outlier excluded) |

**Uniform across slices:** CI coverage is 24/28, identical structure, no warnings
outside the empty-cell skips. The CIs are descriptive over N/R-limited authored samples,
not population intervals.

## 3. Effect-size method summary

`cohens_d.py` emits **60 pairwise rows per slice** (15 cell pairs × 4 metrics).
Across all three slices **every row selects Cliff's δ** (`method="cliffs_delta"`):
the documented selection rule takes the nonparametric branch because effective
independent **N=20 < 30**, and `quality` is additionally ordinal (0/1/2). Cohen's d is
*implemented* but not selected for any cell in any slice. Magnitudes are Cliff's-δ
thresholds (negligible / small / medium / large); direction follows canonical cell
order. **No row is a population-level claim.**

| Benchmark | Rows | Method | Magnitude distribution (neg / small / med / large) |
| --- | --- | --- | --- |
| 01-short-factual | 60 | cliffs_delta (all) | 9 / 6 / 0 / 45 |
| 02-multi-step-reasoning | 60 | cliffs_delta (all) | 11 / 12 / 7 / 30 |
| 03-tool-using-agent | 60 | cliffs_delta (all) | 40 / 15 / 3 / 2 |

Key patterns per slice (gpt-4o → gpt-5.2, directional, this-slice only):

- **01-short-factual:** cost / latency / reasoning-tokens separate **strongly and
  monotonically** (large δ); **quality is negligible-to-small** — no clear quality gain
  from added effort on this slice.
- **02-multi-step-reasoning:** latency and reasoning-tokens rise **strongly and
  monotonically**; **cost is mixed rather than a single threshold** (under `output_only`
  billing `none` is below gpt-4o, `low` is mean-higher but dominance-lower, `medium`
  small-positive, `high`/`xhigh` large-positive); **quality shows a small, consistently
  positive edge (≈ +0.25 δ)** that does **not** grow with effort beyond `none`.
- **03-tool-using-agent:** effects are **predominantly negligible-to-small**;
  reasoning-tokens is the only clearly monotone metric (negligible → large +0.50 at
  xhigh); cost is a uniform small-negative δ with a mixed mean-vs-dominance picture;
  latency negligible; **quality negligible at a near-ceiling plateau** (gpt-4o mean 1.85;
  gpt-5.2 cells 1.97/2.00/1.98/1.97/2.00, **non-monotonic** — no monotonic quality gain).

## 4. Inter-rater status

Uniform across all three slices: **`status="manual_scores_missing"` — not computable.**
This is a methodology gap (T-023), **not** a result. The judge rubric is integer ordinal
`0|1|2`, so the selected statistic is **percent agreement + Cohen's κ** (unweighted
primary, linear-weighted supplementary); **ICC is `null`** (no continuous graded rubric).

| Benchmark | Judge scores | Manual scores | n_overlap | κ / weighted-κ / ICC | Review queue | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 01-short-factual | 354 | 0 | 0 | null / null / null | 36 rows | manual_scores_missing |
| 02-multi-step-reasoning | 360 | 0 | 0 | null / null / null | 36 rows | manual_scores_missing |
| 03-tool-using-agent | 359 | 0 | 0 | null / null / null | 36 rows | manual_scores_missing |

Each slice emits a deterministic **36-row `manual_review_queue`** (`reviewer_score=null`,
10%-per-non-empty-cell quota) for a reviewer to fill. The κ / confusion-matrix path is
implemented and verified on synthetic inputs; it populates automatically once manual
scores land at an accepted path. **No manual scores were fabricated** in any slice to
paper over this gap — judge-vs-human agreement simply cannot be reported yet.

## 5. Cross-benchmark interpretation

Pairing every token/cost/latency finding with its quality finding (D-003 guardrail):

- **Short-factual (01):** the cost / latency / reasoning-token cost of effort moves
  strongly and monotonically, but **quality barely moves** (negligible-to-small δ).
  On this workload shape, added reasoning effort buys spend, not measurable quality.
- **Multi-step-reasoning (02):** a **small, directionally-clear quality edge** exists
  (≈ +0.25 δ), but it reads as a **model-tier / floor effect** — gpt-4o carries 15/60
  judge fails while gpt-5.2 is near-perfect — and it **does not grow monotonically with
  effort** beyond `none`. Effort does not stack additional quality here.
- **Tool-using-agent (03):** a **near-ceiling plateau** — gpt-4o is already at mean
  quality 1.85 and gpt-5.2 sits just under the 2.0 ceiling, non-monotonically. **No
  monotonic quality gain from added effort** is claimable; the directional edge is a
  small, flat near-ceiling offset.

**Shared pattern across all three slices:** reasoning-token (and, where billed into it,
cost/latency) consumption is the metric that responds most clearly and monotonically to
effort, while **quality does not monotonically increase with effort on any of these three
slices** — it is weak (01), a non-growing small edge attributable to model tier (02), or
a near-ceiling plateau (03). On these workload shapes, more effort reliably buys more
reasoning tokens; it does not reliably buy more measured quality.

This is **not** a claim that reasoning effort never helps, nor that it always helps. It
is a statement about **these three slices at this snapshot**: where the task is already
near a quality ceiling or the quality gap is a model-tier floor effect, added effort
spends tokens without a monotone quality return.

## 6. Methodology boundaries

- **Descriptive, not inferential.** Per D-003 these are supplementary descriptive
  statistics over N/R-limited authored samples (N=20, R=3). They are **not** significance
  tests, p-values, or population-level effect estimates. Bootstrap CIs and Cliff's δ are
  reported as descriptive summaries; main-text reporting continues to use mean ± SD per
  frozen methodology v1.0.
- **Effect-size method is fixed by the design.** N=20 (< 30) plus an ordinal quality
  rubric forces Cliff's δ for every row; do not read δ as a Cohen's-d population effect.
- **Quality has an open reliability gap.** Inter-rater agreement is not yet computable in
  any slice (`manual_scores_missing`); all quality findings rest on a single LLM judge
  until manual spot-check scores are committed and κ is computed.
- **Cost is per-slice only.** Billing modes differ (01 reasoning-billed; 02/03
  output-only), so cost magnitudes are not comparable across slices.
- **Single tenant, single region, snapshot in time.** PAYG pricing snapshot
  `pricing/azure-openai-payg-2026-05.yaml` (accessed 2026-05-19). No PTU-specific causal
  claims are made from this PAYG data.
- **Scope of generalization.** Findings describe **this slice / this workload shape**
  only; they are not universal claims about when reasoning pays off.

---

Per-slice provenance, full pairwise δ matrices, and exact cell-level numbers live in the
three per-benchmark `SUMMARY.md` files and their adjacent JSON artifacts. This synthesis
adds no new computation; counts above are read directly from those committed artifacts.
