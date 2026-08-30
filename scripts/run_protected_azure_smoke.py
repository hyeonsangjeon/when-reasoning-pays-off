#!/usr/bin/env python3
"""Run the protected Azure smoke or its no-network offline fake."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from batch_runner.protected_smoke import run_live, run_offline_fake

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--live", action="store_true")
    modes.add_argument("--offline-fake", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    try:
        if args.offline_fake:
            if args.output is None:
                parser.error("--offline-fake requires --output")
            _, health = run_offline_fake(args.output, repo_root=ROOT)
        else:
            if args.output is not None:
                parser.error("--live fixes output from the protected runtime")
            _, health = run_live(repo_root=ROOT)
    except Exception as exc:
        print(
            f"protected Azure smoke refused ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1
    print(f"protected Azure smoke: {health.status}")
    return 0 if health.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
