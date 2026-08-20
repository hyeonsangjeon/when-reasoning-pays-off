"""Strict, versioned contracts for the offline usage analyzer."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from batch_runner.privacy import PrivacyViolation, ensure_safe_identifier

USAGE_SCHEMA_VERSION = "1.0.0"
WORKLOAD_SCHEMA_VERSION = "1.0.0"
METHOD_ID = "usage-profile"
METHOD_VERSION = "1.0.0"
MAX_USAGE_ROWS = 100_000
MAX_JSONL_LINE_BYTES = 1_000_000
MAX_USAGE_FILE_BYTES = 64 * 1024 * 1024
MAX_WORKLOAD_FILE_BYTES = 4 * 1024 * 1024
MAX_TOKEN_COUNT = 2_147_483_647
MAX_DURATION_MS = 604_800_000
MAX_EXPECTED_RPM = 1_000_000_000

ReasoningEffort = Literal[
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
]
Number = Annotated[int | float, Field(ge=0)]


class InputValidationError(ValueError):
    """Safe input error containing only line numbers and field names."""


class StrictContract(BaseModel):
    """Base model that rejects unknown fields at every nesting level."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


_KNOWN_ERROR_FIELDS = {
    "timestamp",
    "provider",
    "model",
    "reasoning_effort",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "latency_ms",
    "status_code",
    "retry_after_ms",
    "schema_version",
    "name",
    "method",
    "id",
    "version",
    "pricing",
    "snapshot_id",
    "snapshot_file",
    "quality",
    "status",
    "thresholds",
    "max_429_rate",
    "max_p95_latency_ms",
    "min_cached_input_ratio",
    "max_reasoning_output_ratio",
    "ptu_sizing",
    "expected_rpm",
    "mean_max_output_tokens",
    "target_utilization",
    "pricing_snapshot_id",
    "pricing_snapshot_file",
    "density_snapshot_id",
    "density_snapshot_file",
}


def _finite_number(
    value: object,
    *,
    field_name: str,
    maximum: float | None = None,
) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be numeric")
    try:
        numeric = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field_name} must be finite and non-negative") from exc
    if not math.isfinite(numeric) or numeric < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")
    if maximum is not None and numeric > maximum:
        raise ValueError(f"{field_name} exceeds the supported maximum")
    return value


def _normalize_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be RFC 3339 text")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except (OverflowError, ValueError) as exc:
        raise ValueError("timestamp must be RFC 3339 text") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    normalized = parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return normalized.replace("+00:00", "Z")


def _safe_relative_resource(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("snapshot_file must be non-empty text")
    if "\\" in value:
        raise ValueError("snapshot_file must use portable forward slashes")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", value):
        raise ValueError("snapshot_file contains unsupported characters")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError("snapshot_file must be a safe relative path")
    return value


class UsageEnvelope(StrictContract):
    """Operational metadata only; content and infrastructure IDs are not fields."""

    timestamp: str
    provider: Literal["azure-openai"]
    model: Annotated[str, Field(min_length=1, max_length=64)]
    reasoning_effort: ReasoningEffort
    input_tokens: Annotated[int, Field(ge=0, le=MAX_TOKEN_COUNT)]
    cached_input_tokens: Annotated[int, Field(ge=0, le=MAX_TOKEN_COUNT)]
    output_tokens: Annotated[int, Field(ge=0, le=MAX_TOKEN_COUNT)]
    reasoning_tokens: Annotated[int, Field(ge=0, le=MAX_TOKEN_COUNT)]
    latency_ms: Number
    status_code: Annotated[int, Field(ge=100, le=599)]
    retry_after_ms: Number | None

    @field_validator("timestamp", mode="before")
    @classmethod
    def validate_timestamp(cls, value: object) -> str:
        return _normalize_timestamp(value)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
            raise ValueError("model must be a safe model identifier")
        ensure_safe_identifier(value, label="model")
        return value

    @field_validator("latency_ms", "retry_after_ms", mode="before")
    @classmethod
    def validate_numbers(cls, value: object, info) -> object:
        if value is None and info.field_name == "retry_after_ms":
            return None
        return _finite_number(
            value,
            field_name=info.field_name,
            maximum=MAX_DURATION_MS,
        )

    @model_validator(mode="after")
    def validate_token_subsets(self) -> "UsageEnvelope":
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens must not exceed input_tokens")
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning_tokens must not exceed output_tokens")
        return self


class MethodSnapshot(StrictContract):
    id: Literal["usage-profile"]
    version: Literal["1.0.0"]


class PricingSnapshot(StrictContract):
    snapshot_id: Annotated[str, Field(min_length=1, max_length=80)]
    snapshot_file: str

    @field_validator("snapshot_id")
    @classmethod
    def validate_snapshot_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value):
            raise ValueError("snapshot_id must be a safe identifier")
        ensure_safe_identifier(value, label="pricing snapshot ID")
        return value

    @field_validator("snapshot_file", mode="before")
    @classmethod
    def validate_snapshot_file(cls, value: object) -> str:
        return _safe_relative_resource(value)


