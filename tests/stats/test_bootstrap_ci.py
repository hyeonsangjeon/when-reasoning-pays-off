"""Unit tests for ``scripts/stats/bootstrap_ci.py`` (revision Phase 2, T-024).

All tests are pure: tiny synthetic inputs, no network, no benchmark full runs,
no wall-clock dependency. They exercise the pure helpers — the bootstrap core,
per-row cost / billing-mode detection, metric extraction, cohort/prefix
resolution, and deterministic ordering — with explicit edge cases (empty cells,
``n < 2``, missing fields, degenerate values, ties in cell ordering).
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.stats import bootstrap_ci as bci  # noqa: E402


# ----------------------------------------------------------------------------
# _stable_seed: deterministic, hash-randomization independent
# ----------------------------------------------------------------------------


def test_stable_seed_is_deterministic_across_calls() -> None:
    a = bci._stable_seed("01", "gpt-5.2", "low", "cost", base_seed=7)
    b = bci._stable_seed("01", "gpt-5.2", "low", "cost", base_seed=7)
    assert a == b
    assert 0 <= a < 2**64


def test_stable_seed_varies_with_parts_and_base_seed() -> None:
    base = bci._stable_seed("01", "gpt-5.2", "low", "cost", base_seed=7)
    assert base != bci._stable_seed("01", "gpt-5.2", "high", "cost", base_seed=7)
    assert base != bci._stable_seed("01", "gpt-5.2", "low", "cost", base_seed=8)


# ----------------------------------------------------------------------------
# bootstrap_ci: small-N branch selection and CI bracketing
# ----------------------------------------------------------------------------


def test_bootstrap_ci_empty_cell_returns_nulls_with_warning() -> None:
    out = bci.bootstrap_ci([], resamples=100, ci_level=0.95, seed=1)
    assert out["n_used"] == 0
    assert out["mean"] is None
    assert out["ci_low"] is None and out["ci_high"] is None
    assert out["warnings"] and "no observations" in out["warnings"][0]


def test_bootstrap_ci_single_observation_degenerates_to_point_estimate() -> None:
    out = bci.bootstrap_ci([4.0], resamples=100, ci_level=0.95, seed=1)
    assert out["n_used"] == 1
    assert out["mean"] == out["ci_low"] == out["ci_high"] == 4.0
    assert any("n=1 < 2" in w for w in out["warnings"])


def test_bootstrap_ci_constant_values_collapse_interval_to_mean() -> None:
    out = bci.bootstrap_ci([2.0, 2.0, 2.0, 2.0], resamples=200, ci_level=0.95, seed=3)
    assert out["n_used"] == 4
    assert out["mean"] == out["ci_low"] == out["ci_high"] == 2.0
    assert out["warnings"] == []


def test_bootstrap_ci_is_deterministic_and_brackets_mean() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    first = bci.bootstrap_ci(values, resamples=2000, ci_level=0.95, seed=11)
    second = bci.bootstrap_ci(values, resamples=2000, ci_level=0.95, seed=11)
    assert first == second
    assert first["ci_low"] <= first["mean"] <= first["ci_high"]
    assert first["mean"] == pytest.approx(3.5)


# ----------------------------------------------------------------------------
# _row_cost_usd + _detect_billing_mode (synthetic pricing stubs)
# ----------------------------------------------------------------------------


class _StubRates:
    """Minimal non-gpt-5.2 rate stub (no reasoning line)."""

    input_per_1m_usd = 1.0
    cached_input_per_1m_usd = 0.5
    output_per_1m_usd = 2.0


def _gpt52_rates() -> bci.Gpt52Rates:
    # Build via the real dataclass so isinstance(rates, Gpt52Rates) holds.
    return bci.Gpt52Rates(
        input_per_1m_usd=1.0,
        cached_input_per_1m_usd=0.5,
        output_per_1m_usd=2.0,
        reasoning_per_1m_usd=4.0,
    )


class _StubPricing:
    def __init__(self, models: dict[str, object]) -> None:
        self.models = models


def test_row_cost_usd_output_only_ignores_reasoning() -> None:
    pricing = _StubPricing({"gpt-4o": _StubRates()})
    row = {
        "model": "gpt-4o",
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "reasoning_tokens": 1_000_000,
    }
    cost = bci._row_cost_usd(row, pricing, bill_reasoning=False)
    # 1e6*1 + 1e6*2 = 3e6 micro-USD -> 3.0 USD; reasoning never billed for non-5.2.
    assert cost == pytest.approx(3.0)


def test_row_cost_usd_bills_reasoning_only_for_gpt52_when_enabled() -> None:
    pricing = _StubPricing({"gpt-5.2": _gpt52_rates()})
    row = {
        "model": "gpt-5.2",
        "input_tokens": 0,
        "output_tokens": 1_000_000,
        "reasoning_tokens": 1_000_000,
    }
    output_only = bci._row_cost_usd(row, pricing, bill_reasoning=False)
    with_reasoning = bci._row_cost_usd(row, pricing, bill_reasoning=True)
    assert output_only == pytest.approx(2.0)
    assert with_reasoning == pytest.approx(6.0)  # +1e6*4 micro-USD


def test_row_cost_usd_subtracts_cached_from_input() -> None:
    pricing = _StubPricing({"gpt-4o": _StubRates()})
    row = {
        "model": "gpt-4o",
        "input_tokens": 1_000_000,
        "cached_tokens": 400_000,
        "output_tokens": 0,
    }
    # 600k*1 + 400k*0.5 = 800k micro-USD -> 0.8 USD.
    assert bci._row_cost_usd(row, pricing, bill_reasoning=False) == pytest.approx(0.8)


def test_detect_billing_mode_picks_output_only_when_it_matches_committed() -> None:
    pricing = _StubPricing({"gpt-5.2": _gpt52_rates()})
    rows = [
        {
            "model": "gpt-5.2",
            "input_tokens": 0,
            "output_tokens": 1_000_000,
            "reasoning_tokens": 1_000_000,
        }
    ]
    used = {("gpt-5.2", "low"): rows}
    # Committed mean equals the output-only figure (2.0).
    bill, detail = bci._detect_billing_mode(used, {("gpt-5.2", "low"): 2.0}, pricing)
    assert bill is False
    assert detail["billing_mode"] == "output_only"
    assert detail["cells_compared"] == 1


def test_detect_billing_mode_picks_reasoning_when_it_matches_committed() -> None:
    pricing = _StubPricing({"gpt-5.2": _gpt52_rates()})
    rows = [
        {
            "model": "gpt-5.2",
            "input_tokens": 0,
            "output_tokens": 1_000_000,
            "reasoning_tokens": 1_000_000,
        }
    ]
    used = {("gpt-5.2", "low"): rows}
    bill, detail = bci._detect_billing_mode(used, {("gpt-5.2", "low"): 6.0}, pricing)
    assert bill is True
    assert detail["billing_mode"] == "reasoning_billed_separately"


def test_detect_billing_mode_skips_cells_without_committed_mean() -> None:
    pricing = _StubPricing({"gpt-5.2": _gpt52_rates()})
    used = {("gpt-5.2", "low"): [{"model": "gpt-5.2", "input_tokens": 0,
                                   "output_tokens": 10, "reasoning_tokens": 0}]}
    # No committed mean for this cell at all -> nothing compared, ties -> reasoning.
    bill, detail = bci._detect_billing_mode(used, {}, pricing)
    assert detail["cells_compared"] == 0
    assert bill is True  # err_incl (0.0) <= err_excl (0.0) tie resolves to True


# ----------------------------------------------------------------------------
# _extract_metric: missing fields counted, cost failures skipped
# ----------------------------------------------------------------------------


def test_extract_metric_quality_counts_missing_judge_scores() -> None:
    rows = [
        {"judge_score": 2},
        {"judge_score": None},
        {"judge_score": 0},
        {},  # absent key behaves like None
    ]
    values, n_missing, warnings = bci._extract_metric(
        rows, "quality", pricing=None, bill_reasoning=False
    )
    assert values == [2.0, 0.0]
    assert n_missing == 2
    assert any("quality" in w for w in warnings)


def test_extract_metric_latency_all_present_no_warning() -> None:
    rows = [{"latency_ms": 100}, {"latency_ms": 250}]
    values, n_missing, warnings = bci._extract_metric(
        rows, "latency", pricing=None, bill_reasoning=False
    )
    assert values == [100.0, 250.0]
    assert n_missing == 0
    assert warnings == []


def test_extract_metric_cost_skips_rows_missing_token_fields() -> None:
    pricing = _StubPricing({"gpt-4o": _StubRates()})
    rows = [
        {"model": "gpt-4o", "input_tokens": 1_000_000, "output_tokens": 0},
        {"model": "gpt-4o"},  # missing required token fields -> KeyError -> skipped
    ]
    values, n_missing, warnings = bci._extract_metric(
        rows, "cost", pricing=pricing, bill_reasoning=False
    )
    assert values == [pytest.approx(1.0)]
    assert n_missing == 1
    assert any("cost skipped" in w for w in warnings)


def test_extract_metric_unknown_metric_raises() -> None:
    with pytest.raises(ValueError):
        bci._extract_metric([{"x": 1}], "nonsense", pricing=None, bill_reasoning=False)


# ----------------------------------------------------------------------------
# _cell_sort: canonical ordering with ties / unknown efforts
# ----------------------------------------------------------------------------


def test_cell_sort_orders_baseline_first_then_effort_rank() -> None:
    keys = [
        ("gpt-5.2", "high"),
        ("gpt-4o", None),
        ("gpt-5.2", "low"),
        ("gpt-5.2", "minimal"),
    ]
    ordered = sorted(keys, key=bci._cell_sort)
    assert ordered == [
        ("gpt-4o", None),
        ("gpt-5.2", "minimal"),
        ("gpt-5.2", "low"),
        ("gpt-5.2", "high"),
    ]


def test_cell_sort_unknown_effort_falls_to_end_deterministically() -> None:
    keys = [("gpt-5.2", "mystery"), ("gpt-5.2", "low")]
    assert sorted(keys, key=bci._cell_sort)[0] == ("gpt-5.2", "low")


# ----------------------------------------------------------------------------
# _resolve_experiment_prefix + _committed_cost_means: source fallbacks
# ----------------------------------------------------------------------------


def test_resolve_experiment_prefix_uses_builtin_map_first(tmp_path: pathlib.Path) -> None:
    prefix, source = bci._resolve_experiment_prefix("02-multi-step-reasoning", tmp_path)
    assert prefix == bci.EXPERIMENT_PREFIXES["02-multi-step-reasoning"]
    assert source == "builtin_map"


def test_resolve_experiment_prefix_reads_analysis_config(tmp_path: pathlib.Path) -> None:
    (tmp_path / "analysis.json").write_text(
        json.dumps({"experiment_prefix": "expXYZ"}), encoding="utf-8"
    )
    prefix, source = bci._resolve_experiment_prefix("99-unknown-bench", tmp_path)
    assert prefix == "expXYZ"
    assert source == "analysis_json_config"


def test_resolve_experiment_prefix_falls_back_to_default(tmp_path: pathlib.Path) -> None:
    prefix, source = bci._resolve_experiment_prefix("99-unknown-bench", tmp_path)
    assert prefix == bci.DEFAULT_EXPERIMENT_PREFIX
    assert source == "analyzer_default"


def test_committed_cost_means_skips_empty_cells(tmp_path: pathlib.Path) -> None:
    (tmp_path / "analysis.json").write_text(
        json.dumps(
            {
                "cell_stats": [
                    {"model": "gpt-4o", "effort": None, "n_used": 3,
                     "mean_usd_per_request": 0.001},
                    {"model": "gpt-5.2", "effort": "minimal", "n_used": 0,
                     "mean_usd_per_request": 0.0},
                ]
            }
        ),
        encoding="utf-8",
    )
    means = bci._committed_cost_means(tmp_path)
    assert means == {("gpt-4o", None): pytest.approx(0.001)}


def test_committed_cost_means_missing_file_returns_empty(tmp_path: pathlib.Path) -> None:
    assert bci._committed_cost_means(tmp_path) == {}
