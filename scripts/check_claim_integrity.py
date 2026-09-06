"""Validate and narrowly render current public benchmark claims."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = (
    REPO_ROOT
    / "batch-runner"
    / "batch_runner"
    / "data"
    / "public_claims.v1.json"
)
DEFAULT_README = REPO_ROOT / "README.md"
START_MARKER = "<!-- CLAIM-INTEGRITY:START current-headlines -->"
PAUSE_MARKER = "<!-- CLAIM-INTEGRITY:PAUSE current-headlines -->"
RESUME_MARKER = "<!-- CLAIM-INTEGRITY:RESUME current-headlines -->"
END_MARKER = "<!-- CLAIM-INTEGRITY:END current-headlines -->"
CANONICAL_REGISTRY_PATH = (
    "docs/blog/articles/when-reasoning-pays-off/numeric-claims.json"
)
_ANALYSIS_PATH_RE = re.compile(
    r"benchmarks/[0-9]{2}-[a-z0-9-]+/analysis\.json"
)
_CHART_PATH_RE = re.compile(
    r"docs/blog/data/chart-data/(?:cost-curves-effort|token-composition)/"
    r"benchmark-[0-9]{2}/(?:cost-per-request|quality|tokens)\.json"
)
_METRIC_UNITS = {
    "mean_usd_per_request": "USD/request",
    "mean_judge_score": "judge-score",
    "mean_reasoning_tokens": "tokens/request",
}
_CLAIM_SLOTS: dict[str, dict[str, object]] = {
    "short-factual-cost-none": {
        "cohort": "current-2026-05-benchmark-01",
        "model": "gpt-5.2",
        "effort": "none",
        "metric": "mean_usd_per_request",
        "unit": "USD/request",
        "source_path": "benchmarks/01-short-factual/analysis.json",
        "chart_path": (
            "docs/blog/data/chart-data/cost-curves-effort/"
            "benchmark-01/cost-per-request.json"
        ),
        "registry_claim_id": "short-factual-none-cost",
    },
    "short-factual-cost-xhigh": {
        "cohort": "current-2026-05-benchmark-01",
        "model": "gpt-5.2",
        "effort": "xhigh",
        "metric": "mean_usd_per_request",
        "unit": "USD/request",
        "source_path": "benchmarks/01-short-factual/analysis.json",
        "chart_path": (
            "docs/blog/data/chart-data/cost-curves-effort/"
            "benchmark-01/cost-per-request.json"
        ),
        "registry_claim_id": "short-factual-xhigh-cost",
    },
    "short-factual-quality-none": {
        "cohort": "current-2026-05-benchmark-01",
        "model": "gpt-5.2",
        "effort": "none",
        "metric": "mean_judge_score",
        "unit": "judge-score",
        "source_path": "benchmarks/01-short-factual/analysis.json",
        "chart_path": (
            "docs/blog/data/chart-data/cost-curves-effort/"
            "benchmark-01/quality.json"
        ),
        "registry_claim_id": "short-factual-none-quality",
    },
    "short-factual-quality-xhigh": {
        "cohort": "current-2026-05-benchmark-01",
        "model": "gpt-5.2",
        "effort": "xhigh",
        "metric": "mean_judge_score",
        "unit": "judge-score",
        "source_path": "benchmarks/01-short-factual/analysis.json",
        "chart_path": (
            "docs/blog/data/chart-data/cost-curves-effort/"
            "benchmark-01/quality.json"
        ),
        "registry_claim_id": None,
    },
    "short-factual-reasoning-none": {
        "cohort": "current-2026-05-benchmark-01",
        "model": "gpt-5.2",
        "effort": "none",
        "metric": "mean_reasoning_tokens",
        "unit": "tokens/request",
        "source_path": "benchmarks/01-short-factual/analysis.json",
        "chart_path": (
            "docs/blog/data/chart-data/token-composition/"
            "benchmark-01/tokens.json"
        ),
        "registry_claim_id": "short-factual-none-reasoning-tokens",
    },
    "short-factual-reasoning-high": {
        "cohort": "current-2026-05-benchmark-01",
        "model": "gpt-5.2",
        "effort": "high",
        "metric": "mean_reasoning_tokens",
        "unit": "tokens/request",
        "source_path": "benchmarks/01-short-factual/analysis.json",
        "chart_path": (
            "docs/blog/data/chart-data/token-composition/"
            "benchmark-01/tokens.json"
        ),
        "registry_claim_id": "short-factual-high-reasoning-tokens",
    },
    "short-factual-reasoning-xhigh": {
        "cohort": "current-2026-05-benchmark-01",
        "model": "gpt-5.2",
        "effort": "xhigh",
        "metric": "mean_reasoning_tokens",
        "unit": "tokens/request",
        "source_path": "benchmarks/01-short-factual/analysis.json",
        "chart_path": (
            "docs/blog/data/chart-data/token-composition/"
            "benchmark-01/tokens.json"
        ),
        "registry_claim_id": None,
    },
    "multi-step-quality-baseline": {
        "cohort": "current-2026-05-benchmark-02",
        "model": "gpt-4o",
        "effort": None,
        "metric": "mean_judge_score",
        "unit": "judge-score",
        "source_path": "benchmarks/02-multi-step-reasoning/analysis.json",
        "chart_path": (
            "docs/blog/data/chart-data/cost-curves-effort/"
            "benchmark-02/quality.json"
        ),
        "registry_claim_id": "multi-step-baseline-quality",
    },
    "multi-step-quality-none": {
        "cohort": "current-2026-05-benchmark-02",
        "model": "gpt-5.2",
        "effort": "none",
        "metric": "mean_judge_score",
        "unit": "judge-score",
        "source_path": "benchmarks/02-multi-step-reasoning/analysis.json",
        "chart_path": (
            "docs/blog/data/chart-data/cost-curves-effort/"
            "benchmark-02/quality.json"
        ),
        "registry_claim_id": None,
    },
}
_COMPARISON_SLOTS = {
    "short-factual-cost-none-to-xhigh": {
        "left_claim_id": "short-factual-cost-none",
        "right_claim_id": "short-factual-cost-xhigh",
        "operation": "ratio",
        "unit": "ratio",
    },
    "multi-step-baseline-to-none-quality": {
        "left_claim_id": "multi-step-quality-baseline",
        "right_claim_id": "multi-step-quality-none",
        "operation": "delta",
        "unit": "judge-score-delta",
    },
}


class ClaimIntegrityError(ValueError):
    """Raised when a public claim cannot be proven from canonical evidence."""


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ClaimIntegrityError(f"{label} is missing") from exc
    except json.JSONDecodeError as exc:
        raise ClaimIntegrityError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ClaimIntegrityError(f"{label} root must be an object")
    return payload


def load_contract(path: Path = DEFAULT_CONTRACT) -> dict[str, Any]:
    """Load a contract without retaining caller-owned mutable state."""

    return copy.deepcopy(_load_json(path, label="claim contract"))


def _safe_repo_path(root: Path, relative: object, *, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ClaimIntegrityError(f"{label} path must be non-empty text")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ClaimIntegrityError(f"{label} path escapes the repository") from exc
    return candidate


def _decimal(value: object, *, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ClaimIntegrityError(f"{label} must be numeric")
    try:
        result = Decimal(str(value))
    except ArithmeticError as exc:
        raise ClaimIntegrityError(f"{label} must be finite numeric data") from exc
    if not result.is_finite():
        raise ClaimIntegrityError(f"{label} must be finite numeric data")
    return result


def _expected_measurement_format(value: Decimal, unit: object) -> str:
    if unit == "USD/request":
        return f"${value.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP):.6f}"
    if unit == "judge-score":
        return format(value, "f")
    if unit == "tokens/request":
        if value == value.to_integral_value():
            return str(int(value))
        rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return format(rounded, "f").rstrip("0").rstrip(".")
    raise ClaimIntegrityError(f"unsupported public claim unit: {unit!r}")


def _resolve_reference(
    root: Path,
    reference: Mapping[str, Any],
    *,
    label: str,
) -> tuple[object, Mapping[str, Any]]:
    allowed = {"path", "selector"}
    unknown = set(reference) - allowed
    if unknown:
        raise ClaimIntegrityError(f"{label} has unknown fields: {sorted(unknown)}")
    path = _safe_repo_path(root, reference.get("path"), label=label)
    payload = _load_json(path, label=label)
    selector = reference.get("selector")
    if not isinstance(selector, Mapping):
        raise ClaimIntegrityError(f"{label} selector must be an object")
    if set(selector) != {"collection", "where", "field"}:
        raise ClaimIntegrityError(
            f"{label} selector requires exactly collection, where, and field"
        )
    collection_name = selector["collection"]
    where = selector["where"]
    field = selector["field"]
    if not isinstance(collection_name, str) or not isinstance(where, Mapping):
        raise ClaimIntegrityError(f"{label} selector has invalid collection/where")
    collection = payload.get(collection_name)
    if not isinstance(collection, list):
        raise ClaimIntegrityError(f"{label} selector collection does not exist")
    matches = [
        row
        for row in collection
        if isinstance(row, Mapping)
        and all(row.get(key) == expected for key, expected in where.items())
    ]
    if len(matches) != 1:
        raise ClaimIntegrityError(
            f"{label} selector resolved {len(matches)} rows; expected exactly one"
        )
    row = matches[0]
    if not isinstance(field, str) or field not in row:
        raise ClaimIntegrityError(f"{label} selector field does not exist")
    return row[field], row


def _required_keys(
    payload: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] = frozenset(),
    label: str,
) -> None:
    missing = required - set(payload)
    unknown = set(payload) - required - optional
    if missing:
        raise ClaimIntegrityError(f"{label} missing fields: {sorted(missing)}")
    if unknown:
        raise ClaimIntegrityError(f"{label} has unknown fields: {sorted(unknown)}")


def _validate_contract_shape(contract: Mapping[str, Any]) -> None:
    _required_keys(
        contract,
        required={
            "schema",
            "schema_version",
            "cohort_policy",
            "source_registry",
            "claims",
            "comparisons",
        },
        label="claim contract",
    )
    if contract["schema"] != "wrpo.public_claim_contract":
        raise ClaimIntegrityError("claim contract schema is unsupported")
    if contract["schema_version"] != "1.0.0":
        raise ClaimIntegrityError("claim contract version is unsupported")
    if not isinstance(contract["claims"], list) or not contract["claims"]:
        raise ClaimIntegrityError("claim contract must contain measurement claims")
    if not isinstance(contract["comparisons"], list) or not contract["comparisons"]:
        raise ClaimIntegrityError("claim contract must contain comparison claims")


def _registry_index(root: Path, contract: Mapping[str, Any]) -> dict[str, Any]:
    registry_ref = contract["source_registry"]
    if not isinstance(registry_ref, Mapping):
        raise ClaimIntegrityError("source_registry must be an object")
    _required_keys(
        registry_ref,
        required={"path", "sha256"},
        label="source_registry",
    )
    if registry_ref["path"] != CANONICAL_REGISTRY_PATH:
        raise ClaimIntegrityError("source_registry must use the canonical ledger")
    path = _safe_repo_path(root, registry_ref["path"], label="source_registry")
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError as exc:
        raise ClaimIntegrityError("source_registry is missing") from exc
    if digest != registry_ref["sha256"]:
        raise ClaimIntegrityError("source_registry sha256 does not match")
    registry = _load_json(path, label="source_registry")
    claims = registry.get("claims")
    if not isinstance(claims, list):
        raise ClaimIntegrityError("source_registry claims must be an array")
    index: dict[str, Any] = {}
    for item in claims:
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            index[item["id"]] = item
    return index


def _measurement_index(
    root: Path,
    contract: Mapping[str, Any],
    registry: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    policy = contract["cohort_policy"]
    if not isinstance(policy, Mapping):
        raise ClaimIntegrityError("cohort_policy must be an object")
    current_prefix = policy.get("current_cohort_prefix")
    forbidden = policy.get("forbidden_effort_by_model")
    if not isinstance(current_prefix, str) or not isinstance(forbidden, Mapping):
        raise ClaimIntegrityError("cohort_policy is invalid")

    required = {
        "claim_id",
        "cohort",
        "model",
        "effort",
        "metric",
        "unit",
        "value",
        "changed_dimensions",
        "format",
        "publication_targets",
        "source",
        "chart_cross_check",
    }
    index: dict[str, Mapping[str, Any]] = {}
    for raw in contract["claims"]:
        if not isinstance(raw, Mapping):
            raise ClaimIntegrityError("measurement claim must be an object")
        label = f"measurement claim {raw.get('claim_id', '<missing>')}"
        _required_keys(
            raw,
            required=required,
            optional={"registry_claim_id"},
            label=label,
        )
        claim_id = raw["claim_id"]
        if not isinstance(claim_id, str) or claim_id in index:
            raise ClaimIntegrityError("measurement claim IDs must be unique strings")
        slot = _CLAIM_SLOTS.get(claim_id)
        if slot is None:
            raise ClaimIntegrityError(f"{label} is not an approved public claim slot")
        for field_name in ("cohort", "model", "effort", "metric", "unit"):
            if raw[field_name] != slot[field_name]:
                raise ClaimIntegrityError(
                    f"{label} {field_name} contradicts its approved slot"
                )
        if raw.get("registry_claim_id") != slot["registry_claim_id"]:
            raise ClaimIntegrityError(f"{label} registry binding contradicts its slot")
        if raw["changed_dimensions"] != []:
            raise ClaimIntegrityError(f"{label} must not declare changed dimensions")
        metric = raw["metric"]
        if metric not in _METRIC_UNITS or raw["unit"] != _METRIC_UNITS[metric]:
            raise ClaimIntegrityError(f"{label} metric/unit contract is invalid")
        if (
            isinstance(raw["cohort"], str)
            and raw["cohort"].startswith(current_prefix)
            and raw["effort"] in forbidden.get(raw["model"], [])
        ):
            raise ClaimIntegrityError(
                f"{label} uses a forbidden effort for the current cohort"
            )

        source = raw["source"]
        chart = raw["chart_cross_check"]
        if not isinstance(source, Mapping) or not isinstance(chart, Mapping):
            raise ClaimIntegrityError(f"{label} evidence references must be objects")
        source_path = source.get("path")
        chart_path = chart.get("path")
        if source_path != slot["source_path"] or chart_path != slot["chart_path"]:
            raise ClaimIntegrityError(f"{label} evidence paths contradict its slot")
        if not isinstance(source_path, str) or not _ANALYSIS_PATH_RE.fullmatch(
            source_path
        ):
            raise ClaimIntegrityError(f"{label} source is not canonical analysis JSON")
        if not isinstance(chart_path, str) or not _CHART_PATH_RE.fullmatch(chart_path):
            raise ClaimIntegrityError(f"{label} chart is not canonical public JSON")
        source_selector = source.get("selector")
        chart_selector = chart.get("selector")
        if not isinstance(source_selector, Mapping) or not isinstance(
            chart_selector, Mapping
        ):
            raise ClaimIntegrityError(f"{label} selectors must be objects")
        expected_source_selector = {
            "collection": "cell_stats",
            "where": {
                "model": raw["model"],
                "effort": raw["effort"],
            },
            "field": metric,
        }
        expected_chart_selector = {
            "collection": "rows",
            "where": {
                "model": raw["model"],
                "effort": "na" if raw["effort"] is None else raw["effort"],
            },
            "field": metric,
        }
        if source_selector != expected_source_selector:
            raise ClaimIntegrityError(f"{label} source selector contradicts claim identity")
        if chart_selector != expected_chart_selector:
            raise ClaimIntegrityError(f"{label} chart selector contradicts claim identity")
        source_value, source_row = _resolve_reference(
            root, source, label=f"{label} source"
        )
        chart_value, _ = _resolve_reference(
            root, chart, label=f"{label} chart_cross_check"
        )
        expected = _decimal(raw["value"], label=f"{label} value")
        expected_format = _expected_measurement_format(expected, raw["unit"])
        if raw["format"] != expected_format:
            raise ClaimIntegrityError(f"{label} format does not match its value and unit")
        if _decimal(source_value, label=f"{label} source value") != expected:
            raise ClaimIntegrityError(f"{label} value does not match analysis source")
        if _decimal(chart_value, label=f"{label} chart value") != expected:
            raise ClaimIntegrityError(f"{label} value does not match public chart")
        if "n_used" in source_row and int(source_row["n_used"]) <= 0:
            raise ClaimIntegrityError(f"{label} resolves to an unmeasured source row")

        registry_claim_id = raw.get("registry_claim_id")
        if registry_claim_id is not None:
            entry = registry.get(registry_claim_id)
            if not isinstance(entry, Mapping):
                raise ClaimIntegrityError(f"{label} registry claim is missing")
            if entry.get("display_value") != raw["format"]:
                raise ClaimIntegrityError(f"{label} format differs from source registry")
            registry_selector = entry.get("selector")
            expected_registry_selector = {
                **expected_chart_selector["where"],
                "field": metric,
            }
            if registry_selector != expected_registry_selector:
                raise ClaimIntegrityError(
                    f"{label} selector differs from source registry"
                )
            if entry.get("source_paths") != [chart_path]:
                raise ClaimIntegrityError(
                    f"{label} evidence path differs from source registry"
                )
            values = entry.get("source_values")
            if not isinstance(values, list) or not any(
                _decimal(value, label=f"{label} registry source value") == expected
                for value in values
            ):
                raise ClaimIntegrityError(f"{label} value differs from source registry")
        index[claim_id] = raw
    return index


def _validate_comparisons(
    contract: Mapping[str, Any],
    claims: Mapping[str, Mapping[str, Any]],
) -> None:
    required = {
        "claim_id",
        "cohort",
        "model",
        "effort",
        "metric",
        "unit",
        "value",
        "changed_dimensions",
        "format",
        "publication_targets",
        "source",
        "operation",
        "causal_attribution",
    }
    seen: set[str] = set()
    for raw in contract["comparisons"]:
        if not isinstance(raw, Mapping):
            raise ClaimIntegrityError("comparison claim must be an object")
        label = f"comparison claim {raw.get('claim_id', '<missing>')}"
        _required_keys(raw, required=required, label=label)
        claim_id = raw["claim_id"]
        if not isinstance(claim_id, str) or claim_id in seen:
            raise ClaimIntegrityError("comparison claim IDs must be unique strings")
        seen.add(claim_id)
        slot = _COMPARISON_SLOTS.get(claim_id)
        if slot is None:
            raise ClaimIntegrityError(f"{label} is not an approved comparison slot")
        source = raw["source"]
        if not isinstance(source, Mapping) or set(source) != {
            "left_claim_id",
            "right_claim_id",
        }:
            raise ClaimIntegrityError(f"{label} source is invalid")
        if (
            source["left_claim_id"] != slot["left_claim_id"]
            or source["right_claim_id"] != slot["right_claim_id"]
            or raw["operation"] != slot["operation"]
            or raw["unit"] != slot["unit"]
        ):
            raise ClaimIntegrityError(f"{label} contradicts its approved slot")
        try:
            left = claims[source["left_claim_id"]]
            right = claims[source["right_claim_id"]]
        except KeyError as exc:
            raise ClaimIntegrityError(f"{label} references a missing claim") from exc
        if (
            raw["cohort"] != left["cohort"]
            or raw["cohort"] != right["cohort"]
            or raw["metric"] != left["metric"]
            or raw["metric"] != right["metric"]
        ):
            raise ClaimIntegrityError(
                f"{label} cohort/metric does not match its source claims"
            )

        expected_dimensions = [
            dimension
            for dimension in ("model", "effort")
            if left[dimension] != right[dimension]
        ]
        if raw["changed_dimensions"] != expected_dimensions:
            raise ClaimIntegrityError(
                f"{label} changed_dimensions do not match source claims"
            )
        model_sides = raw["model"]
        effort_sides = raw["effort"]
        if not isinstance(model_sides, Mapping) or not isinstance(
            effort_sides, Mapping
        ):
            raise ClaimIntegrityError(f"{label} model/effort sides are invalid")
        if model_sides != {"left": left["model"], "right": right["model"]}:
            raise ClaimIntegrityError(f"{label} model sides do not match source claims")
        if effort_sides != {"left": left["effort"], "right": right["effort"]}:
            raise ClaimIntegrityError(f"{label} effort sides do not match source claims")
        if {"model", "effort"}.issubset(expected_dimensions) and raw[
            "causal_attribution"
        ] != "descriptive_only":
            raise ClaimIntegrityError(
                f"{label} changes model and effort and must be descriptive_only"
            )

        left_value = _decimal(left["value"], label=f"{label} left value")
        right_value = _decimal(right["value"], label=f"{label} right value")
        if raw["operation"] == "ratio":
            if left_value == 0:
                raise ClaimIntegrityError(f"{label} cannot divide by zero")
            derived = right_value / left_value
        elif raw["operation"] == "delta":
            derived = right_value - left_value
        else:
            raise ClaimIntegrityError(f"{label} has unsupported operation")
        declared = _decimal(raw["value"], label=f"{label} value")
        tolerance = Decimal("1e-15") if raw["operation"] == "ratio" else Decimal(0)
        if abs(derived - declared) > tolerance:
            raise ClaimIntegrityError(f"{label} value does not match its source claims")
        if raw["operation"] == "ratio":
            rendered_ratio = (
                f"{derived.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}x"
            )
            if raw["format"] != rendered_ratio:
                raise ClaimIntegrityError(
                    f"{label} format does not match the rounded derived ratio"
                )
        else:
            rendered_delta = f"{left['format']} -> {right['format']}"
            if raw["format"] != rendered_delta:
                raise ClaimIntegrityError(
                    f"{label} format does not match its source claim formats"
                )
    if seen != set(_COMPARISON_SLOTS):
        raise ClaimIntegrityError("approved comparison slots are missing")


def validate_contract(
    root: Path = REPO_ROOT,
    contract: Mapping[str, Any] | None = None,
) -> Mapping[str, Mapping[str, Any]]:
    """Validate evidence, charts, registry, cohorts, and comparisons."""

    active = load_contract() if contract is None else copy.deepcopy(dict(contract))
    _validate_contract_shape(active)
    registry = _registry_index(root, active)
    claims = _measurement_index(root, active, registry)
    if set(claims) != set(_CLAIM_SLOTS):
        raise ClaimIntegrityError("public claim slots are missing or duplicated")
    _validate_comparisons(active, claims)
    return claims


def _claim_map(contract: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        claim["claim_id"]: claim
        for claim in contract["claims"]
        if isinstance(claim, Mapping)
    }


def _comparison_map(contract: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        claim["claim_id"]: claim
        for claim in contract["comparisons"]
        if isinstance(claim, Mapping)
    }


def render_readme_block(contract: Mapping[str, Any]) -> str:
    """Render the owned headlines around a gap for handwritten README sections."""

    claims = _claim_map(contract)
    comparisons = _comparison_map(contract)

    def fmt(claim_id: str) -> str:
        return str(claims[claim_id]["format"])

    ratio = comparisons["short-factual-cost-none-to-xhigh"]["format"]
    summary = f"""\
