"""Strict, deterministic Azure OpenAI PAYG pricing snapshots."""

from __future__ import annotations

import datetime
import hashlib
import math
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

SNAPSHOT_SCHEMA_VERSION = "1.0.0"
CANONICAL_PAYG_SNAPSHOT_ID = "azure-openai-payg-sample-2026-05"
CANONICAL_PAYG_SNAPSHOT_PATH = "pricing/azure-openai-payg-sample-2026-05.yaml"
PACKAGED_PAYG_RESOURCE = "azure_sample_pricing.yaml"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DEPLOYMENT_TYPES = {
    "Global Standard",
    "Data Zone Standard",
    "Regional Standard",
    "Provisioned",
}
_TOP_LEVEL_ORDER = (
    "schema_version",
    "snapshot_id",
    "source_url",
    "accessed_date",
    "archive_url",
    "currency",
    "selection_policy",
    "records",
)
_RECORD_KEYS = {
    "model_family",
    "model_version",
    "geography",
    "region",
    "deployment_type",
    "sku",
    "price_key",
    "meters",
    "rates",
}
_METER_KEYS = {"input", "cached_input", "output"}


class PaygSchemaError(ValueError):
    """Raised when an immutable PAYG snapshot is invalid."""


class PricingSelectionError(ValueError):
    """Raised when a ledger cannot select its exact declared price record."""


@dataclass(frozen=True)
class Gpt4oRates:
    input_per_1m_usd: float
    cached_input_per_1m_usd: float
    output_per_1m_usd: float


@dataclass(frozen=True)
class Gpt52Rates:
    input_per_1m_usd: float
    cached_input_per_1m_usd: float
    reasoning_per_1m_usd: float
    output_per_1m_usd: float


@dataclass(frozen=True)
class AzurePriceRecord:
    model_family: str
    model_version: str
    geography: str
    region: str
    deployment_type: str
    sku: str
    price_key: str
    meters: dict[str, str]
    rates: Gpt4oRates | Gpt52Rates


@dataclass(frozen=True)
class PaygPricing:
    source_url: str
    accessed_date: str
    archive_url: str | None
    currency: str
    models: dict[str, Gpt4oRates | Gpt52Rates]
    snapshot_path: str
    snapshot_id: str | None = None
    snapshot_sha256: str | None = None
    selection_policy: dict[str, str] = field(default_factory=dict)
    records: dict[str, AzurePriceRecord] = field(default_factory=dict)


@dataclass(frozen=True)
class PricingSelection:
    snapshot: PaygPricing
    record: AzurePriceRecord


def _date(value: object) -> str:
    if isinstance(value, datetime.datetime):
        raise PaygSchemaError("accessed_date must be a date, not a datetime")
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, str) and _DATE_RE.fullmatch(value):
        return value
    raise PaygSchemaError("accessed_date must match YYYY-MM-DD")


