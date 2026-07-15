# Fixture provenance — benchmarks/01-short-factual/runs/ and judge_runs/

> ## ✅ Status: real swap executed (benchmark 01 is now measured)
>
> The committed `analysis.json`, `analysis.md`, and `results/…/benchmark-01-*`
> chart pairs are now aggregated from the **real Task 007 production cohort**
> (`exp001_short-factual_baseline` / `_gpt4o`, 360 measured calls) plus **360
> real gpt-4o judge runs** committed under
> `benchmarks/01-short-factual/judge_runs_real/`. The aggregate was produced
> offline with the **explicit** flags:
>
> ```
> python -m scripts.analyze_tokens --benchmark 01-short-factual \
>     --experiment-prefix exp001_short-factual_baseline \
>     --judge-dir benchmarks/01-short-factual/judge_runs_real \
>     --out benchmarks/01-short-factual/analysis.json
> ```
>
> Two corrections to the historical narrative below:
> - **The real effort floor is `none`, not `minimal`.** The live gpt-5.2
>   deployment rejects `reasoning.effort = "minimal"` with HTTP 400, so the
>   measured schema is `{none, low, medium, high, xhigh}`. The "5-effort
>   schema with `minimal`" the fixtures were built against was never real;
>   `analyze_tokens` still emits a phantom `minimal` n=0 cell (the union
>   `CANONICAL_EFFORT_ORDER`), which the plotter correctly drops.
> - **The production cohort now HAS judge runs** — in `judge_runs_real/`
>   (below it still reads "no judge runs on disk", true only of the original
>   `judge_runs/` fixture directory).
>
> **Footgun:** `analyze_tokens` still *defaults* `--experiment-prefix` to the
> fixture cohort, so running it with bare flags reproduces the **fixture**
> artifact and would overwrite the committed real `analysis.json`. Always
> pass the explicit `--experiment-prefix` + `--judge-dir` shown above (and in
> `analysis.md` §11) to reproduce the committed numbers. The fixtures are kept
> as a credential-free offline scaffold; they are not the published evidence.

## What is in these directories right now

This branch lands the **Task 008 analysis pipeline** (`scripts/analyze_tokens.py`,
`scripts/plot_results.py`, `scripts/run_judge.py`) and the resulting
`analysis.json` / `analysis.md` / chart pairs. Task 008 sits on top of
**Task 007** (the full 300-call gpt-5.2 effort sweep plus 60-call gpt-4o
baseline). Task 007 was executed earlier under a 4-effort schema
(`{none, low, medium, high, xhigh}`) — those 360 production JSONs landed on
`origin/main` and are still here, **untouched**. They are referenced in the
contract below as the *legacy production cohort*. Task 008 brings a 5-effort
schema (`{minimal, low, medium, high, xhigh}`); to land Task 008 end-to-end
on this branch without re-running Task 007 against the new schema, we
materialize a synthetic *fixture cohort* alongside the production cohort.

The runs/ tree therefore contains, side by side:

1. **8 real smoke-run JSONs** from Phase-1 (`exp_smoke_*`). Genuine Azure
   responses from the smoke gate; kept untouched (append-only invariant).
2. **360 legacy production JSONs from Task 007** with
   `experiment_id = exp001_short-factual_baseline` (gpt-5.2 cells) or
   `experiment_id = exp001_short-factual_baseline_gpt4o` (gpt-4o cells).
   These carry the **superseded 4-effort schema** (gpt-5.2 cells use
   `effort = "none"`, which is not in the Task 008 canonical 5-tier set).
   The fixture marker `"fixture": true` is **absent** from these files.
   They are kept on disk so the analyzer can be re-pointed at them after
   the swap-in below; they are NEVER deleted or modified by this branch.
3. **360 synthetic Task 008 fixtures** with `experiment_id =
   exp008_short-factual_fixture` (gpt-5.2 cells) or
   `exp008_short-factual_fixture_gpt4o` (gpt-4o cells). Every fixture
   JSON carries the marker `"fixture": true` at the top level and a
   `"fixture_note"` field. Filenames include the literal token
   `exp008_short-factual_fixture` so the on-disk filename signals fixture
   provenance too. They are produced by `scripts/_fixture_synth.py` — a
   deterministic, offline synthesizer committed alongside this fixture
   set so the provenance is auditable.

