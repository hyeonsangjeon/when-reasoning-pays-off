"""Tests for the Task 027 PTU-vs-PAYG calculator."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from batch_runner.sizing.model_density import UnknownModelError
from batch_runner.sizing.ptu_vs_payg_calculator import (
    CalculatorError,
    PricingModelError,
    WorkloadShape,
    calculate,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PAYG = REPO_ROOT / "pricing" / "azure-openai-payg-2026-05.yaml"
PTU = REPO_ROOT / "pricing" / "azure-openai-ptu-2026-05.yaml"
FIXTURE = REPO_ROOT / "batch-runner" / "tests" / "fixtures" / "synthetic_workload.yaml"


def _synthetic_workload() -> WorkloadShape:
    with FIXTURE.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return WorkloadShape(**data)


def _pricing_files(
    tmp_path: Path,
    *,
    model_id: str = "gpt-5.2",
    input_rate: float = 1.0,
    cached_rate: float = 0.5,
    output_rate: float = 1.0,
    ptu_hourly: float = 2.0,
    min_ptu: int = 1,
    scale_increment: int = 1,
    max_ptu: int = 100000,
) -> tuple[Path, Path]:
    payg = tmp_path / "payg.yaml"
    ptu = tmp_path / "ptu.yaml"
    payg.write_text(
        yaml.safe_dump(
            {
                "source_url": "https://example.test/payg",
                "accessed_date": "2026-05-28",
                "currency": "USD",
                "models": {
                    model_id: {
                        "input_per_1m_usd": input_rate,
                        "cached_input_per_1m_usd": cached_rate,
                        "output_per_1m_usd": output_rate,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    ptu.write_text(
        yaml.safe_dump(
            {
                "source_url": "https://example.test/ptu",
                "accessed_date": "2026-05-28",
                "currency": "USD",
                "region": "test",
                "models": {
                    model_id: {
                        "ptu_hourly_rate_usd": ptu_hourly,
                        "min_ptu": min_ptu,
                        "scale_increment": scale_increment,
                        "max_ptu_per_deployment": max_ptu,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return payg, ptu


def test_synthetic_worked_example_matches_acceptance_case():
    result = calculate(
        workload=_synthetic_workload(),
        target_utilization=0.7,
        payg_rates_yaml=PAYG,
        ptu_rates_yaml=PTU,
    )
    assert result.recommended_ptu_count == 1650
    assert result.dominant_driver == "max_tokens_oversize"
    assert result.decision == "payg_favorable"
    assert "operational inference" in result.rationale


def test_calculator_is_deterministic_for_identical_inputs():
    workload = _synthetic_workload()
    first = calculate(
        workload=workload,
        target_utilization=0.7,
        payg_rates_yaml=PAYG,
        ptu_rates_yaml=PTU,
    )
    second = calculate(
        workload=workload,
        target_utilization=0.7,
        payg_rates_yaml=PAYG,
        ptu_rates_yaml=PTU,
    )
    assert first == second
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(
        second.to_dict(),
        sort_keys=True,
    )


def test_crossover_uses_ptu_monthly_over_payg_per_call():
    result = calculate(
        workload=_synthetic_workload(),
        target_utilization=0.7,
        payg_rates_yaml=PAYG,
        ptu_rates_yaml=PTU,
    )
    expected_payg_per_call = (
        700 * 1.75 + 300 * 0.175 + 200 * 14.0
    ) / 1_000_000
    expected_ptu_monthly = 1650 * 2.0 * 24 * 30
    expected_crossover = expected_ptu_monthly / (expected_payg_per_call * 60 * 24 * 30)
    assert math.isclose(result.crossover_rpm_ptu_eq_payg, expected_crossover)


def test_reasoning_tokens_are_billed_as_output_tokens(tmp_path: Path):
    payg, ptu = _pricing_files(tmp_path, output_rate=10.0, ptu_hourly=100.0)
    no_reasoning = WorkloadShape(100, 0.0, 100, 0, 100, 10.0, "gpt-5.2")
    with_reasoning = WorkloadShape(100, 0.0, 0, 1000, 1000, 10.0, "gpt-5.2")
    a = calculate(
        workload=no_reasoning,
        payg_rates_yaml=payg,
        ptu_rates_yaml=ptu,
    )
    b = calculate(
        workload=with_reasoning,
        payg_rates_yaml=payg,
        ptu_rates_yaml=ptu,
    )
    assert b.crossover_rpm_ptu_eq_payg < a.crossover_rpm_ptu_eq_payg
    assert b.dominant_driver == "reasoning_accumulation"


def test_output_weighting_can_dominate(tmp_path: Path):
    payg, ptu = _pricing_files(tmp_path, output_rate=1000.0, ptu_hourly=0.01)
    workload = WorkloadShape(1, 1.0, 1000, 0, 1000, 1.0, "gpt-5.2")
    result = calculate(workload=workload, payg_rates_yaml=payg, ptu_rates_yaml=ptu)
    assert result.dominant_driver == "output_weighting"


def test_cache_hit_drop_can_dominate_for_large_uncached_prompt(tmp_path: Path):
    payg, ptu = _pricing_files(
        tmp_path,
        model_id="gpt-4o",
        input_rate=1000.0,
        output_rate=1000.0,
        ptu_hourly=0.01,
    )
    workload = WorkloadShape(10_000, 0.0, 1, 0, 1, 1.0, "gpt-4o")
    result = calculate(workload=workload, payg_rates_yaml=payg, ptu_rates_yaml=ptu)
    assert result.dominant_driver == "cache_hit_drop"
    assert "unspecified" in result.rationale


def test_balanced_driver_when_top_two_are_close(tmp_path: Path):
    payg, ptu = _pricing_files(
        tmp_path,
        model_id="gpt-4.1",
        output_rate=1000.0,
        ptu_hourly=0.01,
    )
    workload = WorkloadShape(1, 1.0, 25, 0, 100, 1.0, "gpt-4.1")
    result = calculate(workload=workload, payg_rates_yaml=payg, ptu_rates_yaml=ptu)
    assert result.dominant_driver == "balanced"


def test_ptu_favorable_decision_when_payg_monthly_exceeds_ptu(tmp_path: Path):
    payg, ptu = _pricing_files(tmp_path, input_rate=10000.0, output_rate=10000.0)
    workload = WorkloadShape(1000, 0.0, 1000, 0, 1000, 500.0, "gpt-5.2")
    result = calculate(workload=workload, payg_rates_yaml=payg, ptu_rates_yaml=ptu)
    assert result.decision == "ptu_favorable"


def test_near_crossover_decision_uses_five_percent_band(tmp_path: Path):
    payg, ptu = _pricing_files(
        tmp_path,
        model_id="gpt-4o",
        input_rate=47_619.04761904762,
        cached_rate=47_619.04761904762,
        output_rate=47_619.04761904762,
        ptu_hourly=2.0,
    )
    workload = WorkloadShape(1, 0.0, 0, 0, 1, 0.7, "gpt-4o")
    result = calculate(workload=workload, payg_rates_yaml=payg, ptu_rates_yaml=ptu)
    assert result.recommended_ptu_count == 1
    assert result.decision == "near_crossover"


def test_recommendation_honors_snapshot_minimum_ptu(tmp_path: Path):
    payg, ptu = _pricing_files(tmp_path, min_ptu=50)
    workload = WorkloadShape(1, 0.0, 1, 0, 1, 0.1, "gpt-5.2")
    result = calculate(
        workload=workload,
        payg_rates_yaml=payg,
        ptu_rates_yaml=ptu,
    )
    assert result.recommended_ptu_count == 50


def test_recommendation_rounds_up_to_snapshot_scale_increment(tmp_path: Path):
    payg, ptu = _pricing_files(
        tmp_path,
        min_ptu=50,
        scale_increment=50,
    )
    workload = WorkloadShape(100, 0.0, 100, 0, 100, 153.0, "gpt-5.2")
    result = calculate(
        workload=workload,
        payg_rates_yaml=payg,
        ptu_rates_yaml=ptu,
    )
    assert result.recommended_ptu_count == 100


def test_required_ptu_above_snapshot_maximum_fails_closed(tmp_path: Path):
    payg, ptu = _pricing_files(tmp_path, max_ptu=1)
    workload = WorkloadShape(1000, 0.0, 1000, 0, 1000, 500.0, "gpt-5.2")
    with pytest.raises(CalculatorError, match="max_ptu_per_deployment"):
        calculate(
            workload=workload,
            payg_rates_yaml=payg,
            ptu_rates_yaml=ptu,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mean_cached_fraction": -0.1},
        {"mean_cached_fraction": 1.1},
        {"expected_rpm": 0.0},
        {"mean_prompt_tokens": -1},
        {"mean_max_output_tokens": 10, "mean_visible_output_tokens": 11},
        {
            "mean_max_output_tokens": 10,
            "mean_visible_output_tokens": 5,
            "mean_reasoning_tokens": 6,
        },
    ],
)
def test_workload_validation_rejects_invalid_shapes(kwargs: dict[str, object]):
    base = {
        "mean_prompt_tokens": 100,
        "mean_cached_fraction": 0.0,
        "mean_visible_output_tokens": 10,
        "mean_reasoning_tokens": 0,
        "mean_max_output_tokens": 10,
        "expected_rpm": 1.0,
        "model_id": "gpt-5.2",
    }
    base.update(kwargs)
    with pytest.raises(CalculatorError):
        WorkloadShape(**base)


def test_calculate_rejects_target_utilization_outside_range():
    with pytest.raises(CalculatorError):
        calculate(
            workload=_synthetic_workload(),
            target_utilization=1.1,
            payg_rates_yaml=PAYG,
            ptu_rates_yaml=PTU,
        )


def test_unknown_density_model_rejected():
    workload = WorkloadShape(100, 0.0, 10, 0, 10, 1.0, "gpt-missing")
    with pytest.raises(UnknownModelError):
        calculate(workload=workload, payg_rates_yaml=PAYG, ptu_rates_yaml=PTU)


def test_missing_pricing_model_rejected(tmp_path: Path):
    payg, ptu = _pricing_files(tmp_path, model_id="gpt-4o")
    workload = WorkloadShape(100, 0.0, 10, 0, 10, 1.0, "gpt-5.2")
    with pytest.raises(PricingModelError):
        calculate(workload=workload, payg_rates_yaml=payg, ptu_rates_yaml=ptu)


def test_leak_calibration_is_loaded_and_labeled(tmp_path: Path):
    leak = tmp_path / "calibration.json"
    leak.write_text(
        json.dumps({"k_leak_tokens_per_ptu_per_second": 3.5}),
        encoding="utf-8",
    )
    result = calculate(
        workload=_synthetic_workload(),
        payg_rates_yaml=PAYG,
        ptu_rates_yaml=PTU,
        leak_calibration_json=leak,
    )
    assert "Task 024 leak calibration supplied" in result.rationale
    assert "operational inference" in result.rationale


def test_cli_outputs_stable_json_twice():
    cmd = [
        sys.executable,
        "-m",
        "scripts.ptu_sizing",
        "--workload",
        str(FIXTURE),
        "--target-util",
        "0.7",
        "--payg-rates",
        str(PAYG),
        "--ptu-rates",
        str(PTU),
    ]
    first = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, check=True)
    second = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, check=True)
    assert first.stdout == second.stdout
    payload = json.loads(first.stdout)
    assert payload["recommended_ptu_count"] == 1650
    assert payload["dominant_driver"] == "max_tokens_oversize"
