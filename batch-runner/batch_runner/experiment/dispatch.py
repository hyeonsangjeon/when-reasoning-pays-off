"""The ``reasoning-payoff experiment run`` dispatcher.

One safe entry point for all 20 catalogued experiments. It resolves an
experiment id (or config filename) to exactly one catalog entry, looks up the
typed adapter that owns it, and then either:

* ``--stage dry-run`` — builds and atomically publishes a normalized, immutable
  :class:`~batch_runner.experiment.plan.ExecutionPlan`. This opens **no** socket,
  resolves **no** credential/token/endpoint, and calls **no** provider. It reads
  only committed bytes (the config YAML and, when present, the input corpora it
  hashes) and validates the config through the runner's own strict loader.

* ``--stage live --confirm-cost`` — delegates to the already-validated runner via
  the clone-only ``experiments.run(...)`` interface, which enforces every live
  guard (CI hard refusal, budget/cost gates, secret redaction, ``store=false``,
  ``max_retries=0``, output locks, campaign gates). The dispatcher adds a CLI
  cost acknowledgement and refuses live for any adapter without a billed path.

Both stages require a **source checkout** (the ``experiments/`` configs and the
``scripts/`` runners are clone-only, never shipped in the wheel). Absent that,
the dispatcher fails with an actionable message and no absolute path leakage.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from batch_runner.experiment.adapters import (
    ExperimentAdapter,
    LiveUnsupportedError,
    get_adapter,
)
from batch_runner.experiment.catalog import find_entry, load_packaged_catalog
from batch_runner.experiment.plan import (
    ExecutionPlan,
    PlanConflictError,
    build_plan,
    publish_plan,
)

# Exit codes — aligned with the wider CLI taxonomy (see docs/20 §12).
EXIT_OK = 0
EXIT_INPUT = 3  # unknown/ambiguous id, or config failed the strict loader
EXIT_PLAN_CONFLICT = 5  # plan exists (immutable), protected tree, unowned output
EXIT_IO = 6
EXIT_COST = 7  # live not confirmed, or adapter has no billed live path
EXIT_SOURCE_MISSING = 8  # source checkout / runtime unavailable (actionable)


class DispatchError(RuntimeError):
    """Base class for dispatcher failures with a stable exit code."""

    exit_code = EXIT_IO

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ExperimentResolutionError(DispatchError):
    """The target matched zero or several experiments."""

    exit_code = EXIT_INPUT


class ConfigValidationError(DispatchError):
    """The config file is missing or failed the runner's strict loader."""

    exit_code = EXIT_INPUT


class SourceCheckoutMissingError(DispatchError):
    """The clone-only experiments/scripts source (or its runtime) is absent."""

    exit_code = EXIT_SOURCE_MISSING


class LiveNotConfirmedError(DispatchError):
    """A live stage was requested without the cost acknowledgement."""

    exit_code = EXIT_COST


class LiveNotSupportedError(DispatchError):
    """The resolved adapter has no billed live path."""

    exit_code = EXIT_COST


@dataclass(frozen=True)
class DryRunOutcome:
    experiment_id: str
    adapter_id: str
    plan_id: str
    plan_path: Path
    plan: ExecutionPlan


@dataclass(frozen=True)
class LiveOutcome:
    experiment_id: str
    adapter_id: str
    exit_code: int


# ---------------------------------------------------------------------------
# Source-checkout discovery (clone-only; never in the wheel)
# ---------------------------------------------------------------------------
def _looks_like_source_root(path: Path) -> bool:
    return (path / "experiments").is_dir() and (path / "scripts").is_dir()


def find_source_root(start: Path | None = None) -> Path | None:
    """Locate the source checkout that holds ``experiments/`` and ``scripts/``.

    Searches, in order: an explicit ``start`` and its parents, the current
    working directory and its parents, and the editable-install layout relative
    to this module. Returns ``None`` when no such root exists (wheel-only).
    """
    candidates: list[Path] = []
    if start is not None:
        candidates.append(start.resolve())
        candidates.extend(start.resolve().parents)
    cwd = Path.cwd().resolve()
    candidates.append(cwd)
    candidates.extend(cwd.parents)
    # Editable/source layout: <repo>/batch-runner/batch_runner/experiment/dispatch.py
    here = Path(__file__).resolve()
    if len(here.parents) >= 4:
        candidates.append(here.parents[3])
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if _looks_like_source_root(candidate):
            return candidate
    return None