**The current evidence is workload-specific and descriptive.** In the current
GPT-5.2 short-factual cohort, `none` and `xhigh` cost
**{fmt("short-factual-cost-none")} → {fmt("short-factual-cost-xhigh")} per request
({ratio})**, while mean judge quality was **{fmt("short-factual-quality-none")} →
{fmt("short-factual-quality-xhigh")}**."""
    return f"""\
{START_MARKER}
{summary}

| Current short-factual cost | Current short-factual quality |
| --- | --- |
| ![Benchmark 01 cost per request remains nearly flat from none to xhigh reasoning effort](docs/assets/benchmark-01-cost-per-request.png) | ![Benchmark 01 judge quality remains nearly flat across measured GPT-5.2 effort levels](docs/assets/benchmark-01-quality.png) |

{PAUSE_MARKER}

{RESUME_MARKER}
## TL;DR — what the current measurements say

{summary} Mean reasoning tokens were
**{fmt("short-factual-reasoning-none")} at `none`,
{fmt("short-factual-reasoning-high")} at `high`, and
{fmt("short-factual-reasoning-xhigh")} at `xhigh`**. The measured floor is
`none`; zero-sample cells are excluded from current public claims.

On the multi-step benchmark, mean judge quality was
**{fmt("multi-step-quality-baseline")} for the GPT-4o baseline and
{fmt("multi-step-quality-none")} for GPT-5.2 at `none`**. Both the **model** and
**effort** dimensions changed, so this comparison does not isolate an
effort-only causal effect. Within GPT-5.2, `none` already reached the measured
quality ceiling in this cohort; higher effort increased cost without improving
that aggregate score.

