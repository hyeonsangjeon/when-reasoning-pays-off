"""Run *any* experiment YAML through the uniform interface.

    # dry-run (default) — no network, no spend:
    python experiments/examples/run_any_experiment.py exp006_cache_key_bucketing_inmemory.yaml

    # real evidence run (requires a clean git tree + Azure env vars):
    python experiments/examples/run_any_experiment.py exp001_short-factual_baseline.yaml --evidence

    # extra runner flags after `--`:
    python experiments/examples/run_any_experiment.py exp001_short-factual_baseline.yaml -- --max-samples 2

You never name the runner: the interface picks the right one from the YAML.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import experiments  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run any experiment YAML.")
    parser.add_argument("config", help="experiment YAML (bare filename or path)")
    parser.add_argument(
        "--evidence",
        action="store_true",
        help="real run (default is a dry-run that spends nothing)",
    )
    parser.add_argument(
        "extra",
        nargs="*",
        help="extra flags forwarded verbatim to the runner (after `--`)",
    )
    args = parser.parse_args(argv)

    spec = experiments.describe(args.config)
    print(spec.summary())
    print()

    result = experiments.run(
        args.config,
        dry_run=not args.evidence,
        extra_args=args.extra or None,
    )
    print()
    print(result.summary())
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
