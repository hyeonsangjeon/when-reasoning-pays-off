"""``prompt_cache_retention`` policy helpers (Task 026).

Implements the retention-policy table and "must be explicit" rule from
the Azure OpenAI PTU Operations Guide §2 ("prompt_cache_retention
Defaults"):

* Eleven listed models support extended retention (``"24h"``).
* On most of those models the default when ``prompt_cache_retention``
  is omitted from the request body is ``"in_memory"`` — that is the
  Guide's "common trap": callers who forget the field silently lose
  cross-request reuse beyond the in-memory window.
* Pricing parity: extended-retention cache reads are billed at the
  same per-token rate as in-memory reads on these models per Guide §2.

This module is intentionally free of ``time``, ``random``, ``datetime``,
and ``uuid`` imports; the retention decision is a pure lookup over a
frozen table.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Literal, Mapping

RetentionValue = Literal["in_memory", "24h"]

# Allowed values for the request-body field. Keep tight; "permanent"
# / "infinite" are not on the Guide §2 list and would silently widen
# the surface.
_ALLOWED_RETENTION: tuple[str, ...] = ("in_memory", "24h")

# Guide §2 table: model_id -> default retention applied when the request
# body omits ``prompt_cache_retention``. All eleven listed models support
# the ``"24h"`` extended value; the defaults below are what Azure applies
# when the caller does not set the field. gpt-5.2 is the headline "must
# be explicit" entry called out in the Guide.
_DEFAULTS: Mapping[str, RetentionValue] = MappingProxyType(
    {
        "gpt-5.4": "in_memory",
        "gpt-5.3-codex": "in_memory",
        "gpt-5.2": "in_memory",
        "gpt-5.1-codex-max": "in_memory",
        "gpt-5.1": "in_memory",
        "gpt-5.1-codex": "in_memory",
        "gpt-5.1-codex-mini": "in_memory",
        "gpt-5.1-chat": "in_memory",
        "gpt-5": "in_memory",
        "gpt-5-codex": "in_memory",
        "gpt-4.1": "in_memory",
    }
)

#: Frozen set of model ids that support ``prompt_cache_retention="24h"``
#: per Guide §2. Exposed so callers can validate ``--model`` flags
#: without re-encoding the table.
EXTENDED_RETENTION_SUPPORTED_MODELS: frozenset[str] = frozenset(_DEFAULTS.keys())


class ImplicitInMemoryError(ValueError):
    """Raised by :func:`ensure_explicit` when retention is omitted.

    The Guide §2 "must be explicit" rule: on models whose documented
    default is ``in_memory``, omitting ``prompt_cache_retention`` from
    the request body silently disables 24h reuse. Callers should make
    a deliberate choice; this exception is how the library forces it.
    """


class UnknownRetentionValueError(ValueError):
    """Raised when a caller passes a value not in ``{"in_memory","24h"}``."""


def default_retention(model_id: str) -> RetentionValue:
    """Return the Guide §2 documented default for ``model_id``.

    Raises ``KeyError`` for models not listed in Guide §2's table.
    Callers that want a non-raising probe should test membership in
    :data:`EXTENDED_RETENTION_SUPPORTED_MODELS` first.
    """
    if not isinstance(model_id, str):
        raise TypeError("model_id must be a string")
    return _DEFAULTS[model_id]


def ensure_explicit(
    model_id: str,
    retention: str | None,
) -> RetentionValue:
    """Validate / enforce explicit ``prompt_cache_retention`` choice.

    Behaviour:

    * If ``model_id`` is not in Guide §2's table, raises ``KeyError``
      — this library does not invent defaults for unknown models.
    * If ``retention is None`` AND the model's documented default is
      ``"in_memory"``, raises :class:`ImplicitInMemoryError`. This
      catches the Guide's "common trap".
    * If ``retention`` is given but not one of ``{"in_memory","24h"}``,
      raises :class:`UnknownRetentionValueError`.
    * Otherwise returns the explicitly-chosen value.

    The returned value is what the caller should place in the request
    body's ``prompt_cache_retention`` field.
    """
    if not isinstance(model_id, str):
        raise TypeError("model_id must be a string")
    if model_id not in _DEFAULTS:
        raise KeyError(model_id)

    if retention is None:
        documented_default = _DEFAULTS[model_id]
        if documented_default == "in_memory":
            raise ImplicitInMemoryError(
                "prompt_cache_retention is required for this model: "
                "documented default is in_memory; pass '24h' or "
                "'in_memory' explicitly"
            )
        return documented_default

    if retention not in _ALLOWED_RETENTION:
        raise UnknownRetentionValueError(
            "prompt_cache_retention must be one of "
            + repr(_ALLOWED_RETENTION)
        )
    return retention  # type: ignore[return-value]
