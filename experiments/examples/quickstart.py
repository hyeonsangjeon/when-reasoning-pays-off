"""Minimal example: describe one experiment, then dry-run it.

    python experiments/examples/quickstart.py

``dry_run=True`` makes ZERO network calls; the runner writes synthetic,
zero-usage records so you can confirm the wiring end-to-end for free.
"""

from __future__ import annotations

import pathlib
import sys

# Make ``import experiments`` work when run as a plain script from anywhere.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import experiments  # noqa: E402

CONFIG = "exp001_short-factual_baseline.yaml"


def main() -> int:
    # Input side: what does this experiment read, sweep, and write?
    spec = experiments.describe(CONFIG)
    print("=== describe (no credentials, no network) ===")
    print(spec.summary())
    print()

    # Output side: actually run it in dry-run mode.
    print("=== run (dry-run) ===")
    result = experiments.run(CONFIG, dry_run=True)
    print(result.summary())
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
