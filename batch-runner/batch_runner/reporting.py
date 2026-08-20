"""Deterministic offline analysis and four-artifact report rendering."""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import shutil
import stat
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml
from pydantic import ValidationError

from batch_runner import __version__
from batch_runner.contracts import (
    METHOD_ID,
    METHOD_VERSION,
    USAGE_SCHEMA_VERSION,
    WORKLOAD_SCHEMA_VERSION,
    InputValidationError,
    UsageEnvelope,
    WorkloadSpec,
    load_usage_jsonl,
    load_workload_yaml,
    resolve_snapshot_path,
)
from batch_runner.privacy import PrivacyViolation, ensure_safe_public_text
from batch_runner.report_contracts import (
    QUALITY_FINDING,
    QUALITY_RECOMMENDATION,
    ReportContract,
)
from batch_runner.release.manifest import deterministic_json_dumps
from batch_runner.sizing.model_density import (
    UnknownModelError,
    UnknownOutputRatioError,
)
from batch_runner.sizing.ptu_vs_payg_calculator import (
    PricingModelError,
    WorkloadShape,
    calculate,
)
from scripts.cost_calculator import (
    Gpt4oReasoningError,
    TokenUsage,
    load_payg_pricing,
    payg_cost_per_call,
)

REPORT_SCHEMA_VERSION = "1.0.0"
POLICY_SCHEMA_VERSION = "1.0.0"
GENERATED_ARTIFACTS = ("report.json", "report.md", "report.html", "policy.json")
MAX_SNAPSHOT_FILE_BYTES = 4 * 1024 * 1024
MAX_REPORT_FILE_BYTES = 16 * 1024 * 1024
_RECOVERY_MARKER_NAME = ".reasoning-payoff-owned"
_RECOVERY_MARKER_BYTES = b"wrpo-report-bundle-v1\n"
_LOCK_MARKER_BYTES = b"wrpo-report-lock-v1\n"
_EFFORT_ORDER = {
    "none": 0,
    "minimal": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
    "xhigh": 5,
    "max": 6,
}


class ReportValidationError(ValueError):
    """Raised for malformed pinned reports without echoing their content."""