class QualityBoundary(StrictContract):
    status: Literal["NOT_MEASURED"]


class SafeThresholds(StrictContract):
    max_429_rate: Number
    max_p95_latency_ms: Number
    min_cached_input_ratio: Number
    max_reasoning_output_ratio: Number

    @field_validator(
        "max_429_rate",
        "max_p95_latency_ms",
        "min_cached_input_ratio",
        "max_reasoning_output_ratio",
        mode="before",
    )
    @classmethod
    def validate_numbers(cls, value: object, info) -> object:
        maximum = (
            MAX_DURATION_MS
            if info.field_name == "max_p95_latency_ms"
            else 1.0
        )
        return _finite_number(
            value,
            field_name=info.field_name,
            maximum=maximum,
        )

    @model_validator(mode="after")
    def validate_ratios(self) -> "SafeThresholds":
        for field_name in (
            "max_429_rate",
            "min_cached_input_ratio",
            "max_reasoning_output_ratio",
        ):
            if float(getattr(self, field_name)) > 1.0:
                raise ValueError(f"{field_name} must be between 0 and 1")
        if float(self.max_p95_latency_ms) <= 0:
            raise ValueError("max_p95_latency_ms must be greater than zero")
        return self


class PTUSizingSpec(StrictContract):
    expected_rpm: Number
    mean_max_output_tokens: Annotated[int, Field(gt=0, le=MAX_TOKEN_COUNT)]
    target_utilization: Number
    pricing_snapshot_id: Annotated[str, Field(min_length=1, max_length=80)]
    pricing_snapshot_file: str
    density_snapshot_id: Annotated[str, Field(min_length=1, max_length=80)]
    density_snapshot_file: str

    @field_validator("expected_rpm", "target_utilization", mode="before")
    @classmethod
    def validate_numbers(cls, value: object, info) -> object:
        checked = _finite_number(
            value,
            field_name=info.field_name,
            maximum=(
                MAX_EXPECTED_RPM
                if info.field_name == "expected_rpm"
                else 1.0
            ),
        )
        if float(checked) <= 0:
            raise ValueError(f"{info.field_name} must be greater than zero")
        return checked

    @field_validator("pricing_snapshot_id", "density_snapshot_id")
    @classmethod
    def validate_snapshot_ids(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value):
            raise ValueError("snapshot ID must be a safe identifier")
        ensure_safe_identifier(value, label="PTU snapshot ID")
        return value

    @field_validator(
        "pricing_snapshot_file",
        "density_snapshot_file",
        mode="before",
    )
    @classmethod
    def validate_snapshot_files(cls, value: object) -> str:
        return _safe_relative_resource(value)

    @model_validator(mode="after")
    def validate_utilization(self) -> "PTUSizingSpec":
        if float(self.target_utilization) > 1.0:
            raise ValueError("target_utilization must not exceed one")
        return self


class WorkloadSpec(StrictContract):
    schema_version: Literal["1.0.0"]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    method: MethodSnapshot
    pricing: PricingSnapshot
    quality: QualityBoundary
    thresholds: SafeThresholds
    ptu_sizing: PTUSizingSpec | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", value):
            raise ValueError("name must be a safe workload slug")
        ensure_safe_identifier(value, label="workload name")
        return value


def _safe_validation_summary(exc: ValidationError) -> str:
    fields: set[str] = set()
    for error in exc.errors(include_url=False, include_input=False):
        safe_parts = [
            (
                str(part)
                if isinstance(part, int) or str(part) in _KNOWN_ERROR_FIELDS
                else "<unknown-field>"
            )
            for part in error.get("loc", ())
        ]
        location = ".".join(safe_parts)
        fields.add(location or "<root>")
    return ", ".join(sorted(fields))


def _contains_privacy_violation(exc: ValidationError) -> bool:
    return any(
        isinstance(error.get("ctx", {}).get("error"), PrivacyViolation)
        for error in exc.errors(include_url=False)
    )


