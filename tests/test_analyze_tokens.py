"""Tests for ``scripts.analyze_tokens``.

Coverage map (.internal/tasks/008-analysis-pipeline.md Test / Verification Plan):

* "Synthetic test: feed analyze_tokens 6 fake JSONs (2 cells × 3 repeats),
  assert mean/std math is exact."
* "Outlier test: inject one 3σ cell with events.cold_start=true, assert it's
  flagged and excluded."
* "scripts.analyze_tokens produces analysis.json whose cell_stats has 6
  entries (1 gpt-4o baseline + 5 gpt-5.2 effort cells)."
* "Byte-stability test: run analyze_tokens.py twice over the same input set;
  the two analysis.json files MUST be byte-identical."
* "Schema-invariant test: feed a synthetic gpt-4o JSON carrying
  reasoning_tokens=42 → loader MUST raise. Feed a synthetic JSON using
  legacy prompt_tokens_details.cached_tokens → loader MUST raise."

These tests are fully offline; no network, no Azure, no real-time variance.
Fixtures are minted in temp directories so they cannot collide with the
checked-in benchmark JSONs.
"""

from __future__ import annotations

import json
import pathlib
import statistics
import subprocess
import sys

import pytest

from scripts.analyze_tokens import (
    CANONICAL_EFFORT_ORDER,
    CellEvent,
    LegacySchemaError,
    MeasurementSchemaError,
    build_analysis,
    flag_outliers,
    load_judge_records,
    load_run_record,
    load_run_records,
    render_json,
)


PRICING_DIR = pathlib.Path(__file__).resolve().parent.parent / "pricing"
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------------
# Helpers — minimal fixture authoring
# ----------------------------------------------------------------------------


def _v1_usage(
    *, input_tokens: int, output_tokens: int, reasoning_tokens: int, cached_tokens: int = 0
) -> dict:
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": cached_tokens},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
        "total_tokens": input_tokens + output_tokens,
    }


def _run_record(
    *,
    sample_id: str,
    model: str,
    effort: str | None,
    repeat: int,
    input_tokens: int = 240,
    output_tokens: int = 12,
    reasoning_tokens: int = 0,
    latency_ms: float = 800.0,
    cold_start: bool = False,
    retry_count: int = 0,
    truncated_output: bool = False,
    experiment_id: str = "exp001_short-factual_baseline",
    sample_idx: int = 0,
    git_commit: str = "test-commit",
) -> dict:
    return {
        "experiment_id": experiment_id,
        "sample_id": sample_id,
        "sample_idx": sample_idx,
        "model": model,
        "deployment_name": model,
        "effort": effort,
        "repeat": repeat,
        "latency_ms": latency_ms,
        "cold_start": cold_start,
        "retry_count": retry_count,
        "truncated_output": truncated_output,
        "git_commit": git_commit,
        "timestamp_utc": "2026-05-20T13:00:00Z",
        "usage": _v1_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
        ),
    }


def _judge_record(
    *,
    sample_id: str,
    model: str,
    effort: str | None,
    repeat: int,
    score: int,
) -> dict:
    return {
        "sample_id": sample_id,
        "model": model,
        "effort": effort,
        "repeat": repeat,
        "score": score,
        "rationale": "test",
        "judge_prompt_sha256": "0" * 64,
    }


