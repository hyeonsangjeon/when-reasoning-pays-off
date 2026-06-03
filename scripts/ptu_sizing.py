#!/usr/bin/env python3
"""CLI wrapper for the Task 027 PTU-vs-PAYG sizing calculator."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BR_ROOT = _REPO_ROOT / "batch-runner"
if str(_BR_ROOT) not in sys.path:
    sys.path.insert(0, str(_BR_ROOT))

from batch_runner.sizing import WorkloadShape, calculate  # noqa: E402

_WORKLOAD_KEYS = {
    "mean_prompt_tokens",
    "mean_cached_fraction",
    "mean_visible_output_tokens",
    "mean_reasoning_tokens",
    "mean_max_output_tokens",
    "expected_rpm",
    "model_id",
}


def _load_workload(path: Path) -> WorkloadShape:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError("workload YAML root must be a mapping")
    extra = set(data) - _WORKLOAD_KEYS
    missing = _WORKLOAD_KEYS - set(data)
    if extra or missing:
        raise ValueError(
            "workload keys mismatch: "
            f"extra={sorted(extra)}, missing={sorted(missing)}"
        )
    return WorkloadShape(
        mean_prompt_tokens=data["mean_prompt_tokens"],
        mean_cached_fraction=data["mean_cached_fraction"],
        mean_visible_output_tokens=data["mean_visible_output_tokens"],
        mean_reasoning_tokens=data["mean_reasoning_tokens"],
        mean_max_output_tokens=data["mean_max_output_tokens"],
        expected_rpm=data["expected_rpm"],
        model_id=data["model_id"],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compute deterministic PTU-vs-PAYG sizing JSON."
    )
    parser.add_argument("--workload", required=True, type=Path)
    parser.add_argument("--target-util", default=0.7, type=float)
    parser.add_argument("--payg-rates", required=True, type=Path)
    parser.add_argument("--ptu-rates", required=True, type=Path)
    parser.add_argument("--leak-calibration", default=None, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        workload = _load_workload(args.workload)
        result = calculate(
            workload=workload,
            target_utilization=args.target_util,
            payg_rates_yaml=args.payg_rates,
            ptu_rates_yaml=args.ptu_rates,
            leak_calibration_json=args.leak_calibration,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