def _https(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.startswith("https://"):
        raise PaygSchemaError(f"{field_name} must be an https:// URL")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PaygSchemaError(f"{field_name} must be a non-empty trimmed string")
    return value


def _rate(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PaygSchemaError(f"{field_name} must be a positive number")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0 or numeric > 1_000_000:
        raise PaygSchemaError(f"{field_name} must be a finite positive number")
    return numeric


def _mapping(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PaygSchemaError(f"{field_name} must be a mapping")
    return value


def _load_legacy_payg(
    root: dict[str, Any], *, raw: bytes, snapshot_path: Path
) -> PaygPricing:
    """Parse the explicitly legacy historical snapshot shape.

    Legacy files remain readable for published-result reproduction, but carry no
    selectable records and therefore cannot authorize an Azure sample run.
    """
    allowed = {"source_url", "accessed_date", "archive_url", "currency", "models"}
    required = allowed - {"archive_url"}
    if set(root) not in {frozenset(required), frozenset(allowed)}:
        raise PaygSchemaError("legacy PAYG top-level keys mismatch")
    expected_order = [
        key
        for key in ("source_url", "accessed_date", "archive_url", "currency", "models")
        if key in root
    ]
    if list(root) != expected_order:
        raise PaygSchemaError("PAYG top-level keys present in wrong order")
    source_url = _https(root["source_url"], "source_url")
    accessed_date = _date(root["accessed_date"])
    archive_value = root.get("archive_url")
    archive_url = (
        None if archive_value is None else _https(archive_value, "archive_url")
    )
    if root["currency"] != "USD":
        raise PaygSchemaError("currency must be 'USD'")
    models_raw = _mapping(root["models"], "models")
    unknown_models = set(models_raw) - {"gpt-4o", "gpt-5.2"}
    if unknown_models:
        raise PaygSchemaError(
            f"unknown legacy PAYG models: {sorted(unknown_models)}"
        )
    models: dict[str, Gpt4oRates | Gpt52Rates] = {}
    for family, rates_value in models_raw.items():
        if family == "gpt-4o":
            expected_keys = {
                "input_per_1m_usd",
                "cached_input_per_1m_usd",
                "output_per_1m_usd",
            }
        else:
            expected_keys = {
                "input_per_1m_usd",
                "cached_input_per_1m_usd",
                "reasoning_per_1m_usd",
                "output_per_1m_usd",
            }
        rates_raw = _mapping(rates_value, f"models.{family}")
        if set(rates_raw) != expected_keys:
            raise PaygSchemaError(f"models.{family} rates keys mismatch")
        if family == "gpt-5.2":
            models[family] = Gpt52Rates(
                input_per_1m_usd=_rate(
                    rates_raw["input_per_1m_usd"], f"models.{family}.input"
                ),
                cached_input_per_1m_usd=_rate(
                    rates_raw["cached_input_per_1m_usd"],
                    f"models.{family}.cached_input",
                ),
                reasoning_per_1m_usd=_rate(
                    rates_raw["reasoning_per_1m_usd"], f"models.{family}.reasoning"
                ),
                output_per_1m_usd=_rate(
                    rates_raw["output_per_1m_usd"], f"models.{family}.output"
                ),
            )
        else:
            models[family] = Gpt4oRates(
                input_per_1m_usd=_rate(
                    rates_raw["input_per_1m_usd"], f"models.{family}.input"
                ),
                cached_input_per_1m_usd=_rate(
                    rates_raw["cached_input_per_1m_usd"],
                    f"models.{family}.cached_input",
                ),
                output_per_1m_usd=_rate(
                    rates_raw["output_per_1m_usd"], f"models.{family}.output"
                ),
            )
    return PaygPricing(
        source_url=source_url,
        accessed_date=accessed_date,
        archive_url=archive_url,
        currency="USD",
        models=models,
        snapshot_path=str(snapshot_path),
        snapshot_sha256=hashlib.sha256(raw).hexdigest(),
    )


def load_payg_pricing(path: str | Path) -> PaygPricing:
    """Parse one immutable snapshot without consulting the wall clock."""
    snapshot_path = Path(path)
    try:
        raw = snapshot_path.read_bytes()
    except OSError:
        raise FileNotFoundError(f"PAYG pricing file not found: {snapshot_path}") from None
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        raise PaygSchemaError("PAYG snapshot is not valid YAML") from None
    root = _mapping(data, "PAYG snapshot root")
    if "schema_version" not in root:
        return _load_legacy_payg(root, raw=raw, snapshot_path=snapshot_path)
    required_keys = set(_TOP_LEVEL_ORDER) - {"archive_url"}
    if set(root) not in {frozenset(required_keys), frozenset(_TOP_LEVEL_ORDER)}:
        raise PaygSchemaError("PAYG top-level keys mismatch")
    expected_order = [key for key in _TOP_LEVEL_ORDER if key in root]
    if list(root) != expected_order:
        raise PaygSchemaError("PAYG top-level keys present in wrong order")
    if root["schema_version"] != SNAPSHOT_SCHEMA_VERSION:
        raise PaygSchemaError("unknown PAYG snapshot schema_version")
    snapshot_id = _text(root["snapshot_id"], "snapshot_id")
    source_url = _https(root["source_url"], "source_url")
    accessed_date = _date(root["accessed_date"])
    archive_value = root.get("archive_url")
    archive_url = (
        None if archive_value is None else _https(archive_value, "archive_url")
    )
    if root["currency"] != "USD":
        raise PaygSchemaError("currency must be 'USD'")
    policy = _mapping(root["selection_policy"], "selection_policy")
    if policy != {
        "mode": "deterministic-pinned",
        "freshness_policy": "not-applied",
    }:
        raise PaygSchemaError("selection_policy must declare deterministic pinned selection")
    records_raw = _mapping(root["records"], "records")
    if not records_raw:
        raise PaygSchemaError("records must not be empty")

    records: dict[str, AzurePriceRecord] = {}
    models: dict[str, Gpt4oRates | Gpt52Rates] = {}
    for key, raw_record in records_raw.items():
        price_key = _text(key, "records key")
        record = _mapping(raw_record, f"records.{price_key}")
        if set(record) != _RECORD_KEYS:
            raise PaygSchemaError(f"records.{price_key} keys mismatch")
        if record["price_key"] != price_key:
            raise PaygSchemaError(f"records.{price_key}.price_key must match its key")
        family = _text(record["model_family"], f"records.{price_key}.model_family")
        version = _text(record["model_version"], f"records.{price_key}.model_version")
        geography = _text(record["geography"], f"records.{price_key}.geography")
        region = _text(record["region"], f"records.{price_key}.region")
        deployment_type = _text(
            record["deployment_type"], f"records.{price_key}.deployment_type"
        )
        if deployment_type not in _DEPLOYMENT_TYPES:
            raise PaygSchemaError(f"records.{price_key}.deployment_type is unsupported")
        sku = _text(record["sku"], f"records.{price_key}.sku")
        meters_raw = _mapping(record["meters"], f"records.{price_key}.meters")
        if set(meters_raw) != _METER_KEYS:
            raise PaygSchemaError(f"records.{price_key}.meters keys mismatch")
        meters = {
            name: _text(value, f"records.{price_key}.meters.{name}")
            for name, value in meters_raw.items()
        }
        rates_raw = _mapping(record["rates"], f"records.{price_key}.rates")
        common_rates = {
            "input_per_1m_usd",
            "cached_input_per_1m_usd",
            "output_per_1m_usd",
        }
        expected_rates = (
            common_rates | {"reasoning_per_1m_usd"}
            if family == "gpt-5.2"
            else common_rates
        )
        if family not in {"gpt-4o", "gpt-5.2"} or set(rates_raw) != expected_rates:
            raise PaygSchemaError(f"records.{price_key}.rates keys mismatch")
        if family == "gpt-5.2":
            rates: Gpt4oRates | Gpt52Rates = Gpt52Rates(
                input_per_1m_usd=_rate(
                    rates_raw["input_per_1m_usd"], f"records.{price_key}.rates.input"
                ),
                cached_input_per_1m_usd=_rate(
                    rates_raw["cached_input_per_1m_usd"],
                    f"records.{price_key}.rates.cached_input",
                ),
                reasoning_per_1m_usd=_rate(
                    rates_raw["reasoning_per_1m_usd"],
                    f"records.{price_key}.rates.reasoning",
                ),
                output_per_1m_usd=_rate(
                    rates_raw["output_per_1m_usd"], f"records.{price_key}.rates.output"
                ),
            )
        else:
            rates = Gpt4oRates(
                input_per_1m_usd=_rate(
                    rates_raw["input_per_1m_usd"], f"records.{price_key}.rates.input"
                ),
                cached_input_per_1m_usd=_rate(
                    rates_raw["cached_input_per_1m_usd"],
                    f"records.{price_key}.rates.cached_input",
                ),
                output_per_1m_usd=_rate(
                    rates_raw["output_per_1m_usd"], f"records.{price_key}.rates.output"
                ),
            )
        if family in models:
            raise PaygSchemaError("each model family must have exactly one snapshot record")
        models[family] = rates
        records[price_key] = AzurePriceRecord(
            model_family=family,
            model_version=version,
            geography=geography,
            region=region,
            deployment_type=deployment_type,
            sku=sku,
            price_key=price_key,
            meters=meters,
            rates=rates,
        )

    return PaygPricing(
        source_url=source_url,
        accessed_date=accessed_date,
        archive_url=archive_url,
        currency="USD",
        models=models,
        snapshot_path=str(snapshot_path),
        snapshot_id=snapshot_id,
        snapshot_sha256=hashlib.sha256(raw).hexdigest(),
        selection_policy={str(k): str(v) for k, v in policy.items()},
        records=records,
    )


def _safe_snapshot_path(value: str) -> str:
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or "\\" in value:
        raise PricingSelectionError("pricing snapshot path must be repository-relative")
    return pure.as_posix()


def _packaged_snapshot_bytes() -> bytes:
    return (
        resources.files("batch_runner.data")
        .joinpath(PACKAGED_PAYG_RESOURCE)
        .read_bytes()
    )


def resolve_pinned_payg_snapshot(
    *, snapshot_id: str, snapshot_path: str, snapshot_sha256: str
) -> PaygPricing:
    """Resolve the one supported snapshot from source or packaged bytes."""
    normalized_path = _safe_snapshot_path(snapshot_path)
    if snapshot_id != CANONICAL_PAYG_SNAPSHOT_ID:
        raise PricingSelectionError("unknown pricing snapshot ID")
    if normalized_path != CANONICAL_PAYG_SNAPSHOT_PATH:
        raise PricingSelectionError("pricing snapshot path does not match snapshot ID")
    if not _SHA256_RE.fullmatch(snapshot_sha256):
        raise PricingSelectionError("pricing snapshot SHA-256 is invalid")

    repository_candidate = Path(__file__).resolve().parents[1] / normalized_path
    if repository_candidate.is_file():
        candidate = repository_candidate
    else:
        package_file = resources.files("batch_runner.data").joinpath(
            PACKAGED_PAYG_RESOURCE
        )
        with resources.as_file(package_file) as extracted:
            candidate = Path(extracted)
            parsed = load_payg_pricing(candidate)
        if parsed.snapshot_sha256 != snapshot_sha256:
            raise PricingSelectionError("packaged pricing snapshot SHA-256 mismatch")
        if parsed.snapshot_id != snapshot_id:
            raise PricingSelectionError("packaged pricing snapshot ID mismatch")
        return parsed

    parsed = load_payg_pricing(candidate)
    if parsed.snapshot_sha256 != snapshot_sha256:
        raise PricingSelectionError("pricing snapshot SHA-256 mismatch")
    if parsed.snapshot_id != snapshot_id:
        raise PricingSelectionError("pricing snapshot ID mismatch")
    if _packaged_snapshot_bytes() != candidate.read_bytes():
        raise PricingSelectionError("packaged pricing snapshot parity mismatch")
    return parsed


def select_price_record(
    snapshot: PaygPricing,
    *,
    price_key: str,
    model_family: str,
    model_version: str,
    geography: str,
    region: str,
    deployment_type: str,
    currency: str,
) -> PricingSelection:
    """Select one exact record; every safe ledger dimension must match."""
    try:
        record = snapshot.records[price_key]
    except KeyError:
        raise PricingSelectionError("pricing price_key is not present in the snapshot") from None
    expected = {
        "model_family": model_family,
        "model_version": model_version,
        "geography": geography,
        "region": region,
        "deployment_type": deployment_type,
        "currency": currency,
        "price_key": price_key,
    }
    actual = {
        "model_family": record.model_family,
        "model_version": record.model_version,
        "geography": record.geography,
        "region": record.region,
        "deployment_type": record.deployment_type,
        "currency": snapshot.currency,
        "price_key": record.price_key,
    }
    mismatches = [name for name in expected if expected[name] != actual[name]]
    if mismatches:
        raise PricingSelectionError(
            "pricing selection mismatch: " + ", ".join(sorted(mismatches))
        )
    return PricingSelection(snapshot=snapshot, record=record)


__all__ = [
    "AzurePriceRecord",
    "CANONICAL_PAYG_SNAPSHOT_ID",
    "CANONICAL_PAYG_SNAPSHOT_PATH",
    "Gpt4oRates",
    "Gpt52Rates",
    "PaygPricing",
    "PaygSchemaError",
    "PricingSelection",
    "PricingSelectionError",
    "load_payg_pricing",
    "resolve_pinned_payg_snapshot",
    "select_price_record",
]
