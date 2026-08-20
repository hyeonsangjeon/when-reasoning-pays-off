"""Model PTU density and output-weight lookup for Task 027.

The data source is the frozen ``pricing/ptu-density-2026-05.yaml``
snapshot. This module performs only local file parsing and arithmetic;
it does not import any SDK client or contact external services.
"""

from __future__ import annotations

import math
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DENSITY_PATH = _REPO_ROOT / "pricing" / "ptu-density-2026-05.yaml"

RatioProvenance = Literal["official_spec", "operational_inference", "unspecified"]


class ModelDensityError(ValueError):
    """Raised when the model-density YAML is malformed."""


class UnknownModelError(KeyError):
    """Raised when a model is absent from the density table."""


class UnknownOutputRatioError(KeyError):
    """Raised when a known model has no usable output-weight ratio."""


@dataclass(frozen=True)
class ModelDensitySnapshot:
    """Parsed immutable view of the Task 027 model-density snapshot."""

    path: Path
    guide_publication_date: str
    guide_source: str
    learn_url: str
    access_date: str | None
    input_tpm_per_ptu: Mapping[str, int]
    output_to_input_token_ratio: Mapping[str, float]
    output_ratio_provenance: Mapping[str, RatioProvenance]


def _require_mapping(value: object, *, label: str) -> MappingABC:
    if not isinstance(value, MappingABC):
        raise ModelDensityError(f"{label} must be a mapping")
    return value


def _require_positive_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelDensityError(f"{label} must be an integer")
    if value <= 0 or value > 2_147_483_647:
        raise ModelDensityError(f"{label} must be > 0")
    return value


