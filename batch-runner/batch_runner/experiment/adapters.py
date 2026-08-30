"""Typed adapter registry for the ``reasoning-payoff experiment run`` dispatcher.

The catalog covers 20 committed ``experiments/exp*.yaml`` files, each consumed
by exactly one runner module under ``scripts/``. Historically a user had to know
which runner to call and how to spell its flags. This registry is the single
typed table the dispatcher consults to answer three questions safely:

* Which validated runner (adapter) owns this experiment?
* Does that runner make a real, billed live call, or is it offline-only?
* What normalized argument vector should a dry-run / live invocation use?

Design guarantees:

* **No shell, no eval.** An adapter names a Python module (``scripts.<name>``)
  and the dispatcher forwards a normalized ``argv`` list to that module's
  already-validated ``main([...])`` / ``load_experiment(...)`` entry points.
  Nothing here builds a shell string or evaluates catalogued text.
* **Every adapter is frozen.** :data:`ADAPTERS` is an immutable mapping keyed by
  the ``runner_module`` (the adapter id). :func:`get_adapter` rejects unknown
  ids with a typed error.
* **Dry-run never authorizes live.** :meth:`ExperimentAdapter.dry_run_argv`
  forwards ``--dry-run`` and, for pricing-aware runners, the offline-only
  ``--pricing-policy historical-replay`` mode. :meth:`live_argv` refuses to
  build an argv for an adapter whose live path is not billed-capable.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping, Sequence

ADAPTER_REGISTRY_VERSION = "1.0.0"

# Offline-only pricing mode; the live measurement runners forward this in a
# dry-run so committed evidence stays deterministic and no fresh pricing (a live
# concern) is required. It can never accompany a live invocation.
_HISTORICAL_REPLAY = "historical-replay"


class AdapterError(ValueError):
    """Base class for adapter-registry failures."""


class UnknownAdapterError(AdapterError):
    """The requested adapter id is not registered."""


class LiveUnsupportedError(AdapterError):
    """The adapter's runner does not perform a billed live invocation."""


@dataclass(frozen=True)
class ExperimentAdapter:
    """One validated runner, described by identity + capability, never behavior.

    Attributes:
        adapter_id: Stable id; equals the catalog ``runner_module`` value.
        version: Adapter contract version (bumped when argv mapping changes).
        source_module: Importable module under the source checkout that owns
            both the strict ``load_experiment`` loader and the ``main([...])``
            entry point. Clone-only — never shipped in the wheel.
        supports_dry_run: Always ``True``; every runner has a ``--dry-run``.
        supports_live: ``True`` only when the runner makes a real billed call.
        live_kind: ``"azure-billed"`` for a runner that spends real budget, or
            ``"offline-simulation"`` for a runner that never leaves the host.
        pricing_policy_aware: Whether the runner accepts ``--pricing-policy``.
    """

    adapter_id: str
    version: str
    source_module: str
    supports_dry_run: bool
    supports_live: bool
    live_kind: Literal["azure-billed", "offline-simulation"]
    pricing_policy_aware: bool

    def dry_run_argv(self, config_rel_path: str, *, allow_dirty: bool = True) -> list[str]:
        """Normalized argv for a dry-run of this adapter's ``main([...])``.

        A dry-run forwards ``--dry-run`` and (for pricing-aware runners) the
        offline-only ``--pricing-policy historical-replay`` mode. ``allow_dirty``
        defaults to ``True`` because a dry-run embeds no meaningful git commit.
        The dispatcher does not itself invoke ``main`` in the dry-run path — it
        builds a static plan — so this vector is the *documented* command scope
        the plan records, and the exact vector a hypothetical offline
        invocation would use.
        """
        argv = ["--experiment", config_rel_path, "--dry-run"]
        if self.pricing_policy_aware:
            argv += ["--pricing-policy", _HISTORICAL_REPLAY]
        if allow_dirty:
            argv.append("--allow-dirty")
        return argv

    def live_argv(
        self, config_rel_path: str, *, extra: Sequence[str] | None = None
    ) -> list[str]:
        """Normalized argv for a billed live invocation of this adapter.

        Raises:
            LiveUnsupportedError: The adapter has no billed live path. Callers
                must check :attr:`supports_live` before any side effect.
        """
        if not self.supports_live or self.live_kind != "azure-billed":
            raise LiveUnsupportedError(
                f"adapter {self.adapter_id!r} has no billed live path "
                f"(live_kind={self.live_kind!r})"
            )
        # Live measurement uses the runner's default (fresh) pricing policy; the
        # offline-only historical-replay mode is never forwarded here.
        argv = ["--experiment", config_rel_path]
        if extra:
            argv += list(extra)
        return argv


