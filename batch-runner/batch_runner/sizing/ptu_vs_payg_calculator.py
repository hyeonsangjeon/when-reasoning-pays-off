"""Pure PTU-vs-PAYG sizing calculator for Task 027.

All inputs are explicit local values or YAML paths. The calculator does
not read environment variables, import network clients, or perform live
service calls.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import yaml

from batch_runner.sizing.model_density import (
    DEFAULT_DENSITY_PATH,
    UnknownModelError,
    UnknownOutputRatioError,
    load_density_snapshot,
)

Decision = Literal["ptu_favorable", "payg_favorable", "near_crossover"]
DominantDriver = Literal[
    "output_weighting",
    "reasoning_accumulation",
    "cache_hit_drop",
    "max_tokens_oversize",
    "balanced",
]

HOURS_PER_MONTH = 24 * 30
MINUTES_PER_MONTH = 60 * HOURS_PER_MONTH
NEAR_CROSSOVER_RELATIVE_BAND = 0.05
BALANCED_DRIVER_PERCENTAGE_POINTS = 5.0


class CalculatorError(ValueError):
    """Raised when calculator inputs are internally inconsistent."""


class PricingSchemaError(ValueError):
    """Raised when a pricing YAML does not match the expected local schema."""


class PricingModelError(KeyError):
    """Raised when a pricing YAML lacks the requested model block."""


@dataclass(frozen=True)
class WorkloadShape:
    mean_prompt_tokens: int
    mean_cached_fraction: float
    mean_visible_output_tokens: int
    mean_reasoning_tokens: int
    mean_max_output_tokens: int
    expected_rpm: float
    model_id: str

    def __post_init__(self) -> None:
        _validate_nonnegative_int("mean_prompt_tokens", self.mean_prompt_tokens)
        _validate_nonnegative_int(
            "mean_visible_output_tokens",
            self.mean_visible_output_tokens,
        )
        _validate_nonnegative_int("mean_reasoning_tokens", self.mean_reasoning_tokens)
        _validate_nonnegative_int(
            "mean_max_output_tokens",
            self.mean_max_output_tokens,
        )
        if self.mean_max_output_tokens < self.mean_visible_output_tokens:
            raise CalculatorError(
                "mean_max_output_tokens must be >= mean_visible_output_tokens"
            )
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise CalculatorError("model_id must be a non-empty string")
        _validate_fraction("mean_cached_fraction", self.mean_cached_fraction)
        _validate_positive_finite("expected_rpm", self.expected_rpm)
        if self.mean_prompt_tokens == 0 and self.mean_max_output_tokens == 0:
            raise CalculatorError(
                "workload must have positive prompt or max-output admission demand"
            )


@dataclass(frozen=True)
class CalculatorResult:
    recommended_ptu_count: int
    crossover_rpm_ptu_eq_payg: float
    decision: Decision
    dominant_driver: DominantDriver
    rationale: str
    inputs_snapshot: WorkloadShape

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable, stable-key result dictionary."""

        return {
            "crossover_rpm_ptu_eq_payg": self.crossover_rpm_ptu_eq_payg,
            "decision": self.decision,
            "dominant_driver": self.dominant_driver,
            "inputs_snapshot": asdict(self.inputs_snapshot),
            "rationale": self.rationale,
            "recommended_ptu_count": self.recommended_ptu_count,
        }


@dataclass(frozen=True)
class _PaygRates:
    input_per_1m_usd: float
    cached_input_per_1m_usd: float
    output_per_1m_usd: float


@dataclass(frozen=True)
class _PtuRates:
    ptu_hourly_rate_usd: float


def _validate_nonnegative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CalculatorError(f"{name} must be an integer")
    if value < 0:
        raise CalculatorError(f"{name} must be >= 0")


