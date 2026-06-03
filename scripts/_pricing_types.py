"""Private dataclasses, error types, and schema-validation helpers used by
``scripts.cost_calculator``.

This module is split out per the task-003 guidance ("if the file exceeds ~400
lines, split helper dataclasses into a private ``scripts/_pricing_types.py``").
The public surface — six top-level functions plus all dataclasses/errors —
remains importable from ``scripts.cost_calculator``.

All dataclasses are ``@dataclass(frozen=True)`` and document field types and
units in their docstrings. The §6.1 (PAYG) and §6.2 (PTU) invariants from
``docs/05-methodology.md`` are encoded at the type level: ``Gpt4oRates`` has no
``reasoning_per_1m_usd`` field (gpt-4o has no reasoning column), ``Gpt52Rates``
requires ``reasoning_per_1m_usd`` as a dedicated separate line — never
collapsed into ``output_per_1m_usd``.

This module has no top-level side effects.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Literal


# ----------------------------------------------------------------------------
# Typed errors
# ----------------------------------------------------------------------------


class PaygSchemaError(ValueError):
    """Raised when a PAYG pricing YAML fails schema validation. CLI exit 2."""


class PtuSchemaError(ValueError):
    """Raised when a PTU pricing YAML fails schema validation. CLI exit 2."""


class Gpt4oReasoningError(ValueError):
    """Raised when a gpt-4o cell carries non-zero reasoning tokens — gpt-4o has
    no reasoning column. This is a data-integrity failure, not a billing
    question. CLI exit 3.
    """


class SnapshotNotFoundError(FileNotFoundError):
    """Raised when no pricing snapshot satisfies the requested target date.
    CLI exit 4.
    """


class MissingBaselineError(ValueError):
    """Raised when the requested PTU baseline cell is absent from the
    analysis.json (e.g. ``--baseline migration`` but no gpt-4o cell). CLI exit 3.
    """


class ThroughputGainError(ValueError):
    """Raised when throughput-gain math is undefined (e.g. target tokens == 0).
    Surfaces division-by-zero as a typed error. CLI exit 3.
    """


# ----------------------------------------------------------------------------
# Frozen data models (immutable; field types and units in docstrings)
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Gpt4oRates:
    """PAYG per-token rates for ``gpt-4o`` (USD per 1,000,000 tokens).

    gpt-4o has no reasoning column — this dataclass deliberately has no
    ``reasoning_per_1m_usd`` field. That field, if present in a gpt-4o YAML
    block, is rejected at load time. This is the §6.1 "dedicated separate
    line / never collapsed" invariant enforced at the type level.

    Attributes:
        input_per_1m_usd: Standard input rate (USD per 1M tokens).
        cached_input_per_1m_usd: Discounted cached-input rate.
        output_per_1m_usd: Standard output rate.
    """

    input_per_1m_usd: float
    cached_input_per_1m_usd: float
    output_per_1m_usd: float


@dataclass(frozen=True)
class Gpt52Rates:
    """PAYG per-token rates for ``gpt-5.2`` (USD per 1,000,000 tokens).

    ``reasoning_per_1m_usd`` is mandatory and is a dedicated separate line —
    never collapsed into ``output_per_1m_usd``. Even when the two are
    numerically equal (as in the 2026-05 snapshot), they are stored
    independently so a future divergence is captured automatically.

    Attributes:
        input_per_1m_usd: Standard input rate (USD per 1M tokens).
        cached_input_per_1m_usd: Discounted cached-input rate.
        reasoning_per_1m_usd: Reasoning-token rate (separate billed category).
        output_per_1m_usd: Standard output rate.
    """

    input_per_1m_usd: float
    cached_input_per_1m_usd: float
    reasoning_per_1m_usd: float
    output_per_1m_usd: float


@dataclass(frozen=True)
class PaygPricing:
    """Parsed contents of a ``pricing/azure-openai-payg-{YYYY-MM}.yaml`` file.

    Attributes:
        source_url: HTTPS URL of the pricing page. Cited in every output header.
        accessed_date: ``YYYY-MM-DD`` string (normalized from YAML date if needed).
        archive_url: Optional ``https://web.archive.org/...`` snapshot URL.
        currency: Must be ``"USD"``.
        models: Mapping of deployment name to model-specific rate dataclass.
        snapshot_path: Filesystem path of the loaded YAML (citation).
    """

    source_url: str
    accessed_date: str
    archive_url: str | None
    currency: str
    models: dict[str, Gpt4oRates | Gpt52Rates]
    snapshot_path: str


@dataclass(frozen=True)
class PtuModelRates:
    """PTU rate + capacity fields for a single model.

    Attributes:
        ptu_hourly_rate_usd: USD per PTU per hour.
        min_ptu: Minimum PTU per regional provisioned deployment.
        max_ptu_per_deployment: Service-wide cap on PTU per deployment.
        baseline_throughput_tpm_per_ptu: Input TPM per PTU (Azure published).
    """

    ptu_hourly_rate_usd: float
    min_ptu: int
    max_ptu_per_deployment: int
    baseline_throughput_tpm_per_ptu: int


@dataclass(frozen=True)
class PtuPricing:
    """Parsed contents of a ``pricing/azure-openai-ptu-{YYYY-MM}.yaml`` file.

    Attributes:
        source_url: HTTPS URL of the provisioned-throughput onboarding doc.
        accessed_date: ``YYYY-MM-DD`` string.
        archive_url: Optional web-archive URL.
        currency: Must be ``"USD"``.
        region: Azure region (e.g. ``"eastus2"``).
        models: Mapping of deployment name to ``PtuModelRates``.
        snapshot_path: Filesystem path of the loaded YAML (citation).
    """

    source_url: str
    accessed_date: str
    archive_url: str | None
    currency: str
    region: str
    models: dict[str, PtuModelRates]
    snapshot_path: str


@dataclass(frozen=True)
class TokenUsage:
    """Token counts for a single LLM call (units: tokens).

    Mirrors the billable categories of §6.1 as they appear in Azure's
    Responses API ``usage`` object. Under the current GPT-5.x contract
    ``reasoning_tokens`` is a labelled subset of ``output_tokens``
    (Azure reports ``total_tokens == input_tokens + output_tokens`` and
    surfaces the reasoning portion under ``output_tokens_details``);
    therefore ``reasoning_tokens`` does not enter the §6.1 cost formula
    additively and is informational for downstream analysis.
    ``cached_tokens`` is the portion of ``input_tokens`` that hit the
    prompt cache.

    Attributes:
        input_tokens: Total input tokens (includes cached subset).
        cached_tokens: Cached portion of input.
        output_tokens: Azure's full ``output_tokens`` field — includes the
            ``reasoning_tokens`` subset for gpt-5.x. Visible output =
            ``output_tokens - reasoning_tokens``.
        reasoning_tokens: Labelled subset of ``output_tokens`` representing
            the reasoning portion of the completion (0 for gpt-4o, since
            gpt-4o has no reasoning column).
    """

    input_tokens: float
    cached_tokens: float
    output_tokens: float
    reasoning_tokens: float


@dataclass(frozen=True)
class CostBreakdown:
    """Per-cell PAYG cost output. Citation fields are always populated —
    ``pricing_source_url`` alone without ``pricing_snapshot_path`` is a defect
    per §6.

    Attributes:
        model: Deployment name (``gpt-4o`` or ``gpt-5.2``).
        effort: Reasoning effort label or ``None`` (gpt-4o is always ``None``).
        input_tokens: Mean input tokens for the cell.
        cached_tokens: Mean cached tokens.
        output_tokens: Mean visible output tokens.
        reasoning_tokens: Mean reasoning tokens (0.0 for gpt-4o).
        usd_per_request: Mean USD per request.
        pricing_snapshot_path: Filesystem path of the PAYG YAML loaded.
        pricing_source_url: ``source_url`` from the YAML.
        pricing_accessed_date: ``accessed_date`` (``YYYY-MM-DD``).
        pricing_archive_url: Optional web-archive URL.
    """

    model: str
    effort: str | None
    input_tokens: float
    cached_tokens: float
    output_tokens: float
    reasoning_tokens: float
    usd_per_request: float
    pricing_snapshot_path: str
    pricing_source_url: str
    pricing_accessed_date: str
    pricing_archive_url: str | None


@dataclass(frozen=True)
class ThroughputGain:
    """Per-cell PTU throughput-gain output. ``baseline_label`` is mandatory —
    a gain factor without a baseline label is not citable per §6.2.

    Attributes:
        model: Deployment name of the target cell.
        effort: Reasoning effort label or ``None``.
        tokens_per_request: Target cell tokens-per-request.
        throughput_gain_factor: Ratio ``baseline / target`` (dimensionless).
        baseline_label: Human-readable baseline identifier.
    """

    model: str
    effort: str | None
    tokens_per_request: float
    throughput_gain_factor: float
    baseline_label: str


@dataclass(frozen=True)
class CellMeasurement:
    """One aggregated cell from a benchmark's ``analysis.json``.

    Attributes:
        model: Deployment name.
        effort: Effort label or ``None`` (gpt-4o is always ``None``).
        sample_count: Number of underlying repeats.
        input_tokens_mean: Mean input tokens across repeats.
        cached_tokens_mean: Mean cached tokens.
        output_tokens_mean: Mean output tokens.
        reasoning_tokens_mean: Mean reasoning tokens (must be 0.0 for gpt-4o).
    """

    model: str
    effort: str | None
    sample_count: int
    input_tokens_mean: float
    cached_tokens_mean: float
    output_tokens_mean: float
    reasoning_tokens_mean: float


@dataclass(frozen=True)
class BaselineSpec:
    """PTU baseline selection.

    ``kind`` is one of:
      - ``"migration"``   → resolved against gpt-4o cell in analysis.json
      - ``"effort-high"`` → resolved against gpt-5.2 effort=high cell
      - ``"custom"``      → ``tokens_per_request`` supplied directly

    Attributes:
        kind: Baseline kind enum.
        label: Human-readable string propagated to the report header.
        tokens_per_request: Required for ``custom``; ignored otherwise.
    """

    kind: Literal["migration", "effort-high", "custom"]
    label: str
    tokens_per_request: float | None = None


@dataclass(frozen=True)
class CostReport:
    """Combined PAYG + PTU report (either lens may be ``None`` when the CLI
    runs only one subcommand).

    Attributes:
        payg_breakdowns: PAYG cells (``None`` if PAYG lens skipped).
        ptu_gains: PTU cells (``None`` if PTU lens skipped).
        payg: PAYG snapshot used.
        ptu: PTU snapshot used.
        ptu_baseline: PTU baseline spec (``None`` if PTU lens skipped).
        ptu_baseline_tokens: Resolved baseline tokens-per-request.
    """

    payg_breakdowns: tuple[CostBreakdown, ...] | None
    ptu_gains: tuple[ThroughputGain, ...] | None
    payg: PaygPricing | None
    ptu: PtuPricing | None
    ptu_baseline: BaselineSpec | None
    ptu_baseline_tokens: float | None
    extra_metadata: dict[str, str] = field(default_factory=dict)


# ----------------------------------------------------------------------------
# Schema-validation primitives (shared by both loaders)
# ----------------------------------------------------------------------------


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

PAYG_TOP_KEYS_REQUIRED = frozenset({"source_url", "accessed_date", "currency", "models"})
PAYG_TOP_KEYS_OPTIONAL = frozenset({"archive_url"})
PAYG_TOP_KEYS_ALLOWED = PAYG_TOP_KEYS_REQUIRED | PAYG_TOP_KEYS_OPTIONAL

# Canonical read-order for PAYG top-level keys (spec: §003 Control). The loader
# enforces this order — same keys in the wrong order is a schema defect, not a
# stylistic choice. ``archive_url`` is optional; when absent it is simply
# skipped in the ordering check.
PAYG_TOP_KEYS_ORDER: tuple[str, ...] = (
    "source_url",
    "accessed_date",
    "archive_url",
    "currency",
    "models",
)

PTU_TOP_KEYS_REQUIRED = frozenset(
    {"source_url", "accessed_date", "currency", "region", "models"}
)
PTU_TOP_KEYS_OPTIONAL = frozenset({"archive_url"})
PTU_TOP_KEYS_ALLOWED = PTU_TOP_KEYS_REQUIRED | PTU_TOP_KEYS_OPTIONAL

# Canonical read-order for PTU top-level keys (spec: §003 Control). Same rule
# as PAYG_TOP_KEYS_ORDER — keys-in-wrong-order is a schema defect.
PTU_TOP_KEYS_ORDER: tuple[str, ...] = (
    "source_url",
    "accessed_date",
    "archive_url",
    "currency",
    "region",
    "models",
)

GPT4O_RATE_KEYS = frozenset({"input_per_1m_usd", "cached_input_per_1m_usd", "output_per_1m_usd"})
GPT52_RATE_KEYS = frozenset(
    {
        "input_per_1m_usd",
        "cached_input_per_1m_usd",
        "reasoning_per_1m_usd",
        "output_per_1m_usd",
    }
)

PTU_RATE_KEYS = frozenset(
    {
        "ptu_hourly_rate_usd",
        "min_ptu",
        "max_ptu_per_deployment",
        "baseline_throughput_tpm_per_ptu",
    }
)


def normalize_accessed_date(raw: object, error_cls: type[ValueError]) -> str:
    """Normalize an ``accessed_date`` YAML value to a ``YYYY-MM-DD`` string.

    The real Task 002 snapshot writes the date unquoted, so PyYAML returns a
    ``datetime.date`` object — not a string. This function accepts either
    representation and normalizes to a canonical string. This is type-safety
    normalization, not value coercion: a ``date(2026, 5, 19)`` becomes
    ``"2026-05-19"`` exactly, with no rounding, no timezone shift, no
    reordering. ``datetime.datetime`` is rejected (date-only required).
    """
    if isinstance(raw, datetime.datetime):
        raise error_cls(
            f"accessed_date must be date-only (YYYY-MM-DD); got datetime: {raw!r}"
        )
    if isinstance(raw, datetime.date):
        return raw.isoformat()
    if isinstance(raw, str):
        if not DATE_RE.match(raw):
            raise error_cls(
                f"accessed_date must match YYYY-MM-DD; got {raw!r}"
            )
        return raw
    raise error_cls(
        f"accessed_date must be str or datetime.date; got {type(raw).__name__}: {raw!r}"
    )


def validate_positive_rate(
    value: object, field_name: str, error_cls: type[ValueError]
) -> float:
    """Assert a YAML rate value is a strictly positive int/float (not bool)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise error_cls(
            f"{field_name} must be a positive number; got {type(value).__name__}: {value!r}"
        )
    if value <= 0:
        raise error_cls(f"{field_name} must be > 0; got {value!r}")
    return float(value)


def validate_positive_int(
    value: object, field_name: str, error_cls: type[ValueError]
) -> int:
    """Assert a YAML integer is strictly positive and not a bool."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise error_cls(
            f"{field_name} must be a positive integer; got {type(value).__name__}: {value!r}"
        )
    if value <= 0:
        raise error_cls(f"{field_name} must be > 0; got {value!r}")
    return value


def validate_https_url(
    value: object, field_name: str, error_cls: type[ValueError]
) -> str:
    if not isinstance(value, str) or not value.startswith("https://"):
        raise error_cls(
            f"{field_name} must be an https:// URL; got {value!r}"
        )
    return value