class OutputConflictError(ValueError):
    """Raised when an output directory is non-empty or unsafe to replace."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_snapshot_bytes(path: Path, *, label: str) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_SNAPSHOT_FILE_BYTES + 1)
    except OSError as exc:
        raise InputValidationError(f"{label} could not be read") from exc
    if len(data) > MAX_SNAPSHOT_FILE_BYTES:
        raise InputValidationError(
            f"{label} exceeds the {MAX_SNAPSHOT_FILE_BYTES}-byte safety limit"
        )
    return data


def _load_payg_pricing_bytes(data: bytes) -> Any:
    with tempfile.TemporaryDirectory(prefix="reasoning-payoff-pricing-") as raw_dir:
        path = Path(raw_dir) / "pricing.yaml"
        path.write_bytes(data)
        try:
            return load_payg_pricing(path)
        except (
            OSError,
            UnicodeError,
            OverflowError,
            RecursionError,
            ValueError,
            yaml.YAMLError,
        ) as exc:
            raise InputValidationError("pricing snapshot is invalid") from exc


def _timestamp(value: str) -> datetime:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(candidate)


def claim_registry_bytes() -> bytes:
    """Return the bundled public claim contract bytes."""

    return (
        resources.files("batch_runner.data")
        .joinpath("public_claims.v1.json")
        .read_bytes()
    )


def _rounded(value: float, digits: int = 6) -> float:
    return float(f"{value:.{digits}f}")


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return float(ordered[rank - 1])


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _row_ranges(indices: Iterable[int]) -> dict[str, object]:
    ordered = sorted(set(indices))
    if not ordered:
        return {"ranges": [], "count": 0}
    ranges: list[str] = []
    start = previous = ordered[0]
    for current in ordered[1:]:
        if current == previous + 1:
            previous = current
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = current
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return {"ranges": ranges, "count": len(ordered)}


def _pricing_metadata(
    *,
    workload: WorkloadSpec,
    pricing_bytes: bytes,
    pricing: Any,
    usage_models: set[str],
) -> dict[str, object]:
    return {
        "snapshot_id": workload.pricing.snapshot_id,
        "sha256": _sha256(pricing_bytes),
        "accessed_date": pricing.accessed_date,
        "currency": pricing.currency,
        "modeled_models": sorted(set(pricing.models).intersection(usage_models)),
    }


def _ptu_sizing_analysis(
    *,
    rows: Sequence[UsageEnvelope],
    workload: WorkloadSpec,
    payg_bytes: bytes,
    ptu_bytes: bytes | None,
    density_bytes: bytes | None,
) -> dict[str, object]:
    spec = workload.ptu_sizing
    all_source_rows = _row_ranges(range(1, len(rows) + 1))
    if spec is None:
        return {
            "status": "NOT_MODELED",
            "reason": "required capacity inputs were not supplied",
            "missing_inputs": [
                "expected_rpm",
                "mean_max_output_tokens",
            ],
            "source_rows": all_source_rows,
        }
    if (
        ptu_bytes is None
        or density_bytes is None
    ):
        raise InputValidationError("PTU sizing snapshots could not be read")
    models = sorted({row.model for row in rows})
    if len(models) != 1:
        return {
            "status": "NOT_MODELED",
            "reason": "PTU sizing requires exactly one model per report",
            "missing_inputs": [],
            "source_rows": all_source_rows,
        }
    successful_pairs = [
        (index, row)
        for index, row in enumerate(rows, start=1)
        if 200 <= row.status_code < 300
    ]
    successful = [row for _, row in successful_pairs]
    if not successful:
        return {
            "status": "NOT_MODELED",
            "reason": "PTU sizing requires at least one successful usage row",
            "missing_inputs": [],
            "source_rows": all_source_rows,
        }
    payg_pricing = _load_payg_pricing_bytes(payg_bytes)
    if models[0] not in payg_pricing.models:
        return {
            "status": "NOT_MODELED",
            "reason": "pinned PAYG snapshot does not support the analyzed model",
            "missing_inputs": [],
            "source_rows": all_source_rows,
        }
    input_total = sum(row.input_tokens for row in successful)
    cached_total = sum(row.cached_input_tokens for row in successful)
    mean_full_output_tokens = math.ceil(
        _mean([row.output_tokens for row in successful])
    )
    mean_reasoning_tokens = round(
        _mean([row.reasoning_tokens for row in successful])
    )
    mean_visible_output_tokens = mean_full_output_tokens - mean_reasoning_tokens
    if spec.mean_max_output_tokens < mean_full_output_tokens:
        raise InputValidationError(
            "workload failed fields: ptu_sizing.mean_max_output_tokens"
        )
    try:
        shape = WorkloadShape(
            mean_prompt_tokens=round(_mean([row.input_tokens for row in successful])),
            mean_cached_fraction=_ratio(cached_total, input_total),
            mean_visible_output_tokens=mean_visible_output_tokens,
            mean_reasoning_tokens=mean_reasoning_tokens,
            mean_max_output_tokens=spec.mean_max_output_tokens,
            expected_rpm=float(spec.expected_rpm),
            model_id=models[0],
        )
    except (OverflowError, ValueError) as exc:
        raise InputValidationError("PTU sizing inputs are incompatible") from exc
    with tempfile.TemporaryDirectory(prefix="reasoning-payoff-ptu-") as raw_dir:
        snapshot_dir = Path(raw_dir)
        payg_path = snapshot_dir / "payg.yaml"
        ptu_path = snapshot_dir / "ptu.yaml"
        density_path = snapshot_dir / "density.yaml"
        payg_path.write_bytes(payg_bytes)
        ptu_path.write_bytes(ptu_bytes)
        density_path.write_bytes(density_bytes)
        try:
            result = calculate(
                workload=shape,
                target_utilization=float(spec.target_utilization),
                payg_rates_yaml=payg_path,
                ptu_rates_yaml=ptu_path,
                density_snapshot_yaml=density_path,
            )
        except (
            PricingModelError,
            UnknownModelError,
            UnknownOutputRatioError,
        ):
            return {
                "status": "NOT_MODELED",
                "reason": "pinned PTU snapshots do not support the analyzed model",
                "missing_inputs": [],
                "source_rows": all_source_rows,
            }
        except (
            OSError,
            UnicodeError,
            OverflowError,
            RecursionError,
            ValueError,
            yaml.YAMLError,
        ) as exc:
            raise InputValidationError("PTU sizing inputs are incompatible") from exc
    result_payload = result.to_dict()
    result_payload["rationale"] = (
        "Modeled from successful-row request shape, declared expected RPM, "
        "declared maximum output tokens, target utilization, and pinned PTU "
        "pricing and density snapshots. Dominant modeled driver: "
        f"{result.dominant_driver}."
    )
    return {
        "status": "MODELED",
        "reason": "existing batch_runner PTU calculator",
        "missing_inputs": [],
        "source_rows": _row_ranges(index for index, _ in successful_pairs),
        "pricing_snapshot": {
            "snapshot_id": spec.pricing_snapshot_id,
            "sha256": _sha256(ptu_bytes),
        },
        "density_snapshot": {
            "snapshot_id": spec.density_snapshot_id,
            "sha256": _sha256(density_bytes),
        },
        "result": result_payload,
    }


def _group_rows(
    indexed_rows: Sequence[tuple[int, UsageEnvelope]],
) -> list[tuple[tuple[str, str], list[tuple[int, UsageEnvelope]]]]:
    groups: dict[tuple[str, str], list[tuple[int, UsageEnvelope]]] = defaultdict(list)
    for index, row in indexed_rows:
        groups[(row.model, row.reasoning_effort)].append((index, row))
    return sorted(
        groups.items(),
        key=lambda item: (item[0][0], _EFFORT_ORDER[item[0][1]]),
    )


def _aggregate_rows(
    indexed_rows: Sequence[tuple[int, UsageEnvelope]],
    pricing: Any,
) -> dict[str, object]:
    rows = [row for _, row in indexed_rows]
    input_tokens = sum(row.input_tokens for row in rows)
    cached_tokens = sum(row.cached_input_tokens for row in rows)
    output_tokens = sum(row.output_tokens for row in rows)
    reasoning_tokens = sum(row.reasoning_tokens for row in rows)
    retry_values = [
        float(row.retry_after_ms)
        for row in rows
        if row.status_code == 429 and row.retry_after_ms is not None
    ]
    costs: list[float] = []
    cost_status = "NOT_MODELED"
    for row in rows:
        try:
            breakdown = payg_cost_per_call(
                TokenUsage(
                    input_tokens=float(row.input_tokens),
                    cached_tokens=float(row.cached_input_tokens),
                    output_tokens=float(row.output_tokens),
                    reasoning_tokens=float(row.reasoning_tokens),
                ),
                pricing,
                row.model,
            )
        except KeyError:
            costs = []
            break
        except Gpt4oReasoningError as exc:
            raise InputValidationError(
                "usage input failed fields: reasoning_tokens"
            ) from exc
        costs.append(float(breakdown.usd_per_request))
    if len(costs) == len(rows):
        cost_status = "MODELED"

    return {
        "request_count": len(rows),
        "successful_request_count": sum(
            1 for row in rows if 200 <= row.status_code < 300
        ),
        "status_429_count": sum(1 for row in rows if row.status_code == 429),
        "status_429_rate": _ratio(
            sum(row.status_code == 429 for row in rows),
            len(rows),
        ),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cached_input_ratio": _ratio(cached_tokens, input_tokens),
        "reasoning_output_ratio": _ratio(reasoning_tokens, output_tokens),
        "mean_latency_ms": _mean([float(row.latency_ms) for row in rows]),
        "p95_latency_ms": _nearest_rank(
            [float(row.latency_ms) for row in rows],
            0.95,
        ),
        "mean_retry_after_ms_on_429": (
            _mean(retry_values) if retry_values else None
        ),
        "modeled_cost_status": cost_status,
        "mean_modeled_usd_per_request": (
            _rounded(_mean(costs), 9) if cost_status == "MODELED" else None
        ),
        "source_rows": _row_ranges(index for index, _ in indexed_rows),
    }


def _provenance(
    *,
    pricing_metadata: Mapping[str, object],
    claim_registry_sha256: str,
    pricing_used: bool,
    ptu_sizing: Mapping[str, object],
    ptu_used: bool,
) -> dict[str, object]:
    return {
        "method_id": METHOD_ID,
        "method_version": METHOD_VERSION,
        "usage_schema_version": USAGE_SCHEMA_VERSION,
        "workload_schema_version": WORKLOAD_SCHEMA_VERSION,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "cli_version": __version__,
        "pricing_snapshot": (
            {
                "status": "USED",
                "snapshot_id": pricing_metadata["snapshot_id"],
                "sha256": pricing_metadata["sha256"],
            }
            if pricing_used
            else {"status": "NOT_USED"}
        ),
        "ptu_sizing_snapshots": (
            {
                "status": "USED",
                "pricing_snapshot": ptu_sizing["pricing_snapshot"],
                "density_snapshot": ptu_sizing["density_snapshot"],
            }
            if ptu_used
            else {"status": "NOT_USED"}
        ),
        "claim_registry_sha256": claim_registry_sha256,
    }


def _conclusion(
    *,
    conclusion_id: str,
    boundary: str,
    finding: str,
    evidence: list[dict[str, object]],
    assumptions: list[str],
    confidence: str,
    recommendation: str,
    source_rows: Mapping[str, object],
    selector: str,
    pricing_metadata: Mapping[str, object],
    claim_registry_sha256: str,
    ptu_sizing: Mapping[str, object],
    pricing_used: bool = False,
    ptu_used: bool = False,
) -> dict[str, object]:
    return {
        "id": conclusion_id,
        "boundary": boundary,
        "finding": finding,
        "evidence": evidence,
        "assumptions": assumptions,
        "confidence": confidence,
        "recommendation": recommendation,
        "source_rows": dict(source_rows),
        "selector": selector,
        "provenance": _provenance(
            pricing_metadata=pricing_metadata,
            claim_registry_sha256=claim_registry_sha256,
            pricing_used=pricing_used,
            ptu_sizing=ptu_sizing,
            ptu_used=ptu_used,
        ),
    }


def _build_policy(
    *,
    aggregate: Mapping[str, object],
    workload: WorkloadSpec,
    claim_registry_sha256: str,
    pricing_metadata: Mapping[str, object],
    ptu_sizing: Mapping[str, object],
    usage_sha256: str,
    workload_sha256: str,
    source_rows: Mapping[str, object],
) -> dict[str, object]:
    def candidate(
        *,
        candidate_id: str,
        action: str,
        requires_quality_evaluation: bool,
        reason: str,
        conclusion_refs: list[str],
        confidence: str,
        metric: str,
        value: object,
        unit: str,
        assumptions: list[str],
        selector: str,
    ) -> dict[str, object]:
        return {
            "id": candidate_id,
            "action": action,
            "apply_automatically": False,
            "requires_quality_evaluation": requires_quality_evaluation,
            "reason": reason,
            "conclusion_refs": conclusion_refs,
            "confidence": confidence,
            "evidence": [{"metric": metric, "value": value, "unit": unit}],
            "assumptions": assumptions,
            "source_rows": dict(source_rows),
            "selector": selector,
        }

    candidates: list[dict[str, object]] = [
        candidate(
            candidate_id="reasoning-effort-quality-experiment",
            action="RUN_CONTROLLED_EXPERIMENT",
            requires_quality_evaluation=True,
            reason=(
                "Usage metadata measures token and latency behavior but cannot "
                "establish that an effort change preserves quality."
            ),
            conclusion_refs=["quality-boundary", "reasoning-token-share"],
            confidence="HIGH",
            metric="quality_status",
            value="NOT_MEASURED",
            unit="status",
            assumptions=["No evaluator or task-success measurements were supplied."],
            selector="workload.quality.status",
        )
    ]
    if float(aggregate["reasoning_output_ratio"]) > float(
        workload.thresholds.max_reasoning_output_ratio
    ):
        candidates.append(
            candidate(
                candidate_id="investigate-reasoning-share",
                action="INVESTIGATE_REASONING_EFFORT_WITH_QUALITY_EVAL",
                requires_quality_evaluation=True,
                reason=(
                    "Measured reasoning/output ratio exceeds the workload threshold."
                ),
                conclusion_refs=["reasoning-token-share", "quality-boundary"],
                confidence="HIGH",
                metric="reasoning_output_ratio",
                value=aggregate["reasoning_output_ratio"],
                unit="fraction",
                assumptions=[
                    "reasoning_tokens is a labeled subset of output_tokens."
                ],
                selector="sum(reasoning_tokens) / sum(output_tokens)",
            )
        )
    if float(aggregate["status_429_rate"]) > float(
        workload.thresholds.max_429_rate
    ):
        candidates.append(
            candidate(
                candidate_id="investigate-429-pressure",
                action="INVESTIGATE_CAPACITY_OR_RETRY_POLICY",
                requires_quality_evaluation=False,
                reason="Measured HTTP 429 rate exceeds the workload threshold.",
                conclusion_refs=["throttling-observation", "ptu-applicability"],
                confidence="HIGH",
                metric="status_429_rate",
                value=aggregate["status_429_rate"],
                unit="fraction",
                assumptions=["Every attempted request is represented by one row."],
                selector="count(status_code == 429) / count(rows)",
            )
        )
    if float(aggregate["cached_input_ratio"]) < float(
        workload.thresholds.min_cached_input_ratio
    ):
        candidates.append(
            candidate(
                candidate_id="investigate-cache-reuse",
                action="CHECK_STABLE_PREFIX_AND_CACHE_ACCOUNTING",
                requires_quality_evaluation=False,
                reason="Measured cached-input ratio is below the workload threshold.",
                conclusion_refs=["cache-observation"],
                confidence="HIGH",
                metric="cached_input_ratio",
                value=aggregate["cached_input_ratio"],
                unit="fraction",
                assumptions=["cached_input_tokens is a subset of input_tokens."],
                selector="sum(cached_input_tokens) / sum(input_tokens)",
            )
        )
    if float(aggregate["p95_latency_ms"]) > float(
        workload.thresholds.max_p95_latency_ms
    ):
        candidates.append(
            candidate(
                candidate_id="investigate-tail-latency",
                action="PROFILE_P95_LATENCY_BY_EFFORT",
                requires_quality_evaluation=True,
                reason="Measured p95 latency exceeds the workload threshold.",
                conclusion_refs=["tail-latency-observation", "quality-boundary"],
                confidence="HIGH",
                metric="p95_latency_ms",
                value=aggregate["p95_latency_ms"],
                unit="milliseconds",
                assumptions=["latency_ms uses a consistent client-side boundary."],
                selector="nearest_rank(rows[*].latency_ms, 0.95)",
            )
        )
    ptu_snapshot_provenance: dict[str, object]
    if ptu_sizing["status"] == "MODELED":
        ptu_snapshot_provenance = {
            "status": "USED",
            "pricing_snapshot": ptu_sizing["pricing_snapshot"],
            "density_snapshot": ptu_sizing["density_snapshot"],
        }
    else:
        ptu_snapshot_provenance = {"status": "NOT_USED"}
    return {
        "schema": "wrpo.policy_candidate",
        "schema_version": POLICY_SCHEMA_VERSION,
        "status": "REVIEW_REQUIRED",
        "auto_apply": False,
        "quality_boundary": "NOT_MEASURED",
        "cli_version": __version__,
        "method_id": METHOD_ID,
        "method_version": METHOD_VERSION,
        "usage_schema_version": USAGE_SCHEMA_VERSION,
        "workload_schema_version": WORKLOAD_SCHEMA_VERSION,
        "input_usage_sha256": usage_sha256,
        "input_workload_sha256": workload_sha256,
        "source_rows": dict(source_rows),
        "pricing_snapshot": {
            "snapshot_id": pricing_metadata["snapshot_id"],
            "sha256": pricing_metadata["sha256"],
        },
        "ptu_sizing_snapshots": ptu_snapshot_provenance,
        "claim_registry_sha256": claim_registry_sha256,
        "candidates": candidates,
    }


def build_report(
    *,
    rows: Sequence[UsageEnvelope],
    usage_sha256: str,
    workload: WorkloadSpec,
    workload_bytes: bytes,
    pricing: Any,
    pricing_bytes: bytes,
    ptu_sizing: Mapping[str, object],
) -> dict[str, object]:
    """Build a deterministic report object from validated local inputs."""

    indexed_rows = list(enumerate(rows, start=1))
    if sum(row.input_tokens for row in rows) == 0:
        raise InputValidationError(
            "usage input requires positive aggregate input_tokens"
        )
    if sum(row.output_tokens for row in rows) == 0:
        raise InputValidationError(
            "usage input requires positive aggregate output_tokens"
        )
    workload_sha256 = _sha256(workload_bytes)
    aggregate = _aggregate_rows(indexed_rows, pricing)
    pricing_metadata = _pricing_metadata(
        workload=workload,
        pricing_bytes=pricing_bytes,
        pricing=pricing,
        usage_models={row.model for row in rows},
    )
    registry_sha = _sha256(claim_registry_bytes())
    group_aggregates: list[dict[str, object]] = []
    for (model, effort), group in _group_rows(indexed_rows):
        group_aggregates.append(
            {
                "model": model,
                "reasoning_effort": effort,
                **_aggregate_rows(group, pricing),
            }
        )

    source_rows = aggregate["source_rows"]
    assert isinstance(source_rows, Mapping)
    if aggregate["modeled_cost_status"] == "MODELED":
        cost_value = aggregate["mean_modeled_usd_per_request"]
        cost_finding = (
            "Mean modeled PAYG cost is "
            f"${float(cost_value):.9f} per attempted request."
        )
        cost_confidence = "MEDIUM"
        cost_recommendation = (
            "Treat cost as modeled from the pinned snapshot, not as an invoice."
        )
        cost_assumptions = [
            "The pinned pricing snapshot applies to every modeled row.",
            "output_tokens already includes reasoning_tokens; reasoning is not added twice.",
            "Rows with zero usage contribute zero modeled token cost.",
        ]
    else:
        cost_value = None
        cost_finding = (
            "PAYG cost is NOT_MODELED because the pinned pricing snapshot does "
            "not contain every analyzed model."
        )
        cost_confidence = "HIGH"
        cost_recommendation = (
            "Add a pinned pricing entry for every analyzed model before making "
            "a cost decision."
        )
        cost_assumptions = [
            "No numeric PAYG cost is emitted when any analyzed model is unpriced."
        ]
    if ptu_sizing["status"] == "MODELED":
        ptu_result = ptu_sizing["result"]
        assert isinstance(ptu_result, Mapping)
        ptu_boundary = "MODELED"
        ptu_finding = (
            "PTU sizing is MODELED by the existing calculator: "
            f"decision={ptu_result['decision']}, "
            f"recommended_ptu_count={ptu_result['recommended_ptu_count']}, "
            "crossover_rpm="
            f"{float(ptu_result['crossover_rpm_ptu_eq_payg']):.3f}."
        )
        ptu_evidence = [
            {
                "metric": "recommended_ptu_count",
                "value": ptu_result["recommended_ptu_count"],
                "unit": "PTU",
            },
            {
                "metric": "crossover_rpm_ptu_eq_payg",
                "value": ptu_result["crossover_rpm_ptu_eq_payg"],
                "unit": "requests/minute",
            },
        ]
        ptu_assumptions = [
            "expected_rpm and mean_max_output_tokens come from WorkloadSpec.",
            "Successful usage rows define the mean request shape.",
            "The pinned PTU pricing and density snapshots remain applicable.",
        ]
        ptu_recommendation = (
            "Review the modeled decision against live capacity availability; "
            "quota does not guarantee capacity."
        )
        ptu_confidence = "MEDIUM"
        ptu_selector = (
            "existing ptu_vs_payg_calculator.calculate(successful rows, "
            "WorkloadSpec.ptu_sizing)"
        )
        ptu_source_rows = ptu_sizing["source_rows"]
    else:
        ptu_boundary = "NOT_MODELED"
        ptu_finding = f"PTU sizing is NOT_MODELED: {ptu_sizing['reason']}."
        ptu_evidence = [
            {
                "metric": "missing_capacity_inputs",
                "value": ptu_sizing["missing_inputs"],
                "unit": "field-names",
            }
        ]
        ptu_assumptions = [
            "No PTU decision is emitted while the stated applicability "
            "condition is unmet."
        ]
        ptu_recommendation = (
            "Resolve the stated applicability condition before using PTU sizing."
        )
        ptu_confidence = "HIGH"
        ptu_selector = "WorkloadSpec.ptu_sizing applicability"
        ptu_source_rows = source_rows
    conclusions = [
        _conclusion(
            conclusion_id="quality-boundary",
            boundary="NOT_MEASURED",
            finding=QUALITY_FINDING,
            evidence=[
                {
                    "metric": "quality_status",
                    "value": "NOT_MEASURED",
                    "unit": "status",
                }
            ],
            assumptions=["No evaluator or task-success measurements were supplied."],
            confidence="HIGH",
            recommendation=QUALITY_RECOMMENDATION,
            source_rows=source_rows,
            selector="workload.quality.status",
            pricing_metadata=pricing_metadata,
            claim_registry_sha256=registry_sha,
            ptu_sizing=ptu_sizing,
        ),
        _conclusion(
            conclusion_id="reasoning-token-share",
            boundary="MEASURED",
            finding=(
                "Reasoning tokens are "
                f"{float(aggregate['reasoning_output_ratio']):.1%} of measured "
                "output tokens."
            ),
            evidence=[
                {
                    "metric": "reasoning_output_ratio",
                    "value": aggregate["reasoning_output_ratio"],
                    "unit": "fraction",
                }
            ],
            assumptions=[
                "reasoning_tokens is a labeled subset of output_tokens.",
                "Rows follow UsageEnvelope v1 token semantics.",
            ],
            confidence="HIGH",
            recommendation=(
                "Use the measured share to choose experiment candidates; do "
                "not infer quality impact from token share alone."
            ),
            source_rows=source_rows,
            selector="sum(reasoning_tokens) / sum(output_tokens)",
            pricing_metadata=pricing_metadata,
            claim_registry_sha256=registry_sha,
            ptu_sizing=ptu_sizing,
        ),
        _conclusion(
            conclusion_id="cache-observation",
            boundary="MEASURED",
            finding=(
                "Cached input is "
                f"{float(aggregate['cached_input_ratio']):.1%} of measured input."
            ),
            evidence=[
                {
                    "metric": "cached_input_ratio",
                    "value": aggregate["cached_input_ratio"],
                    "unit": "fraction",
                }
            ],
            assumptions=["cached_input_tokens is a subset of input_tokens."],
            confidence="HIGH",
            recommendation=(
                "Investigate prefix stability only if this ratio misses the "
                "workload threshold; model-specific cache semantics remain external."
            ),
            source_rows=source_rows,
            selector="sum(cached_input_tokens) / sum(input_tokens)",
            pricing_metadata=pricing_metadata,
            claim_registry_sha256=registry_sha,
            ptu_sizing=ptu_sizing,
        ),
        _conclusion(
            conclusion_id="throttling-observation",
            boundary="MEASURED",
            finding=(
                "HTTP 429 responses are "
                f"{float(aggregate['status_429_rate']):.1%} of measured requests."
            ),
            evidence=[
                {
                    "metric": "status_429_rate",
                    "value": aggregate["status_429_rate"],
                    "unit": "fraction",
                }
            ],
            assumptions=["Every attempted request is represented by one input row."],
            confidence="HIGH",
            recommendation=(
                "Compare the measured rate with the safe threshold and inspect "
                "capacity or retry policy before changing routing."
            ),
            source_rows=source_rows,
            selector="count(status_code == 429) / count(rows)",
            pricing_metadata=pricing_metadata,
            claim_registry_sha256=registry_sha,
            ptu_sizing=ptu_sizing,
        ),
        _conclusion(
            conclusion_id="tail-latency-observation",
            boundary="MEASURED",
            finding=(
                "Nearest-rank p95 latency is "
                f"{float(aggregate['p95_latency_ms']):.0f} ms."
            ),
            evidence=[
                {
                    "metric": "p95_latency_ms",
                    "value": aggregate["p95_latency_ms"],
                    "unit": "milliseconds",
                }
            ],
            assumptions=["latency_ms uses a consistent client-side boundary."],
            confidence="HIGH",
            recommendation="Inspect latency by effort and status before tuning.",
            source_rows=source_rows,
            selector="nearest_rank(rows[*].latency_ms, 0.95)",
            pricing_metadata=pricing_metadata,
            claim_registry_sha256=registry_sha,
            ptu_sizing=ptu_sizing,
        ),
        _conclusion(
            conclusion_id="payg-cost-model",
            boundary=str(aggregate["modeled_cost_status"]),
            finding=cost_finding,
            evidence=[
                {
                    "metric": "mean_modeled_usd_per_request",
                    "value": cost_value,
                    "unit": "USD/request",
                }
            ],
            assumptions=cost_assumptions,
            confidence=cost_confidence,
            recommendation=cost_recommendation,
            source_rows=source_rows,
            selector="mean(existing cost_calculator.payg_cost_per_call(rows[*]))",
            pricing_metadata=pricing_metadata,
            claim_registry_sha256=registry_sha,
            ptu_sizing=ptu_sizing,
            pricing_used=aggregate["modeled_cost_status"] == "MODELED",
        ),
        _conclusion(
            conclusion_id="ptu-applicability",
            boundary=ptu_boundary,
            finding=ptu_finding,
            evidence=ptu_evidence,
            assumptions=ptu_assumptions,
            confidence=ptu_confidence,
            recommendation=ptu_recommendation,
            source_rows=ptu_source_rows,
            selector=ptu_selector,
            pricing_metadata=pricing_metadata,
            claim_registry_sha256=registry_sha,
            ptu_sizing=ptu_sizing,
            pricing_used=ptu_boundary == "MODELED",
            ptu_used=ptu_boundary == "MODELED",
        ),
    ]
    policy = _build_policy(
        aggregate=aggregate,
        workload=workload,
        claim_registry_sha256=registry_sha,
        pricing_metadata=pricing_metadata,
        ptu_sizing=ptu_sizing,
        usage_sha256=usage_sha256,
        workload_sha256=workload_sha256,
        source_rows=source_rows,
    )
    report = {
        "schema": "wrpo.provenance_report",
        "schema_version": REPORT_SCHEMA_VERSION,
        "cli_version": __version__,
        "method": {
            "id": METHOD_ID,
            "version": METHOD_VERSION,
        },
        "contracts": {
            "usage_envelope": USAGE_SCHEMA_VERSION,
            "workload_spec": WORKLOAD_SCHEMA_VERSION,
        },
        "input": {
            "usage_sha256": usage_sha256,
            "workload_sha256": workload_sha256,
            "row_count": len(rows),
            "row_dimensions": [
                {
                    "row": index,
                    "model": row.model,
                    "reasoning_effort": row.reasoning_effort,
                    "status_code": row.status_code,
                }
                for index, row in indexed_rows
            ],
            "window_start_utc": min(rows, key=lambda row: _timestamp(row.timestamp)).timestamp,
            "window_end_utc": max(rows, key=lambda row: _timestamp(row.timestamp)).timestamp,
        },
        "workload": {
            "name": workload.name,
            "quality_status": workload.quality.status,
            "thresholds": workload.thresholds.model_dump(mode="json"),
        },
        "pricing_snapshot": pricing_metadata,
        "ptu_sizing": dict(ptu_sizing),
        "claim_registry": {
            "schema_version": "1.0.0",
            "sha256": registry_sha,
        },
        "boundaries": {
            "usage_metrics": "MEASURED",
            "payg_cost": aggregate["modeled_cost_status"],
            "quality": "NOT_MEASURED",
            "ptu_sizing": ptu_boundary,
        },
        "aggregate": aggregate,
        "groups": group_aggregates,
        "conclusions": conclusions,
        "policy": policy,
        "privacy": {
            "raw_prompt_or_completion_included": False,
            "infrastructure_identifiers_included": False,
            "unknown_input_fields_accepted": False,
        },
    }
    serialized_report = deterministic_json_dumps(report)
    if len(serialized_report.encode("utf-8")) > MAX_REPORT_FILE_BYTES:
        raise InputValidationError("generated report exceeds the file-size limit")
    ensure_safe_public_text(serialized_report, label="generated report")
    return report


def analyze_files(usage_path: Path, workload_path: Path) -> dict[str, object]:
    """Load local inputs and build a report without network access."""

    rows, usage_sha256 = load_usage_jsonl(usage_path)
    workload, workload_bytes = load_workload_yaml(workload_path)
    pricing_path = resolve_snapshot_path(
        workload_path,
        workload.pricing.snapshot_file,
    )
    pricing_bytes = _read_snapshot_bytes(pricing_path, label="pricing snapshot")
    pricing = _load_payg_pricing_bytes(pricing_bytes)
    ptu_bytes: bytes | None = None
    density_bytes: bytes | None = None
    if workload.ptu_sizing is not None:
        ptu_path = resolve_snapshot_path(
            workload_path,
            workload.ptu_sizing.pricing_snapshot_file,
        )
        density_path = resolve_snapshot_path(
            workload_path,
            workload.ptu_sizing.density_snapshot_file,
        )
        ptu_bytes = _read_snapshot_bytes(ptu_path, label="PTU pricing snapshot")
        density_bytes = _read_snapshot_bytes(
            density_path,
            label="PTU density snapshot",
        )
    ptu_sizing = _ptu_sizing_analysis(
        rows=rows,
        workload=workload,
        payg_bytes=pricing_bytes,
        ptu_bytes=ptu_bytes,
        density_bytes=density_bytes,
    )
    return build_report(
        rows=rows,
        usage_sha256=usage_sha256,
        workload=workload,
        workload_bytes=workload_bytes,
        pricing=pricing,
        pricing_bytes=pricing_bytes,
        ptu_sizing=ptu_sizing,
    )


def _md(value: object) -> str:
    return (
        html.escape(str(value), quote=True)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\n", " ")
    )


def _source_rows_text(source_rows: Mapping[str, object]) -> str:
    ranges = source_rows.get("ranges", [])
    return ", ".join(str(item) for item in ranges) if ranges else "none"


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render a deterministic, PR-friendly Markdown report."""

    validate_report(report)
    workload = report["workload"]
    aggregate = report["aggregate"]
    aggregate_cost = aggregate["mean_modeled_usd_per_request"]
    aggregate_cost_text = (
        f"${float(aggregate_cost):.9f}"
        if aggregate_cost is not None
        else "NOT_MODELED"
    )
    lines = [
        "# Reasoning payoff provenance report",
        "",
        f"**Workload:** {_md(workload['name'])}",
        "",
        (
            f"**Boundary:** quality={report['boundaries']['quality']}; "
            f"PAYG cost={report['boundaries']['payg_cost']}; "
            f"PTU sizing={report['boundaries']['ptu_sizing']}."
        ),
        "",
        (
            "**Input provenance:** "
            f"`usage sha256:{report['input']['usage_sha256']}`; "
            f"`workload sha256:{report['input']['workload_sha256']}`."
        ),
        "",
        "## Aggregate observations",
        "",
        "| Metric | Value | Boundary |",
        "| --- | ---: | --- |",
        f"| Requests | {aggregate['request_count']} | MEASURED |",
        f"| HTTP 429 rate | {float(aggregate['status_429_rate']):.1%} | MEASURED |",
        f"| Cached-input ratio | {float(aggregate['cached_input_ratio']):.1%} | MEASURED |",
        f"| Reasoning/output ratio | {float(aggregate['reasoning_output_ratio']):.1%} | MEASURED |",
        f"| p95 latency | {float(aggregate['p95_latency_ms']):.0f} ms | MEASURED |",
        (
            "| Mean PAYG cost | "
            f"{aggregate_cost_text} "
            f"| {aggregate['modeled_cost_status']} |"
        ),
        "| Quality | NOT_MEASURED | NOT_MEASURED |",
        "",
        "## Model and effort cells",
        "",
        "| Model | Effort | Requests | 429 rate | Cache ratio | Reasoning/output | p95 ms | Modeled USD/request |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for group in report["groups"]:
        modeled = group["mean_modeled_usd_per_request"]
        lines.append(
            "| "
            + " | ".join(
                [
                    _md(group["model"]),
                    _md(group["reasoning_effort"]),
                    str(group["request_count"]),
                    f"{float(group['status_429_rate']):.1%}",
                    f"{float(group['cached_input_ratio']):.1%}",
                    f"{float(group['reasoning_output_ratio']):.1%}",
                    f"{float(group['p95_latency_ms']):.0f}",
                    f"${float(modeled):.9f}" if modeled is not None else "NOT_MODELED",
                ]
            )
            + " |"
        )
    lines.extend(["", "## Conclusions", ""])
    for conclusion in report["conclusions"]:
        provenance = conclusion["provenance"]
        pricing = provenance["pricing_snapshot"]
        pricing_text = (
            f"{pricing['snapshot_id']} / {pricing['sha256']}"
            if pricing["status"] == "USED"
            else "not used"
        )
        ptu_snapshots = provenance["ptu_sizing_snapshots"]
        ptu_text = (
            "pricing "
            f"{ptu_snapshots['pricing_snapshot']['snapshot_id']} / "
            f"{ptu_snapshots['pricing_snapshot']['sha256']}; density "
            f"{ptu_snapshots['density_snapshot']['snapshot_id']} / "
            f"{ptu_snapshots['density_snapshot']['sha256']}"
            if ptu_snapshots["status"] == "USED"
            else "not used"
        )
        lines.extend(
            [
                f"### {_md(conclusion['id'])}",
                "",
                f"- **Boundary:** {_md(conclusion['boundary'])}",
                f"- **Finding:** {_md(conclusion['finding'])}",
                f"- **Confidence:** {_md(conclusion['confidence'])}",
                f"- **Recommendation:** {_md(conclusion['recommendation'])}",
                (
                    "- **Source rows / selector:** "
                    f"{_md(_source_rows_text(conclusion['source_rows']))} / "
                    f"`{_md(conclusion['selector'])}`"
                ),
                (
                    "- **Versions:** "
                    f"method {provenance['method_version']}; "
                    f"UsageEnvelope {provenance['usage_schema_version']}; "
                    f"WorkloadSpec {provenance['workload_schema_version']}; "
                    f"CLI {provenance['cli_version']}"
                ),
                f"- **Pricing snapshot:** {_md(pricing_text)}",
                f"- **PTU sizing snapshots:** {_md(ptu_text)}",
                (
                    "- **Claim registry:** "
                    f"`sha256:{provenance['claim_registry_sha256']}`"
                ),
                "- **Assumptions:** "
                + "; ".join(_md(item) for item in conclusion["assumptions"]),
                "",
            ]
        )
    lines.extend(
        [
            "## Privacy and interpretation",
            "",
            "- The report contains aggregate operational metadata only.",
            "- It contains no prompt, completion, endpoint, credential, request ID, or user identifier.",
            "- A usage-only report cannot claim that an effort change preserves quality.",
            "",
        ]
    )
    rendered = "\n".join(lines)
    ensure_safe_public_text(rendered, label="generated Markdown")
    return rendered


def _h(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_html(report: Mapping[str, Any]) -> str:
    """Render self-contained, script-free HTML with escaped interpolations."""

    validate_report(report)
    aggregate = report["aggregate"]
    aggregate_cost = aggregate["mean_modeled_usd_per_request"]
    aggregate_cost_text = (
        f"${float(aggregate_cost):.9f}"
        if aggregate_cost is not None
        else "NOT_MODELED"
    )
    group_rows = []
    for group in report["groups"]:
        modeled = group["mean_modeled_usd_per_request"]
        rate_429 = f"{float(group['status_429_rate']):.1%}"
        cache_ratio = f"{float(group['cached_input_ratio']):.1%}"
        reasoning_ratio = f"{float(group['reasoning_output_ratio']):.1%}"
        p95_latency = f"{float(group['p95_latency_ms']):.0f}"
        modeled_cost = (
            f"${float(modeled):.9f}" if modeled is not None else "NOT_MODELED"
        )
        group_rows.append(
            "<tr>"
            f"<td>{_h(group['model'])}</td>"
            f"<td>{_h(group['reasoning_effort'])}</td>"
            f"<td>{_h(group['request_count'])}</td>"
            f"<td>{_h(rate_429)}</td>"
            f"<td>{_h(cache_ratio)}</td>"
            f"<td>{_h(reasoning_ratio)}</td>"
            f"<td>{_h(p95_latency)}</td>"
            f"<td>{_h(modeled_cost)}</td>"
            "</tr>"
        )
    conclusion_cards = []
    for conclusion in report["conclusions"]:
        provenance = conclusion["provenance"]
        pricing = provenance["pricing_snapshot"]
        pricing_text = (
            f"{pricing['snapshot_id']} / {pricing['sha256']}"
            if pricing["status"] == "USED"
            else "not used"
        )
        ptu_snapshots = provenance["ptu_sizing_snapshots"]
        ptu_text = (
            "pricing "
            f"{ptu_snapshots['pricing_snapshot']['snapshot_id']} / "
            f"{ptu_snapshots['pricing_snapshot']['sha256']}; density "
            f"{ptu_snapshots['density_snapshot']['snapshot_id']} / "
            f"{ptu_snapshots['density_snapshot']['sha256']}"
            if ptu_snapshots["status"] == "USED"
            else "not used"
        )
        assumptions = "".join(
            f"<li>{_h(item)}</li>" for item in conclusion["assumptions"]
        )
        conclusion_cards.append(
            "<article class=\"card\">"
            f"<h3>{_h(conclusion['id'])}</h3>"
            f"<p><strong>Boundary:</strong> {_h(conclusion['boundary'])}</p>"
            f"<p><strong>Finding:</strong> {_h(conclusion['finding'])}</p>"
            f"<p><strong>Confidence:</strong> {_h(conclusion['confidence'])}</p>"
            f"<p><strong>Recommendation:</strong> {_h(conclusion['recommendation'])}</p>"
            "<p><strong>Source rows / selector:</strong> "
            f"{_h(_source_rows_text(conclusion['source_rows']))} / "
            f"<code>{_h(conclusion['selector'])}</code></p>"
            "<p><strong>Versions:</strong> "
            f"method {_h(provenance['method_version'])}; "
            f"UsageEnvelope {_h(provenance['usage_schema_version'])}; "
            f"WorkloadSpec {_h(provenance['workload_schema_version'])}; "
            f"CLI {_h(provenance['cli_version'])}</p>"
            f"<p><strong>Pricing snapshot:</strong> {_h(pricing_text)}</p>"
            f"<p><strong>PTU sizing snapshots:</strong> {_h(ptu_text)}</p>"
            "<p><strong>Claim registry:</strong> "
            f"<code>sha256:{_h(provenance['claim_registry_sha256'])}</code></p>"
            f"<details><summary>Assumptions</summary><ul>{assumptions}</ul></details>"
            "</article>"
        )
    title = f"Reasoning payoff report: {report['workload']['name']}"
    rendered = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHZpZXdCb3g9JzAgMCA2NCA2NCc+PHJlY3Qgd2lkdGg9JzY0JyBoZWlnaHQ9JzY0JyByeD0nMTInIGZpbGw9JyMwOTY5ZGEnLz48cGF0aCBkPSdNMTcgMTloMzB2OEgxN3ptMCAxOGgyMHY4SDE3eicgZmlsbD0nd2hpdGUnLz48L3N2Zz4=">
  <title>{_h(title)}</title>
  <style>
    :root {{ color-scheme: light dark; --bg:#f6f8fa; --fg:#1f2328; --card:#fff; --line:#d0d7de; --accent:#0969da; }}
    @media (prefers-color-scheme: dark) {{ :root {{ --bg:#0d1117; --fg:#e6edf3; --card:#161b22; --line:#30363d; --accent:#58a6ff; }} }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--fg); font:16px/1.55 system-ui,sans-serif; }}
    main {{ max-width:1100px; margin:auto; padding:2rem 1rem 4rem; }}
    h1,h2,h3 {{ line-height:1.2; }}
    .lede {{ border-left:.35rem solid var(--accent); padding:.75rem 1rem; background:var(--card); }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:1rem; }}
    .card {{ background:var(--card); border:1px solid var(--line); border-radius:.6rem; padding:1rem; overflow-wrap:anywhere; }}
    .table-wrap {{ overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:.6rem; }}
    table {{ border-collapse:collapse; width:100%; min-width:760px; }}
    th,td {{ border-bottom:1px solid var(--line); padding:.65rem; text-align:left; }}
    th {{ position:sticky; top:0; background:var(--card); }}
    code {{ overflow-wrap:anywhere; }}
    .boundary {{ font-weight:700; color:var(--accent); }}
  </style>
</head>
<body>
<main>
  <h1>{_h(title)}</h1>
  <p class="lede"><span class="boundary">Quality: NOT_MEASURED.</span>
  This offline report measures usage behavior and models cost from a pinned
  snapshot; it does not claim that changing effort preserves quality.</p>
  <p><strong>Input provenance:</strong>
  <code>usage sha256:{_h(report['input']['usage_sha256'])}</code>;
  <code>workload sha256:{_h(report['input']['workload_sha256'])}</code>.</p>
  <h2>Aggregate observations</h2>
  <div class="grid">
    <section class="card"><h3>Requests</h3><p>{_h(aggregate['request_count'])}</p></section>
    <section class="card"><h3>HTTP 429 rate</h3><p>{_h(f"{float(aggregate['status_429_rate']):.1%}")}</p></section>
    <section class="card"><h3>Cached-input ratio</h3><p>{_h(f"{float(aggregate['cached_input_ratio']):.1%}")}</p></section>
    <section class="card"><h3>Reasoning/output ratio</h3><p>{_h(f"{float(aggregate['reasoning_output_ratio']):.1%}")}</p></section>
    <section class="card"><h3>p95 latency</h3><p>{_h(f"{float(aggregate['p95_latency_ms']):.0f}")} ms</p></section>
    <section class="card"><h3>Mean modeled PAYG cost</h3><p>{_h(aggregate_cost_text)}</p></section>
  </div>
  <h2>Model and effort cells</h2>
  <div class="table-wrap"><table>
    <caption>Usage metrics by model and reasoning effort</caption>
    <thead><tr><th>Model</th><th>Effort</th><th>Requests</th><th>429 rate</th><th>Cache ratio</th><th>Reasoning/output</th><th>p95 ms</th><th>Modeled USD/request</th></tr></thead>
    <tbody>{''.join(group_rows)}</tbody>
  </table></div>
  <h2>Conclusions and provenance</h2>
  <div class="grid">{''.join(conclusion_cards)}</div>
  <h2>Privacy boundary</h2>
  <p>This report contains aggregate operational metadata only. It contains no
  prompt, completion, endpoint, credential, request ID, user identifier, or
  executable script.</p>
</main>
</body>
</html>
"""
    ensure_safe_public_text(rendered, label="generated HTML")
    return rendered


def validate_report(report: Mapping[str, Any]) -> None:
    """Validate the stable top-level report contract before re-rendering."""

    required = {
        "schema",
        "schema_version",
        "cli_version",
        "method",
        "contracts",
        "input",
        "workload",
        "pricing_snapshot",
        "ptu_sizing",
        "claim_registry",
        "boundaries",
        "aggregate",
        "groups",
        "conclusions",
        "policy",
        "privacy",
    }
    if set(report) != required:
        raise ReportValidationError("report has unknown or missing top-level fields")
    try:
        ReportContract.model_validate(report)
    except ValidationError as exc:
        if any(
            isinstance(error.get("ctx", {}).get("error"), PrivacyViolation)
            for error in exc.errors(include_url=False)
        ):
            raise PrivacyViolation(
                "pinned report contains prohibited private data"
            ) from exc
        fields = set()
        errors = getattr(exc, "errors", lambda **_: [])(
            include_url=False,
            include_input=False,
        )
        for error in errors:
            parts = []
            for index, part in enumerate(error.get("loc", ())):
                if isinstance(part, int):
                    parts.append(str(part))
                elif (
                    error.get("type") == "extra_forbidden"
                    and index == len(error.get("loc", ())) - 1
                ):
                    parts.append("<unknown-field>")
                elif re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(part)):
                    parts.append(str(part))
                else:
                    parts.append("<field>")
            fields.add(".".join(parts))
        summary = ", ".join(sorted(field for field in fields if field)) or "<root>"
        raise ReportValidationError(
            f"report failed nested contract fields: {summary}"
        ) from exc
    ensure_safe_public_text(
        deterministic_json_dumps(report),
        label="pinned report",
    )


def load_report(path: Path) -> dict[str, Any]:
    """Read a pinned report without exposing its path or content in errors."""

    try:
        with path.open("rb") as handle:
            raw_bytes = handle.read(MAX_REPORT_FILE_BYTES + 1)
    except OSError as exc:
        raise ReportValidationError("pinned report could not be read") from exc
    if len(raw_bytes) > MAX_REPORT_FILE_BYTES:
        raise ReportValidationError("pinned report exceeds the file-size limit")
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReportValidationError("pinned report must be UTF-8") from exc
    ensure_safe_public_text(raw, label="pinned report")
    try:
        payload = json.loads(raw)
    except (ValueError, OverflowError, RecursionError) as exc:
        raise ReportValidationError("pinned report is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ReportValidationError("pinned report root must be an object")
    validate_report(payload)
    return payload


def _artifact_bytes(report: Mapping[str, Any]) -> dict[str, bytes]:
    validate_report(report)
    policy = report["policy"]
    return {
        "report.json": deterministic_json_dumps(report).encode("utf-8"),
        "report.md": render_markdown(report).encode("utf-8"),
        "report.html": render_html(report).encode("utf-8"),
        "policy.json": deterministic_json_dumps(policy).encode("utf-8"),
    }


def _bundle_names(path: Path) -> set[str]:
    return {entry.name for entry in path.iterdir()}


def _require_owned_recovery_dir(path: Path, *, complete: bool) -> None:
    if path.is_symlink() or not path.is_dir():
        raise OutputConflictError("bundle recovery path is not an owned directory")
    marker = path / _RECOVERY_MARKER_NAME
    try:
        marker_bytes = marker.read_bytes()
    except OSError as exc:
        raise OutputConflictError("bundle recovery path is not owned") from exc
    if marker.is_symlink() or marker_bytes != _RECOVERY_MARKER_BYTES:
        raise OutputConflictError("bundle recovery path is not owned")
    names = _bundle_names(path)
    allowed = set(GENERATED_ARTIFACTS) | {_RECOVERY_MARKER_NAME}
    expected = allowed if complete else names
    if (
        not names.issubset(allowed)
        or (complete and names != expected)
        or any(
            entry.is_symlink() or not entry.is_file()
            for entry in path.iterdir()
        )
    ):
        raise OutputConflictError("bundle recovery path has unexpected content")


def _require_complete_bundle(path: Path, *, marker_allowed: bool) -> None:
    if path.is_symlink() or not path.is_dir():
        raise OutputConflictError("output bundle is not a directory")
    names = _bundle_names(path)
    expected = set(GENERATED_ARTIFACTS)
    if marker_allowed and _RECOVERY_MARKER_NAME in names:
        expected.add(_RECOVERY_MARKER_NAME)
        marker = path / _RECOVERY_MARKER_NAME
        if marker.is_symlink() or marker.read_bytes() != _RECOVERY_MARKER_BYTES:
            raise OutputConflictError("output bundle recovery marker is invalid")
    if names != expected or any(
        entry.is_symlink() or not entry.is_file()
        for entry in path.iterdir()
    ):
        raise OutputConflictError("existing generated bundle is incomplete")


def _require_bundle_matches_artifacts(
    path: Path,
    artifacts: Mapping[str, bytes],
) -> None:
    for name, expected in artifacts.items():
        try:
            actual = (path / name).read_bytes()
        except OSError as exc:
            raise OutputConflictError(
                "existing generated bundle could not be verified"
            ) from exc
        if actual != expected:
            raise OutputConflictError(
                "existing generated bundle does not match the pinned report"
            )


def _write_recovery_marker(path: Path) -> None:
    marker = path / _RECOVERY_MARKER_NAME
    with marker.open("xb") as handle:
        handle.write(_RECOVERY_MARKER_BYTES)
        handle.flush()
        os.fsync(handle.fileno())


def _remove_recovery_marker(path: Path) -> None:
    marker = path / _RECOVERY_MARKER_NAME
    if marker.exists():
        marker.unlink()


@contextmanager
def _bundle_lock(out_dir: Path):
    lock_path = out_dir.parent / f".{out_dir.name}.reasoning-payoff.lock"
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if lock_path.is_symlink():
        raise OutputConflictError("bundle lock is not owned")
    try:
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_EXCL | os.O_RDWR | nofollow,
            0o600,
        )
        created = True
    except FileExistsError:
        created = False
        try:
            descriptor = os.open(lock_path, os.O_RDWR | nofollow)
        except OSError as exc:
            raise OutputConflictError("bundle lock is not owned") from exc
    handle = os.fdopen(descriptor, "r+b")
    if created:
        handle.write(_LOCK_MARKER_BYTES)
        handle.flush()
        os.fsync(handle.fileno())
    elif (
        not stat.S_ISREG(os.fstat(handle.fileno()).st_mode)
        or handle.read() != _LOCK_MARKER_BYTES
    ):
        handle.close()
        raise OutputConflictError("bundle lock is not owned")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise OutputConflictError(
                    "another process is writing this report bundle"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OutputConflictError(
                    "another process is writing this report bundle"
                ) from exc
        yield not created
    finally:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _write_report_bundle_locked(
    report: Mapping[str, Any],
    out_dir: Path,
    *,
    allow_existing_generated: bool,
) -> None:
    """Stage a complete bundle and recover interrupted directory replacements."""

    out_dir = out_dir.resolve()
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = out_dir.parent / f".{out_dir.name}.staging"
    backup = out_dir.parent / f".{out_dir.name}.backup"
    if stage.exists():
        _require_owned_recovery_dir(stage, complete=False)
        shutil.rmtree(stage)
    if backup.exists():
        _require_owned_recovery_dir(backup, complete=True)
        if out_dir.exists():
            _require_complete_bundle(out_dir, marker_allowed=True)
            _remove_recovery_marker(out_dir)
            shutil.rmtree(backup)
        else:
            os.replace(backup, out_dir)
            _remove_recovery_marker(out_dir)
    if out_dir.exists() and (out_dir / _RECOVERY_MARKER_NAME).exists():
        _require_complete_bundle(out_dir, marker_allowed=True)
        _remove_recovery_marker(out_dir)

    artifacts = _artifact_bytes(report)
    if out_dir.exists() and not out_dir.is_dir():
        raise OutputConflictError("output target exists and is not a directory")
    existing: set[str] = set()
    if out_dir.exists():
        existing = {path.name for path in out_dir.iterdir()}
        if existing and not allow_existing_generated:
            raise OutputConflictError("output directory must be empty")
        unexpected = existing - set(GENERATED_ARTIFACTS)
        if unexpected:
            raise OutputConflictError(
                "output directory contains files outside the generated bundle"
            )

    stage.mkdir()
    _write_recovery_marker(stage)
    try:
        for name, data in artifacts.items():
            target = stage / name
            with target.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        if not existing:
            if out_dir.exists():
                out_dir.rmdir()
            os.replace(stage, out_dir)
            _remove_recovery_marker(out_dir)
        else:
            _require_complete_bundle(out_dir, marker_allowed=False)
            _require_bundle_matches_artifacts(out_dir, artifacts)
            _write_recovery_marker(out_dir)
            os.replace(out_dir, backup)
            try:
                os.replace(stage, out_dir)
            except OSError:
                os.replace(backup, out_dir)
                _remove_recovery_marker(out_dir)
                raise
            _remove_recovery_marker(out_dir)
            shutil.rmtree(backup)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def write_report_bundle(
    report: Mapping[str, Any],
    out_dir: Path,
    *,
    allow_existing_generated: bool,
) -> None:
    """Serialize writers and publish a failure-recoverable staged bundle."""

    resolved = out_dir.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with _bundle_lock(resolved) as previously_owned:
        if (
            allow_existing_generated
            and resolved.is_dir()
            and any(resolved.iterdir())
            and not previously_owned
        ):
            raise OutputConflictError("existing output bundle is not owned")
        _write_report_bundle_locked(
            report,
            resolved,
            allow_existing_generated=allow_existing_generated,
        )


__all__ = [
    "GENERATED_ARTIFACTS",
    "MAX_REPORT_FILE_BYTES",
    "OutputConflictError",
    "POLICY_SCHEMA_VERSION",
    "REPORT_SCHEMA_VERSION",
    "ReportValidationError",
    "analyze_files",
    "build_report",
    "claim_registry_bytes",
    "load_report",
    "render_html",
    "render_markdown",
    "validate_report",
    "write_report_bundle",
]
