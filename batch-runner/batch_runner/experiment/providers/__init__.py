"""Provider adapters — the ``EXECUTE`` stage.

Three providers, one normalized output (:class:`~batch_runner.experiment.record.OutputRecord`):

* :mod:`~batch_runner.experiment.providers.azure` — a real Azure OpenAI call in
  Microsoft Foundry (billed; Entra ID auth).
* :mod:`~batch_runner.experiment.providers.ollama` — a real local Ollama call
  (no cloud cost; localhost only by default).
* :mod:`~batch_runner.experiment.providers.mock` — a deterministic offline
  preview used for tests and shape checks (documented *after* the real paths).

Only ``azure`` and ``ollama`` open a socket, and only inside ``run_row``.
Constructing a provider and calling ``prepare`` on the mock never touches the
network.
"""

from __future__ import annotations

from batch_runner.experiment.providers.base import (
    Provider,
    ResolvedEndpoint,
    build_provider,
    resolve_endpoint,
)

__all__ = ["Provider", "ResolvedEndpoint", "build_provider", "resolve_endpoint"]
