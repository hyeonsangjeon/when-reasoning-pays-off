"""The versioned, strict *run ledger* — the ``IN`` stage of an experiment.

A ledger is a small YAML (or JSON) file that fully describes one real run
*before* it happens: which dataset goes in, which provider and model execute
it, where the endpoint and credentials come from (by environment-variable
**name**, never value), the execution limits, and where the output lands.

Design rules (all enforced here):

* **Fail closed.** Every model forbids unknown fields (``extra="forbid"``) and
  runs in strict mode, so a typo or an injected key is rejected, not ignored.
* **Names, never secrets.** Endpoint and auth are given as environment-variable
  *names*; the ledger never stores an endpoint URL host or a credential.
* **Safe identifiers.** Report-visible identifiers (experiment id, model,
  dataset path) are screened by :mod:`batch_runner.privacy` so a credential- or
  hostname-shaped value cannot smuggle itself into an artifact.
* **Value-free errors.** Validation failures name the field, never the value.

The parsed, validated object is :class:`RunLedger`.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from batch_runner.privacy import (
    PrivacyViolation,
    ensure_safe_identifier,
    ensure_safe_public_text,
)

LEDGER_SCHEMA_VERSION = "1.0.0"

MAX_LEDGER_FILE_BYTES = 256 * 1024
MAX_SAMPLES_CEILING = 50
MAX_CONCURRENCY = 32
MAX_TIMEOUT_SECONDS = 3_600
MAX_OUTPUT_TOKENS_CEILING = 32_768
MAX_REPEATS = 20
MAX_RECORDS_CEILING = 50

#: The exact, ordered artifact set the runner writes. The ledger may not
#: advertise a different set (see :class:`OutputSpec`).
FIXED_ARTIFACTS = [
    "run.json",
    "records.jsonl",
    "summary.md",
    "manifest.json",
    "artifacts.sha256",
]

Provider = Literal["azure", "ollama", "mock"]
DataFormat = Literal["json", "jsonl"]
AuthMode = Literal["none", "entra"]
SampleSelector = Literal["first", "all"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]


def _is_gpt_5_2_deployment(model: str) -> bool:
    """True when a model/deployment name clearly denotes the gpt-5.2 family.

    Deployment names are operator-chosen, so match both the dotted marketing
    form (``gpt-5.2``) and the dashed form that portals often produce
    (``gpt-5-2-...``). This is deliberately conservative: it only fires on an
    unmistakable gpt-5.2 marker so other models keep the permissive default.
    """
    lowered = model.lower()
    return "gpt-5.2" in lowered or "gpt-5-2" in lowered

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

_ALLOWED_ROW_TYPES = frozenset({"string", "integer", "number", "boolean"})


class LedgerError(ValueError):
    """A ledger validation failure that names fields, never values."""


class _Strict(BaseModel):
    """Base model: reject unknown fields, strict types, no inf/NaN."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def _require_env_name(value: str, *, field: str) -> str:
    if not _ENV_NAME_RE.fullmatch(value):
        raise ValueError(
            f"{field} must be an UPPER_SNAKE_CASE environment-variable name, "
            "not a value"
        )
    return value


def _require_safe_relative_path(value: str, *, field: str) -> str:
    if not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty path with no surrounding space")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{field} contains a control character")
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
        raise ValueError(f"{field} must be a local path, not a URL")
    pure = PurePosixPath(value)
    if pure.is_absolute() or value.startswith("\\") or re.match(r"^[A-Za-z]:", value):
        raise ValueError(f"{field} must be a repo-relative path, not absolute")
    if any(part == ".." for part in pure.parts):
        raise ValueError(f"{field} must not escape its directory with '..'")
    try:
        ensure_safe_public_text(value, label=field)
    except PrivacyViolation as exc:  # pragma: no cover - message is value-free
        raise ValueError(str(exc)) from None
    return value


class ExperimentIdentity(_Strict):
    """Who this run is and why it exists."""

    id: str = Field(min_length=1, max_length=120)
    purpose: str = Field(min_length=1, max_length=2_000)

    @field_validator("id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", value):
            raise ValueError(
                "id must be alphanumeric with '.', '_' or '-' and start "
                "alphanumeric"
            )
        try:
            ensure_safe_identifier(value, label="experiment.id")
        except PrivacyViolation as exc:
            raise ValueError(str(exc)) from None
        return value