def _write_json(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


def _seed_full_benchmark(
    tmp_path: pathlib.Path,
    *,
    n_samples: int = 4,
    repeats: int = 3,
    seed: int = 0,
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """Mint a deterministic, minimal benchmark layout under ``tmp_path``."""
    import random

    rng = random.Random(seed)
    runs_dir = tmp_path / "runs"
    judge_dir = tmp_path / "judge_runs"
    dataset_path = tmp_path / "dataset.json"

    dataset = [
        {
            "id": f"sf_{i + 1:02d}",
            "input": {"x": i},
            "expected_output_shape": "one sentence",
            "quality_rubric_notes": "ok",
            "tags": ["extraction"] if i % 2 == 0 else ["formatting"],
        }
        for i in range(n_samples)
    ]
    dataset_path.write_text(json.dumps(dataset, indent=2), encoding="utf-8")

    file_count = 0
    for s_idx, sample in enumerate(dataset):
        sid = sample["id"]
        # gpt-4o baseline (no effort)
        for r in range(repeats):
            file_count += 1
            payload = _run_record(
                sample_id=sid,
                sample_idx=s_idx,
                model="gpt-4o",
                effort=None,
                repeat=r,
                input_tokens=240 + rng.randint(-3, 3),
                output_tokens=12 + rng.randint(-2, 2),
                reasoning_tokens=0,
                latency_ms=800.0 + rng.uniform(-50, 50),
                experiment_id="exp001_short-factual_baseline_gpt4o",
            )
            _write_json(
                runs_dir / f"r_{file_count:04d}_gpt-4o_null_r{r}.json", payload
            )
            _write_json(
                judge_dir / f"j_{file_count:04d}_gpt-4o_null_r{r}.json",
                _judge_record(
                    sample_id=sid, model="gpt-4o", effort=None, repeat=r, score=2
                ),
            )
        for effort in CANONICAL_EFFORT_ORDER:
            for r in range(repeats):
                file_count += 1
                # ``none`` reasoning baseline is 0; clamp the jitter floor so
                # the synthetic fixture never goes negative (would fail the
                # analyzer's reasoning_tokens >= 0 schema check).
                base_reasoning = {"none": 0, "minimal": 4, "low": 30, "medium": 80, "high": 180, "xhigh": 300}[effort]
                jittered_reasoning = max(0, base_reasoning + rng.randint(-5, 5))
                payload = _run_record(
                    sample_id=sid,
                    sample_idx=s_idx,
                    model="gpt-5.2",
                    effort=effort,
                    repeat=r,
                    input_tokens=240 + rng.randint(-3, 3),
                    output_tokens=14 + rng.randint(-2, 2),
                    reasoning_tokens=jittered_reasoning,
                    latency_ms=1200.0 + rng.uniform(-50, 50),
                    experiment_id="exp001_short-factual_baseline",
                )
                _write_json(
                    runs_dir / f"r_{file_count:04d}_gpt-5.2_{effort}_r{r}.json",
                    payload,
                )
                _write_json(
                    judge_dir / f"j_{file_count:04d}_gpt-5.2_{effort}_r{r}.json",
                    _judge_record(
                        sample_id=sid,
                        model="gpt-5.2",
                        effort=effort,
                        repeat=r,
                        score=2,
                    ),
                )
    return runs_dir, judge_dir, dataset_path


# ----------------------------------------------------------------------------
# load_run_record — schema validation
# ----------------------------------------------------------------------------


def test_loads_a_clean_responses_v1_record(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "ok.json"
    _write_json(
        p,
        _run_record(
            sample_id="sf_01",
            model="gpt-5.2",
            effort="high",
            repeat=0,
            reasoning_tokens=180,
        ),
    )
    rec = load_run_record(p)
    assert rec.model == "gpt-5.2"
    assert rec.effort == "high"
    assert rec.reasoning_tokens == 180
    assert rec.cached_tokens == 0
    assert rec.total_tokens == 240 + 12


def test_rejects_legacy_prompt_tokens_details(tmp_path: pathlib.Path) -> None:
    """legacy ``prompt_tokens_details.cached_tokens`` must raise."""
    p = tmp_path / "legacy.json"
    payload = _run_record(
        sample_id="sf_01", model="gpt-5.2", effort="minimal", repeat=0
    )
    # Replace v1 paths with legacy classic-Completions field name.
    payload["usage"] = {
        "input_tokens": 240,
        "prompt_tokens_details": {"cached_tokens": 0},
        "output_tokens": 12,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 252,
    }
    _write_json(p, payload)
    with pytest.raises(LegacySchemaError):
        load_run_record(p)


def test_rejects_legacy_completion_tokens_details(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "legacy2.json"
    payload = _run_record(
        sample_id="sf_01", model="gpt-5.2", effort="minimal", repeat=0
    )
    payload["usage"] = {
        "input_tokens": 240,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 12,
        "completion_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 252,
    }
    _write_json(p, payload)
    with pytest.raises(LegacySchemaError):
        load_run_record(p)


def test_rejects_gpt4o_with_nonzero_reasoning(tmp_path: pathlib.Path) -> None:
    """gpt-4o cells with reasoning_tokens != 0 are data-integrity failures."""
    p = tmp_path / "gpt4o_bad.json"
    _write_json(
        p,
        _run_record(
            sample_id="sf_01",
            model="gpt-4o",
            effort=None,
            repeat=0,
            reasoning_tokens=42,
        ),
    )
    with pytest.raises(MeasurementSchemaError, match="gpt-4o"):
        load_run_record(p)


def test_rejects_gpt52_without_reasoning_field(tmp_path: pathlib.Path) -> None:
    """gpt-5.2 cells without the reasoning_tokens key must raise — minimal is
    allowed to be 0 but the field itself must exist."""
    p = tmp_path / "gpt52_missing.json"
    payload = _run_record(
        sample_id="sf_01", model="gpt-5.2", effort="minimal", repeat=0
    )
    payload["usage"]["output_tokens_details"] = {}
    _write_json(p, payload)
    with pytest.raises(MeasurementSchemaError, match="reasoning_tokens"):
        load_run_record(p)


def test_rejects_unknown_effort_for_gpt52(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "bad_effort.json"
    payload = _run_record(
        sample_id="sf_01", model="gpt-5.2", effort="ultra", repeat=0
    )
    _write_json(p, payload)
    with pytest.raises(MeasurementSchemaError, match="effort"):
        load_run_record(p)


def test_rejects_gpt4o_with_non_null_effort(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "gpt4o_with_effort.json"
    payload = _run_record(
        sample_id="sf_01", model="gpt-4o", effort="high", repeat=0
    )
    _write_json(p, payload)
    with pytest.raises(MeasurementSchemaError, match="gpt-4o"):
        load_run_record(p)


# ----------------------------------------------------------------------------
# flag_outliers — the spec function
# ----------------------------------------------------------------------------


def test_flag_outliers_only_fires_with_both_3sigma_and_flagged_event() -> None:
    """A 3σ deviation without a flagged event is a finding, not an outlier."""
    values = [100.0] * 20 + [200.0]
    events = [CellEvent(False, 0, False)] * 21
    # No event flagged → no outlier even though the last value is 3σ out.
    flags = flag_outliers(values, events)
    assert flags == [None] * 21


def test_flag_outliers_fires_with_event_and_3sigma() -> None:
    values = [100.0] * 20 + [200.0]
    events = [CellEvent(False, 0, False)] * 20 + [CellEvent(True, 0, False)]
    flags = flag_outliers(values, events)
    assert flags[-1] == "3sigma_with_flagged_event"
    assert all(f is None for f in flags[:-1])


def test_flag_outliers_does_not_fire_for_flagged_but_within_3sigma() -> None:
    """A cold-start row near the mean stays in the aggregate."""
    values = [100.0, 101.0, 99.0, 100.5, 99.5, 101.0]
    events = [CellEvent(False, 0, False)] * 5 + [CellEvent(True, 0, False)]
    flags = flag_outliers(values, events)
    assert all(f is None for f in flags)


def test_flag_outliers_handles_tiny_lists() -> None:
    # n<2 → no stdev → no flags
    assert flag_outliers([1.0], [CellEvent(True, 0, False)]) == [None]
    assert flag_outliers([], []) == []


def test_flag_outliers_misaligned_lengths_raises() -> None:
    with pytest.raises(ValueError):
        flag_outliers([1.0, 2.0], [CellEvent(False, 0, False)])


# ----------------------------------------------------------------------------
# Aggregation math — synthetic 6-row test from the spec
# ----------------------------------------------------------------------------


def test_synthetic_2cells_3repeats_mean_std_exact(tmp_path: pathlib.Path) -> None:
    """6 fake JSONs (2 cells × 3 repeats); mean / std must be the exact
    statistics.mean / statistics.stdev of the inputs (rounded to 6 dp)."""
    runs_dir = tmp_path / "runs"
    judge_dir = tmp_path / "judge_runs"
    dataset_path = tmp_path / "dataset.json"
    dataset = [
        {"id": "sf_01", "input": {}, "expected_output_shape": "", "quality_rubric_notes": "", "tags": []},
    ]
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")

    gpt4o_out = [10, 12, 14]
    gpt52_out = [20, 22, 24]
    gpt52_reasoning = [30, 32, 34]
    for r in range(3):
        _write_json(
            runs_dir / f"r_gpt4o_{r}.json",
            _run_record(
                sample_id="sf_01",
                model="gpt-4o",
                effort=None,
                repeat=r,
                output_tokens=gpt4o_out[r],
                experiment_id="exp001_short-factual_baseline_gpt4o",
            ),
        )
        _write_json(
            runs_dir / f"r_gpt52_{r}.json",
            _run_record(
                sample_id="sf_01",
                model="gpt-5.2",
                effort="high",
                repeat=r,
                output_tokens=gpt52_out[r],
                reasoning_tokens=gpt52_reasoning[r],
            ),
        )

    payload = build_analysis(
        benchmark_name="test-bench",
        runs_dir=runs_dir,
        judge_dir=judge_dir,
        dataset_path=dataset_path,
        pricing_dir=PRICING_DIR,
        experiment_prefix="exp001_short-factual_baseline",
    )
    by_key = {(s["model"], s["effort"]): s for s in payload["cell_stats"]}
    g4 = by_key[("gpt-4o", None)]
    g5 = by_key[("gpt-5.2", "high")]

    assert g4["n_used"] == 3
    assert g4["n_excluded"] == 0
    assert g4["mean_output_tokens"] == round(statistics.mean(gpt4o_out), 6)
    assert g4["std_output_tokens"] == round(statistics.stdev(gpt4o_out), 6)
    assert g4["mean_reasoning_tokens"] == 0.0

    assert g5["n_used"] == 3
    assert g5["mean_output_tokens"] == round(statistics.mean(gpt52_out), 6)
    assert g5["std_output_tokens"] == round(statistics.stdev(gpt52_out), 6)
    assert g5["mean_reasoning_tokens"] == round(statistics.mean(gpt52_reasoning), 6)
    assert g5["std_reasoning_tokens"] == round(statistics.stdev(gpt52_reasoning), 6)

    # Cost figure must originate from cost_calculator (positive, with citation).
    assert g5["mean_usd_per_request"] > 0
    assert g5["pricing_citation_id"] in payload["pricing_citations"]


# ----------------------------------------------------------------------------
# Outlier integration test — cold_start row excluded
# ----------------------------------------------------------------------------


def test_cold_start_3sigma_row_is_flagged_and_excluded(tmp_path: pathlib.Path) -> None:
    """Inject one cold-start row that is > 3σ on latency; it must be flagged
    and excluded from cell_stats, but remain in the raw JSON tree."""
    runs_dir = tmp_path / "runs"
    judge_dir = tmp_path / "judge_runs"
    dataset_path = tmp_path / "dataset.json"
    # 20 tight-variance normal rows + one cold-start spike — with R≈20 the
    # outlier's deviation comfortably exceeds 3σ of the full set.
    dataset_path.write_text(
        json.dumps(
            [{"id": f"sf_{i + 1:02d}", "input": {}, "tags": []} for i in range(21)]
        ),
        encoding="utf-8",
    )
    for i in range(20):
        _write_json(
            runs_dir / f"r{i:02d}.json",
            _run_record(
                sample_id=f"sf_{i + 1:02d}",
                sample_idx=i,
                model="gpt-5.2",
                effort="high",
                repeat=0,
                latency_ms=800.0 + (i % 3) * 0.5,
            ),
        )
    _write_json(
        runs_dir / "r_outlier.json",
        _run_record(
            sample_id="sf_21",
            sample_idx=20,
            model="gpt-5.2",
            effort="high",
            repeat=0,
            latency_ms=2200.0,  # ~700 above the cluster, ~30σ given σ≈0.2
            cold_start=True,
        ),
    )

    payload = build_analysis(
        benchmark_name="t",
        runs_dir=runs_dir,
        judge_dir=judge_dir,
        dataset_path=dataset_path,
        pricing_dir=PRICING_DIR,
        experiment_prefix="exp001_short-factual_baseline",
    )
    g5 = next(s for s in payload["cell_stats"] if s["effort"] == "high")
    assert g5["n_excluded"] == 1
    assert g5["n_used"] == 20
    # The raw row is still present in cells with outlier_reason set.
    raw_outlier = next(c for c in payload["cells"] if c["sample_id"] == "sf_21")
    assert raw_outlier["outlier_reason"] == "3sigma_with_flagged_event"

    # And the row file itself is still on disk — never deleted.
    assert (runs_dir / "r_outlier.json").exists()


def test_quality_outcome_alone_is_never_an_outlier_criterion(
    tmp_path: pathlib.Path,
) -> None:
    """A row with a partial judge score but no operational flag must remain in
    the aggregate. (Spec: "Quality outcomes (e.g. a partial judge score) are
    measurement results, not instrumentation flags, and MUST NOT be used as
    outlier exclusion criteria.")"""
    runs_dir = tmp_path / "runs"
    judge_dir = tmp_path / "judge_runs"
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps([{"id": "sf_01", "input": {}, "tags": []}]), encoding="utf-8")

    for r in range(3):
        _write_json(
            runs_dir / f"r{r}.json",
            _run_record(
                sample_id="sf_01",
                model="gpt-5.2",
                effort="low",
                repeat=r,
                latency_ms=1200.0 + r * 5,
            ),
        )
    # Judge gives a partial score on the first row — must NOT flag it.
    for r, score in enumerate((1, 2, 2)):
        _write_json(
            judge_dir / f"j{r}.json",
            _judge_record(
                sample_id="sf_01", model="gpt-5.2", effort="low", repeat=r, score=score
            ),
        )

    payload = build_analysis(
        benchmark_name="t",
        runs_dir=runs_dir,
        judge_dir=judge_dir,
        dataset_path=dataset_path,
        pricing_dir=PRICING_DIR,
        experiment_prefix="exp001_short-factual_baseline",
    )
    g5 = next(s for s in payload["cell_stats"] if s["effort"] == "low")
    assert g5["n_used"] == 3
    assert g5["n_excluded"] == 0


# ----------------------------------------------------------------------------
# Top-level "6 cell_stats" success-criteria invariant
# ----------------------------------------------------------------------------


def test_cell_stats_has_exactly_seven_entries_in_canonical_order(
    tmp_path: pathlib.Path,
) -> None:
    """Task 009 expanded CANONICAL_EFFORT_ORDER to include both ``none``
    (production Foundry v1 schema, used by Task 007 + Task 009) and
    ``minimal`` (legacy fixture schema, used by Task 008 offline fixtures).
    The canonical cell ordering therefore now has seven rows: the gpt-4o
    baseline plus six gpt-5.2 effort tiers."""
    runs_dir, judge_dir, dataset_path = _seed_full_benchmark(tmp_path)
    payload = build_analysis(
        benchmark_name="t",
        runs_dir=runs_dir,
        judge_dir=judge_dir,
        dataset_path=dataset_path,
        pricing_dir=PRICING_DIR,
        experiment_prefix="exp001_short-factual_baseline",
    )
    assert len(payload["cell_stats"]) == 7
    expected = [
        ("gpt-4o", None),
        ("gpt-5.2", "none"),
        ("gpt-5.2", "minimal"),
        ("gpt-5.2", "low"),
        ("gpt-5.2", "medium"),
        ("gpt-5.2", "high"),
        ("gpt-5.2", "xhigh"),
    ]
    got = [(s["model"], s["effort"]) for s in payload["cell_stats"]]
    assert got == expected


def test_pricing_citation_propagates_into_every_cell_stats(
    tmp_path: pathlib.Path,
) -> None:
    runs_dir, judge_dir, dataset_path = _seed_full_benchmark(tmp_path)
    payload = build_analysis(
        benchmark_name="t",
        runs_dir=runs_dir,
        judge_dir=judge_dir,
        dataset_path=dataset_path,
        pricing_dir=PRICING_DIR,
        experiment_prefix="exp001_short-factual_baseline",
    )
    for s in payload["cell_stats"]:
        cite = payload["pricing_citations"][s["pricing_citation_id"]]
        assert cite["source_url"].startswith("https://")
        assert cite["accessed_date"]
        assert cite["snapshot_path"]
        assert cite["lens"] == "payg"


# ----------------------------------------------------------------------------
# Byte-stability
# ----------------------------------------------------------------------------


def test_two_runs_produce_byte_identical_json(tmp_path: pathlib.Path) -> None:
    runs_dir, judge_dir, dataset_path = _seed_full_benchmark(tmp_path)
    p1 = build_analysis(
        benchmark_name="t",
        runs_dir=runs_dir,
        judge_dir=judge_dir,
        dataset_path=dataset_path,
        pricing_dir=PRICING_DIR,
        experiment_prefix="exp001_short-factual_baseline",
    )
    p2 = build_analysis(
        benchmark_name="t",
        runs_dir=runs_dir,
        judge_dir=judge_dir,
        dataset_path=dataset_path,
        pricing_dir=PRICING_DIR,
        experiment_prefix="exp001_short-factual_baseline",
    )
    assert render_json(p1) == render_json(p2)


def test_cli_invocation_is_idempotent(tmp_path: pathlib.Path) -> None:
    """``python -m scripts.analyze_tokens`` writes byte-identical output on
    successive invocations over the same input set."""
    runs_dir, judge_dir, dataset_path = _seed_full_benchmark(tmp_path)
    out_a = tmp_path / "a.json"
    out_b = tmp_path / "b.json"
    common = [
        sys.executable,
        "-m",
        "scripts.analyze_tokens",
        "--benchmark",
        "t",
        "--runs-dir",
        str(runs_dir),
        "--judge-dir",
        str(judge_dir),
        "--dataset",
        str(dataset_path),
        "--pricing-dir",
        str(PRICING_DIR),
    ]
    res_a = subprocess.run(
        common + ["--out", str(out_a)], capture_output=True, text=True, cwd=REPO_ROOT
    )
    res_b = subprocess.run(
        common + ["--out", str(out_b)], capture_output=True, text=True, cwd=REPO_ROOT
    )
    assert res_a.returncode == 0, res_a.stderr
    assert res_b.returncode == 0, res_b.stderr
    assert out_a.read_bytes() == out_b.read_bytes()


# ----------------------------------------------------------------------------
# Experiment prefix filter — smoke runs ignored by default
# ----------------------------------------------------------------------------


def test_experiment_prefix_filter_excludes_smoke_runs(tmp_path: pathlib.Path) -> None:
    runs_dir = tmp_path / "runs"
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps([{"id": "sf_01", "input": {}, "tags": []}]), encoding="utf-8")

    _write_json(
        runs_dir / "smoke.json",
        _run_record(
            sample_id="sf_01",
            model="gpt-5.2",
            effort="high",
            repeat=0,
            experiment_id="exp_smoke_01",
        ),
    )
    _write_json(
        runs_dir / "prod.json",
        _run_record(
            sample_id="sf_01",
            model="gpt-5.2",
            effort="high",
            repeat=0,
            experiment_id="exp001_short-factual_baseline",
        ),
    )
    records = load_run_records(
        runs_dir, experiment_prefix="exp001_short-factual_baseline"
    )
    assert len(records) == 1
    assert records[0].experiment_id == "exp001_short-factual_baseline"


# ----------------------------------------------------------------------------
# Cross-cohort isolation — default CLI must NOT cross-contaminate when a
# sibling legacy production JSON (different effort schema) co-exists with the
# fixture cohort in the same runs/ directory.
# ----------------------------------------------------------------------------


def test_default_prefix_skips_sibling_legacy_cohort_without_validating_it(
    tmp_path: pathlib.Path,
) -> None:
    """Regression for codex review (BLOCKER A).

    The legacy Task 007 production cohort writes
    ``experiment_id=exp001_short-factual_baseline`` and a 4-effort schema in
    which gpt-5.2 cells carry ``effort='none'`` — a value not in the Task
    008 canonical 5-tier set ``{minimal,low,medium,high,xhigh}``. When the
    user runs the **default** CLI command on a runs/ tree that holds both
    cohorts, the analyzer MUST:

    1. Aggregate only the Task 008 fixture cohort
       (``exp008_short-factual_fixture[ _gpt4o]``).
    2. Skip every legacy ``exp001_short-factual_baseline*`` file BEFORE
       running schema validation — so the sibling cohort's superseded
       ``effort='none'`` value never reaches the validator.

    Failing either invariant cross-contaminates one cohort with the other
    and matches the codex BLOCKER A failure mode.
    """
    from scripts._fixture_synth import (
        FIXTURE_EXPERIMENT_ID_GPT4O,
        FIXTURE_EXPERIMENT_ID_GPT52,
    )

    runs_dir = tmp_path / "runs"
    judge_dir = tmp_path / "judge_runs"
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(
        json.dumps([{"id": "sf_01", "input": {}, "tags": []}]), encoding="utf-8"
    )

    # Fixture cohort row — uses the new default prefix.
    _write_json(
        runs_dir
        / "20260520T130910Z_exp008_short-factual_fixture_000_gpt-5.2_minimal_r0.json",
        _run_record(
            sample_id="sf_01",
            model="gpt-5.2",
            effort="minimal",
            repeat=0,
            reasoning_tokens=4,
            experiment_id=FIXTURE_EXPERIMENT_ID_GPT52,
        ),
    )
    _write_json(
        runs_dir
        / "20260520T130000Z_exp008_short-factual_fixture_gpt4o_000_gpt-4o_null_r0.json",
        _run_record(
            sample_id="sf_01",
            model="gpt-4o",
            effort=None,
            repeat=0,
            experiment_id=FIXTURE_EXPERIMENT_ID_GPT4O,
        ),
    )

    # Sibling legacy production cohort row — same dir, distinct experiment
    # id, and a SUPERSEDED 4-effort schema value ('none') that would blow up
    # the strict validator if it were ever inspected. The peek-then-validate
    # contract must skip this file silently because its experiment_id does
    # not match the default --experiment-prefix.
    legacy_payload = _run_record(
        sample_id="sf_01",
        model="gpt-5.2",
        effort="high",  # ← we'll overwrite this below
        repeat=0,
        experiment_id="exp001_short-factual_baseline",
    )
    legacy_payload["effort"] = "none"  # superseded legacy schema value
    _write_json(
        runs_dir
        / "20260520T205625Z_exp001_short-factual_baseline_000_gpt-5.2_none_r0.json",
        legacy_payload,
    )

    # 1. Loader (default prefix) must not raise on the sibling file.
    records = load_run_records(runs_dir)
    assert {r.experiment_id for r in records} == {
        FIXTURE_EXPERIMENT_ID_GPT4O,
        FIXTURE_EXPERIMENT_ID_GPT52,
    }
    assert len(records) == 2

    # 2. Full default-CLI invocation succeeds end-to-end on the mixed tree.
    out_path = tmp_path / "analysis.json"
    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.analyze_tokens",
            "--benchmark",
            "01-short-factual",
            "--runs-dir",
            str(runs_dir),
            "--judge-dir",
            str(judge_dir),
            "--dataset",
            str(dataset_path),
            "--pricing-dir",
            str(PRICING_DIR),
            "--out",
            str(out_path),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert res.returncode == 0, (
        "default-prefix CLI crashed on mixed-cohort tree (cross-contamination "
        f"regression). stderr={res.stderr}"
    )
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["experiment_prefix"] == "exp008_short-factual_fixture"
    assert set(payload["experiment_ids"]) == {
        FIXTURE_EXPERIMENT_ID_GPT4O,
        FIXTURE_EXPERIMENT_ID_GPT52,
    }
    # Legacy production experiment_id MUST NOT have leaked into the
    # aggregate — that's the cross-contamination invariant.
    assert "exp001_short-factual_baseline" not in payload["experiment_ids"]
    assert payload["run_count"] == 2


def test_peek_experiment_id_returns_none_for_non_dict_or_missing_field(
    tmp_path: pathlib.Path,
) -> None:
    """Peek probe must never raise — see load_run_records docstring."""
    from scripts.analyze_tokens import peek_experiment_id

    list_json = tmp_path / "list.json"
    list_json.write_text("[1, 2, 3]", encoding="utf-8")
    assert peek_experiment_id(list_json) is None

    no_eid = tmp_path / "no_eid.json"
    no_eid.write_text('{"sample_id": "sf_01"}', encoding="utf-8")
    assert peek_experiment_id(no_eid) is None

    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not json", encoding="utf-8")
    assert peek_experiment_id(bad_json) is None

    ok = tmp_path / "ok.json"
    ok.write_text('{"experiment_id": "exp008_short-factual_fixture"}', encoding="utf-8")
    assert peek_experiment_id(ok) == "exp008_short-factual_fixture"


# ----------------------------------------------------------------------------
# Judge record loading — score-range validation
# ----------------------------------------------------------------------------


def test_judge_loader_rejects_out_of_range_score(tmp_path: pathlib.Path) -> None:
    judge_dir = tmp_path / "judge"
    _write_json(
        judge_dir / "bad.json",
        {
            "sample_id": "sf_01",
            "model": "gpt-5.2",
            "effort": "high",
            "repeat": 0,
            "score": 3,  # out of range
        },
    )
    with pytest.raises(MeasurementSchemaError):
        load_judge_records(judge_dir)


def test_judge_loader_returns_empty_on_missing_dir(tmp_path: pathlib.Path) -> None:
    """A missing judge_runs/ dir is graceful — judge pass is optional."""
    assert load_judge_records(tmp_path / "does_not_exist") == []


# ----------------------------------------------------------------------------
# Task 010: tool_efficiency_breakdown aggregation (gated)
# ----------------------------------------------------------------------------


def test_tool_efficiency_breakdown_absent_when_judges_lack_field(tmp_path):
    """Benchmarks 01/02 reproduce: judge JSONs without tool_efficiency_score
    must not trigger the tool_efficiency_breakdown block."""
    import json
    import pathlib
    from scripts import analyze_tokens as at

    runs_dir = tmp_path / "runs"
    judge_dir = tmp_path / "judge_runs"
    runs_dir.mkdir()
    judge_dir.mkdir()
    # One synthetic run + one synthetic judge JSON, neither with tool_efficiency_score
    run_path = runs_dir / "20260524T130000Z_exptest_001_gpt-4o_null_r0.json"
    run_path.write_text(
        json.dumps(
            {
                "experiment_id": "exptest_no_tools",
                "sample_id": "s_01",
                "model": "gpt-4o",
                "effort": None,
                "repeat": 0,
                "latency_ms": 1000.0,
                "usage": {
                    "input_tokens": 100,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 50,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 150,
                },
                "cold_start": False,
                "retry_count": 0,
                "truncated_output": False,
            }
        ),
        encoding="utf-8",
    )
    judge_path = judge_dir / "judge_001_gpt-4o_null_r0.json"
    judge_path.write_text(
        json.dumps(
            {
                "sample_id": "s_01",
                "model": "gpt-4o",
                "effort": None,
                "repeat": 0,
                "score": 2,
                "rationale": "ok",
            }
        ),
        encoding="utf-8",
    )
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(
        json.dumps([{"id": "s_01", "input": "x", "tags": ["t1"]}]),
        encoding="utf-8",
    )
    payload = at.build_analysis(
        benchmark_name="test",
        runs_dir=runs_dir,
        judge_dir=judge_dir,
        dataset_path=dataset_path,
        pricing_dir=pathlib.Path(__file__).resolve().parent / "fixtures" / "pricing",
        experiment_prefix="exptest",
    )
    assert "tool_efficiency_breakdown" not in payload


def test_tool_efficiency_breakdown_present_when_judges_carry_field(tmp_path):
    """Benchmark 03 reproduces: judge JSONs WITH tool_efficiency_score
    must trigger the tool_efficiency_breakdown block."""
    import json
    import pathlib
    from scripts import analyze_tokens as at

    runs_dir = tmp_path / "runs"
    judge_dir = tmp_path / "judge_runs"
    runs_dir.mkdir()
    judge_dir.mkdir()
    for r in range(3):
        run_path = runs_dir / f"20260524T1300{r:02d}Z_exptest_001_gpt-5.2_low_r{r}.json"
        run_path.write_text(
            json.dumps(
                {
                    "experiment_id": "exptest_tool_loop",
                    "sample_id": "s_01",
                    "model": "gpt-5.2",
                    "effort": "low",
                    "repeat": r,
                    "latency_ms": 1000.0,
                    "usage": {
                        "input_tokens": 100,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens": 50,
                        "output_tokens_details": {"reasoning_tokens": 10},
                        "total_tokens": 150,
                    },
                    "tool_calls": [{"iteration": 1, "tool_name": "calculator"}],
                    "cold_start": False,
                    "retry_count": 0,
                    "truncated_output": False,
                }
            ),
            encoding="utf-8",
        )
        judge_path = judge_dir / f"judge_001_gpt-5.2_low_r{r}.json"
        judge_path.write_text(
            json.dumps(
                {
                    "sample_id": "s_01",
                    "model": "gpt-5.2",
                    "effort": "low",
                    "repeat": r,
                    "score": 2,
                    "tool_efficiency_score": 0.9 + r * 0.01,
                    "rationale": "ok",
                }
            ),
            encoding="utf-8",
        )
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(
        json.dumps([{"id": "s_01", "input": "x", "tags": ["t1"]}]),
        encoding="utf-8",
    )
    payload = at.build_analysis(
        benchmark_name="test",
        runs_dir=runs_dir,
        judge_dir=judge_dir,
        dataset_path=dataset_path,
        pricing_dir=pathlib.Path(__file__).resolve().parent / "fixtures" / "pricing",
        experiment_prefix="exptest",
    )
    assert "tool_efficiency_breakdown" in payload
    breakdown = payload["tool_efficiency_breakdown"]
    by_cell = breakdown["by_cell"]
    assert len(by_cell) == 1
    cell = by_cell[0]
    assert cell["model"] == "gpt-5.2"
    assert cell["effort"] == "low"
    assert cell["n"] == 3
    assert 0.89 <= cell["mean_tool_efficiency_score"] <= 0.93
    # p10, p50, p90 present and in range
    for k in ("p10_tool_efficiency_score", "p50_tool_efficiency_score", "p90_tool_efficiency_score"):
        assert 0.0 <= cell[k] <= 1.0
    # mean tool-call count from raw JSON tool_calls list length
    assert cell["mean_tool_call_count"] == 1.0


def test_tool_efficiency_score_out_of_range_raises(tmp_path):
    """An out-of-range tool_efficiency_score must raise during judge JSON load."""
    import json
    import pytest
    from scripts import analyze_tokens as at

    judge_dir = tmp_path / "judge_runs"
    judge_dir.mkdir()
    judge_path = judge_dir / "judge_001_gpt-5.2_low_r0.json"
    judge_path.write_text(
        json.dumps(
            {
                "sample_id": "s_01",
                "model": "gpt-5.2",
                "effort": "low",
                "repeat": 0,
                "score": 2,
                "tool_efficiency_score": 1.5,  # out of range
                "rationale": "bad",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(at.MeasurementSchemaError):
        at.load_judge_records(judge_dir)