# ---------------------------------------------------------------------------
# The frozen registry. Keyed by adapter id (== catalog ``runner_module``).
# Every one of the five runners makes a real Azure OpenAI billed call in its
# evidence path, so all are ``azure-billed``. The ``offline-simulation`` branch
# is retained as a typed capability so a future non-billing runner is rejected
# from ``--stage live`` before any side effect (see :meth:`live_argv`).
# ---------------------------------------------------------------------------
_ADAPTER_LIST: tuple[ExperimentAdapter, ...] = (
    ExperimentAdapter(
        adapter_id="run_benchmark",
        version="1.0.0",
        source_module="scripts.run_benchmark",
        supports_dry_run=True,
        supports_live=True,
        live_kind="azure-billed",
        pricing_policy_aware=False,
    ),
    ExperimentAdapter(
        adapter_id="simulate_spillover",
        version="1.0.0",
        source_module="scripts.simulate_spillover",
        supports_dry_run=True,
        supports_live=True,
        live_kind="azure-billed",
        pricing_policy_aware=False,
    ),
    ExperimentAdapter(
        adapter_id="measure_dual_spillover",
        version="1.0.0",
        source_module="scripts.measure_dual_spillover",
        supports_dry_run=True,
        supports_live=True,
        live_kind="azure-billed",
        pricing_policy_aware=True,
    ),
    ExperimentAdapter(
        adapter_id="measure_cache_key_bucketing",
        version="1.0.0",
        source_module="scripts.measure_cache_key_bucketing",
        supports_dry_run=True,
        supports_live=True,
        live_kind="azure-billed",
        pricing_policy_aware=True,
    ),
    ExperimentAdapter(
        adapter_id="measure_max_output_tokens_sweep",
        version="1.0.0",
        source_module="scripts.measure_max_output_tokens_sweep",
        supports_dry_run=True,
        supports_live=True,
        live_kind="azure-billed",
        pricing_policy_aware=True,
    ),
)


def _build_registry() -> dict[str, ExperimentAdapter]:
    registry: dict[str, ExperimentAdapter] = {}
    for adapter in _ADAPTER_LIST:
        if adapter.adapter_id in registry:
            # A duplicate adapter id would make dispatch ambiguous; refuse to
            # construct the registry at import time rather than pick silently.
            raise AdapterError(
                f"duplicate adapter id in registry: {adapter.adapter_id!r}"
            )
        registry[adapter.adapter_id] = adapter
    return registry


ADAPTERS: Mapping[str, ExperimentAdapter] = MappingProxyType(_build_registry())


def get_adapter(adapter_id: str) -> ExperimentAdapter:
    """Return the registered adapter for ``adapter_id`` or raise.

    Raises:
        UnknownAdapterError: No adapter is registered under ``adapter_id``.
    """
    try:
        return ADAPTERS[adapter_id]
    except KeyError:
        known = ", ".join(sorted(ADAPTERS))
        raise UnknownAdapterError(
            f"unknown adapter {adapter_id!r}; known adapters: {known}"
        ) from None


def adapter_ids() -> list[str]:
    """Sorted list of registered adapter ids."""
    return sorted(ADAPTERS)


__all__ = [
    "ADAPTERS",
    "ADAPTER_REGISTRY_VERSION",
    "AdapterError",
    "ExperimentAdapter",
    "LiveUnsupportedError",
    "UnknownAdapterError",
    "adapter_ids",
    "get_adapter",
]