class EndpointSource(_Strict):
    """Where the base URL comes from — an env-var name, never the URL itself."""

    env_var: str = Field(min_length=1, max_length=64)
    # A local, non-secret default (used by Ollama's localhost API). Azure has
    # no default: its endpoint must come from the environment.
    default: str | None = Field(default=None, max_length=200)

    @field_validator("env_var")
    @classmethod
    def _env_name(cls, value: str) -> str:
        return _require_env_name(value, field="endpoint.env_var")

    @field_validator("default")
    @classmethod
    def _local_default(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # Only an explicit localhost URL is allowed as a committed default so a
        # ledger can never ship a remote endpoint as its fallback.
        if not re.match(r"^http://(localhost|127\.0\.0\.1|\[::1\]):\d{2,5}/?$", value):
            raise ValueError(
                "endpoint.default may only be a localhost http URL "
                "(e.g. http://localhost:11434)"
            )
        return value


class AuthSpec(_Strict):
    """Authentication mode plus the *names* of any env vars it consumes."""

    mode: AuthMode
    # Environment-variable NAMES only (e.g. for documentation of what the
    # credential provider reads). Never credential values.
    env_vars: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("env_vars")
    @classmethod
    def _env_names(cls, value: list[str]) -> list[str]:
        for name in value:
            _require_env_name(name, field="auth.env_vars[]")
        return value


class RowShape(_Strict):
    """The declared shape of one input row."""

    required_fields: dict[str, str] = Field(min_length=1, max_length=32)
    optional_fields: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("required_fields", "optional_fields")
    @classmethod
    def _known_types(cls, value: dict[str, str]) -> dict[str, str]:
        for field_name, type_name in value.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", field_name):
                raise ValueError("row_shape field names must be identifiers")
            if type_name not in _ALLOWED_ROW_TYPES:
                raise ValueError(
                    "row_shape types must be one of "
                    "string, integer, number, boolean"
                )
        return value

    @model_validator(mode="after")
    def _no_overlap(self) -> RowShape:
        overlap = set(self.required_fields) & set(self.optional_fields)
        if overlap:
            raise ValueError("a field cannot be both required and optional")
        for required_name in ("id", "input"):
            if self.required_fields.get(required_name) != "string":
                raise ValueError(
                    "row_shape.required_fields must include string fields "
                    "'id' and 'input'"
                )
        return self


class InputSpec(_Strict):
    """The ``DATA`` stage: the dataset file, its format, and its declared shape."""

    path: str = Field(min_length=1, max_length=400)
    format: DataFormat
    row_shape: RowShape
    max_records: Literal[50]
    sample_selector: SampleSelector = "first"

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        return _require_safe_relative_path(value, field="input.path")


class CostSpec(_Strict):
    """Cost boundary for the run. Only ``azure`` is billed."""

    billed: bool
    confirmed: bool = False
    estimated_usd: float = Field(default=0.0, ge=0.0, le=1_000_000.0)
    hard_ceiling_usd: float = Field(default=0.0, ge=0.0, le=1_000_000.0)
    pricing_snapshot_id: str | None = Field(default=None, min_length=1, max_length=120)
    pricing_model: str | None = Field(default=None, min_length=1, max_length=120)
    input_per_1m_usd: float | None = Field(default=None, gt=0.0, le=1_000_000.0)
    output_per_1m_usd: float | None = Field(default=None, gt=0.0, le=1_000_000.0)

    @model_validator(mode="after")
    def _ceiling_covers_estimate(self) -> CostSpec:
        if self.billed and self.hard_ceiling_usd <= 0.0:
            raise ValueError("a billed run must set cost.hard_ceiling_usd > 0")
        if self.estimated_usd > self.hard_ceiling_usd and self.hard_ceiling_usd > 0.0:
            raise ValueError("cost.estimated_usd exceeds cost.hard_ceiling_usd")
        return self


class ExecutionSpec(_Strict):
    """The ``EXECUTE`` stage limits."""

    max_samples: int = Field(ge=1, le=MAX_SAMPLES_CEILING)
    # This first release runs strictly one request at a time. Concurrency is
    # constrained to exactly 1 rather than recorded as an unimplemented knob.
    concurrency: int = Field(default=1, ge=1, le=1)
    timeout_seconds: int = Field(default=60, ge=1, le=MAX_TIMEOUT_SECONDS)
    max_output_tokens: int = Field(default=256, ge=1, le=MAX_OUTPUT_TOKENS_CEILING)
    repeats: int = Field(default=1, ge=1, le=MAX_REPEATS)
    reasoning_effort: ReasoningEffort | None = None
    capture_io: bool = False
    cost: CostSpec


class OutputSpec(_Strict):
    """The ``OUT`` stage: where artifacts are written."""

    dir: Literal["out"]
    # The runner writes exactly these five artifacts, in this order. The list
    # is fixed rather than free-form so the ledger cannot advertise files that
    # are never produced.
    artifacts: list[str] = Field(
        default_factory=lambda: list(FIXED_ARTIFACTS),
    )

    @field_validator("artifacts")
    @classmethod
    def _fixed_artifacts(cls, value: list[str]) -> list[str]:
        if value != FIXED_ARTIFACTS:
            raise ValueError(
                "output.artifacts must be exactly "
                f"{FIXED_ARTIFACTS!r} for this release"
            )
        return value

    @field_validator("artifacts")
    @classmethod
    def _artifact_names(cls, value: list[str]) -> list[str]:
        for name in value:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
                raise ValueError("output.artifacts entries must be simple file names")
        return value


class Provenance(_Strict):
    """Method identity recorded in every artifact."""

    method_id: str = Field(min_length=1, max_length=64)
    method_version: str = Field(min_length=1, max_length=32)


class RunLedger(_Strict):
    """A fully-validated, versioned description of one real experiment run."""

    schema_version: str
    experiment: ExperimentIdentity
    provider: Provider
    model: str = Field(min_length=1, max_length=120)
    endpoint: EndpointSource
    auth: AuthSpec
    input: InputSpec
    execution: ExecutionSpec
    output: OutputSpec
    provenance: Provenance

    @field_validator("schema_version")
    @classmethod
    def _known_schema(cls, value: str) -> str:
        if value != LEDGER_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {LEDGER_SCHEMA_VERSION!r}"
            )
        return value

    @field_validator("model")
    @classmethod
    def _safe_model(cls, value: str) -> str:
        try:
            ensure_safe_identifier(value, label="model")
        except PrivacyViolation as exc:
            raise ValueError(str(exc)) from None
        return value

    @model_validator(mode="after")
    def _cross_field(self) -> RunLedger:
        billed = self.execution.cost.billed
        if self.provider == "azure":
            if self.auth.mode != "entra":
                raise ValueError("azure provider requires auth.mode 'entra'")
            if not billed:
                raise ValueError("azure provider must set execution.cost.billed true")
            if self.endpoint.default is not None:
                raise ValueError("azure endpoint must not carry a committed default")
            cost = self.execution.cost
            if any(
                value is None
                for value in (
                    cost.pricing_snapshot_id,
                    cost.pricing_model,
                    cost.input_per_1m_usd,
                    cost.output_per_1m_usd,
                )
            ):
                raise ValueError(
                    "azure provider requires a pinned pricing snapshot ID, "
                    "pricing model, and input/output rates"
                )
            # gpt-5.2 rejects "minimal" at the service; refuse it up front so a
            # billed call never fails after the money is committed.
            if (
                self.execution.reasoning_effort == "minimal"
                and _is_gpt_5_2_deployment(self.model)
            ):
                raise ValueError(
                    "reasoning_effort 'minimal' is not accepted by the gpt-5.2 "
                    "deployment; use one of: none, low, medium, high, xhigh"
                )
        else:
            if self.auth.mode != "none":
                raise ValueError(
                    f"{self.provider} provider requires auth.mode 'none'"
                )
            if billed:
                raise ValueError(
                    f"{self.provider} provider must set execution.cost.billed false"
                )
            cost = self.execution.cost
            if any(
                value is not None
                for value in (
                    cost.pricing_snapshot_id,
                    cost.pricing_model,
                    cost.input_per_1m_usd,
                    cost.output_per_1m_usd,
                )
            ):
                raise ValueError(
                    f"{self.provider} provider must not declare Azure pricing"
                )
        if self.provider != "azure" and self.execution.reasoning_effort is not None:
            raise ValueError(
                "reasoning_effort is only supported by the azure provider"
            )
        if self.execution.max_samples > self.input.max_records:
            raise ValueError(
                "execution.max_samples must not exceed input.max_records"
            )
        # The endpoint env-var is endpoint configuration, never an auth input.
        # Listing it under auth.env_vars would misclassify the endpoint (and
        # risk its resolved value being read as auth metadata downstream).
        if self.endpoint.env_var in self.auth.env_vars:
            raise ValueError(
                "endpoint.env_var must not appear in auth.env_vars: the endpoint "
                "URL is configuration, not a credential input"
            )
        return self

    def canonical_json(self) -> str:
        """Deterministic JSON serialization for hashing and provenance."""
        import json

        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def parse_ledger(data: Any) -> RunLedger:
    """Validate a raw mapping into a :class:`RunLedger`, failing closed.

    Raises:
        LedgerError: With a value-free message on any validation failure.
    """
    if not isinstance(data, dict):
        raise LedgerError("ledger root must be a mapping")
    try:
        return RunLedger.model_validate(data)
    except ValidationError as exc:
        raise LedgerError(_format_validation_error(exc)) from None