def load_usage_jsonl(path: Path) -> tuple[list[UsageEnvelope], str]:
    """Load strict JSONL without including raw lines or paths in exceptions."""

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise InputValidationError("usage input could not be read") from exc
    if size > MAX_USAGE_FILE_BYTES:
        raise InputValidationError(
            f"usage input exceeds the {MAX_USAGE_FILE_BYTES}-byte safety limit"
        )

    rows: list[UsageEnvelope] = []
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                digest.update(raw_line)
                if not raw_line.strip():
                    continue
                if len(raw_line) > MAX_JSONL_LINE_BYTES:
                    raise InputValidationError(
                        f"usage line {line_number} is too large"
                    )
                if len(rows) >= MAX_USAGE_ROWS:
                    raise InputValidationError(
                        f"usage input exceeds the {MAX_USAGE_ROWS}-row safety limit"
                    )
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise InputValidationError(
                        f"usage line {line_number} must be UTF-8"
                    ) from exc
                try:
                    payload = json.loads(line)
                except (ValueError, OverflowError, RecursionError) as exc:
                    raise InputValidationError(
                        f"usage line {line_number} is not valid JSON"
                    ) from exc
                if not isinstance(payload, dict):
                    raise InputValidationError(
                        f"usage line {line_number} must be a JSON object"
                    )
                try:
                    rows.append(UsageEnvelope.model_validate(payload))
                except ValidationError as exc:
                    if _contains_privacy_violation(exc):
                        raise PrivacyViolation(
                            "usage input contains prohibited private data"
                        ) from exc
                    fields = _safe_validation_summary(exc)
                    raise InputValidationError(
                        f"usage line {line_number} failed fields: {fields}"
                    ) from exc
    except OSError as exc:
        raise InputValidationError("usage input could not be read") from exc
    if not rows:
        raise InputValidationError("usage input contains no records")
    return rows, digest.hexdigest()


def load_workload_yaml(path: Path) -> tuple[WorkloadSpec, bytes]:
    """Load a strict WorkloadSpec without echoing path, free text, or values."""

    try:
        with path.open("rb") as handle:
            raw_bytes = handle.read(MAX_WORKLOAD_FILE_BYTES + 1)
    except OSError as exc:
        raise InputValidationError("workload input could not be read") from exc
    if len(raw_bytes) > MAX_WORKLOAD_FILE_BYTES:
        raise InputValidationError(
            f"workload input exceeds the {MAX_WORKLOAD_FILE_BYTES}-byte safety limit"
        )
    try:
        payload = yaml.safe_load(raw_bytes.decode("utf-8"))
    except (ValueError, OverflowError, RecursionError, yaml.YAMLError) as exc:
        raise InputValidationError("workload input is not valid UTF-8 YAML") from exc
    if not isinstance(payload, dict):
        raise InputValidationError("workload input root must be a mapping")
    try:
        workload = WorkloadSpec.model_validate(payload)
    except ValidationError as exc:
        if _contains_privacy_violation(exc):
            raise PrivacyViolation(
                "workload input contains prohibited private data"
            ) from exc
        fields = _safe_validation_summary(exc)
        raise InputValidationError(f"workload failed fields: {fields}") from exc
    return workload, raw_bytes


def resolve_snapshot_path(workload_path: Path, snapshot_file: str) -> Path:
    """Resolve a validated relative snapshot path beside the workload file."""

    try:
        base = workload_path.resolve().parent
        candidate = (base / snapshot_file).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise InputValidationError("pricing snapshot path is invalid") from exc
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise InputValidationError("pricing snapshot escapes workload directory") from exc
    return candidate


__all__ = [
    "InputValidationError",
    "MAX_USAGE_ROWS",
    "MAX_USAGE_FILE_BYTES",
    "MAX_WORKLOAD_FILE_BYTES",
    "MAX_TOKEN_COUNT",
    "MAX_DURATION_MS",
    "MAX_EXPECTED_RPM",
    "METHOD_ID",
    "METHOD_VERSION",
    "USAGE_SCHEMA_VERSION",
    "WORKLOAD_SCHEMA_VERSION",
    "PricingSnapshot",
    "PTUSizingSpec",
    "QualityBoundary",
    "SafeThresholds",
    "UsageEnvelope",
    "WorkloadSpec",
    "load_usage_jsonl",
    "load_workload_yaml",
    "resolve_snapshot_path",
]