def _require_source_root(start: Path | None = None) -> Path:
    root = find_source_root(start)
    if root is None:
        raise SourceCheckoutMissingError(
            "experiment run needs the source checkout (the experiments/ configs "
            "and scripts/ runners are not shipped in the wheel). Clone the "
            "repository and run this command from inside it. Read-only browsing "
            "with `experiment list` / `experiment describe` works from the wheel."
        )
    return root


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def resolve_entry(target: str) -> dict[str, Any]:
    """Resolve ``target`` to exactly one catalog entry (deterministic).

    Raises:
        ExperimentResolutionError: The target matched no experiment, or matched
            several by prefix (ambiguous). The message lists the candidates.
    """
    catalog = load_packaged_catalog()
    entry = find_entry(catalog, target)
    if entry is not None:
        return entry
    candidates = sorted(
        e["experiment_id"]
        for e in catalog["experiments"]
        if e["experiment_id"].startswith(target)
    )
    if len(candidates) == 1:
        resolved = find_entry(catalog, candidates[0])
        assert resolved is not None
        return resolved
    if candidates:
        joined = ", ".join(candidates)
        raise ExperimentResolutionError(
            f"{target!r} matches several experiments; pick one: {joined}"
        )
    raise ExperimentResolutionError(f"no experiment matches {target!r}")


def _adapter_for(entry: dict[str, Any]) -> ExperimentAdapter:
    adapter_block = entry.get("adapter")
    if not isinstance(adapter_block, dict) or not isinstance(
        adapter_block.get("id"), str
    ):
        raise ConfigValidationError(
            f"catalog entry {entry.get('experiment_id')!r} has no adapter identifier"
        )
    adapter_id = str(adapter_block["id"])
    if adapter_id != str(entry.get("runner_module")):
        raise ConfigValidationError(
            f"catalog entry {entry.get('experiment_id')!r} has inconsistent "
            "adapter and runner identifiers"
        )
    return get_adapter(adapter_id)


