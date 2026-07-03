"""Unit tests for ``scripts/stats/cohens_d.py`` (revision Phase 2, T-024).

Pure tests over tiny synthetic inputs: the effect-size primitives (Cohen's d,
Cliff's delta), the method-selection rule (small-N nonparametric branch,
ordinal metric branch, degenerate-variance fallback), per-sample aggregation of
R-repeat pseudoreplicates, magnitude binning, and summary stats. No network, no
benchmark full runs.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.stats import cohens_d as cd  # noqa: E402


# ----------------------------------------------------------------------------
# cohens_d: small-N guard, degenerate variance, sign/direction
# ----------------------------------------------------------------------------


def test_cohens_d_small_n_returns_none() -> None:
    assert cd.cohens_d([1.0], [2.0, 3.0]) is None
    assert cd.cohens_d([1.0, 2.0], [3.0]) is None


def test_cohens_d_zero_pooled_variance_returns_none() -> None:
    # No spread in either group -> pooled variance 0 -> undefined.
    assert cd.cohens_d([5.0, 5.0, 5.0], [9.0, 9.0, 9.0]) is None


def test_cohens_d_positive_when_b_larger() -> None:
    d = cd.cohens_d([1.0, 2.0, 3.0], [4.0, 5.0, 6.0])
    assert d is not None and d > 0


def test_cohens_d_matches_closed_form() -> None:
    a = [1.0, 2.0, 3.0]
    b = [2.0, 3.0, 4.0]
    # means 2 and 3, each var=1 (ddof=1), pooled var=1 -> d = (3-2)/1 = 1.0
    assert cd.cohens_d(a, b) == pytest.approx(1.0)


# ----------------------------------------------------------------------------
# cliffs_delta: ties, full dominance, empty
# ----------------------------------------------------------------------------


def test_cliffs_delta_full_dominance_is_one() -> None:
    assert cd.cliffs_delta([1.0, 2.0], [3.0, 4.0]) == pytest.approx(1.0)


def test_cliffs_delta_reverse_dominance_is_minus_one() -> None:
    assert cd.cliffs_delta([3.0, 4.0], [1.0, 2.0]) == pytest.approx(-1.0)


def test_cliffs_delta_all_ties_is_zero() -> None:
    assert cd.cliffs_delta([2.0, 2.0, 2.0], [2.0, 2.0, 2.0]) == pytest.approx(0.0)


def test_cliffs_delta_empty_group_is_zero() -> None:
    assert cd.cliffs_delta([], [1.0, 2.0]) == 0.0


# ----------------------------------------------------------------------------
# _magnitude: threshold binning on |value|
# ----------------------------------------------------------------------------


def test_magnitude_bins_by_absolute_value() -> None:
    th = cd._COHENS_D_THRESHOLDS
    assert cd._magnitude(0.1, th) == "negligible"
    assert cd._magnitude(-0.3, th) == "small"  # uses abs value
    assert cd._magnitude(0.6, th) == "medium"
    assert cd._magnitude(1.5, th) == "large"


# ----------------------------------------------------------------------------
# _select_method: branch selection (the T-022 reliability rule)
# ----------------------------------------------------------------------------


def test_select_method_small_n_continuous_uses_cliffs_delta() -> None:
    # Effective N below COHENS_D_MIN_N -> nonparametric branch.
    a = [1.0, 2.0, 3.0]
    b = [4.0, 5.0, 6.0]
    method, effect, detail, warnings = cd._select_method("latency", a, b)
    assert method == "cliffs_delta"
    assert detail["method"] == "cliffs_delta"
    assert detail["effective_n"] == 3
    assert "< 30" in detail["reason"] or "< {}".format(cd.COHENS_D_MIN_N) in detail["reason"]
    assert effect == pytest.approx(1.0)
    assert warnings  # supplementary descriptive caveat recorded


def test_select_method_ordinal_metric_always_nonparametric() -> None:
    # Even with large N, the ordinal quality score uses the rank statistic.
    a = [0.0] * 40
    b = [2.0] * 40
    method, effect, detail, _ = cd._select_method("quality", a, b)
    assert method == "cliffs_delta"
    assert detail["metric_type"] == "ordinal"
    assert effect == pytest.approx(1.0)


def test_select_method_large_n_continuous_uses_cohens_d() -> None:
    # Sufficient N + continuous + non-degenerate variance -> Cohen's d.
    a = [float(i % 5) for i in range(40)]
    b = [float(i % 5) + 3.0 for i in range(40)]
    method, effect, detail, _ = cd._select_method("latency", a, b)
    assert method == "cohens_d"
    assert detail["method"] == "cohens_d"
    assert effect is not None and effect > 0


def test_select_method_large_n_but_degenerate_variance_falls_back() -> None:
    # Sufficient N + continuous, but zero spread -> Cohen's d undefined -> Cliff's.
    a = [1.0] * 40
    b = [2.0] * 40
    method, effect, detail, _ = cd._select_method("latency", a, b)
    assert method == "cliffs_delta"
    assert "degenerate" in detail["reason"]
    assert effect == pytest.approx(1.0)


# ----------------------------------------------------------------------------
# _cell_metric_samples: collapse R-repeat pseudoreplicates, missing fields
# ----------------------------------------------------------------------------


def test_cell_metric_samples_collapses_repeats_to_one_per_sample() -> None:
    rows = [
        {"sample_id": "s1", "repeat": 0, "judge_score": 0},
        {"sample_id": "s1", "repeat": 1, "judge_score": 2},  # mean -> 1.0
        {"sample_id": "s2", "repeat": 0, "judge_score": 2},
    ]
    values, n_used, n_missing, warnings = cd._cell_metric_samples(
        rows, "quality", pricing=None, bill_reasoning=False
    )
    # One value per sample, ordered by sample_id.
    assert values == [pytest.approx(1.0), pytest.approx(2.0)]
    assert n_used == 3
    assert n_missing == 0
    assert warnings == []


def test_cell_metric_samples_counts_missing_and_warns() -> None:
    rows = [
        {"sample_id": "s1", "repeat": 0, "latency_ms": 100},
        {"sample_id": "s1", "repeat": 1, "latency_ms": None},  # missing
        {"sample_id": "s2", "repeat": 0, "latency_ms": 300},
    ]
    values, n_used, n_missing, warnings = cd._cell_metric_samples(
        rows, "latency", pricing=None, bill_reasoning=False
    )
    assert values == [pytest.approx(100.0), pytest.approx(300.0)]
    assert n_used == 2
    assert n_missing == 1
    assert any("missing" in w for w in warnings)


def test_cell_metric_samples_empty_cell_yields_no_values() -> None:
    values, n_used, n_missing, warnings = cd._cell_metric_samples(
        [], "quality", pricing=None, bill_reasoning=False
    )
    assert values == []
    assert n_used == 0
    assert n_missing == 0


# ----------------------------------------------------------------------------
# _summary_stats + _cell_label
# ----------------------------------------------------------------------------


def test_summary_stats_empty_is_all_none() -> None:
    assert cd._summary_stats([]) == {"mean": None, "median": None, "sd": None}


def test_summary_stats_single_value_zero_sd() -> None:
    out = cd._summary_stats([4.0])
    assert out["mean"] == 4.0
    assert out["median"] == 4.0
    assert out["sd"] == 0.0


def test_summary_stats_multiple_values() -> None:
    out = cd._summary_stats([1.0, 3.0])
    assert out["mean"] == pytest.approx(2.0)
    assert out["median"] == pytest.approx(2.0)
    assert out["sd"] == pytest.approx(1.4142135623730951, abs=1e-6)


def test_cell_label_baseline_for_none_effort() -> None:
    assert cd._cell_label("gpt-4o", None) == "gpt-4o/baseline"
    assert cd._cell_label("gpt-5.2", "low") == "gpt-5.2/low"
