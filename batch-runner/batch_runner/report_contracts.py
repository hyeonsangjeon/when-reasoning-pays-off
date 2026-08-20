"""Strict nested contract for deterministic report re-rendering."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from importlib import resources
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from batch_runner import __version__
from batch_runner.contracts import (
    METHOD_ID,
    METHOD_VERSION,
    MAX_USAGE_ROWS,
    USAGE_SCHEMA_VERSION,
    WORKLOAD_SCHEMA_VERSION,
    SafeThresholds,
)
from batch_runner.privacy import ensure_safe_identifier

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Boundary = Literal["MEASURED", "MODELED", "NOT_MODELED", "NOT_MEASURED"]
Number = int | float
PTUReason = Literal[
    "required capacity inputs were not supplied",
    "PTU sizing requires exactly one model per report",
    "PTU sizing requires at least one successful usage row",
    "pinned PAYG snapshot does not support the analyzed model",
    "pinned PTU snapshots do not support the analyzed model",
    "existing batch_runner PTU calculator",
]
QUALITY_FINDING = (
    "Quality is NOT_MEASURED. Usage-only input cannot establish that lowering "
    "or raising reasoning effort preserves quality."
)
QUALITY_RECOMMENDATION = (
    "Run a controlled quality experiment across candidate effort levels before "
    "changing production policy."
)


def _report_identifier(value: str) -> str:
    ensure_safe_identifier(value, label="report identifier")
    return value


SafeModelIdentifier = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"),
    AfterValidator(_report_identifier),
]
SafeSnapshotIdentifier = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"),
    AfterValidator(_report_identifier),
]
SafeWorkloadIdentifier = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"),
    AfterValidator(_report_identifier),
]


class StrictReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class SourceRows(StrictReportModel):
    ranges: Annotated[list[str], Field(max_length=MAX_USAGE_ROWS)]
    count: Annotated[int, Field(ge=0, le=MAX_USAGE_ROWS)]

    @model_validator(mode="after")
    def validate_ranges(self) -> "SourceRows":
        if any(not re.fullmatch(r"\d+(?:-\d+)?", item) for item in self.ranges):
            raise ValueError("source row ranges are invalid")
        total = 0
        previous_end = 0
        for item in self.ranges:
            if "-" in item:
                start_text, end_text = item.split("-", maxsplit=1)
                start, end = int(start_text), int(end_text)
            else:
                start = end = int(item)
            if (
                start < 1
                or end < start
                or end > MAX_USAGE_ROWS
                or start <= previous_end
            ):
                raise ValueError("source row range bounds are invalid")
            total += end - start + 1
            if total > MAX_USAGE_ROWS:
                raise ValueError("source row ranges exceed the row limit")
            previous_end = end
        if total != self.count:
            raise ValueError("source row range count mismatch")
        return self

    def indices(self) -> set[int]:
        values: set[int] = set()
        for item in self.ranges:
            if "-" in item:
                start_text, end_text = item.split("-", maxsplit=1)
                values.update(range(int(start_text), int(end_text) + 1))
            else:
                values.add(int(item))
        return values


class SnapshotRef(StrictReportModel):
    snapshot_id: SafeSnapshotIdentifier
    sha256: Sha256


class PricingUse(StrictReportModel):
    status: Literal["USED", "NOT_USED"]
    snapshot_id: SafeSnapshotIdentifier | None = None
    sha256: Sha256 | None = None

    @model_validator(mode="after")
    def validate_status(self) -> "PricingUse":
        if self.status == "USED" and (self.snapshot_id is None or self.sha256 is None):
            raise ValueError("used pricing provenance requires snapshot ID and hash")
        if self.status == "NOT_USED" and (
            self.snapshot_id is not None or self.sha256 is not None
        ):
            raise ValueError("unused pricing provenance must not carry snapshot data")
        return self


class PTUSnapshotUse(StrictReportModel):
    status: Literal["USED", "NOT_USED"]
    pricing_snapshot: SnapshotRef | None = None
    density_snapshot: SnapshotRef | None = None

    @model_validator(mode="after")
    def validate_status(self) -> "PTUSnapshotUse":
        supplied = self.pricing_snapshot is not None and self.density_snapshot is not None
        if self.status == "USED" and not supplied:
            raise ValueError("used PTU provenance requires both snapshot hashes")
        if self.status == "NOT_USED" and (
            self.pricing_snapshot is not None or self.density_snapshot is not None
        ):
            raise ValueError("unused PTU provenance must not carry snapshot data")
        return self


class ConclusionProvenance(StrictReportModel):
    method_id: Literal["usage-profile"]
    method_version: Literal["1.0.0"]
    usage_schema_version: Literal["1.0.0"]
    workload_schema_version: Literal["1.0.0"]
    report_schema_version: Literal["1.0.0"]
    cli_version: Literal["0.2.0"]
    pricing_snapshot: PricingUse
    ptu_sizing_snapshots: PTUSnapshotUse
    claim_registry_sha256: Sha256


EvidenceValue = str | int | float | list[str] | None


class Evidence(StrictReportModel):
    metric: Annotated[str, Field(min_length=1)]
    value: EvidenceValue
    unit: Annotated[str, Field(min_length=1)]


class Conclusion(StrictReportModel):
    id: Annotated[str, Field(pattern=r"^[a-z0-9-]+$")]
    boundary: Boundary
    finding: Annotated[str, Field(min_length=1)]
    evidence: Annotated[list[Evidence], Field(min_length=1)]
    assumptions: Annotated[list[str], Field(min_length=1)]
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    recommendation: Annotated[str, Field(min_length=1)]
    source_rows: SourceRows
    selector: Annotated[str, Field(min_length=1)]
    provenance: ConclusionProvenance


class MethodSummary(StrictReportModel):
    id: Literal["usage-profile"]
    version: Literal["1.0.0"]


class ContractSummary(StrictReportModel):
    usage_envelope: Literal["1.0.0"]
    workload_spec: Literal["1.0.0"]


class RowDimension(StrictReportModel):
    row: Annotated[int, Field(gt=0)]
    model: SafeModelIdentifier
    reasoning_effort: Literal[
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    status_code: Annotated[int, Field(ge=100, le=599)]


class InputSummary(StrictReportModel):
    usage_sha256: Sha256
    workload_sha256: Sha256
    row_count: Annotated[int, Field(gt=0, le=MAX_USAGE_ROWS)]
    row_dimensions: Annotated[
        list[RowDimension],
        Field(min_length=1, max_length=MAX_USAGE_ROWS),
    ]
    window_start_utc: Annotated[
        str,
        Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"),
    ]
    window_end_utc: Annotated[
        str,
        Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"),
    ]

    @model_validator(mode="after")
    def validate_window(self) -> "InputSummary":
        if len(self.row_dimensions) != self.row_count or any(
            item.row != expected
            for expected, item in enumerate(self.row_dimensions, start=1)
        ):
            raise ValueError("row dimensions do not cover the input in order")
        parsed: list[datetime] = []
        for value in (self.window_start_utc, self.window_end_utc):
            candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
            try:
                timestamp = datetime.fromisoformat(candidate)
            except ValueError as exc:
                raise ValueError("input windows must be RFC 3339 timestamps") from exc
            if timestamp.tzinfo is None:
                raise ValueError("input windows must include timezones")
            parsed.append(timestamp)
        if parsed[0] > parsed[1]:
            raise ValueError("input window start exceeds end")
        return self


class WorkloadSummary(StrictReportModel):
    name: SafeWorkloadIdentifier
    quality_status: Literal["NOT_MEASURED"]
    thresholds: SafeThresholds


class PricingMetadata(StrictReportModel):
    snapshot_id: SafeSnapshotIdentifier
    sha256: Sha256
    accessed_date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    currency: Literal["USD"]
    modeled_models: list[SafeModelIdentifier]


class ClaimRegistrySummary(StrictReportModel):
    schema_version: Literal["1.0.0"]
    sha256: Sha256


class BoundarySummary(StrictReportModel):
    usage_metrics: Literal["MEASURED"]
    payg_cost: Literal["MODELED", "NOT_MODELED"]
    quality: Literal["NOT_MEASURED"]
    ptu_sizing: Literal["MODELED", "NOT_MODELED"]


class AggregateMetrics(StrictReportModel):
    request_count: Annotated[int, Field(gt=0, le=MAX_USAGE_ROWS)]
    successful_request_count: Annotated[int, Field(ge=0, le=MAX_USAGE_ROWS)]
    status_429_count: Annotated[int, Field(ge=0, le=MAX_USAGE_ROWS)]
    status_429_rate: Annotated[float, Field(ge=0, le=1)]
    input_tokens: Annotated[int, Field(ge=0)]
    cached_input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]
    reasoning_tokens: Annotated[int, Field(ge=0)]
    cached_input_ratio: Annotated[float, Field(ge=0, le=1)]
    reasoning_output_ratio: Annotated[float, Field(ge=0, le=1)]
    mean_latency_ms: Annotated[float, Field(ge=0)]
    p95_latency_ms: Annotated[float, Field(ge=0)]
    mean_retry_after_ms_on_429: Annotated[float, Field(ge=0)] | None
    modeled_cost_status: Literal["MODELED", "NOT_MODELED"]
    mean_modeled_usd_per_request: Annotated[float, Field(ge=0)] | None
    source_rows: SourceRows

    @model_validator(mode="after")
    def validate_counts_and_cost(self) -> "AggregateMetrics":
        if self.successful_request_count > self.request_count:
            raise ValueError("successful count exceeds request count")
        if self.status_429_count > self.request_count:
            raise ValueError("429 count exceeds request count")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached tokens exceed input tokens")
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning tokens exceed output tokens")
        if self.modeled_cost_status == "MODELED" and (
            self.mean_modeled_usd_per_request is None
        ):
            raise ValueError("modeled cost requires a value")
        if self.modeled_cost_status == "NOT_MODELED" and (
            self.mean_modeled_usd_per_request is not None
        ):
            raise ValueError("unmodeled cost must not carry a value")
        return self


class GroupAggregate(AggregateMetrics):
    model: SafeModelIdentifier
    reasoning_effort: Literal[
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]


class PTUInputSnapshot(StrictReportModel):
    mean_prompt_tokens: Annotated[int, Field(ge=0)]
    mean_cached_fraction: Annotated[float, Field(ge=0, le=1)]
    mean_visible_output_tokens: Annotated[int, Field(ge=0)]
    mean_reasoning_tokens: Annotated[int, Field(ge=0)]
    mean_max_output_tokens: Annotated[int, Field(gt=0)]
    expected_rpm: Annotated[float, Field(gt=0)]
    model_id: SafeModelIdentifier

    @model_validator(mode="after")
    def validate_output_cap(self) -> "PTUInputSnapshot":
        full_output = self.mean_visible_output_tokens + self.mean_reasoning_tokens
        if self.mean_max_output_tokens < full_output:
            raise ValueError("mean max output tokens do not cover full output")
        return self


class PTUCalculatorResult(StrictReportModel):
    crossover_rpm_ptu_eq_payg: Annotated[float, Field(gt=0)]
    decision: Literal["ptu_favorable", "payg_favorable", "near_crossover"]
    dominant_driver: Literal[
        "output_weighting",
        "reasoning_accumulation",
        "cache_hit_drop",
        "max_tokens_oversize",
        "balanced",
    ]
    inputs_snapshot: PTUInputSnapshot
    rationale: Annotated[str, Field(min_length=1)]
    recommended_ptu_count: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def validate_rationale(self) -> "PTUCalculatorResult":
        expected = (
            "Modeled from successful-row request shape, declared expected RPM, "
            "declared maximum output tokens, target utilization, and pinned PTU "
            "pricing and density snapshots. Dominant modeled driver: "
            f"{self.dominant_driver}."
        )
        if self.rationale != expected:
            raise ValueError("PTU rationale differs from the modeled result")
        return self


class PTUSizingSummary(StrictReportModel):
    status: Literal["MODELED", "NOT_MODELED"]
    reason: PTUReason
    missing_inputs: list[str]
    source_rows: SourceRows
    pricing_snapshot: SnapshotRef | None = None
    density_snapshot: SnapshotRef | None = None
    result: PTUCalculatorResult | None = None

    @model_validator(mode="after")
    def validate_status(self) -> "PTUSizingSummary":
        modeled_fields = (
            self.pricing_snapshot is not None
            and self.density_snapshot is not None
            and self.result is not None
        )
        if self.status == "MODELED":
            if not modeled_fields:
                raise ValueError("modeled PTU sizing requires snapshots and result")
            if self.reason != "existing batch_runner PTU calculator":
                raise ValueError("modeled PTU sizing reason is invalid")
            if self.missing_inputs:
                raise ValueError("modeled PTU sizing cannot have missing inputs")
        else:
            if (
                self.pricing_snapshot is not None
                or self.density_snapshot is not None
                or self.result is not None
            ):
                raise ValueError("unmodeled PTU sizing must not carry modeled data")
            if self.reason == "existing batch_runner PTU calculator":
                raise ValueError("unmodeled PTU sizing reason is invalid")
            expected_missing = (
                ["expected_rpm", "mean_max_output_tokens"]
                if self.reason == "required capacity inputs were not supplied"
                else []
            )
            if self.missing_inputs != expected_missing:
                raise ValueError("PTU missing inputs contradict the reason")
        return self


class PolicyCandidate(StrictReportModel):
    id: Annotated[str, Field(pattern=r"^[a-z0-9-]+$")]
    action: Annotated[str, Field(pattern=r"^[A-Z0-9_]+$")]
    apply_automatically: Literal[False]
    requires_quality_evaluation: bool
    reason: Annotated[str, Field(min_length=1)]
    conclusion_refs: Annotated[list[str], Field(min_length=1)]
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    evidence: Annotated[list[Evidence], Field(min_length=1)]
    assumptions: Annotated[list[str], Field(min_length=1)]
    source_rows: SourceRows
    selector: Annotated[str, Field(min_length=1)]


class PolicyContract(StrictReportModel):
    schema_id: Literal["wrpo.policy_candidate"] = Field(alias="schema")
    schema_version: Literal["1.0.0"]
    status: Literal["REVIEW_REQUIRED"]
    auto_apply: Literal[False]
    quality_boundary: Literal["NOT_MEASURED"]
    cli_version: Literal["0.2.0"]
    method_id: Literal["usage-profile"]
    method_version: Literal["1.0.0"]
    usage_schema_version: Literal["1.0.0"]
    workload_schema_version: Literal["1.0.0"]
    input_usage_sha256: Sha256
    input_workload_sha256: Sha256
    source_rows: SourceRows
    pricing_snapshot: SnapshotRef
    ptu_sizing_snapshots: PTUSnapshotUse
    claim_registry_sha256: Sha256
    candidates: Annotated[list[PolicyCandidate], Field(min_length=1)]


class PrivacySummary(StrictReportModel):
    raw_prompt_or_completion_included: Literal[False]
    infrastructure_identifiers_included: Literal[False]
    unknown_input_fields_accepted: Literal[False]


class ReportContract(StrictReportModel):
    schema_id: Literal["wrpo.provenance_report"] = Field(alias="schema")
    schema_version: Literal["1.0.0"]
    cli_version: Literal["0.2.0"]
    method: MethodSummary
    contracts: ContractSummary
    input: InputSummary
    workload: WorkloadSummary
    pricing_snapshot: PricingMetadata
    ptu_sizing: PTUSizingSummary
    claim_registry: ClaimRegistrySummary
    boundaries: BoundarySummary
    aggregate: AggregateMetrics
    groups: Annotated[list[GroupAggregate], Field(min_length=1)]
    conclusions: Annotated[list[Conclusion], Field(min_length=1)]
    policy: PolicyContract
    privacy: PrivacySummary

    @model_validator(mode="after")
    def validate_cross_references(self) -> "ReportContract":
        bundled_claim_hash = hashlib.sha256(
            resources.files("batch_runner.data")
            .joinpath("public_claims.v1.json")
            .read_bytes()
        ).hexdigest()
        if self.claim_registry.sha256 != bundled_claim_hash:
            raise ValueError("claim registry hash differs from the bundled contract")
        if self.cli_version != __version__:
            raise ValueError("CLI version mismatch")
        if self.method.id != METHOD_ID or self.method.version != METHOD_VERSION:
            raise ValueError("method mismatch")
        if self.contracts.usage_envelope != USAGE_SCHEMA_VERSION:
            raise ValueError("usage schema mismatch")
        if self.contracts.workload_spec != WORKLOAD_SCHEMA_VERSION:
            raise ValueError("workload schema mismatch")
        if self.boundaries.payg_cost != self.aggregate.modeled_cost_status:
            raise ValueError("PAYG boundary mismatch")
        if self.boundaries.ptu_sizing != self.ptu_sizing.status:
            raise ValueError("PTU boundary mismatch")
        if self.policy.claim_registry_sha256 != self.claim_registry.sha256:
            raise ValueError("policy claim registry mismatch")
        if self.policy.cli_version != self.cli_version:
            raise ValueError("policy CLI version mismatch")
        if self.policy.method_id != self.method.id:
            raise ValueError("policy method ID mismatch")
        if self.policy.method_version != self.method.version:
            raise ValueError("policy method version mismatch")
        if self.policy.usage_schema_version != self.contracts.usage_envelope:
            raise ValueError("policy usage schema mismatch")
        if self.policy.workload_schema_version != self.contracts.workload_spec:
            raise ValueError("policy workload schema mismatch")
        if self.policy.input_usage_sha256 != self.input.usage_sha256:
            raise ValueError("policy input hash mismatch")
        if self.policy.input_workload_sha256 != self.input.workload_sha256:
            raise ValueError("policy workload hash mismatch")
        if self.policy.source_rows != self.aggregate.source_rows:
            raise ValueError("policy source rows mismatch")
        if self.policy.pricing_snapshot != SnapshotRef(
            snapshot_id=self.pricing_snapshot.snapshot_id,
            sha256=self.pricing_snapshot.sha256,
        ):
            raise ValueError("policy pricing snapshot mismatch")
        if self.input.row_count != self.aggregate.request_count:
            raise ValueError("input and aggregate row counts differ")
        if self.aggregate.source_rows.count != self.aggregate.request_count:
            raise ValueError("aggregate source rows do not cover all requests")
        expected_rows = set(range(1, self.input.row_count + 1))
        if self.aggregate.source_rows.indices() != expected_rows:
            raise ValueError("aggregate source rows are outside the input")
        ptu_source_rows = self.ptu_sizing.source_rows.indices()
        if not ptu_source_rows.issubset(expected_rows):
            raise ValueError("PTU source rows are outside the input")
        expected_429_rate = (
            self.aggregate.status_429_count / self.aggregate.request_count
        )
        expected_cache_ratio = (
            self.aggregate.cached_input_tokens / self.aggregate.input_tokens
            if self.aggregate.input_tokens
            else 0.0
        )
        expected_reasoning_ratio = (
            self.aggregate.reasoning_tokens / self.aggregate.output_tokens
            if self.aggregate.output_tokens
            else 0.0
        )
        tolerance = 0.0000005
        if abs(self.aggregate.status_429_rate - expected_429_rate) > tolerance:
            raise ValueError("aggregate 429 rate is inconsistent")
        if abs(self.aggregate.cached_input_ratio - expected_cache_ratio) > tolerance:
            raise ValueError("aggregate cache ratio is inconsistent")
        if (
            abs(self.aggregate.reasoning_output_ratio - expected_reasoning_ratio)
            > tolerance
        ):
            raise ValueError("aggregate reasoning ratio is inconsistent")
        keys = [(group.model, group.reasoning_effort) for group in self.groups]
        if len(keys) != len(set(keys)):
            raise ValueError("group model/effort keys are not unique")
        grouped_rows: set[int] = set()
        dimensions = {
            item.row: (item.model, item.reasoning_effort, item.status_code)
            for item in self.input.row_dimensions
        }
        expected_successful = sum(
            200 <= item.status_code < 300
            for item in self.input.row_dimensions
        )
        expected_429_count = sum(
            item.status_code == 429 for item in self.input.row_dimensions
        )
        if self.aggregate.successful_request_count != expected_successful:
            raise ValueError("aggregate successful count contradicts row statuses")
        if self.aggregate.status_429_count != expected_429_count:
            raise ValueError("aggregate 429 count contradicts row statuses")
        distinct_models = {item.model for item in self.input.row_dimensions}
        if self.ptu_sizing.status == "MODELED":
            if (
                self.ptu_sizing.reason != "existing batch_runner PTU calculator"
                or len(distinct_models) != 1
                or expected_successful == 0
                or self.ptu_sizing.missing_inputs
                or self.ptu_sizing.result is None
                or self.ptu_sizing.result.inputs_snapshot.model_id
                != next(iter(distinct_models))
            ):
                raise ValueError("modeled PTU reason mismatch")
        elif self.ptu_sizing.reason == "required capacity inputs were not supplied":
            if self.ptu_sizing.missing_inputs != [
                "expected_rpm",
                "mean_max_output_tokens",
            ] or ptu_source_rows != expected_rows:
                raise ValueError("missing PTU inputs contradict the applicability reason")
        elif self.ptu_sizing.reason == (
            "PTU sizing requires exactly one model per report"
        ):
            if (
                len(distinct_models) <= 1
                or self.ptu_sizing.missing_inputs
                or ptu_source_rows != expected_rows
            ):
                raise ValueError("PTU model count contradicts the applicability reason")
        elif self.ptu_sizing.reason == (
            "PTU sizing requires at least one successful usage row"
        ):
            if (
                len(distinct_models) != 1
                or expected_successful != 0
                or self.ptu_sizing.missing_inputs
                or ptu_source_rows != expected_rows
            ):
                raise ValueError("PTU row statuses contradict the applicability reason")
        elif self.ptu_sizing.reason == (
            "pinned PAYG snapshot does not support the analyzed model"
        ):
            if (
                len(distinct_models) != 1
                or expected_successful == 0
                or self.boundaries.payg_cost != "NOT_MODELED"
                or self.ptu_sizing.missing_inputs
                or ptu_source_rows != expected_rows
            ):
                raise ValueError("PAYG snapshot support reason contradicts report rows")
        elif self.ptu_sizing.reason == (
            "pinned PTU snapshots do not support the analyzed model"
        ):
            if (
                len(distinct_models) != 1
                or expected_successful == 0
                or self.ptu_sizing.missing_inputs
                or ptu_source_rows != expected_rows
            ):
                raise ValueError("PTU snapshot support reason contradicts report rows")
        for group in self.groups:
            if group.source_rows.count != group.request_count:
                raise ValueError("group source row count mismatch")
            indices = group.source_rows.indices()
            if not indices.issubset(expected_rows):
                raise ValueError("group source rows are outside the input")
            if grouped_rows.intersection(indices):
                raise ValueError("group source rows overlap")
            if any(
                dimensions[index][:2] != (group.model, group.reasoning_effort)
                for index in indices
            ):
                raise ValueError("group source rows contradict row dimensions")
            expected_group_successful = sum(
                200 <= dimensions[index][2] < 300 for index in indices
            )
            expected_group_429 = sum(
                dimensions[index][2] == 429 for index in indices
            )
            if group.successful_request_count != expected_group_successful:
                raise ValueError("group successful count contradicts row statuses")
            if group.status_429_count != expected_group_429:
                raise ValueError("group 429 count contradicts row statuses")
            grouped_rows.update(indices)
            group_429_rate = group.status_429_count / group.request_count
            group_cache_ratio = (
                group.cached_input_tokens / group.input_tokens
                if group.input_tokens
                else 0.0
            )
            group_reasoning_ratio = (
                group.reasoning_tokens / group.output_tokens
                if group.output_tokens
                else 0.0
            )
            if abs(group.status_429_rate - group_429_rate) > tolerance:
                raise ValueError("group 429 rate is inconsistent")
            if abs(group.cached_input_ratio - group_cache_ratio) > tolerance:
                raise ValueError("group cache ratio is inconsistent")
            if abs(group.reasoning_output_ratio - group_reasoning_ratio) > tolerance:
                raise ValueError("group reasoning ratio is inconsistent")
        if grouped_rows != expected_rows:
            raise ValueError("group source rows do not partition the input")
        additive_fields = (
            "request_count",
            "successful_request_count",
            "status_429_count",
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
        )
        for field_name in additive_fields:
            grouped = sum(getattr(group, field_name) for group in self.groups)
            if grouped != getattr(self.aggregate, field_name):
                raise ValueError(f"group sum differs for {field_name}")
        all_groups_modeled = all(
            group.modeled_cost_status == "MODELED" for group in self.groups
        )
        if (self.aggregate.modeled_cost_status == "MODELED") != all_groups_modeled:
            raise ValueError("aggregate PAYG status contradicts group statuses")
        if all_groups_modeled:
            weighted_cost = sum(
                float(group.mean_modeled_usd_per_request) * group.request_count
                for group in self.groups
            ) / self.aggregate.request_count
            aggregate_cost = self.aggregate.mean_modeled_usd_per_request
            assert aggregate_cost is not None
            if abs(aggregate_cost - weighted_cost) > 0.000000005:
                raise ValueError("aggregate PAYG cost contradicts group costs")
        expected_ids = [
            "quality-boundary",
            "reasoning-token-share",
            "cache-observation",
            "throttling-observation",
            "tail-latency-observation",
            "payg-cost-model",
            "ptu-applicability",
        ]
        if [item.id for item in self.conclusions] != expected_ids:
            raise ValueError("conclusion identities or order mismatch")
        for item in self.conclusions:
            if item.provenance.claim_registry_sha256 != self.claim_registry.sha256:
                raise ValueError("conclusion claim registry mismatch")
            expected_conclusion_rows = (
                self.ptu_sizing.source_rows
                if item.id == "ptu-applicability"
                else self.aggregate.source_rows
            )
            if item.source_rows != expected_conclusion_rows:
                raise ValueError("conclusion source rows mismatch")
            provenance = item.provenance
            if provenance.cli_version != self.cli_version:
                raise ValueError("conclusion CLI version mismatch")
            if provenance.method_id != self.method.id:
                raise ValueError("conclusion method ID mismatch")
            if provenance.method_version != self.method.version:
                raise ValueError("conclusion method version mismatch")
            if provenance.usage_schema_version != self.contracts.usage_envelope:
                raise ValueError("conclusion usage schema mismatch")
            if provenance.workload_schema_version != self.contracts.workload_spec:
                raise ValueError("conclusion workload schema mismatch")
        by_id = {item.id: item for item in self.conclusions}
        for conclusion_id, conclusion in by_id.items():
            pricing_should_be_used = (
                conclusion_id == "payg-cost-model"
                and self.aggregate.modeled_cost_status == "MODELED"
            ) or (
                conclusion_id == "ptu-applicability"
                and self.ptu_sizing.status == "MODELED"
            )
            ptu_should_be_used = (
                conclusion_id == "ptu-applicability"
                and self.ptu_sizing.status == "MODELED"
            )
            expected_pricing_status = (
                "USED" if pricing_should_be_used else "NOT_USED"
            )
            expected_ptu_status = "USED" if ptu_should_be_used else "NOT_USED"
            if conclusion.provenance.pricing_snapshot.status != expected_pricing_status:
                raise ValueError(f"{conclusion_id} pricing provenance mismatch")
            if (
                conclusion.provenance.ptu_sizing_snapshots.status
                != expected_ptu_status
            ):
                raise ValueError(f"{conclusion_id} PTU provenance mismatch")

        def require_conclusion(
            conclusion_id: str,
            *,
            boundary: str,
            finding: str,
            evidence: list[tuple[str, object, str]],
            selector: str,
            confidence: str,
            recommendation: str,
            assumptions: list[str],
        ) -> Conclusion:
            item = by_id[conclusion_id]
            actual_evidence = [
                (entry.metric, entry.value, entry.unit)
                for entry in item.evidence
            ]
            if item.boundary != boundary:
                raise ValueError(f"{conclusion_id} boundary mismatch")
            if item.finding != finding:
                raise ValueError(f"{conclusion_id} finding mismatch")
            if actual_evidence != evidence:
                raise ValueError(f"{conclusion_id} evidence mismatch")
            if item.selector != selector:
                raise ValueError(f"{conclusion_id} selector mismatch")
            if item.confidence != confidence:
                raise ValueError(f"{conclusion_id} confidence mismatch")
            if item.recommendation != recommendation:
                raise ValueError(f"{conclusion_id} recommendation mismatch")
            if item.assumptions != assumptions:
                raise ValueError(f"{conclusion_id} assumptions mismatch")
            return item

        require_conclusion(
            "quality-boundary",
            boundary="NOT_MEASURED",
            finding=QUALITY_FINDING,
            evidence=[("quality_status", "NOT_MEASURED", "status")],
            selector="workload.quality.status",
            confidence="HIGH",
            recommendation=QUALITY_RECOMMENDATION,
            assumptions=["No evaluator or task-success measurements were supplied."],
        )
        require_conclusion(
            "reasoning-token-share",
            boundary="MEASURED",
            finding=(
                "Reasoning tokens are "
                f"{self.aggregate.reasoning_output_ratio:.1%} of measured "
                "output tokens."
            ),
            evidence=[
                (
                    "reasoning_output_ratio",
                    self.aggregate.reasoning_output_ratio,
                    "fraction",
                )
            ],
            selector="sum(reasoning_tokens) / sum(output_tokens)",
            confidence="HIGH",
            recommendation=(
                "Use the measured share to choose experiment candidates; do "
                "not infer quality impact from token share alone."
            ),
            assumptions=[
                "reasoning_tokens is a labeled subset of output_tokens.",
                "Rows follow UsageEnvelope v1 token semantics.",
            ],
        )
        require_conclusion(
            "cache-observation",
            boundary="MEASURED",
            finding=(
                f"Cached input is {self.aggregate.cached_input_ratio:.1%} "
                "of measured input."
            ),
            evidence=[
                (
                    "cached_input_ratio",
                    self.aggregate.cached_input_ratio,
                    "fraction",
                )
            ],
            selector="sum(cached_input_tokens) / sum(input_tokens)",
            confidence="HIGH",
            recommendation=(
                "Investigate prefix stability only if this ratio misses the "
                "workload threshold; model-specific cache semantics remain external."
            ),
            assumptions=["cached_input_tokens is a subset of input_tokens."],
        )
        require_conclusion(
            "throttling-observation",
            boundary="MEASURED",
            finding=(
                f"HTTP 429 responses are {self.aggregate.status_429_rate:.1%} "
                "of measured requests."
            ),
            evidence=[
                ("status_429_rate", self.aggregate.status_429_rate, "fraction")
            ],
            selector="count(status_code == 429) / count(rows)",
            confidence="HIGH",
            recommendation=(
                "Compare the measured rate with the safe threshold and inspect "
                "capacity or retry policy before changing routing."
            ),
            assumptions=["Every attempted request is represented by one input row."],
        )
        require_conclusion(
            "tail-latency-observation",
            boundary="MEASURED",
            finding=(
                "Nearest-rank p95 latency is "
                f"{self.aggregate.p95_latency_ms:.0f} ms."
            ),
            evidence=[
                ("p95_latency_ms", self.aggregate.p95_latency_ms, "milliseconds")
            ],
            selector="nearest_rank(rows[*].latency_ms, 0.95)",
            confidence="HIGH",
            recommendation="Inspect latency by effort and status before tuning.",
            assumptions=["latency_ms uses a consistent client-side boundary."],
        )
        if self.aggregate.modeled_cost_status == "MODELED":
            cost_value = self.aggregate.mean_modeled_usd_per_request
            assert cost_value is not None
            cost_finding = (
                f"Mean modeled PAYG cost is ${cost_value:.9f} "
                "per attempted request."
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
                "PAYG cost is NOT_MODELED because the pinned pricing snapshot "
                "does not contain every analyzed model."
            )
            cost_confidence = "HIGH"
            cost_recommendation = (
                "Add a pinned pricing entry for every analyzed model before "
                "making a cost decision."
            )
            cost_assumptions = [
                "No numeric PAYG cost is emitted when any analyzed model is unpriced."
            ]
        cost = require_conclusion(
            "payg-cost-model",
            boundary=self.aggregate.modeled_cost_status,
            finding=cost_finding,
            evidence=[
                ("mean_modeled_usd_per_request", cost_value, "USD/request")
            ],
            selector="mean(existing cost_calculator.payg_cost_per_call(rows[*]))",
            confidence=cost_confidence,
            recommendation=cost_recommendation,
            assumptions=cost_assumptions,
        )
        if self.ptu_sizing.status == "MODELED":
            assert self.ptu_sizing.result is not None
            result = self.ptu_sizing.result
            ptu_finding = (
                "PTU sizing is MODELED by the existing calculator: "
                f"decision={result.decision}, "
                f"recommended_ptu_count={result.recommended_ptu_count}, "
                f"crossover_rpm={result.crossover_rpm_ptu_eq_payg:.3f}."
            )
            ptu_evidence = [
                ("recommended_ptu_count", result.recommended_ptu_count, "PTU"),
                (
                    "crossover_rpm_ptu_eq_payg",
                    result.crossover_rpm_ptu_eq_payg,
                    "requests/minute",
                ),
            ]
            ptu_selector = (
                "existing ptu_vs_payg_calculator.calculate(successful rows, "
                "WorkloadSpec.ptu_sizing)"
            )
            ptu_confidence = "MEDIUM"
            ptu_recommendation = (
                "Review the modeled decision against live capacity availability; "
                "quota does not guarantee capacity."
            )
            ptu_assumptions = [
                "expected_rpm and mean_max_output_tokens come from WorkloadSpec.",
                "Successful usage rows define the mean request shape.",
                "The pinned PTU pricing and density snapshots remain applicable.",
            ]
        else:
            ptu_finding = (
                f"PTU sizing is NOT_MODELED: {self.ptu_sizing.reason}."
            )
            ptu_evidence = [
                (
                    "missing_capacity_inputs",
                    self.ptu_sizing.missing_inputs,
                    "field-names",
                )
            ]
            ptu_selector = "WorkloadSpec.ptu_sizing applicability"
            ptu_confidence = "HIGH"
            ptu_recommendation = (
                "Resolve the stated applicability condition before using PTU sizing."
            )
            ptu_assumptions = [
                "No PTU decision is emitted while the stated applicability "
                "condition is unmet."
            ]
        ptu = require_conclusion(
            "ptu-applicability",
            boundary=self.ptu_sizing.status,
            finding=ptu_finding,
            evidence=ptu_evidence,
            selector=ptu_selector,
            confidence=ptu_confidence,
            recommendation=ptu_recommendation,
            assumptions=ptu_assumptions,
        )
        if ptu.source_rows != self.ptu_sizing.source_rows:
            raise ValueError("PTU conclusion source rows mismatch")
        if self.boundaries.payg_cost == "MODELED":
            if cost.provenance.pricing_snapshot.status != "USED":
                raise ValueError("modeled PAYG conclusion must cite pricing")
            if (
                cost.provenance.pricing_snapshot.snapshot_id
                != self.pricing_snapshot.snapshot_id
                or cost.provenance.pricing_snapshot.sha256
                != self.pricing_snapshot.sha256
            ):
                raise ValueError("PAYG pricing provenance mismatch")
        if self.ptu_sizing.status == "MODELED":
            successful_rows = {
                item.row
                for item in self.input.row_dimensions
                if 200 <= item.status_code < 300
            }
            if self.ptu_sizing.source_rows.indices() != successful_rows:
                raise ValueError("PTU source rows are not the successful requests")
            ptu_use = ptu.provenance.ptu_sizing_snapshots
            if ptu_use.status != "USED":
                raise ValueError("modeled PTU conclusion must cite snapshots")
            if (
                ptu_use.pricing_snapshot != self.ptu_sizing.pricing_snapshot
                or ptu_use.density_snapshot != self.ptu_sizing.density_snapshot
            ):
                raise ValueError("PTU snapshot provenance mismatch")
            if self.policy.ptu_sizing_snapshots != ptu_use:
                raise ValueError("policy PTU snapshot provenance mismatch")
            if ptu.provenance.pricing_snapshot.status != "USED":
                raise ValueError("modeled PTU conclusion must cite PAYG pricing")
            if (
                ptu.provenance.pricing_snapshot.snapshot_id
                != self.pricing_snapshot.snapshot_id
                or ptu.provenance.pricing_snapshot.sha256
                != self.pricing_snapshot.sha256
            ):
                raise ValueError("PTU PAYG pricing provenance mismatch")
        elif self.policy.ptu_sizing_snapshots.status != "NOT_USED":
            raise ValueError("unmodeled PTU policy must not cite snapshots")
        conclusion_ids = set(expected_ids)
        candidate_ids = [candidate.id for candidate in self.policy.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("policy candidate IDs are not unique")
        for candidate in self.policy.candidates:
            if not set(candidate.conclusion_refs).issubset(conclusion_ids):
                raise ValueError("policy candidate references unknown conclusion")
            if candidate.source_rows != self.aggregate.source_rows:
                raise ValueError("policy candidate source rows mismatch")
        source_rows_payload = self.aggregate.source_rows.model_dump()

        def expected_candidate(
            *,
            candidate_id: str,
            action: str,
            requires_quality_evaluation: bool,
            reason: str,
            conclusion_refs: list[str],
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
                "confidence": "HIGH",
                "evidence": [{"metric": metric, "value": value, "unit": unit}],
                "assumptions": assumptions,
                "source_rows": source_rows_payload,
                "selector": selector,
            }

        expected_candidates = [
            expected_candidate(
                candidate_id="reasoning-effort-quality-experiment",
                action="RUN_CONTROLLED_EXPERIMENT",
                requires_quality_evaluation=True,
                reason=(
                    "Usage metadata measures token and latency behavior but cannot "
                    "establish that an effort change preserves quality."
                ),
                conclusion_refs=["quality-boundary", "reasoning-token-share"],
                metric="quality_status",
                value="NOT_MEASURED",
                unit="status",
                assumptions=[
                    "No evaluator or task-success measurements were supplied."
                ],
                selector="workload.quality.status",
            )
        ]
        thresholds = self.workload.thresholds
        if (
            self.aggregate.reasoning_output_ratio
            > thresholds.max_reasoning_output_ratio
        ):
            expected_candidates.append(
                expected_candidate(
                    candidate_id="investigate-reasoning-share",
                    action="INVESTIGATE_REASONING_EFFORT_WITH_QUALITY_EVAL",
                    requires_quality_evaluation=True,
                    reason=(
                        "Measured reasoning/output ratio exceeds the workload "
                        "threshold."
                    ),
                    conclusion_refs=["reasoning-token-share", "quality-boundary"],
                    metric="reasoning_output_ratio",
                    value=self.aggregate.reasoning_output_ratio,
                    unit="fraction",
                    assumptions=[
                        "reasoning_tokens is a labeled subset of output_tokens."
                    ],
                    selector="sum(reasoning_tokens) / sum(output_tokens)",
                )
            )
        if self.aggregate.status_429_rate > thresholds.max_429_rate:
            expected_candidates.append(
                expected_candidate(
                    candidate_id="investigate-429-pressure",
                    action="INVESTIGATE_CAPACITY_OR_RETRY_POLICY",
                    requires_quality_evaluation=False,
                    reason="Measured HTTP 429 rate exceeds the workload threshold.",
                    conclusion_refs=[
                        "throttling-observation",
                        "ptu-applicability",
                    ],
                    metric="status_429_rate",
                    value=self.aggregate.status_429_rate,
                    unit="fraction",
                    assumptions=[
                        "Every attempted request is represented by one row."
                    ],
                    selector="count(status_code == 429) / count(rows)",
                )
            )
        if self.aggregate.cached_input_ratio < thresholds.min_cached_input_ratio:
            expected_candidates.append(
                expected_candidate(
                    candidate_id="investigate-cache-reuse",
                    action="CHECK_STABLE_PREFIX_AND_CACHE_ACCOUNTING",
                    requires_quality_evaluation=False,
                    reason=(
                        "Measured cached-input ratio is below the workload threshold."
                    ),
                    conclusion_refs=["cache-observation"],
                    metric="cached_input_ratio",
                    value=self.aggregate.cached_input_ratio,
                    unit="fraction",
                    assumptions=[
                        "cached_input_tokens is a subset of input_tokens."
                    ],
                    selector="sum(cached_input_tokens) / sum(input_tokens)",
                )
            )
        if self.aggregate.p95_latency_ms > thresholds.max_p95_latency_ms:
            expected_candidates.append(
                expected_candidate(
                    candidate_id="investigate-tail-latency",
                    action="PROFILE_P95_LATENCY_BY_EFFORT",
                    requires_quality_evaluation=True,
                    reason="Measured p95 latency exceeds the workload threshold.",
                    conclusion_refs=[
                        "tail-latency-observation",
                        "quality-boundary",
                    ],
                    metric="p95_latency_ms",
                    value=self.aggregate.p95_latency_ms,
                    unit="milliseconds",
                    assumptions=[
                        "latency_ms uses a consistent client-side boundary."
                    ],
                    selector="nearest_rank(rows[*].latency_ms, 0.95)",
                )
            )
        actual_candidates = [
            candidate.model_dump() for candidate in self.policy.candidates
        ]
        if actual_candidates != expected_candidates:
            raise ValueError("policy candidate semantics mismatch")
        return self


__all__ = [
    "PolicyContract",
    "QUALITY_FINDING",
    "QUALITY_RECOMMENDATION",
    "ReportContract",
]