# ---------------------------------------------------------------------------
# Strict-loader validation (no env / no network)
# ---------------------------------------------------------------------------
def _validate_with_strict_loader(
    adapter: ExperimentAdapter, config_path: Path, repo_root: Path
) -> None:
    """Validate the config through the runner's own ``load_experiment``.

    The strict loaders parse YAML and enforce every documented config invariant
    without reading an environment variable or opening a socket. Import failure
    (a missing runtime dependency such as POSIX ``fcntl``, or an absent scripts
    package) is reported as a source/runtime problem, not a config error.
    """
    import sys  # noqa: PLC0415

    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        module = importlib.import_module(adapter.source_module)
    except ImportError as exc:
        raise SourceCheckoutMissingError(
            f"the runner {adapter.source_module!r} could not be imported on this "
            f"platform/runtime ({exc.__class__.__name__}); dry-run validation "
            "needs the source checkout and its base runtime"
        ) from None
    loader = getattr(module, "load_experiment", None)
    if loader is None:  # pragma: no cover - every runner exposes load_experiment
        raise SourceCheckoutMissingError(
            f"runner {adapter.source_module!r} exposes no strict loader"
        )
    try:
        loader(str(config_path))
    except FileNotFoundError as exc:
        raise ConfigValidationError(f"experiment config not found: {exc}") from None
    except ValueError as exc:
        raise ConfigValidationError(f"experiment config is invalid: {exc}") from None


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------
def dispatch_dry_run(
    target: str,
    *,
    out_dir: Path,
    source_root: Path | None = None,
) -> DryRunOutcome:
    """Build and atomically publish an immutable execution plan.

    Opens no socket, resolves no credential/endpoint, calls no provider.
    """
    import yaml  # noqa: PLC0415 - a pyyaml load, no network

    entry = resolve_entry(target)
    adapter = _adapter_for(entry)
    repo_root = _require_source_root(source_root)

    config_path = (repo_root / str(entry["config_path"])).resolve()
    if not config_path.is_file() or repo_root not in config_path.parents:
        raise ConfigValidationError(
            f"experiment config {entry['config_path']!r} is missing from the "
            "source checkout"
        )
    config_bytes = config_path.read_bytes()
    try:
        parsed = yaml.safe_load(config_bytes.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise ConfigValidationError(f"experiment config is not valid YAML: {exc}") from None
    config = parsed if isinstance(parsed, dict) else {}

    # Strict validation through the runner's own loader (still no env/network).
    _validate_with_strict_loader(adapter, config_path, repo_root)

    plan = build_plan(
        entry=entry,
        config=config,
        config_bytes=config_bytes,
        adapter=adapter,
        repo_root=repo_root,
        stage="dry-run",
    )
    plan_path = publish_plan(plan, out_dir, repo_root=repo_root)
    return DryRunOutcome(
        experiment_id=plan.identity.experiment_id,
        adapter_id=adapter.adapter_id,
        plan_id=plan.plan_id,
        plan_path=plan_path,
        plan=plan,
    )


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------
def preflight_live(
    target: str,
    *,
    confirm_cost: bool,
    source_root: Path | None = None,
) -> tuple[dict[str, Any], ExperimentAdapter, Path]:
    """Run every live guard that must pass *before* any side effect.

    Returns the resolved entry, adapter, and source root when live may proceed.

    Raises:
        LiveNotConfirmedError: ``--confirm-cost`` was not supplied.
        LiveNotSupportedError: The adapter has no billed live path.
        SourceCheckoutMissingError: The clone-only source/runtime is absent.
        ConfigValidationError: The config failed the strict loader.
    """
    entry = resolve_entry(target)
    adapter = _adapter_for(entry)
    if not confirm_cost:
        raise LiveNotConfirmedError(
            "live runs are billed Azure OpenAI calls; pass --confirm-cost to "
            "acknowledge cost. The experiment YAML's own budget confirmation "
            "still applies and is enforced by the runner."
        )
    try:
        # Refuse an unsupported/offline-only live path before touching source.
        adapter.live_argv(str(entry["config_path"]))
    except LiveUnsupportedError as exc:
        raise LiveNotSupportedError(str(exc)) from None
    repo_root = _require_source_root(source_root)
    config_path = (repo_root / str(entry["config_path"])).resolve()
    if not config_path.is_file() or repo_root not in config_path.parents:
        raise ConfigValidationError(
            f"experiment config {entry['config_path']!r} is missing from the "
            "source checkout"
        )
    _validate_with_strict_loader(adapter, config_path, repo_root)
    return entry, adapter, repo_root


def dispatch_live(
    target: str,
    *,
    confirm_cost: bool,
    source_root: Path | None = None,
    extra_args: Sequence[str] | None = None,
) -> LiveOutcome:
    """Delegate a live run directly through the registered typed adapter.

    The runner enforces every live guard (CI refusal, cost/budget, redaction,
    ``store=false``, ``max_retries=0``, locks, campaign gates). This never
    forwards the offline-only historical-replay pricing policy, so a historical
    replay can never initiate a live call.
    """
    entry, adapter, repo_root = preflight_live(
        target, confirm_cost=confirm_cost, source_root=source_root
    )
    import sys  # noqa: PLC0415

    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        module = importlib.import_module(adapter.source_module)
    except ImportError as exc:
        raise SourceCheckoutMissingError(
            f"the runner {adapter.source_module!r} is unavailable "
            f"({exc.__class__.__name__}); a live run needs the source checkout "
            "and its Azure runtime"
        ) from None
    runner = getattr(module, "main", None)
    if not callable(runner):
        raise SourceCheckoutMissingError(
            f"runner {adapter.source_module!r} exposes no callable main entry point"
        )
    argv = adapter.live_argv(str(entry["config_path"]), extra=extra_args)
    exit_code = int(runner(argv))
    return LiveOutcome(
        experiment_id=str(entry["experiment_id"]),
        adapter_id=adapter.adapter_id,
        exit_code=exit_code,
    )


__all__ = [
    "ConfigValidationError",
    "DispatchError",
    "DryRunOutcome",
    "EXIT_COST",
    "EXIT_INPUT",
    "EXIT_IO",
    "EXIT_OK",
    "EXIT_PLAN_CONFLICT",
    "EXIT_SOURCE_MISSING",
    "ExperimentResolutionError",
    "LiveNotConfirmedError",
    "LiveNotSupportedError",
    "LiveOutcome",
    "SourceCheckoutMissingError",
    "PlanConflictError",
    "dispatch_dry_run",
    "dispatch_live",
    "find_source_root",
    "preflight_live",
    "resolve_entry",
]
