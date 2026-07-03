"""Unit tests for ``scripts/stats/inter_rater.py`` (revision Phase 2, T-024).

Pure tests over tiny synthetic inputs. The synthetic ``(judge, manual)`` pairs
and manual-score records here are **test-only fixtures** — they never touch the
repository's production manual spot-check data (which is intentionally absent;
see T-023). Coverage: Cohen's kappa (weighted/unweighted, degenerate/undefined
cases), percent agreement, confusion matrix, manual-score discovery/parsing
(missing files, malformed records, out-of-range scores), effort normalization,
even-spaced review-queue sampling, and the 10% quota.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.stats import inter_rater as ir  # noqa: E402


# ----------------------------------------------------------------------------
# cohens_kappa: perfect, undefined, partial, weighted
# ----------------------------------------------------------------------------


def test_cohens_kappa_empty_is_none() -> None:
    assert ir.cohens_kappa([]) is None


def test_cohens_kappa_perfect_agreement_is_one() -> None:
    pairs = [(0, 0), (1, 1), (2, 2), (0, 0), (2, 2)]
    assert ir.cohens_kappa(pairs) == pytest.approx(1.0)


def test_cohens_kappa_undefined_when_all_one_category() -> None:
    # Both raters always say 2 -> expected agreement is 1 -> 1 - p_e == 0 -> None.
    pairs = [(2, 2), (2, 2), (2, 2)]
    assert ir.cohens_kappa(pairs) is None


def test_cohens_kappa_partial_agreement_between_zero_and_one() -> None:
    pairs = [(0, 0), (1, 1), (2, 2), (0, 1), (1, 0), (2, 1)]
    k = ir.cohens_kappa(pairs)
    assert k is not None and 0.0 < k < 1.0


def test_cohens_kappa_weighted_rewards_near_misses() -> None:
    # Off-by-one disagreements: linear-weighted kappa should exceed unweighted.
    pairs = [(0, 0), (1, 1), (2, 2), (0, 1), (1, 2), (2, 1)]
    unweighted = ir.cohens_kappa(pairs, weighted=False)
    weighted = ir.cohens_kappa(pairs, weighted=True)
    assert unweighted is not None and weighted is not None
    assert weighted > unweighted


# ----------------------------------------------------------------------------
# percent_agreement
# ----------------------------------------------------------------------------


def test_percent_agreement_empty_is_none() -> None:
    assert ir.percent_agreement([]) is None


def test_percent_agreement_counts_exact_matches() -> None:
    pairs = [(0, 0), (1, 1), (2, 0), (2, 2)]
    assert ir.percent_agreement(pairs) == pytest.approx(0.75)


# ----------------------------------------------------------------------------
# _confusion_matrix
# ----------------------------------------------------------------------------


def test_confusion_matrix_tallies_by_category() -> None:
    pairs = [(0, 0), (0, 1), (2, 2)]
    matrix = ir._confusion_matrix(pairs)
    assert matrix["0"]["0"] == 1
    assert matrix["0"]["1"] == 1
    assert matrix["2"]["2"] == 1
    assert matrix["1"]["1"] == 0
    # Full 3x3 over the 0|1|2 categories.
    assert set(matrix) == {"0", "1", "2"}


# ----------------------------------------------------------------------------
# _kappa_interpretation: Landis & Koch bands
# ----------------------------------------------------------------------------


def test_kappa_interpretation_bands() -> None:
    assert ir._kappa_interpretation(None) is None
    assert ir._kappa_interpretation(-0.1) == "poor"
    assert ir._kappa_interpretation(0.1) == "slight"
    assert ir._kappa_interpretation(0.5) == "moderate"
    assert ir._kappa_interpretation(1.0) == "almost_perfect"


# ----------------------------------------------------------------------------
# _normalize_effort
# ----------------------------------------------------------------------------


def test_normalize_effort_collapses_null_spellings_to_none() -> None:
    assert ir._normalize_effort(None) is None
    assert ir._normalize_effort("") is None
    assert ir._normalize_effort("null") is None


def test_normalize_effort_preserves_real_effort_tiers() -> None:
    assert ir._normalize_effort("low") == "low"
    assert ir._normalize_effort("None") == "none"  # textual 'none' is a real tier
    assert ir._normalize_effort("baseline") == "baseline"


# ----------------------------------------------------------------------------
# _records_from_obj
# ----------------------------------------------------------------------------


def test_records_from_obj_accepts_bare_list() -> None:
    recs = ir._records_from_obj([{"sample_id": "s1"}, "junk", {"sample_id": "s2"}])
    assert recs == [{"sample_id": "s1"}, {"sample_id": "s2"}]


def test_records_from_obj_accepts_wrapper_keys() -> None:
    assert ir._records_from_obj({"scores": [{"x": 1}]}) == [{"x": 1}]
    assert ir._records_from_obj({"records": [{"y": 2}]}) == [{"y": 2}]
    assert ir._records_from_obj({"nope": []}) == []
    assert ir._records_from_obj(42) == []


# ----------------------------------------------------------------------------
# _load_manual_scores: missing files, parsing, validation (test-only fixtures)
# ----------------------------------------------------------------------------


def test_load_manual_scores_missing_returns_empty(tmp_path: pathlib.Path) -> None:
    scores, found, warnings = ir._load_manual_scores(
        "01-short-factual",
        benchmarks_dir=tmp_path / "benchmarks",
        output_dir=tmp_path / "out",
    )
    assert scores == {}
    assert found == []
    assert warnings == []


def test_load_manual_scores_parses_valid_records(tmp_path: pathlib.Path) -> None:
    bench_dir = tmp_path / "benchmarks" / "01-short-factual"
    bench_dir.mkdir(parents=True)
    # Test-only synthetic manual pairs — NOT production reviewer data.
    (bench_dir / "manual_spot_checks.json").write_text(
        json.dumps(
            [
                {"sample_id": "s1", "model": "gpt-5.2", "effort": "low",
                 "repeat": 0, "reviewer_score": 2},
                {"sample_id": "s2", "model": "gpt-4o", "effort": None,
                 "repeat": 1, "reviewer_score": 0},
            ]
        ),
        encoding="utf-8",
    )
    scores, found, warnings = ir._load_manual_scores(
        "01-short-factual",
        benchmarks_dir=tmp_path / "benchmarks",
        output_dir=tmp_path / "out",
    )
    assert scores[("s1", "gpt-5.2", "low", 0)] == 2
    assert scores[("s2", "gpt-4o", None, 1)] == 0
    assert len(found) == 1


def test_load_manual_scores_skips_malformed_and_out_of_range(
    tmp_path: pathlib.Path,
) -> None:
    bench_dir = tmp_path / "benchmarks" / "01-short-factual"
    bench_dir.mkdir(parents=True)
    (bench_dir / "manual_spot_checks.json").write_text(
        json.dumps(
            [
                {"model": "gpt-5.2", "effort": "low", "repeat": 0,
                 "reviewer_score": 1},  # missing sample_id
                {"sample_id": "s2", "model": "gpt-5.2", "effort": "low",
                 "repeat": 0, "reviewer_score": 5},  # out of rubric range
                {"sample_id": "s3", "model": "gpt-5.2", "effort": "low",
                 "repeat": "x", "reviewer_score": 1},  # non-int repeat
                {"sample_id": "s4", "model": "gpt-5.2", "effort": "low",
                 "repeat": 0, "reviewer_score": 1},  # valid
            ]
        ),
        encoding="utf-8",
    )
    scores, found, warnings = ir._load_manual_scores(
        "01-short-factual",
        benchmarks_dir=tmp_path / "benchmarks",
        output_dir=tmp_path / "out",
    )
    assert scores == {("s4", "gpt-5.2", "low", 0): 1}
    assert len(found) == 1
    assert len(warnings) >= 3


def test_load_manual_scores_accepts_manual_score_alias(tmp_path: pathlib.Path) -> None:
    bench_dir = tmp_path / "benchmarks" / "01-short-factual"
    bench_dir.mkdir(parents=True)
    (bench_dir / "manual_spot_checks.json").write_text(
        json.dumps(
            {"scores": [{"sample_id": "s1", "model": "gpt-4o", "effort": None,
                         "repeat": 0, "manual_score": 2}]}
        ),
        encoding="utf-8",
    )
    scores, found, _ = ir._load_manual_scores(
        "01-short-factual",
        benchmarks_dir=tmp_path / "benchmarks",
        output_dir=tmp_path / "out",
    )
    assert scores == {("s1", "gpt-4o", None, 0): 2}


def test_load_manual_scores_output_dir_path_is_root_invariant(
    tmp_path: pathlib.Path,
) -> None:
    """Manual scores under output_dir record the stable logical path.

    Regression for T-064: a committed inter_rater.json generated with
    ``--output-dir results/supplementary`` records the manual source as
    ``results/supplementary/<benchmark>/manual_spot_checks.json``. The repro
    check pre-seeds the same file into a system tmp ``output_dir`` outside the
    repo, and the regenerated report must record the *same* logical path (not
    an absolute ``/tmp/...`` path) so the byte-for-byte comparison still holds.
    """
    benchmark = "01-short-factual"
    out_dir = tmp_path / "alpha" / "beta" / "tmp-output-root"
    (out_dir / benchmark).mkdir(parents=True)
    (out_dir / benchmark / "manual_spot_checks.json").write_text(
        json.dumps(
            [{"sample_id": "s1", "model": "gpt-5.2", "effort": "low",
              "repeat": 0, "reviewer_score": 2}]
        ),
        encoding="utf-8",
    )

    scores, found, warnings = ir._load_manual_scores(
        benchmark,
        benchmarks_dir=tmp_path / "benchmarks",  # no committed inputs here
        output_dir=out_dir,
    )

    assert scores == {("s1", "gpt-5.2", "low", 0): 2}
    assert warnings == []
    # The recorded path is output-root-invariant: the canonical logical
    # location, not the physical tmp output_dir path.
    expected = "results/supplementary/01-short-factual/manual_spot_checks.json"
    assert found == [expected]
    assert str(out_dir) not in found[0]


def test_manual_source_label_invariant_across_output_roots(
    tmp_path: pathlib.Path,
) -> None:
    benchmark = "02-multi-step-reasoning"
    committed_root = tmp_path / "results" / "supplementary"
    tmp_root = tmp_path / "var" / "folders" / "repro-stats-xyz"
    committed = committed_root / benchmark / "manual_spot_checks.json"
    seeded = tmp_root / benchmark / "manual_spot_checks.json"
    committed.parent.mkdir(parents=True)
    seeded.parent.mkdir(parents=True)
    committed.touch()
    seeded.touch()

    label_committed = ir._manual_source_label(committed, output_dir=committed_root)
    label_seeded = ir._manual_source_label(seeded, output_dir=tmp_root)

    assert label_committed == label_seeded
    assert label_committed == (
        "results/supplementary/02-multi-step-reasoning/manual_spot_checks.json"
    )


def test_manual_source_label_keeps_rel_for_benchmarks_inputs(
    tmp_path: pathlib.Path,
) -> None:
    # A benchmarks/ input source is not under output_dir, so it falls through
    # to the ordinary repo-relative _rel rendering (here: an absolute path,
    # because the synthetic tmp tree is outside REPO_ROOT).
    bench_src = tmp_path / "benchmarks" / "01-short-factual" / "manual_spot_checks.json"
    bench_src.parent.mkdir(parents=True)
    bench_src.touch()
    out_dir = tmp_path / "out"
    label = ir._manual_source_label(bench_src, output_dir=out_dir)
    assert label == ir._rel(bench_src)
    assert not label.startswith("results/supplementary/")


# ----------------------------------------------------------------------------
# _even_sample_indices: deterministic even spacing, small N
# ----------------------------------------------------------------------------


def test_even_sample_indices_degenerate_counts() -> None:
    assert ir._even_sample_indices(0, 5) == []
    assert ir._even_sample_indices(5, 0) == []
    assert ir._even_sample_indices(5, 1) == [0]


def test_even_sample_indices_caps_at_n() -> None:
    assert ir._even_sample_indices(3, 10) == [0, 1, 2]


def test_even_sample_indices_spreads_and_is_sorted() -> None:
    picks = ir._even_sample_indices(10, 3)
    assert picks == sorted(picks)
    assert len(picks) == 3
    assert picks[0] == 0 and picks[-1] == 9


def test_even_sample_indices_backfills_on_collisions() -> None:
    # With n small relative to k, rounding collisions must still yield k distinct.
    picks = ir._even_sample_indices(4, 4)
    assert sorted(picks) == [0, 1, 2, 3]


# ----------------------------------------------------------------------------
# _expected_min_manual: 10% quota with floor
# ----------------------------------------------------------------------------


def test_expected_min_manual_quota() -> None:
    assert ir._expected_min_manual(0) == 0
    assert ir._expected_min_manual(1) == 1  # floor of 1 for any non-empty cell
    assert ir._expected_min_manual(10) == 1
    assert ir._expected_min_manual(11) == 2  # ceil(0.1*11)
    assert ir._expected_min_manual(60) == 6


# ----------------------------------------------------------------------------
# _cell_label
# ----------------------------------------------------------------------------


def test_cell_label_baseline_for_none_effort() -> None:
    assert ir._cell_label("gpt-4o", None) == "gpt-4o/baseline"
    assert ir._cell_label("gpt-5.2", "high") == "gpt-5.2/high"