def _validate_fraction(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalculatorError(f"{name} must be a number")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0.0 or numeric > 1.0:
        raise CalculatorError(f"{name} must be between 0.0 and 1.0")


def _validate_positive_finite(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalculatorError(f"{name} must be a number")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0.0:
        raise CalculatorError(f"{name} must be a finite value > 0")


def _load_yaml_mapping(path: str | Path, *, label: str) -> dict:
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise PricingSchemaError(f"{label} YAML root must be a mapping")
    return data


def _positive_rate(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PricingSchemaError(f"{label} must be a number")
    rate = float(value)
    if not math.isfinite(rate) or rate <= 0.0:
        raise PricingSchemaError(f"{label} must be a finite value > 0")
    return rate


def _load_payg_rates(path: str | Path, model_id: str) -> _PaygRates:
    data = _load_yaml_mapping(path, label="PAYG pricing")
    models = data.get("models")
    if not isinstance(models, dict):
        raise PricingSchemaError("PAYG pricing models must be a mapping")
    if model_id not in models:
        raise PricingModelError(model_id)
    block = models[model_id]
    if not isinstance(block, dict):
        raise PricingSchemaError(f"PAYG pricing model {model_id!r} must be a mapping")
    return _PaygRates(
        input_per_1m_usd=_positive_rate(
            block.get("input_per_1m_usd"),
            label=f"PAYG {model_id} input_per_1m_usd",
        ),
        cached_input_per_1m_usd=_positive_rate(
            block.get("cached_input_per_1m_usd"),
            label=f"PAYG {model_id} cached_input_per_1m_usd",
        ),
        output_per_1m_usd=_positive_rate(
            block.get("output_per_1m_usd"),
            label=f"PAYG {model_id} output_per_1m_usd",
        ),
    )


def _load_ptu_rates(path: str | Path, model_id: str) -> _PtuRates:
    data = _load_yaml_mapping(path, label="PTU pricing")
    models = data.get("models")
    if not isinstance(models, dict):
        raise PricingSchemaError("PTU pricing models must be a mapping")
    if model_id not in models:
        raise PricingModelError(model_id)
    block = models[model_id]
    if not isinstance(block, dict):
        raise PricingSchemaError(f"PTU pricing model {model_id!r} must be a mapping")
    return _PtuRates(
        ptu_hourly_rate_usd=_positive_rate(
            block.get("ptu_hourly_rate_usd"),
            label=f"PTU {model_id} ptu_hourly_rate_usd",
        )
    )


def _load_leak_calibration(path: str | Path | None) -> float | None:
    if path is None:
        return None
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise CalculatorError("leak_calibration_json root must be a mapping")
    value = data.get("k_leak_tokens_per_ptu_per_second")
    if value is None:
        raise CalculatorError(
            "leak_calibration_json must contain k_leak_tokens_per_ptu_per_second"
        )
    _validate_positive_finite("k_leak_tokens_per_ptu_per_second", value)
    return float(value)


def _payg_cost_per_call_usd(workload: WorkloadShape, rates: _PaygRates) -> float:
    cached_prompt = workload.mean_prompt_tokens * float(workload.mean_cached_fraction)
    non_cached_prompt = workload.mean_prompt_tokens - cached_prompt
    output_tokens = workload.mean_visible_output_tokens + workload.mean_reasoning_tokens
    return (
        non_cached_prompt * rates.input_per_1m_usd
        + cached_prompt * rates.cached_input_per_1m_usd
        + output_tokens * rates.output_per_1m_usd
    ) / 1_000_000.0


def _driver_percentages(
    workload: WorkloadShape,
    *,
    non_cached_prompt_tokens: float,
    output_weight: float,
    admission_tokens_per_call: float,
) -> dict[str, float]:
    if admission_tokens_per_call <= 0:
        return {
            "output_weighting": 0.0,
            "reasoning_accumulation": 0.0,
            "cache_hit_drop": 0.0,
            "max_tokens_oversize": 0.0,
        }

    components = {
        "output_weighting": workload.mean_max_output_tokens
        * max(output_weight - 1.0, 0.0),
        "reasoning_accumulation": workload.mean_reasoning_tokens * output_weight,
        "cache_hit_drop": non_cached_prompt_tokens,
        "max_tokens_oversize": max(
            workload.mean_max_output_tokens - workload.mean_visible_output_tokens,
            0,
        )
        * output_weight,
    }
    return {
        name: (value / admission_tokens_per_call) * 100.0
        for name, value in components.items()
    }


def _dominant_driver(percentages: dict[str, float]) -> DominantDriver:
    ranked = sorted(percentages.items(), key=lambda item: (-item[1], item[0]))
    if not ranked or ranked[0][1] <= 0.0:
        return "balanced"
    if len(ranked) > 1:
        gap = ranked[0][1] - ranked[1][1]
        if gap <= BALANCED_DRIVER_PERCENTAGE_POINTS:
            return "balanced"
    return ranked[0][0]  # type: ignore[return-value]


def _decision(current_payg_monthly: float, ptu_monthly: float) -> Decision:
    denominator = max(current_payg_monthly, ptu_monthly, 1e-12)
    relative_gap = abs(current_payg_monthly - ptu_monthly) / denominator
    if relative_gap <= NEAR_CROSSOVER_RELATIVE_BAND:
        return "near_crossover"
    if current_payg_monthly > ptu_monthly:
        return "ptu_favorable"
    return "payg_favorable"


def _fmt(value: float) -> str:
    return f"{value:.6g}"


def _ratio_note(model_id: str, output_weight: float, provenance: str) -> str:
    if provenance == "operational_inference":
        return (
            f"output_weight={_fmt(output_weight)} for {model_id} is operational "
            "inference from the Guide §3 GPT-4.1-and-later note"
        )
    if provenance == "unspecified":
        return (
            f"output_weight for {model_id} is unspecified by Microsoft Learn; "
            "calculator used 1.0 and flags this as a sizing caveat"
        )
    return f"output_weight={_fmt(output_weight)} for {model_id} is official spec"


def _build_rationale(
    *,
    workload: WorkloadShape,
    density_tpm_per_ptu: int,
    output_weight: float,
    output_ratio_provenance: str,
    cached_prompt_tokens: float,
    non_cached_prompt_tokens: float,
    admission_tokens_per_call: float,
    demand_tpm: float,
    target_utilization: float,
    payg_cost_per_call: float,
    current_payg_monthly: float,
    ptu_monthly: float,
    percentages: dict[str, float],
    dominant_driver: DominantDriver,
    leak_k: float | None,
) -> str:
    leak_note = (
        "no Task 024 leak calibration supplied"
        if leak_k is None
        else (
            "Task 024 leak calibration supplied "
            f"(k={_fmt(leak_k)} tokens/PTU/sec, operational inference); "
            "steady-state sizing still uses the Guide §3 TPM/PTU table"
        )
    )
    pct = ", ".join(
        f"{name}={_fmt(percentages[name])}%"
        for name in (
            "output_weighting",
            "reasoning_accumulation",
            "cache_hit_drop",
            "max_tokens_oversize",
        )
    )
    return (
        "Guide §3 diagnostic "
        f"({pct}); dominant_driver={dominant_driver}. "
        f"{_ratio_note(workload.model_id, output_weight, output_ratio_provenance)}. "
        "Sizing used "
        f"cached_prompt={_fmt(cached_prompt_tokens)}, "
        f"non_cached_prompt={_fmt(non_cached_prompt_tokens)}, "
        f"admission_tokens_per_call={_fmt(admission_tokens_per_call)}, "
        f"demand_tpm={_fmt(demand_tpm)}, "
        f"input_tpm_per_ptu={density_tpm_per_ptu}, "
        f"target_utilization={_fmt(float(target_utilization))}. "
        "Cost comparison used "
        f"payg_cost_per_call_usd={_fmt(payg_cost_per_call)}, "
        f"current_payg_monthly_usd={_fmt(current_payg_monthly)}, "
        f"ptu_monthly_usd={_fmt(ptu_monthly)}. "
        f"{leak_note}."
    )


def calculate(
    *,
    workload: WorkloadShape,
    target_utilization: float = 0.7,
    payg_rates_yaml: Path,
    ptu_rates_yaml: Path,
    leak_calibration_json: Path | None = None,
) -> CalculatorResult:
    """Return deterministic PTU sizing, crossover, and diagnostic result."""

    if not isinstance(workload, WorkloadShape):
        raise CalculatorError("workload must be a WorkloadShape")
    _validate_positive_finite("target_utilization", target_utilization)
    if float(target_utilization) > 1.0:
        raise CalculatorError("target_utilization must be <= 1.0")

    density = load_density_snapshot(DEFAULT_DENSITY_PATH)
    try:
        density_tpm_per_ptu = density.input_tpm_per_ptu[workload.model_id]
    except KeyError as exc:
        raise UnknownModelError(workload.model_id) from exc
    try:
        output_weight = density.output_to_input_token_ratio[workload.model_id]
        provenance = density.output_ratio_provenance[workload.model_id]
    except KeyError as exc:
        raise UnknownOutputRatioError(workload.model_id) from exc

    payg_rates = _load_payg_rates(payg_rates_yaml, workload.model_id)
    ptu_rates = _load_ptu_rates(ptu_rates_yaml, workload.model_id)
    leak_k = _load_leak_calibration(leak_calibration_json)

    cached_prompt = workload.mean_prompt_tokens * float(workload.mean_cached_fraction)
    non_cached_prompt = workload.mean_prompt_tokens - cached_prompt
    admission_tokens_per_call = non_cached_prompt + (
        workload.mean_max_output_tokens * output_weight
    )
    if admission_tokens_per_call <= 0:
        raise CalculatorError("admission token demand must be > 0")

    demand_tpm = admission_tokens_per_call * float(workload.expected_rpm)
    recommended_ptu_count = math.ceil(
        demand_tpm / float(density_tpm_per_ptu) / float(target_utilization)
    )

    payg_cost_per_call = _payg_cost_per_call_usd(workload, payg_rates)
    if payg_cost_per_call <= 0:
        raise CalculatorError("PAYG per-call cost must be > 0")
    ptu_monthly = (
        float(recommended_ptu_count)
        * ptu_rates.ptu_hourly_rate_usd
        * float(HOURS_PER_MONTH)
    )
    current_payg_monthly = (
        payg_cost_per_call * float(workload.expected_rpm) * float(MINUTES_PER_MONTH)
    )
    crossover_rpm = ptu_monthly / (payg_cost_per_call * float(MINUTES_PER_MONTH))

    percentages = _driver_percentages(
        workload,
        non_cached_prompt_tokens=non_cached_prompt,
        output_weight=output_weight,
        admission_tokens_per_call=admission_tokens_per_call,
    )
    dominant = _dominant_driver(percentages)
    rationale = _build_rationale(
        workload=workload,
        density_tpm_per_ptu=density_tpm_per_ptu,
        output_weight=output_weight,
        output_ratio_provenance=provenance,
        cached_prompt_tokens=cached_prompt,
        non_cached_prompt_tokens=non_cached_prompt,
        admission_tokens_per_call=admission_tokens_per_call,
        demand_tpm=demand_tpm,
        target_utilization=float(target_utilization),
        payg_cost_per_call=payg_cost_per_call,
        current_payg_monthly=current_payg_monthly,
        ptu_monthly=ptu_monthly,
        percentages=percentages,
        dominant_driver=dominant,
        leak_k=leak_k,
    )
    return CalculatorResult(
        recommended_ptu_count=int(recommended_ptu_count),
        crossover_rpm_ptu_eq_payg=float(crossover_rpm),
        decision=_decision(current_payg_monthly, ptu_monthly),
        dominant_driver=dominant,
        rationale=rationale,
        inputs_snapshot=workload,
    )


__all__ = [
    "BALANCED_DRIVER_PERCENTAGE_POINTS",
    "CalculatorError",
    "CalculatorResult",
    "Decision",
    "DominantDriver",
    "HOURS_PER_MONTH",
    "MINUTES_PER_MONTH",
    "NEAR_CROSSOVER_RELATIVE_BAND",
    "PricingModelError",
    "PricingSchemaError",
    "WorkloadShape",
    "calculate",
]