def load_ledger(path: Path) -> RunLedger:
    """Read and validate a ledger file (YAML or JSON), failing closed."""
    resolved = path
    try:
        size = resolved.stat().st_size
    except OSError:
        raise LedgerError("ledger file could not be read") from None
    if size > MAX_LEDGER_FILE_BYTES:
        raise LedgerError("ledger file is too large")
    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise LedgerError("ledger file could not be read as UTF-8 text") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise LedgerError("ledger file is not valid YAML/JSON") from None
    return parse_ledger(data)


def _format_validation_error(exc: ValidationError) -> str:
    """Render a pydantic error as a compact, value-free message."""
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        msg = str(err.get("msg", "invalid"))
        # Strip any echoed input value pydantic appends.
        msg = msg.split(", input_value", 1)[0]
        parts.append(f"{loc or '<root>'}: {msg}")
    return "ledger rejected — " + "; ".join(parts[:8])


__all__ = [
    "LEDGER_SCHEMA_VERSION",
    "LedgerError",
    "RunLedger",
    "ExperimentIdentity",
    "EndpointSource",
    "AuthSpec",
    "RowShape",
    "InputSpec",
    "CostSpec",
    "ExecutionSpec",
    "OutputSpec",
    "Provenance",
    "parse_ledger",
    "load_ledger",
]