The judge_runs/ directory contains **360 synthetic judge JSONs** generated
in the same pass, also marked `"fixture": true`. The **real** judge runs for
the production cohort live separately under
`benchmarks/01-short-factual/judge_runs_real/` (360 real gpt-4o judge JSONs,
no fixture marker) and are the judge source for the committed `analysis.json`.

## Cohort isolation contract

These fixtures use `experiment_id = exp008_short-factual_fixture` (gpt-5.2)
/ `exp008_short-factual_fixture_gpt4o` (gpt-4o) and live alongside but
separate from Task 007 production raw data
(`exp001_short-factual_baseline` / `exp001_short-factual_baseline_gpt4o`).
`scripts/analyze_tokens.py` defaults `--experiment-prefix` to
`exp008_short-factual_fixture` and filters by experiment_id **before**
running the strict 5-tier schema validator, so the legacy production
files coexist in the same directory without cross-contaminating the
Task 008 aggregate (and without tripping a schema error on the legacy
`effort = "none"` value).

### Swap-in procedure (real Task 007 → analysis.json)

When real Task 007 + judge runs become available against the new 5-effort
schema:

1. Re-run Task 007 with the 5-effort schema
   (`{minimal, low, medium, high, xhigh}`) and land the resulting JSONs
   under `experiment_id = exp001_short-factual_baseline` /
   `exp001_short-factual_baseline_gpt4o`. The new files will overwrite or
   supersede the legacy 4-effort production cohort.
2. Run `python -m scripts.run_judge --benchmark 01-short-factual --confirm`
   to generate the judge runs.
3. Re-run the analyzer pointed at the production cohort **and its real judge
   directory** (this is the command that produced the committed artifact):
   ```
   python -m scripts.analyze_tokens \
       --benchmark 01-short-factual \
       --experiment-prefix exp001_short-factual_baseline \
       --judge-dir benchmarks/01-short-factual/judge_runs_real
   ```
   This emits `benchmarks/01-short-factual/analysis.json` aggregated from
   the real cohort instead of the fixture cohort.
4. Re-run `python -m scripts.plot_results --benchmark 01-short-factual`.
   The plotter consumes `analysis.json`, so chart pairs are regenerated
   from the real data with no flag change required.

The fixture cohort can then be optionally pruned (every file under
runs/ and judge_runs/ with `"fixture": true`).

## Reproducing the fixtures

```
python -m scripts._fixture_synth \
    --dataset benchmarks/01-short-factual/dataset.json \
    --runs-dir benchmarks/01-short-factual/runs \
    --judge-dir benchmarks/01-short-factual/judge_runs \
    --seed 4242
```

Re-running the command over the same target directories overwrites the
fixture files with byte-identical content (deterministic seed +
`_per_row_seed` SHA-256 derivation; immune to PYTHONHASHSEED).

## How fixtures shape realistic outputs

- **Input tokens** anchored to the 240-tokens-per-cell value observed in
  the real smoke runs, varying by sample to exercise the variance code path.
- **Reasoning tokens** follow a monotonic-by-effort profile bounded at
  ~300 tokens for `xhigh` — realistic for short-factual where the rubric is
  ~one sentence. Mean stays well below max_output_tokens=4096 so no
  truncation is expected.
- **Latency** follows an effort-monotonic curve (820 ms gpt-4o → 3050 ms
  gpt-5.2 xhigh) consistent with the 2300-2750 ms gpt-5.2 high values
  observed in smoke.
- **Operational events** (`cold_start`, `retry_count>0`, `truncated_output`)
  injected sparsely (~1-2% each) so the outlier code path is genuinely
  exercised. Flagged rows are inflated past 3σ on output / reasoning /
  latency so they are correctly excluded by `flag_outliers`. **No quality
  outcomes** are used as outlier criteria — that would silently bias the
  quality distribution, in violation of the methodology §8 outlier policy.
- **Judge scores** drawn from a per-(model, effort) categorical
  distribution biased toward score=2 (the null-case benchmark: every
  effort tier passes most samples; quality lift from reasoning is small).
