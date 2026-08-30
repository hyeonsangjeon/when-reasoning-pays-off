"""Normalized, versioned execution plan for ``experiment run --stage dry-run``.

A dry-run never opens a socket, never resolves a credential/token/endpoint, and
never calls a provider. Instead it writes an immutable *execution plan* — the
same DATA -> IN -> EXECUTE -> OUT view the read-only catalog renders, but
grounded in the actual on-disk config and input bytes, plus the typed adapter
that would run it and the bounded knobs it would sweep.

The plan is a strict, ``extra="forbid"`` Pydantic document (so an unexpected
field fails closed) with a stable :data:`PLAN_SCHEMA_VERSION`. Every path it
records is repo-relative; no absolute host path is ever embedded. The two most
important safety fields are ``network_calls`` and ``billed_calls``, both pinned
to ``0`` for a dry-run and asserted by the test-suite.

Publication is atomic and immutable: the plan id is deterministic from the
config bytes + adapter + stage, so re-planning the same experiment yields the
same id, and a second publication into a directory that already holds that id is
*refused*, never overwritten.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from batch_runner import __version__
from batch_runner.experiment.adapters import (
    ADAPTER_REGISTRY_VERSION,
    ExperimentAdapter,
)
from batch_runner.experiment.catalog import CATALOG_SCHEMA_VERSION
from batch_runner.experiment.locking import (
    OWNED_MARKER_BYTES,
    OWNED_MARKER_NAME,
    valid_owned_marker,
)

PLAN_SCHEMA_VERSION = "1.0.0"

# Repo-relative subtrees a plan must never be written into: committed evidence
# corpora and published results/blog. The dispatcher owns its output elsewhere.
PROTECTED_SUBTREES = ("benchmarks", "results", "docs/blog")

_SHA256_RE = r"^[0-9a-f]{64}$"
_KNOB_LIST_CAP = 64


class PlanConflictError(RuntimeError):
    """A plan directory already exists (immutable) or the target is unsafe."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)


class PlanGeneratedBy(_StrictModel):
    tool: Literal["reasoning-payoff"]
    package_version: str = Field(min_length=1, max_length=64)
    adapter_registry_version: str = Field(min_length=1, max_length=32)
    catalog_schema_version: str = Field(min_length=1, max_length=32)


class PlanIdentity(_StrictModel):
    experiment_id: str = Field(min_length=1, max_length=200)
    config_path: str = Field(min_length=1, max_length=400)
    config_sha256: str = Field(pattern=_SHA256_RE)
    family: str = Field(min_length=1, max_length=16)
    read_benchmark: str = Field(min_length=1, max_length=120)


class PlanAdapter(_StrictModel):
    id: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=32)
    source_module: str = Field(min_length=1, max_length=200)
    supports_live: bool
    live_kind: Literal["azure-billed", "offline-simulation"]
    pricing_policy_aware: bool


class PlanInputFile(_StrictModel):
    path: str = Field(min_length=1, max_length=400)
    format: str = Field(min_length=1, max_length=40)
    shape: str = Field(min_length=1, max_length=200)
    present: bool
    sha256: str | None = Field(default=None, pattern=_SHA256_RE)


class PlanData(_StrictModel):
    inputs: list[PlanInputFile]


class PlanIn(_StrictModel):
    provider: Literal["azure"]
    model_family: str = Field(min_length=1, max_length=60)
    endpoint_env: str = Field(min_length=1, max_length=120)
    auth_mode: str = Field(min_length=1, max_length=40)
    auth_mode_env: str = Field(min_length=1, max_length=120)
    audience: str = Field(min_length=1, max_length=200)
    credentials_resolved: Literal[False]
    endpoint_resolved: Literal[False]


class PlanPricing(_StrictModel):
    applicable: bool
    policy: str = Field(min_length=1, max_length=40)
    snapshot_path: str | None = Field(default=None, max_length=400)
    snapshot_sha256: str | None = Field(default=None, pattern=_SHA256_RE)


class PlanKnobs(_StrictModel):
    variable: str = Field(min_length=1, max_length=400)
    bounded: dict[str, Any]


