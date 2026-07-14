"""Print the full experiment catalog: one input -> variable -> output row each.

    python experiments/examples/describe_all.py

Uses only YAML parsing — no credentials, no network, no heavy imports.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import experiments  # noqa: E402


def main() -> int:
    specs = experiments.list_experiments()
    print(f"{len(specs)} experiments — intent / task / input / variable / output\n")
    for spec in specs:
        print("=" * 78)
        print(spec.summary())
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
