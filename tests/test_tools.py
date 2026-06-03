"""tests/test_tools.py — unit tests for scripts/tools.py (benchmark 03)."""

from __future__ import annotations

import pathlib

import pytest

from scripts.tools import (
    TOOL_REGISTRY,
    CalculatorError,
    WebSearchError,
    calculator,
    load_search_kb,
    make_web_search,
    web_search,
)


class TestCalculator:
    def test_simple_addition(self) -> None:
        assert calculator("1 + 1") == "2"

    def test_percentage(self) -> None:
        # 17.3% of 241000
        assert calculator("17.3/100*241000") == "41693"

    def test_multiplication_large(self) -> None:
        assert calculator("4567 * 8923") == "40751341"

    def test_division_with_round(self) -> None:
        assert calculator("round((123 + 456) / 7, 2)") == "82.71"

    def test_sqrt(self) -> None:
        assert calculator("sqrt(2025)") == "45"

    def test_subtraction(self) -> None:
        assert calculator("2026 - 1987") == "39"

    def test_power(self) -> None:
        assert calculator("17**2") == "289"

    def test_combined_expression(self) -> None:
        assert calculator("(2026 - 2009) ** 2") == "289"

    def test_decimal_precision(self) -> None:
        # Verify Decimal arithmetic (no float rounding errors)
        # 0.1 + 0.2 in float would be 0.30000000000000004
        assert calculator("0.1 + 0.2") == "0.3"

    def test_negative_number(self) -> None:
        assert calculator("-5 + 10") == "5"

    def test_unary_plus(self) -> None:
        assert calculator("+7") == "7"

    def test_abs(self) -> None:
        assert calculator("abs(-42)") == "42"

    def test_floor_div(self) -> None:
        assert calculator("17 // 5") == "3"

    def test_mod(self) -> None:
        assert calculator("17 % 5") == "2"

    def test_round_no_decimals(self) -> None:
        assert calculator("round(3.7, 0)") == "4"

    def test_empty_expr_raises(self) -> None:
        with pytest.raises(CalculatorError):
            calculator("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(CalculatorError):
            calculator("   ")

    def test_syntax_error_raises(self) -> None:
        with pytest.raises(CalculatorError):
            calculator("1 +")

    def test_division_by_zero_raises(self) -> None:
        with pytest.raises(CalculatorError, match="division by zero"):
            calculator("1 / 0")

    def test_attribute_access_blocked(self) -> None:
        with pytest.raises(CalculatorError):
            calculator("(1).__class__")

    def test_function_call_unknown_blocked(self) -> None:
        with pytest.raises(CalculatorError, match="unknown function"):
            calculator("exp(1)")

    def test_non_string_raises(self) -> None:
        with pytest.raises(CalculatorError):
            calculator(42)  # type: ignore[arg-type]

    def test_sqrt_negative_raises(self) -> None:
        with pytest.raises(CalculatorError, match="sqrt of negative"):
            calculator("sqrt(-1)")

    def test_determinism(self) -> None:
        """Two calls with the same expression return byte-identical strings."""
        for expr in ("17.3/100*241000", "round(123/7, 4)", "sqrt(2)", "0.1 + 0.2"):
            a = calculator(expr)
            b = calculator(expr)
            assert a == b, f"non-deterministic for {expr!r}: {a!r} vs {b!r}"


class TestWebSearch:
    def test_stub_returns_no_results(self) -> None:
        assert web_search("anything") == "no results"

    def test_stub_non_string_raises(self) -> None:
        with pytest.raises(WebSearchError):
            web_search(42)  # type: ignore[arg-type]

    def test_make_web_search_exact_match(self) -> None:
        kb = {"population of Vega City": "812400"}
        fn = make_web_search(kb)
        assert fn("population of Vega City") == "812400"

    def test_make_web_search_no_results(self) -> None:
        kb = {"population of Vega City": "812400"}
        fn = make_web_search(kb)
        assert fn("Vega City pop") == "no results"

    def test_make_web_search_whitespace_normalized(self) -> None:
        kb = {"population of Vega City": "812400"}
        fn = make_web_search(kb)
        assert fn("  population of Vega City  ") == "812400"

    def test_make_web_search_case_sensitive(self) -> None:
        # By design exact-match — case mismatch is a miss.
        kb = {"population of Vega City": "812400"}
        fn = make_web_search(kb)
        assert fn("Population of Vega City") == "no results"


class TestLoadSearchKb:
    def test_load_benchmark_kb(self) -> None:
        kb = load_search_kb("benchmarks/03-tool-using-agent/search_kb.json")
        # Spot-check: known KB entries
        assert kb["population of Vega City"] == "812400"
        assert kb["founding year of Atlas Foundation"] == "1987"
        assert kb["headquarters city of Helio Robotics"] == "Aurora"

    def test_missing_file_raises(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_search_kb(tmp_path / "missing.json")

    def test_non_object_raises(self, tmp_path: pathlib.Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ValueError, match="must be a JSON object"):
            load_search_kb(p)


class TestToolRegistry:
    def test_registry_keys(self) -> None:
        assert set(TOOL_REGISTRY) == {"calculator", "web_search"}

    def test_registry_callables(self) -> None:
        assert callable(TOOL_REGISTRY["calculator"])
        assert callable(TOOL_REGISTRY["web_search"])


class TestBenchmarkAnswers:
    """End-to-end sanity: the dataset's verifiable_answer values are
    re-derivable using the tools. If this test fails, the dataset is wrong.
    """

    @pytest.fixture()
    def web_search_bound(self) -> "any":  # type: ignore[name-defined]
        return make_web_search(
            load_search_kb("benchmarks/03-tool-using-agent/search_kb.json")
        )

    def test_tu_07(self) -> None:
        assert calculator("17.3/100*241000") == "41693"

    def test_tu_08(self) -> None:
        assert calculator("4567*8923") == "40751341"

    def test_tu_09(self) -> None:
        assert calculator("round((123+456)/7, 2)") == "82.71"

    def test_tu_10(self) -> None:
        assert calculator("sqrt(2025)") == "45"

    def test_tu_11(self, web_search_bound: "any") -> None:  # type: ignore[name-defined]
        assert web_search_bound("population of Vega City") == "812400"

    def test_tu_15(self, web_search_bound: "any") -> None:  # type: ignore[name-defined]
        pop = web_search_bound("population of Vega City")
        assert pop == "812400"
        assert calculator(f"0.125*{pop}") == "101550"

    def test_tu_16(self, web_search_bound: "any") -> None:  # type: ignore[name-defined]
        year = web_search_bound("founding year of Atlas Foundation")
        assert year == "1987"
        assert calculator(f"2026-{year}") == "39"

    def test_tu_17(self, web_search_bound: "any") -> None:  # type: ignore[name-defined]
        helio = web_search_bound("annual revenue of Helio Robotics")
        vega = web_search_bound("annual revenue of Vega Logistics")
        assert calculator(f"{helio}+{vega}") == "2950000000"

    def test_tu_18(self, web_search_bound: "any") -> None:  # type: ignore[name-defined]
        emp = web_search_bound("employees at Helio Robotics")
        assert emp == "13000"
        assert calculator(f"{emp}*(1-0.07)") == "12090"

    def test_tu_19(self, web_search_bound: "any") -> None:  # type: ignore[name-defined]
        yr = web_search_bound("founding year of Helio Robotics")
        assert yr == "2009"
        age = calculator(f"2026-{yr}")
        assert calculator(f"{age}*{age}") == "289"

    def test_tu_20(self, web_search_bound: "any") -> None:  # type: ignore[name-defined]
        vega = web_search_bound("population of Vega City")
        north = web_search_bound("population of Northbridge")
        assert calculator(f"round({vega}/{north}, 2)") == "1.74"