class PlanOutputs(_StrictModel):
    output_dir: str = Field(min_length=1, max_length=400)
    artifacts: list[str]


class PlanCommandScope(_StrictModel):
    adapter_argv: list[str]
    reference_invocation: str = Field(min_length=1, max_length=600)
    executed: Literal[False]


class ExecutionPlan(_StrictModel):
    schema_version: Literal["1.0.0"]
    plan_id: str = Field(min_length=1, max_length=200)
    stage: Literal["dry-run"]
    network_calls: Literal[0]
    billed_calls: Literal[0]
    generated_by: PlanGeneratedBy
    identity: PlanIdentity
    adapter: PlanAdapter
    data: PlanData
    input: PlanIn
    pricing: PlanPricing
    knobs: PlanKnobs
    outputs: PlanOutputs
    command_scope: PlanCommandScope


# ---------------------------------------------------------------------------
# Bounded-knob extraction
# ---------------------------------------------------------------------------
def _bounded_scalar(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        return value[:200]
    return None


def _bounded_list(value: Any) -> list[Any] | None:
    if not isinstance(value, list):
        return None
    out: list[Any] = []
    for item in value[:_KNOB_LIST_CAP]:
        scalar = _bounded_scalar(item)
        if scalar is not None:
            out.append(scalar)
    return out


def _extract_bounded_knobs(config: dict[str, Any]) -> dict[str, Any]:
    """Pull a small, capped set of well-known knobs from any family's YAML.

    The keys differ across the five runner families (``call_params`` vs
    ``request_template``, ``sweep.effort`` vs ``sweep.bucket_cardinality``,
    ``policy.type`` vs ``deployment.tpm``). This reads only known keys, coerces
    to safe scalars, and caps list length, so no unbounded or attacker-shaped
    value lands in the plan.
    """
    knobs: dict[str, Any] = {}

    for size_key in ("dataset_size", "repeats", "concurrency", "corpus_seed"):
        if size_key in config:
            scalar = _bounded_scalar(config[size_key])
            if scalar is not None:
                knobs[size_key] = scalar

    call_params = config.get("call_params")
    if isinstance(call_params, dict) and "max_output_tokens" in call_params:
        scalar = _bounded_scalar(call_params["max_output_tokens"])
        if scalar is not None:
            knobs["max_output_tokens"] = scalar

    request_template = config.get("request_template")
    if isinstance(request_template, dict):
        for rt_key in ("max_output_tokens", "prompt_cache_retention"):
            if rt_key in request_template:
                scalar = _bounded_scalar(request_template[rt_key])
                if scalar is not None:
                    knobs[rt_key] = scalar

    sweep = config.get("sweep")
    if isinstance(sweep, dict):
        for sweep_key in ("effort", "bucket_cardinality", "max_output_tokens"):
            if sweep_key in sweep:
                as_list = _bounded_list(sweep[sweep_key])
                if as_list is not None:
                    knobs[f"sweep_{sweep_key}"] = as_list

    policy = config.get("policy")
    if isinstance(policy, dict) and "type" in policy:
        scalar = _bounded_scalar(policy["type"])
        if scalar is not None:
            knobs["policy_type"] = scalar
    elif isinstance(config.get("policy_type"), str):
        knobs["policy_type"] = config["policy_type"][:200]

    return knobs


# ---------------------------------------------------------------------------
# Pricing identity
# ---------------------------------------------------------------------------
def _pricing_identity(
    adapter: ExperimentAdapter, config: dict[str, Any], repo_root: Path
) -> PlanPricing:
    if not adapter.pricing_policy_aware:
        return PlanPricing(applicable=False, policy="not-applicable")
    # Dry-run always uses the offline-only historical-replay policy so no fresh
    # pricing (a live concern) is required and no live call can be initiated.
    snapshot_rel = config.get("pricing_snapshot_path")
    snapshot_path: str | None = None
    snapshot_sha: str | None = None
    if isinstance(snapshot_rel, str) and snapshot_rel:
        snapshot_path = snapshot_rel
        candidate = (repo_root / snapshot_rel).resolve()
        try:
            if candidate.is_file() and repo_root in candidate.parents:
                snapshot_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            snapshot_sha = None
    return PlanPricing(
        applicable=True,
        policy="historical-replay",
        snapshot_path=snapshot_path,
        snapshot_sha256=snapshot_sha,
    )


# ---------------------------------------------------------------------------
# Input hashing
# ---------------------------------------------------------------------------
def _hash_input_files(
    entry: dict[str, Any], repo_root: Path
) -> list[PlanInputFile]:
    inputs: list[PlanInputFile] = []
    for f in entry.get("data", {}).get("files", []):
        rel = str(f["path"])
        fmt = str(f.get("format", "unknown"))
        shape = str(f.get("top_level", "(text)"))
        candidate = (repo_root / rel).resolve()
        present = False
        sha: str | None = None
        try:
            # Confinement: only hash a real file that lives under the repo root.
            if candidate.is_file() and repo_root in candidate.parents:
                present = True
                sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            present = False
            sha = None
        inputs.append(
            PlanInputFile(
                path=rel, format=fmt, shape=shape, present=present, sha256=sha
            )
        )
    return inputs


# ---------------------------------------------------------------------------
# Plan id + build
# ---------------------------------------------------------------------------
def _plan_id(config_sha256: str, adapter_id: str, stage: str) -> str:
    """Deterministic, immutable plan id from config bytes + adapter + stage."""
    digest = hashlib.sha256(
        f"{stage}\n{adapter_id}\n{config_sha256}".encode("utf-8")
    ).hexdigest()
    return f"plan-{stage}-{config_sha256[:8]}-{digest[:8]}"


def build_plan(
    *,
    entry: dict[str, Any],
    config: dict[str, Any],
    config_bytes: bytes,
    adapter: ExperimentAdapter,
    repo_root: Path,
    stage: str = "dry-run",
) -> ExecutionPlan:
    """Build a normalized :class:`ExecutionPlan` with no side effect.

    Args:
        entry: The derived catalog entry (DATA/IN/EXECUTE/OUT dict).
        config: The parsed experiment YAML as a mapping.
        config_bytes: Raw YAML bytes (hashed for identity + plan id).
        adapter: The typed adapter that owns this experiment.
        repo_root: Source-checkout root, used only to hash repo-relative inputs.
        stage: Only ``"dry-run"`` produces a plan.
    """
    config_sha = hashlib.sha256(config_bytes).hexdigest()
    in_stage = entry["in"]
    plan = ExecutionPlan(
        schema_version=PLAN_SCHEMA_VERSION,
        plan_id=_plan_id(config_sha, adapter.adapter_id, stage),
        stage="dry-run",
        network_calls=0,
        billed_calls=0,
        generated_by=PlanGeneratedBy(
            tool="reasoning-payoff",
            package_version=__version__,
            adapter_registry_version=ADAPTER_REGISTRY_VERSION,
            catalog_schema_version=CATALOG_SCHEMA_VERSION,
        ),
        identity=PlanIdentity(
            experiment_id=str(entry["experiment_id"]),
            config_path=str(entry["config_path"]),
            config_sha256=config_sha,
            family=str(entry["family"]),
            read_benchmark=str(entry["read_benchmark"]),
        ),
        adapter=PlanAdapter(
            id=adapter.adapter_id,
            version=adapter.version,
            source_module=adapter.source_module,
            supports_live=adapter.supports_live,
            live_kind=adapter.live_kind,
            pricing_policy_aware=adapter.pricing_policy_aware,
        ),
        data=PlanData(inputs=_hash_input_files(entry, repo_root)),
        input=PlanIn(
            provider="azure",
            model_family=str(in_stage["model"]),
            endpoint_env=str(in_stage["endpoint_env"]),
            auth_mode=str(in_stage["auth_mode"]),
            auth_mode_env=str(in_stage["auth_mode_env"]),
            audience=str(in_stage["audience"]),
            credentials_resolved=False,
            endpoint_resolved=False,
        ),
        pricing=_pricing_identity(adapter, config, repo_root),
        knobs=PlanKnobs(
            variable=str(entry["variable"]),
            bounded=_extract_bounded_knobs(config),
        ),
        outputs=PlanOutputs(
            output_dir=str(entry["out"]["output_dir"]),
            artifacts=[str(a) for a in entry["out"]["artifacts"]],
        ),
        command_scope=PlanCommandScope(
            adapter_argv=adapter.dry_run_argv(str(entry["config_path"])),
            reference_invocation=str(entry["execute"]["command"]),
            executed=False,
        ),
    )
    return plan


def plan_to_json(plan: ExecutionPlan) -> str:
    """Serialize a plan to stable, ASCII, sorted-key JSON with a trailing NL."""
    payload = plan.model_dump(mode="json")
    return json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# Atomic, immutable publication
# ---------------------------------------------------------------------------
def _reject_protected_tree(out_dir: Path, repo_root: Path) -> None:
    try:
        rel = out_dir.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return  # Output is outside the repo entirely; always allowed.
    parts = rel.parts
    for protected in PROTECTED_SUBTREES:
        prot_parts = tuple(protected.split("/"))
        if parts[: len(prot_parts)] == prot_parts:
            raise PlanConflictError(
                "refusing to write an execution plan inside a protected tree "
                f"({protected}); choose an output directory outside it"
            )


def _ensure_owned_plan_root(root: Path) -> None:
    if root.is_symlink():
        raise PlanConflictError("plan output path is not a real directory")
    if root.exists():
        if not root.is_dir():
            raise PlanConflictError("plan output path is not a real directory")
        if not valid_owned_marker(root) and any(root.iterdir()):
            raise PlanConflictError(
                "plan output directory exists and is not owned by this tool"
            )
    else:
        root.mkdir(parents=True, exist_ok=False, mode=0o700)
    marker = root / OWNED_MARKER_NAME
    if not valid_owned_marker(root):
        with marker.open("xb") as handle:
            handle.write(OWNED_MARKER_BYTES)
            handle.flush()
            os.fsync(handle.fileno())


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":  # pragma: no cover - platform specific
        if directory.is_symlink() or not directory.is_dir():
            raise PlanConflictError("plan output parent is not a safe directory")
        return
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_plan(plan: ExecutionPlan, out_dir: Path, *, repo_root: Path) -> Path:
    """Atomically publish ``plan`` into an owned, immutable directory.

    Returns the repo-relative-safe path to the written ``plan.json``.

    Raises:
        PlanConflictError: The target sits in a protected tree, the output root
            is unowned, or a plan with this immutable id already exists (no
            overwrite).
    """
    out_dir = out_dir.resolve()
    _reject_protected_tree(out_dir, repo_root)
    _ensure_owned_plan_root(out_dir)

    plans_root = out_dir / "plans"
    _ensure_owned_plan_root(plans_root)

    final = plans_root / plan.plan_id
    if final.exists() or final.is_symlink():
        raise PlanConflictError(
            f"plan {plan.plan_id!r} already exists; plans are immutable and are "
            "never overwritten"
        )

    text = plan_to_json(plan)
    stage: Path | None = None
    for _ in range(8):
        candidate = plans_root / f".{plan.plan_id}.staging-{secrets.token_hex(8)}"
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:  # pragma: no cover - name collision is rare
            continue
        stage = candidate
        break
    if stage is None:  # pragma: no cover - exhausting 8 names is infeasible
        raise PlanConflictError("could not reserve a staging directory for the plan")

    try:
        with (stage / OWNED_MARKER_NAME).open("xb") as handle:
            handle.write(OWNED_MARKER_BYTES)
            handle.flush()
            os.fsync(handle.fileno())
        with (stage / "plan.json").open("wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        if final.exists() or final.is_symlink():
            raise PlanConflictError(
                f"plan {plan.plan_id!r} already exists; refusing to overwrite"
            )
        os.replace(stage, final)
        stage = None
        _fsync_directory(plans_root)
    finally:
        if stage is not None and stage.exists():
            import shutil  # noqa: PLC0415 - cleanup-only, rare path

            shutil.rmtree(stage, ignore_errors=True)
    return final / "plan.json"


__all__ = [
    "ExecutionPlan",
    "PLAN_SCHEMA_VERSION",
    "PROTECTED_SUBTREES",
    "PlanConflictError",
    "build_plan",
    "plan_to_json",
    "publish_plan",
]