def _require_positive_ratio(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelDensityError(f"{label} must be a number")
    try:
        ratio = float(value)
    except (OverflowError, ValueError) as exc:
        raise ModelDensityError(f"{label} must be finite and > 0") from exc
    if not math.isfinite(ratio) or ratio <= 0 or ratio > 1_000_000.0:
        raise ModelDensityError(f"{label} must be finite and > 0")
    return ratio


def _require_nonempty_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelDensityError(f"{label} must be a non-empty string")
    return value


def _parse_input_tpm(raw: object) -> dict[str, int]:
    block = _require_mapping(raw, label="input_tpm_per_ptu")
    parsed: dict[str, int] = {}
    for model_id, value in block.items():
        model = _require_nonempty_string(model_id, label="input_tpm_per_ptu model")
        parsed[model] = _require_positive_int(
            value,
            label=f"input_tpm_per_ptu[{model!r}]",
        )
    if not parsed:
        raise ModelDensityError("input_tpm_per_ptu must not be empty")
    return parsed


def _parse_output_ratios(
    raw: object,
    *,
    known_models: set[str],
) -> tuple[dict[str, float], dict[str, RatioProvenance]]:
    block = _require_mapping(raw, label="output_to_input_token_ratio")
    ratios: dict[str, float] = {}
    provenance: dict[str, RatioProvenance] = {}

    # The task's minimal schema is flat. The checked-in snapshot keeps the
    # same values grouped by provenance so operational inference is machine
    # readable. Support both shapes.
    flat_shape = all(
        not isinstance(value, (MappingABC, list, tuple, set))
        for value in block.values()
    )
    if flat_shape:
        for model_id, value in block.items():
            model = _require_nonempty_string(
                model_id,
                label="output_to_input_token_ratio model",
            )
            ratios[model] = _require_positive_ratio(
                value,
                label=f"output_to_input_token_ratio[{model!r}]",
            )
            provenance[model] = "official_spec"
    else:
        explicit = _require_mapping(block.get("explicit", {}), label="explicit")
        for model_id, value in explicit.items():
            model = _require_nonempty_string(model_id, label="explicit model")
            ratios[model] = _require_positive_ratio(value, label=f"explicit[{model!r}]")
            provenance[model] = "official_spec"

        inferred = _require_mapping(
            block.get("operational_inference", {}),
            label="operational_inference",
        )
        for model_id, value in inferred.items():
            model = _require_nonempty_string(
                model_id,
                label="operational_inference model",
            )
            ratios[model] = _require_positive_ratio(
                value,
                label=f"operational_inference[{model!r}]",
            )
            provenance[model] = "operational_inference"

        unspecified = block.get("unspecified", [])
        if not isinstance(unspecified, list):
            raise ModelDensityError("unspecified must be a list")
        for model_id in unspecified:
            model = _require_nonempty_string(model_id, label="unspecified model")
            ratios[model] = 1.0
            provenance[model] = "unspecified"

    for model in ratios:
        if model not in known_models:
            raise ModelDensityError(
                f"output_to_input_token_ratio contains unknown model {model!r}"
            )
    if not ratios:
        raise ModelDensityError("output_to_input_token_ratio must not be empty")
    return ratios, provenance


def load_density_snapshot(path: str | Path = DEFAULT_DENSITY_PATH) -> ModelDensitySnapshot:
    """Load and validate a local model-density YAML snapshot."""

    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    root = _require_mapping(data, label="density YAML root")

    guide_publication_date = _require_nonempty_string(
        root.get("guide_publication_date"),
        label="guide_publication_date",
    )
    guide_source = _require_nonempty_string(root.get("guide_source"), label="guide_source")
    learn_url = _require_nonempty_string(root.get("learn_url"), label="learn_url")
    if not learn_url.startswith("https://"):
        raise ModelDensityError("learn_url must be an https URL")
    access_date_raw = root.get("access_date")
    if access_date_raw is None:
        access_date = None
    else:
        access_date = _require_nonempty_string(access_date_raw, label="access_date")

    input_tpm = _parse_input_tpm(root.get("input_tpm_per_ptu"))
    ratios, provenance = _parse_output_ratios(
        root.get("output_to_input_token_ratio"),
        known_models=set(input_tpm),
    )

    return ModelDensitySnapshot(
        path=p,
        guide_publication_date=guide_publication_date,
        guide_source=guide_source,
        learn_url=learn_url,
        access_date=access_date,
        input_tpm_per_ptu=MappingProxyType(dict(input_tpm)),
        output_to_input_token_ratio=MappingProxyType(dict(ratios)),
        output_ratio_provenance=MappingProxyType(dict(provenance)),
    )


def input_tpm_per_ptu(
    model_id: str,
    *,
    density_yaml: str | Path = DEFAULT_DENSITY_PATH,
) -> int:
    """Return Guide §3 input TPM per PTU for ``model_id``."""

    snapshot = load_density_snapshot(density_yaml)
    try:
        return snapshot.input_tpm_per_ptu[model_id]
    except KeyError as exc:
        raise UnknownModelError(model_id) from exc


def output_to_input_ratio(
    model_id: str,
    *,
    density_yaml: str | Path = DEFAULT_DENSITY_PATH,
) -> float:
    """Return output-token weight in input-token equivalents.

    ``gpt-4o`` is explicitly marked as unspecified in the snapshot, so the
    deterministic calculator uses ``1.0`` and exposes that provenance through
    :func:`output_ratio_provenance`.
    """

    snapshot = load_density_snapshot(density_yaml)
    if model_id not in snapshot.input_tpm_per_ptu:
        raise UnknownModelError(model_id)
    try:
        return snapshot.output_to_input_token_ratio[model_id]
    except KeyError as exc:
        raise UnknownOutputRatioError(model_id) from exc


def output_ratio_provenance(
    model_id: str,
    *,
    density_yaml: str | Path = DEFAULT_DENSITY_PATH,
) -> RatioProvenance:
    """Return the provenance label for ``model_id``'s output ratio."""

    snapshot = load_density_snapshot(density_yaml)
    if model_id not in snapshot.input_tpm_per_ptu:
        raise UnknownModelError(model_id)
    try:
        return snapshot.output_ratio_provenance[model_id]
    except KeyError as exc:
        raise UnknownOutputRatioError(model_id) from exc


__all__ = [
    "DEFAULT_DENSITY_PATH",
    "ModelDensityError",
    "ModelDensitySnapshot",
    "RatioProvenance",
    "UnknownModelError",
    "UnknownOutputRatioError",
    "input_tpm_per_ptu",
    "load_density_snapshot",
    "output_ratio_provenance",
    "output_to_input_ratio",
]