- **Treat effort as a workload-specific tuning parameter, not a quality
  guarantee.** Run an evaluation before changing production policy.
- **Separate model changes from effort changes.** A cross-model comparison is
  useful evidence, but it is not an effort-only experiment.
- **Trace every headline.** The values above resolve through the versioned
  [public claim contract](batch-runner/batch_runner/data/public_claims.v1.json)
  to canonical analysis and public chart JSON.

<sub>Current headline values are generated only inside this marker block.
Historical benchmark, result, and blog inputs remain read-only.</sub>
{END_MARKER}"""


def _readme_block_spans(readme: str) -> tuple[int, int, int, int]:
    markers = (START_MARKER, PAUSE_MARKER, RESUME_MARKER, END_MARKER)
    if any(readme.count(marker) != 1 for marker in markers):
        raise ClaimIntegrityError("README must contain exactly one of each claim marker")
    start, pause, resume, end = (readme.index(marker) for marker in markers)
    if not start < pause < resume < end:
        raise ClaimIntegrityError("README claim markers are out of order")
    return start, pause + len(PAUSE_MARKER), resume, end + len(END_MARKER)


def replace_readme_block(readme: str, rendered: str) -> str:
    """Replace owned headlines without touching the handwritten middle."""

    start, gap_start, gap_end, end = _readme_block_spans(readme)
    _, rendered_gap_start, rendered_gap_end, _ = _readme_block_spans(rendered)
    replacement = (
        rendered[:rendered_gap_start]
        + readme[gap_start:gap_end]
        + rendered[rendered_gap_end:]
    )
    return readme[:start] + replacement + readme[end:]


def check_readme(readme: str, contract: Mapping[str, Any]) -> None:
    """Reject legacy/current-cohort drift and require byte-exact generated prose."""

    normalized_readme = readme.lower().replace(" ", "")
    for legacy in contract["cohort_policy"]["legacy_headline_patterns"]:
        if str(legacy).lower().replace(" ", "") in normalized_readme:
            raise ClaimIntegrityError("README contains a legacy current headline")
    start, gap_start, gap_end, end = _readme_block_spans(readme)
    block = readme[start:gap_start] + "\n\n" + readme[gap_end:end]
    if re.search(r"gpt-?5\.2.{0,100}\bminimal\b", block, re.IGNORECASE | re.DOTALL):
        raise ClaimIntegrityError("README current GPT-5.2 cohort must not use minimal")
    if block != render_readme_block(contract):
        raise ClaimIntegrityError(
            "README claim marker block differs from the contract render"
        )


def run_check(
    *,
    root: Path = REPO_ROOT,
    contract_path: Path = DEFAULT_CONTRACT,
    readme_path: Path = DEFAULT_README,
) -> tuple[int, int]:
    contract = load_contract(contract_path)
    validate_contract(root, contract)
    try:
        readme = readme_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ClaimIntegrityError("README is missing") from exc
    check_readme(readme, contract)
    return len(contract["claims"]), len(contract["comparisons"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check or narrowly render evidence-backed README claims."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="Validate contract, evidence, charts, and README")
    render = subparsers.add_parser("render", help="Render the owned README marker block")
    render.add_argument(
        "--write",
        action="store_true",
        help="Replace the existing marker block in README.md",
    )
    return parser


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        contract = load_contract()
        validate_contract(REPO_ROOT, contract)
        rendered = render_readme_block(contract)
        if args.command == "render":
            if args.write:
                current = DEFAULT_README.read_text(encoding="utf-8")
                _atomic_write_text(
                    DEFAULT_README,
                    replace_readme_block(current, rendered),
                )
                print("claim integrity: rendered README marker block")
            else:
                print(rendered)
            return 0
        measurements, comparisons = run_check()
    except (ClaimIntegrityError, OSError) as exc:
        print(f"claim integrity: FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        "claim integrity: OK "
        f"({measurements} measurements, {comparisons} comparisons)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
