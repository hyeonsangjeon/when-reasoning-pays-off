"""Real-run experiment layer for the ``reasoning-payoff experiment`` commands.

This package turns every experiment into the same explicit four-stage flow::

    DATA  ->  IN  ->  EXECUTE  ->  OUT

* ``DATA``    — a small input dataset (``json`` or ``jsonl``) with an explicit
  row shape (see :mod:`batch_runner.experiment.dataset`).
* ``IN``      — a versioned, strict *run ledger* naming the provider, model,
  endpoint environment-variable, input dataset, and execution settings
  (see :mod:`batch_runner.experiment.ledger`).
* ``EXECUTE`` — a real call to Azure OpenAI in Microsoft Foundry (billed) or a
  local Ollama server (no cloud cost), or a deterministic offline mock preview
  (see :mod:`batch_runner.experiment.providers`).
* ``OUT``     — deterministic-structure artifacts under an immutable owned run
  directory: ``run.json``, ``records.jsonl``, ``summary.md``,
  ``manifest.json``, and ``artifacts.sha256``
  (see :mod:`batch_runner.experiment.runner`).

Everything here is import-safe with no network access: constructing a ledger,
loading a dataset, and building the catalog never open a socket. Only the
``azure`` and ``ollama`` providers make outbound calls, and only when
:func:`batch_runner.experiment.runner.run_ledger` actually executes them.
"""

from __future__ import annotations

SCHEMA_VERSION = "1.0.0"
METHOD_ID = "experiment-runner"
METHOD_VERSION = "1.0.0"

__all__ = ["SCHEMA_VERSION", "METHOD_ID", "METHOD_VERSION"]
