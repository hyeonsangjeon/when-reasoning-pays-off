"""Experiment catalog + a one-call Python interface.

This directory holds the experiment definitions (``exp*.yaml``) for the study.
Each YAML is the *input* to one runner under ``scripts/``. Import this package
to drive any experiment with a single, uniform call instead of remembering
which runner and flags each one needs.

Quick start (no tokens spent — ``dry_run=True`` makes zero network calls)::

    import experiments

    # 1. See what an experiment reads, sweeps, and writes — no credentials needed.
    spec = experiments.describe("exp001_short-factual_baseline.yaml")
    print(spec.summary())

    # 2. Run it (dry-run first to verify the wiring).
    result = experiments.run("exp001_short-factual_baseline.yaml", dry_run=True)
    print(result.summary())

    # 3. Browse the whole catalog.
    for s in experiments.list_experiments():
        print(s.experiment_id, "->", s.variable)

Runnable, copy-paste examples live in ``experiments/examples/``.
"""

from __future__ import annotations

from .runner import (
    RUNNERS,
    ExperimentResult,
    ExperimentSpec,
    describe,
    list_experiments,
    run,
)

__all__ = [
    "RUNNERS",
    "ExperimentResult",
    "ExperimentSpec",
    "describe",
    "list_experiments",
    "run",
]
