"""cost_calculator.py — single offline source of every USD-per-request and PTU
throughput-gain figure cited in the when-reasoning-pays-off repo.

Four invariants this module enforces (do not delete; grep-targets for the §6
contract in docs/05-methodology.md):

  (a) PAYG formula verbatim from docs/05-methodology.md §6.1, reflecting
      Azure's actual Responses-API usage contract for GPT-5.x (``reasoning_tokens``
      is a labelled subset of ``output_tokens`` — i.e. Azure reports
      ``total_tokens == input_tokens + output_tokens`` and surfaces the
      reasoning portion under ``output_tokens_details.reasoning_tokens`` for
      transparency):

        non_cached_input = input_tokens - cached_tokens
        cost = (non_cached_input * input_per_1m_usd
                + cached_tokens   * cached_input_per_1m_usd
                + output_tokens   * output_per_1m_usd) / 1_000_000

      ``output_tokens`` already includes the reasoning portion, so reasoning
      tokens are NOT a separately-billed line under today's contract — they
      are billed at the output rate via ``output_tokens``. The schema retains
      ``reasoning_per_1m_usd`` as a separate dedicated rate (invariant c) so
      that the day Azure splits the meter, the YAML's published rates will
      diverge and a code change can re-introduce the additive term without
      a silent schema migration.

  (b) PTU formula verbatim from docs/05-methodology.md §6.2:

        throughput_gain_factor = baseline.tokens_per_request / target.tokens_per_request

      Tokens-per-request is ``input_tokens + output_tokens`` (Azure's
      ``total_tokens``). Reasoning is NOT added — it is already inside
      output (invariant a).

  (c) ``reasoning_per_1m_usd`` is a dedicated separate schema line — never
      collapsed into ``output_per_1m_usd`` at load time. ``gpt-4o`` has no
      reasoning column; non-zero reasoning tokens on gpt-4o is a
      data-integrity failure (``Gpt4oReasoningError``), NOT a billing
      question. Today's published rate for gpt-5.2
      (``reasoning_per_1m_usd == output_per_1m_usd``) is documentary —
      retained so a future Azure meter split is captured automatically in
      the YAML diff and surfaces as a schema discrepancy rather than a
      silent cost shift.

  (d) Every cost-report header carries the pricing snapshot citation:
      ``pricing_snapshot_path`` + ``pricing_source_url`` +
      ``pricing_accessed_date``. URL alone is not acceptable. PTU figures also
      carry a ``baseline_label`` — a throughput-gain factor without a baseline
      label is not citable per §6.2.

Default snapshot resolution does NOT call ``datetime.date.today()`` or
``datetime.datetime.now()``: when no ``--snapshot-date`` is supplied the resolver
deterministically picks the file with the largest in-file ``accessed_date`` (so
CI runs are reproducible across calendar days). Filename month is NOT
authoritative — only the in-file ``accessed_date`` is.

No top-level side effects: no ``print()``, no ``logging.basicConfig()``, no file
open, no ``os.environ`` read at import time. All I/O lives inside functions
called from ``main()``.

Helper dataclasses and primitives live in the private companion module
``scripts/_pricing_types.py`` (per the task 003 size-budget guidance). The
six public functions enumerated below remain importable from this module.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import pathlib
import sys
from typing import Literal

import yaml

from scripts._pricing_types import (
    DATE_RE,
    GPT4O_RATE_KEYS,
    GPT52_RATE_KEYS,
    PAYG_TOP_KEYS_ALLOWED,
    PAYG_TOP_KEYS_ORDER,
    PAYG_TOP_KEYS_REQUIRED,
    PTU_RATE_KEYS,
    PTU_TOP_KEYS_ALLOWED,
    PTU_TOP_KEYS_ORDER,
    PTU_TOP_KEYS_REQUIRED,
    BaselineSpec,
    CellMeasurement,
    CostBreakdown,
    CostReport,
    Gpt4oRates,
    Gpt4oReasoningError,
    Gpt52Rates,
    MissingBaselineError,
    PaygPricing,
    PaygSchemaError,
    PtuModelRates,
    PtuPricing,
    PtuSchemaError,
    SnapshotNotFoundError,
    ThroughputGain,
    ThroughputGainError,
    TokenUsage,
    normalize_accessed_date,
    validate_https_url,
    validate_positive_int,
    validate_positive_rate,
)

__all__ = [
    "BaselineSpec",
    "CellMeasurement",
    "CostBreakdown",
    "CostReport",
    "Gpt4oRates",
    "Gpt4oReasoningError",
    "Gpt52Rates",
    "MissingBaselineError",
    "PaygPricing",
    "PaygSchemaError",
    "PtuModelRates",
    "PtuPricing",
    "PtuSchemaError",
    "SnapshotNotFoundError",
    "ThroughputGain",
    "ThroughputGainError",
    "TokenUsage",
    "load_payg_pricing",
    "load_ptu_pricing",
    "payg_cost_per_call",
    "ptu_throughput_gain",
    "render_cost_report",
    "resolve_active_snapshot",
    "main",
]

logger = logging.getLogger("scripts.cost_calculator")


# ----------------------------------------------------------------------------
# YAML loaders
# ----------------------------------------------------------------------------


def load_payg_pricing(path: str | pathlib.Path) -> PaygPricing:
    """Load and validate a PAYG pricing YAML.

    Args:
        path: Filesystem path to the YAML snapshot.

    Returns:
        Immutable ``PaygPricing`` with all rates as ``float`` and citation
        fields populated. ``models`` is a dict of deployment-name to a
        model-specific rate dataclass — ``gpt-4o`` maps to ``Gpt4oRates``
        (three fields, NO reasoning), ``gpt-5.2`` to ``Gpt52Rates`` (four
        fields including the dedicated separate line ``reasoning_per_1m_usd``).

    Raises:
        PaygSchemaError: On any schema mismatch — extra keys, missing keys,
            wrong types, non-USD currency, non-https source_url, non-positive
            rates, accidental ``reasoning_per_1m_usd`` on the gpt-4o block,
            or a missing ``reasoning_per_1m_usd`` on gpt-5.2 (the never
            collapsed invariant — the field MUST be explicit, never inferred
            from ``output_per_1m_usd``).
        FileNotFoundError: If ``path`` does not exist.
    """
    p = pathlib.Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"PAYG pricing file not found: {p}")

    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        raise PaygSchemaError(
            f"PAYG YAML root must be a mapping; got {type(data).__name__}"
        )

    actual_keys = set(data.keys())
    extra = actual_keys - PAYG_TOP_KEYS_ALLOWED
    missing = PAYG_TOP_KEYS_REQUIRED - actual_keys
    if extra or missing:
        raise PaygSchemaError(
            f"PAYG top-level keys mismatch in {p}: "
            f"extra={sorted(extra)}, missing={sorted(missing)}; "
            f"allowed={sorted(PAYG_TOP_KEYS_ALLOWED)}"
        )

    # Read-order check: the spec (.internal/tasks/003-cost-calculator.md
    # Control "PAYG YAML top-level keys (read order checked, not just parse)")
    # requires the present keys to appear in canonical order. ``archive_url``
    # is optional and is filtered out of the expected order when absent.
    actual_order = list(data.keys())
    expected_order = [k for k in PAYG_TOP_KEYS_ORDER if k in actual_keys]
    if actual_order != expected_order:
        raise PaygSchemaError(
            f"PAYG top-level keys present in wrong order in {p}: "
            f"got {actual_order}; expected {expected_order} "
            f"(canonical order: {list(PAYG_TOP_KEYS_ORDER)})"
        )

    source_url = validate_https_url(data["source_url"], "source_url", PaygSchemaError)

    accessed_date = normalize_accessed_date(data["accessed_date"], PaygSchemaError)

    archive_url_raw = data.get("archive_url")
    if archive_url_raw is None:
        archive_url: str | None = None
    else:
        archive_url = validate_https_url(archive_url_raw, "archive_url", PaygSchemaError)

    currency = data["currency"]
    if currency != "USD":
        raise PaygSchemaError(f"currency must be 'USD'; got {currency!r}")

    models_raw = data["models"]
    if not isinstance(models_raw, dict):
        raise PaygSchemaError(
            f"models must be a mapping; got {type(models_raw).__name__}"
        )

    models: dict[str, Gpt4oRates | Gpt52Rates] = {}
    for model_name, block in models_raw.items():
        if not isinstance(block, dict):
            raise PaygSchemaError(
                f"models[{model_name!r}] must be a mapping; got {type(block).__name__}"
            )
        block_keys = set(block.keys())
        if model_name == "gpt-4o":
            if block_keys != GPT4O_RATE_KEYS:
                raise PaygSchemaError(
                    f"gpt-4o key set must be exactly {sorted(GPT4O_RATE_KEYS)}; "
                    f"got {sorted(block_keys)} in {p}. "
                    f"(reasoning_per_1m_usd is a dedicated separate line for gpt-5.2 "
                    f"and MUST NOT appear on gpt-4o — gpt-4o has no reasoning column.)"
                )
            models[model_name] = Gpt4oRates(
                input_per_1m_usd=validate_positive_rate(
                    block["input_per_1m_usd"], "gpt-4o.input_per_1m_usd", PaygSchemaError
                ),
                cached_input_per_1m_usd=validate_positive_rate(
                    block["cached_input_per_1m_usd"],
                    "gpt-4o.cached_input_per_1m_usd",
                    PaygSchemaError,
                ),
                output_per_1m_usd=validate_positive_rate(
                    block["output_per_1m_usd"], "gpt-4o.output_per_1m_usd", PaygSchemaError
                ),
            )
        elif model_name == "gpt-5.2":
            if block_keys != GPT52_RATE_KEYS:
                raise PaygSchemaError(
                    f"gpt-5.2 key set must be exactly {sorted(GPT52_RATE_KEYS)}; "
                    f"got {sorted(block_keys)} in {p}. "
                    f"(reasoning_per_1m_usd is required and never collapsed into "
                    f"output_per_1m_usd.)"
                )
            models[model_name] = Gpt52Rates(
                input_per_1m_usd=validate_positive_rate(
                    block["input_per_1m_usd"], "gpt-5.2.input_per_1m_usd", PaygSchemaError
                ),
                cached_input_per_1m_usd=validate_positive_rate(
                    block["cached_input_per_1m_usd"],
                    "gpt-5.2.cached_input_per_1m_usd",
                    PaygSchemaError,
                ),
                reasoning_per_1m_usd=validate_positive_rate(
                    block["reasoning_per_1m_usd"],
                    "gpt-5.2.reasoning_per_1m_usd",
                    PaygSchemaError,
                ),
                output_per_1m_usd=validate_positive_rate(
                    block["output_per_1m_usd"], "gpt-5.2.output_per_1m_usd", PaygSchemaError
                ),
            )
        else:
            raise PaygSchemaError(
                f"Unknown PAYG model {model_name!r}; expected one of {{'gpt-4o', 'gpt-5.2'}}"
            )

    return PaygPricing(
        source_url=source_url,
        accessed_date=accessed_date,
        archive_url=archive_url,
        currency=currency,
        models=models,
        snapshot_path=str(p),
    )


def load_ptu_pricing(path: str | pathlib.Path) -> PtuPricing:
    """Load and validate a PTU pricing YAML.

    Raises:
        PtuSchemaError: On schema mismatch (extra/missing keys, non-USD
            currency, missing region, non-positive numerics).
        FileNotFoundError: If ``path`` does not exist.
    """
    p = pathlib.Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"PTU pricing file not found: {p}")

    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        raise PtuSchemaError(
            f"PTU YAML root must be a mapping; got {type(data).__name__}"
        )

    actual_keys = set(data.keys())
    extra = actual_keys - PTU_TOP_KEYS_ALLOWED
    missing = PTU_TOP_KEYS_REQUIRED - actual_keys
    if extra or missing:
        raise PtuSchemaError(
            f"PTU top-level keys mismatch in {p}: "
            f"extra={sorted(extra)}, missing={sorted(missing)}; "
            f"allowed={sorted(PTU_TOP_KEYS_ALLOWED)}"
        )

    # Read-order check: the spec (.internal/tasks/003-cost-calculator.md
    # Control "PTU YAML top-level keys") requires canonical key order.
    # ``archive_url`` is optional and skipped when absent.
    actual_order = list(data.keys())
    expected_order = [k for k in PTU_TOP_KEYS_ORDER if k in actual_keys]
    if actual_order != expected_order:
        raise PtuSchemaError(
            f"PTU top-level keys present in wrong order in {p}: "
            f"got {actual_order}; expected {expected_order} "
            f"(canonical order: {list(PTU_TOP_KEYS_ORDER)})"
        )

    source_url = validate_https_url(data["source_url"], "source_url", PtuSchemaError)

    accessed_date = normalize_accessed_date(data["accessed_date"], PtuSchemaError)

    archive_url_raw = data.get("archive_url")
    if archive_url_raw is None:
        archive_url: str | None = None
    else:
        archive_url = validate_https_url(archive_url_raw, "archive_url", PtuSchemaError)

    currency = data["currency"]
    if currency != "USD":
        raise PtuSchemaError(f"currency must be 'USD'; got {currency!r}")

    region = data["region"]
    if not isinstance(region, str) or not region.strip():
        raise PtuSchemaError(f"region must be a non-empty string; got {region!r}")

    models_raw = data["models"]
    if not isinstance(models_raw, dict):
        raise PtuSchemaError(
            f"models must be a mapping; got {type(models_raw).__name__}"
        )

    models: dict[str, PtuModelRates] = {}
    for model_name, block in models_raw.items():
        if not isinstance(block, dict):
            raise PtuSchemaError(
                f"models[{model_name!r}] must be a mapping; got {type(block).__name__}"
            )
        block_keys = set(block.keys())
        if block_keys != PTU_RATE_KEYS:
            raise PtuSchemaError(
                f"PTU model {model_name!r} key set must be exactly "
                f"{sorted(PTU_RATE_KEYS)}; got {sorted(block_keys)} in {p}"
            )
        models[model_name] = PtuModelRates(
            ptu_hourly_rate_usd=validate_positive_rate(
                block["ptu_hourly_rate_usd"],
                f"{model_name}.ptu_hourly_rate_usd",
                PtuSchemaError,
            ),
            min_ptu=validate_positive_int(
                block["min_ptu"], f"{model_name}.min_ptu", PtuSchemaError
            ),
            max_ptu_per_deployment=validate_positive_int(
                block["max_ptu_per_deployment"],
                f"{model_name}.max_ptu_per_deployment",
                PtuSchemaError,
            ),
            baseline_throughput_tpm_per_ptu=validate_positive_int(
                block["baseline_throughput_tpm_per_ptu"],
                f"{model_name}.baseline_throughput_tpm_per_ptu",
                PtuSchemaError,
            ),
        )

    return PtuPricing(
        source_url=source_url,
        accessed_date=accessed_date,
        archive_url=archive_url,
        currency=currency,
        region=region,
        models=models,
        snapshot_path=str(p),
    )


# ----------------------------------------------------------------------------
# Pure compute functions
# ----------------------------------------------------------------------------


def payg_cost_per_call(
    usage: TokenUsage, pricing: PaygPricing, model: str
) -> CostBreakdown:
    """Compute USD per request for one cell, using the §6.1 PAYG formula.

    docs/05-methodology.md §6.1 formula — verbatim, reflecting Azure's
    Responses-API contract where ``reasoning_tokens`` is a labelled subset
    of ``output_tokens`` (Azure reports
    ``total_tokens == input_tokens + output_tokens``):

        non_cached_input = input_tokens - cached_tokens
        cost = (non_cached_input * input_per_1m_usd
                + cached_tokens   * cached_input_per_1m_usd
                + output_tokens   * output_per_1m_usd) / 1_000_000

    ``output_tokens`` already includes the reasoning portion, so reasoning
    tokens are NOT added as a separately-billed line — they are billed at
    the output rate by virtue of being inside ``output_tokens``. The
    ``reasoning_per_1m_usd`` schema field is retained as a dedicated line
    (per docs/05-methodology.md §6.1 "never collapsed" invariant) so that
    the day Azure splits the meter, the YAML diff captures the divergence
    automatically. ``gpt-4o`` has no reasoning column; calling with
    ``reasoning_tokens > 0`` on ``gpt-4o`` raises ``Gpt4oReasoningError``.

    Args:
        usage: Per-cell mean token counts. ``output_tokens`` is the full
            Azure ``output_tokens`` field (includes the reasoning subset);
            ``reasoning_tokens`` is the labelled subset and is informational
            for this calculation (does not enter the cost formula under
            today's Azure contract).
        pricing: Loaded PAYG snapshot.
        model: Deployment name (``gpt-4o`` or ``gpt-5.2``).

    Returns:
        ``CostBreakdown`` with citation fields populated from ``pricing``.

    Raises:
        Gpt4oReasoningError: ``model == 'gpt-4o'`` and
            ``usage.reasoning_tokens > 0``.
        KeyError: ``model`` not present in ``pricing.models``.
    """
    rates = pricing.models[model]

    if model == "gpt-4o":
        # gpt-4o has no reasoning column — non-zero reasoning is a data
        # integrity failure, NOT a billing question. Never collapsed.
        if usage.reasoning_tokens > 0:
            raise Gpt4oReasoningError(
                f"gpt-4o cannot produce reasoning tokens; got "
                f"reasoning_tokens={usage.reasoning_tokens}. This is a data "
                f"integrity failure (gpt-4o has no reasoning column in the "
                f"pricing schema by the dedicated separate line invariant)."
            )
        assert isinstance(rates, Gpt4oRates)
        non_cached_input = usage.input_tokens - usage.cached_tokens
        cost = (
            non_cached_input * rates.input_per_1m_usd
            + usage.cached_tokens * rates.cached_input_per_1m_usd
            + usage.output_tokens * rates.output_per_1m_usd
        ) / 1_000_000
    elif model == "gpt-5.2":
        assert isinstance(rates, Gpt52Rates)
        # Azure GPT-5.x contract: reasoning_tokens is a labelled subset of
        # output_tokens (total_tokens == input + output). Billing follows
        # the schema: output_tokens billed at output_per_1m_usd already
        # covers reasoning. reasoning_per_1m_usd is retained on the schema
        # for divergence-detection (invariant c) — multiplying it here
        # would double-count reasoning.
        non_cached_input = usage.input_tokens - usage.cached_tokens
        cost = (
            non_cached_input * rates.input_per_1m_usd
            + usage.cached_tokens * rates.cached_input_per_1m_usd
            + usage.output_tokens * rates.output_per_1m_usd
        ) / 1_000_000
        # Touch the field to keep the schema-load invariant intentional:
        # if a refactor ever drops reasoning_per_1m_usd from Gpt52Rates this
        # line surfaces the regression at compute time, not silently.
        _ = rates.reasoning_per_1m_usd
    else:
        raise KeyError(
            f"Unknown PAYG model {model!r}; expected 'gpt-4o' or 'gpt-5.2'"
        )

    return CostBreakdown(
        model=model,
        effort=None,  # filled in by render layer (CostBreakdown is per-call)
        input_tokens=usage.input_tokens,
        cached_tokens=usage.cached_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        usd_per_request=cost,
        pricing_snapshot_path=pricing.snapshot_path,
        pricing_source_url=pricing.source_url,
        pricing_accessed_date=pricing.accessed_date,
        pricing_archive_url=pricing.archive_url,
    )


def ptu_throughput_gain(
    target_tokens: float,
    baseline_tokens: float,
    *,
    baseline_label: str,
) -> ThroughputGain:
    """Compute throughput-gain factor per the §6.2 PTU lens.

    docs/05-methodology.md §6.2 formula — verbatim:

        throughput_gain_factor = baseline.tokens_per_request / target.tokens_per_request

    ``baseline_label`` is mandatory and propagated to the report header. A
    ``ThroughputGain`` without a baseline label is not citable per §6.2 —
    the factor is meaningless without it.

    Raises:
        ThroughputGainError: ``target_tokens`` is zero or negative — surfaces
            division-by-zero as a typed error.
        ValueError: ``baseline_label`` is empty or not a string.
    """
    if not isinstance(baseline_label, str) or not baseline_label.strip():
        raise ValueError(
            "baseline_label must be a non-empty string (a ThroughputGain "
            "without a baseline label is not citable per §6.2)."
        )
    if target_tokens <= 0:
        raise ThroughputGainError(
            f"target_tokens must be > 0 to compute throughput gain; got {target_tokens}"
        )
    if baseline_tokens < 0:
        raise ThroughputGainError(
            f"baseline_tokens must be >= 0; got {baseline_tokens}"
        )
    factor = baseline_tokens / target_tokens
    return ThroughputGain(
        model="",
        effort=None,
        tokens_per_request=target_tokens,
        throughput_gain_factor=factor,
        baseline_label=baseline_label,
    )


# ----------------------------------------------------------------------------
# Snapshot resolution
# ----------------------------------------------------------------------------


_SNAPSHOT_GLOB = "azure-openai-{kind}-*.yaml"


def resolve_active_snapshot(
    kind: Literal["payg", "ptu"],
    target_date: datetime.date | None,
    pricing_dir: str | pathlib.Path = "pricing",
) -> pathlib.Path:
    """Pick the active pricing snapshot file for a given ``kind``.

    Resolution rule (canonical; identical to the Implementation Notes in
    .internal/tasks/003-cost-calculator.md):

    1. Enumerate every ``pricing_dir / 'azure-openai-{kind}-*.yaml'``.
    2. Load each one (full schema validation) and read its in-file
       ``accessed_date`` — normalized to ``YYYY-MM-DD``. Parse to
       ``datetime.date`` for comparison.
    3. Filename month is NOT authoritative — we never use ``YYYY-MM-as-day-1``
       inferred from the filename. (Rationale: a snapshot accessed mid-month
       must not be confused with one accessed day-1 of the same month.)
    4. If ``target_date is None``: return the file with the largest
       ``accessed_date`` overall (tie-break: alphabetical by filename). This
       is the "default = newest, deterministic" rule — we do NOT call
       ``datetime.date.today()`` here.
    5. If ``target_date`` is a ``datetime.date``: filter to files where
       ``accessed_date <= target_date``, return the largest (tie-break:
       alphabetical).

    Raises:
        SnapshotNotFoundError: No candidate qualified.
    """
    pdir = pathlib.Path(pricing_dir)
    if not pdir.is_dir():
        raise SnapshotNotFoundError(
            f"pricing_dir does not exist or is not a directory: {pdir}"
        )

    pattern = _SNAPSHOT_GLOB.format(kind=kind)
    candidates_paths = sorted(pdir.glob(pattern))
    if not candidates_paths:
        raise SnapshotNotFoundError(
            f"No snapshot files matching {pattern!r} in {pdir}"
        )

    loader = load_payg_pricing if kind == "payg" else load_ptu_pricing
    candidates: list[tuple[datetime.date, pathlib.Path]] = []
    for p in candidates_paths:
        snap = loader(p)
        try:
            d = datetime.date.fromisoformat(snap.accessed_date)
        except ValueError as exc:  # defensive; load would have caught
            raise SnapshotNotFoundError(
                f"Snapshot {p} has unparseable accessed_date {snap.accessed_date!r}: {exc}"
            ) from exc
        candidates.append((d, p))

    if target_date is not None:
        candidates = [(d, p) for d, p in candidates if d <= target_date]
        if not candidates:
            raise SnapshotNotFoundError(
                f"No snapshot with accessed_date <= {target_date.isoformat()} "
                f"in {pdir} for kind={kind!r}. (Filename month is not used for "
                f"resolution — only the in-file accessed_date.)"
            )

    candidates.sort(key=lambda item: (item[0], item[1].name))
    max_date = candidates[-1][0]
    same_date = sorted(p for d, p in candidates if d == max_date)
    return same_date[0]


# ----------------------------------------------------------------------------
# Baseline resolution + rendering
# ----------------------------------------------------------------------------


def _cell_tokens(cell: CellMeasurement) -> float:
    """Sum of token counts representing tokens-per-request for a cell.

    Convention: ``tokens_per_request = input_tokens + output_tokens``
    (Azure's ``total_tokens`` for the Responses API on GPT-5.x).
    ``output_tokens`` already includes the reasoning subset under Azure's
    current usage contract — adding ``reasoning_tokens`` would double-count
    them. ``cached_tokens`` is a subset of ``input_tokens`` (already counted).
    This matches the §6.2 interpretation of tokens-per-request as the
    workload signature and is consistent with the §6.1 cost formula's
    treatment of ``output_tokens`` as the superset.
    """
    return cell.input_tokens_mean + cell.output_tokens_mean


def _resolve_baseline_tokens(
    cells: list[CellMeasurement], spec: BaselineSpec
) -> float:
    """Resolve baseline tokens-per-request from a ``BaselineSpec``."""
    if spec.kind == "custom":
        if spec.tokens_per_request is None or spec.tokens_per_request <= 0:
            raise MissingBaselineError(
                f"custom baseline requires tokens_per_request > 0; got "
                f"{spec.tokens_per_request!r}"
            )
        return float(spec.tokens_per_request)
    if spec.kind == "migration":
        match = [c for c in cells if c.model == "gpt-4o"]
        if not match:
            raise MissingBaselineError(
                "baseline 'migration' requires a gpt-4o cell in analysis.json; "
                "none found."
            )
        if len(match) > 1:
            raise MissingBaselineError(
                f"baseline 'migration' expected exactly 1 gpt-4o cell; got "
                f"{len(match)}."
            )
        return _cell_tokens(match[0])
    if spec.kind == "effort-high":
        match = [c for c in cells if c.model == "gpt-5.2" and c.effort == "high"]
        if not match:
            raise MissingBaselineError(
                "baseline 'effort-high' requires a gpt-5.2 cell with effort=high "
                "in analysis.json; none found."
            )
        if len(match) > 1:
            raise MissingBaselineError(
                f"baseline 'effort-high' expected exactly 1 gpt-5.2 effort=high "
                f"cell; got {len(match)}."
            )
        return _cell_tokens(match[0])
    raise MissingBaselineError(f"Unknown baseline kind {spec.kind!r}")


def render_cost_report(
    cells: list[CellMeasurement],
    payg: PaygPricing | None,
    ptu: PtuPricing | None,
    *,
    ptu_baseline: BaselineSpec | None = None,
) -> CostReport:
    """Build a ``CostReport`` from aggregated cells and snapshots.

    Args:
        cells: Per-cell measurements (each cell is one ``(model, effort)``).
        payg: PAYG pricing snapshot (``None`` skips PAYG lens — used by the
            ``ptu`` subcommand, which is independent of PAYG and MUST NOT
            require a PAYG snapshot on disk).
        ptu: PTU pricing snapshot (``None`` skips PTU lens).
        ptu_baseline: Required iff ``ptu`` is given.

    Raises:
        Gpt4oReasoningError: gpt-4o cell carries non-zero reasoning_tokens_mean.
        MissingBaselineError: PTU baseline cannot be resolved.
        ThroughputGainError: A target cell has zero/negative tokens.
    """
    # gpt-4o data-integrity check applies regardless of which lens is
    # requested — gpt-4o has no reasoning column (the dedicated separate line
    # invariant), so non-zero reasoning_tokens_mean is a data defect even
    # when the caller only wants the PTU lens.
    for cell in cells:
        if cell.model == "gpt-4o" and cell.reasoning_tokens_mean > 0:
            raise Gpt4oReasoningError(
                f"gpt-4o cell has reasoning_tokens_mean={cell.reasoning_tokens_mean}; "
                f"gpt-4o has no reasoning column. Data integrity failure."
            )

    payg_breakdowns: list[CostBreakdown] | None
    if payg is not None:
        payg_breakdowns = []
        for cell in cells:
            usage = TokenUsage(
                input_tokens=cell.input_tokens_mean,
                cached_tokens=cell.cached_tokens_mean,
                output_tokens=cell.output_tokens_mean,
                reasoning_tokens=cell.reasoning_tokens_mean,
            )
            breakdown = payg_cost_per_call(usage, payg, cell.model)
            payg_breakdowns.append(
                CostBreakdown(
                    model=cell.model,
                    effort=cell.effort,
                    input_tokens=usage.input_tokens,
                    cached_tokens=usage.cached_tokens,
                    output_tokens=usage.output_tokens,
                    reasoning_tokens=usage.reasoning_tokens,
                    usd_per_request=breakdown.usd_per_request,
                    pricing_snapshot_path=breakdown.pricing_snapshot_path,
                    pricing_source_url=breakdown.pricing_source_url,
                    pricing_accessed_date=breakdown.pricing_accessed_date,
                    pricing_archive_url=breakdown.pricing_archive_url,
                )
            )
    else:
        payg_breakdowns = None

    ptu_gains: list[ThroughputGain] | None
    baseline_tokens_resolved: float | None
    if ptu is not None:
        if ptu_baseline is None:
            raise ValueError(
                "render_cost_report: ptu_baseline is required when ptu is not None"
            )
        baseline_tokens_resolved = _resolve_baseline_tokens(cells, ptu_baseline)
        ptu_gains = []
        for cell in cells:
            target_tokens = _cell_tokens(cell)
            gain = ptu_throughput_gain(
                target_tokens=target_tokens,
                baseline_tokens=baseline_tokens_resolved,
                baseline_label=ptu_baseline.label,
            )
            ptu_gains.append(
                ThroughputGain(
                    model=cell.model,
                    effort=cell.effort,
                    tokens_per_request=gain.tokens_per_request,
                    throughput_gain_factor=gain.throughput_gain_factor,
                    baseline_label=gain.baseline_label,
                )
            )
    else:
        ptu_gains = None
        baseline_tokens_resolved = None

    return CostReport(
        payg_breakdowns=tuple(payg_breakdowns) if payg_breakdowns is not None else None,
        ptu_gains=tuple(ptu_gains) if ptu_gains is not None else None,
        payg=payg,
        ptu=ptu,
        ptu_baseline=ptu_baseline if ptu is not None else None,
        ptu_baseline_tokens=baseline_tokens_resolved,
    )


# ----------------------------------------------------------------------------
# Output rendering (JSON + Markdown)
# ----------------------------------------------------------------------------


def _payg_lens_dict(report: CostReport) -> dict:
    """Serialize the PAYG lens to a plain dict (deterministic key order)."""
    assert report.payg is not None
    return {
        "lens": "payg",
        "snapshot": {
            "path": report.payg.snapshot_path,
            "source_url": report.payg.source_url,
            "accessed_date": report.payg.accessed_date,
            "archive_url": report.payg.archive_url,
        },
        "currency": report.payg.currency,
        "cells": [
            {
                "model": b.model,
                "effort": b.effort,
                "input_tokens_mean": b.input_tokens,
                "cached_tokens_mean": b.cached_tokens,
                "output_tokens_mean": b.output_tokens,
                "reasoning_tokens_mean": b.reasoning_tokens,
                "usd_per_request": b.usd_per_request,
            }
            for b in (report.payg_breakdowns or ())
        ],
    }


def _ptu_lens_dict(report: CostReport) -> dict:
    """Serialize the PTU lens to a plain dict (deterministic key order)."""
    assert report.ptu is not None
    assert report.ptu_baseline is not None
    assert report.ptu_baseline_tokens is not None
    return {
        "lens": "ptu",
        "baseline": {
            "label": report.ptu_baseline.label,
            "tokens_per_request": report.ptu_baseline_tokens,
        },
        "snapshot": {
            "path": report.ptu.snapshot_path,
            "source_url": report.ptu.source_url,
            "accessed_date": report.ptu.accessed_date,
            "archive_url": report.ptu.archive_url,
        },
        "currency": report.ptu.currency,
        "region": report.ptu.region,
        "cells": [
            {
                "model": g.model,
                "effort": g.effort,
                "tokens_per_request": g.tokens_per_request,
                "throughput_gain_factor": g.throughput_gain_factor,
                "baseline_label": g.baseline_label,
            }
            for g in (report.ptu_gains or ())
        ],
    }


def render_json(report: CostReport, *, lens: Literal["payg", "ptu", "both"]) -> str:
    """Render the report as a deterministic JSON string (no timestamps)."""
    if lens == "payg":
        payload: dict = _payg_lens_dict(report)
    elif lens == "ptu":
        payload = _ptu_lens_dict(report)
    elif lens == "both":
        payg_d = _payg_lens_dict(report)
        ptu_d = _ptu_lens_dict(report)
        payg_d.pop("lens", None)
        ptu_d.pop("lens", None)
        payload = {"payg_lens": payg_d, "ptu_lens": ptu_d}
    else:
        raise ValueError(f"Unknown lens {lens!r}")
    return json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False)


def _format_md_number(x: float) -> str:
    """Format a float for Markdown — strip trailing zeros, keep 0 as 0."""
    if x == 0:
        return "0"
    if abs(x - round(x)) < 1e-9 and abs(x) < 1e6:
        return f"{int(round(x))}"
    return f"{x:.6g}"


def _render_payg_markdown(report: CostReport) -> str:
    assert report.payg is not None
    lines: list[str] = []
    lines.append("## PAYG lens — USD per request")
    lines.append("")
    lines.append(f"- Snapshot: `{report.payg.snapshot_path}`")
    lines.append(f"- Source: {report.payg.source_url}")
    lines.append(f"- Accessed: {report.payg.accessed_date}")
    if report.payg.archive_url is not None:
        lines.append(f"- Archive: {report.payg.archive_url}")
    lines.append(f"- Currency: {report.payg.currency}")
    lines.append("")
    lines.append(
        "| model | effort | input_tokens | cached_tokens | output_tokens "
        "| reasoning_tokens | usd_per_request |"
    )
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for b in report.payg_breakdowns or ():
        lines.append(
            "| {model} | {effort} | {it} | {ct} | {ot} | {rt} | {usd} |".format(
                model=b.model,
                effort=b.effort if b.effort is not None else "—",
                it=_format_md_number(b.input_tokens),
                ct=_format_md_number(b.cached_tokens),
                ot=_format_md_number(b.output_tokens),
                rt=_format_md_number(b.reasoning_tokens),
                usd=f"{b.usd_per_request:.6f}",
            )
        )
    return "\n".join(lines)


def _render_ptu_markdown(report: CostReport) -> str:
    assert report.ptu is not None
    assert report.ptu_baseline is not None
    assert report.ptu_baseline_tokens is not None
    lines: list[str] = []
    lines.append("## PTU lens — throughput gain factor")
    lines.append("")
    lines.append(
        f"- Baseline: `{report.ptu_baseline.label}` "
        f"(tokens_per_request = {_format_md_number(report.ptu_baseline_tokens)})"
    )
    lines.append(f"- Snapshot: `{report.ptu.snapshot_path}`")
    lines.append(f"- Source: {report.ptu.source_url}")
    lines.append(f"- Accessed: {report.ptu.accessed_date}")
    if report.ptu.archive_url is not None:
        lines.append(f"- Archive: {report.ptu.archive_url}")
    lines.append(f"- Region: {report.ptu.region}")
    lines.append("")
    lines.append(
        "| model | effort | tokens_per_request | throughput_gain_factor | baseline_label |"
    )
    lines.append("| --- | --- | ---: | ---: | --- |")
    for g in report.ptu_gains or ():
        lines.append(
            "| {model} | {effort} | {tpr} | {gain} | {label} |".format(
                model=g.model,
                effort=g.effort if g.effort is not None else "—",
                tpr=_format_md_number(g.tokens_per_request),
                gain=f"{g.throughput_gain_factor:.4f}",
                label=g.baseline_label,
            )
        )
    return "\n".join(lines)


def render_markdown(report: CostReport, *, lens: Literal["payg", "ptu", "both"]) -> str:
    """Render the report as Markdown.

    For ``lens == 'both'`` the two ``## `` sub-headings (``## PAYG lens — USD
    per request`` and ``## PTU lens — throughput gain factor``) are emitted
    as physically separated subsections per §6 — never collapsed.
    """
    if lens == "payg":
        return _render_payg_markdown(report) + "\n"
    if lens == "ptu":
        return _render_ptu_markdown(report) + "\n"
    if lens == "both":
        return (
            _render_payg_markdown(report)
            + "\n\n"
            + _render_ptu_markdown(report)
            + "\n"
        )
    raise ValueError(f"Unknown lens {lens!r}")


# ----------------------------------------------------------------------------
# analysis.json loader (stub schema; future analyze_tokens.py honors this)
# ----------------------------------------------------------------------------


def load_analysis(path: str | pathlib.Path) -> tuple[list[CellMeasurement], dict]:
    """Load an ``analysis.json`` stub. Returns ``(cells, metadata)``."""
    p = pathlib.Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"analysis.json not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or "cells" not in data:
        raise ValueError(f"analysis.json must have top-level key 'cells': {p}")
    cells: list[CellMeasurement] = []
    for raw in data["cells"]:
        cells.append(
            CellMeasurement(
                model=raw["model"],
                effort=raw.get("effort"),
                sample_count=int(raw["sample_count"]),
                input_tokens_mean=float(raw["input_tokens_mean"]),
                cached_tokens_mean=float(raw["cached_tokens_mean"]),
                output_tokens_mean=float(raw["output_tokens_mean"]),
                reasoning_tokens_mean=float(raw["reasoning_tokens_mean"]),
            )
        )
    metadata = {k: v for k, v in data.items() if k != "cells"}
    return cells, metadata


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


_CLI_EPILOG = """\
Exit codes:
  0  success
  1  argparse / generic Python error
  2  YAML schema validation failure
  3  data integrity failure (e.g., gpt-4o with reasoning_tokens > 0)
  4  missing snapshot for the requested date

Default --snapshot-date: when omitted, the newest snapshot is resolved
deterministically by in-file accessed_date (NOT datetime.date.today()) so CI
runs are reproducible across calendar days.
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.cost_calculator",
        description=(
            "Offline derivation of PAYG USD-per-request (§6.1) and PTU "
            "throughput-gain factor (§6.2) from a committed pricing snapshot."
        ),
        epilog=_CLI_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    for name, help_text in [
        ("payg", "Apply PAYG snapshot; emit USD-per-request per (model, effort)."),
        ("ptu", "Apply PTU baseline; emit throughput-gain factor per cell."),
        ("both", "Emit both lenses in two physically separated subsections."),
    ]:
        sp = subparsers.add_parser(
            name,
            help=help_text,
            description=help_text,
            epilog=_CLI_EPILOG,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        sp.add_argument("analysis_json", help="Path to analysis.json")
        sp.add_argument(
            "--pricing-dir",
            default="pricing",
            help="Directory of dated snapshots (default: pricing)",
        )
        sp.add_argument(
            "--snapshot-date",
            default=None,
            help=(
                "Active snapshot resolution target as YYYY-MM-DD. "
                "Default: newest snapshot by in-file accessed_date (NOT today())."
            ),
        )
        sp.add_argument(
            "--output",
            choices=["json", "markdown"],
            default="json",
            help="Output format (default: json)",
        )
        if name in ("ptu", "both"):
            sp.add_argument(
                "--baseline",
                choices=["migration", "effort-high", "custom"],
                required=True,
                help="PTU baseline selection",
            )
            sp.add_argument(
                "--baseline-tokens",
                type=float,
                default=None,
                help="Baseline tokens-per-request (required iff --baseline custom)",
            )
    return parser


def _parse_snapshot_date(s: str | None) -> datetime.date | None:
    if s is None:
        return None
    if not DATE_RE.match(s):
        raise ValueError(f"--snapshot-date must be YYYY-MM-DD; got {s!r}")
    return datetime.date.fromisoformat(s)


def _resolve_baseline_spec(args: argparse.Namespace) -> BaselineSpec:
    kind = args.baseline
    if kind == "migration":
        return BaselineSpec(kind="migration", label="gpt-4o")
    if kind == "effort-high":
        return BaselineSpec(kind="effort-high", label="gpt-5.2 effort=high")
    if kind == "custom":
        tokens = args.baseline_tokens
        if tokens is None:
            raise ValueError("--baseline custom requires --baseline-tokens")
        return BaselineSpec(
            kind="custom",
            label=f"custom:{tokens}",
            tokens_per_request=float(tokens),
        )
    raise ValueError(f"Unknown baseline kind {kind!r}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns process exit code.

    Exit codes: 0 success; 1 generic/argparse; 2 schema validation;
    3 data integrity; 4 missing snapshot.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 1

    try:
        target_date = _parse_snapshot_date(args.snapshot_date)
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    sub = args.subcommand
    try:
        # PAYG snapshot is resolved/loaded ONLY when the requested lens needs
        # it. The ``ptu`` subcommand is independent of PAYG — a pricing dir
        # containing only the PTU YAML must work for a PTU-only run.
        payg: PaygPricing | None = None
        if sub in ("payg", "both"):
            payg_path = resolve_active_snapshot(
                kind="payg", target_date=target_date, pricing_dir=args.pricing_dir
            )
            payg = load_payg_pricing(payg_path)

        ptu: PtuPricing | None = None
        baseline_spec: BaselineSpec | None = None
        if sub in ("ptu", "both"):
            ptu_path = resolve_active_snapshot(
                kind="ptu", target_date=target_date, pricing_dir=args.pricing_dir
            )
            ptu = load_ptu_pricing(ptu_path)
            baseline_spec = _resolve_baseline_spec(args)

        cells, _metadata = load_analysis(args.analysis_json)
        report = render_cost_report(
            cells=cells, payg=payg, ptu=ptu, ptu_baseline=baseline_spec
        )
    except SnapshotNotFoundError as exc:
        sys.stderr.write(f"error: snapshot not found: {exc}\n")
        return 4
    except (PaygSchemaError, PtuSchemaError) as exc:
        sys.stderr.write(f"error: pricing YAML schema validation failed: {exc}\n")
        return 2
    except (Gpt4oReasoningError, MissingBaselineError, ThroughputGainError) as exc:
        sys.stderr.write(f"error: data integrity: {exc}\n")
        return 3
    except FileNotFoundError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    if sub == "payg":
        lens: Literal["payg", "ptu", "both"] = "payg"
    elif sub == "ptu":
        lens = "ptu"
    else:
        lens = "both"

    if args.output == "json":
        sys.stdout.write(render_json(report, lens=lens) + "\n")
    else:
        sys.stdout.write(render_markdown(report, lens=lens))
    return 0


if __name__ == "__main__":  # pragma: no cover - thin wrapper
    sys.exit(main())
