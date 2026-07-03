"""Shared helpers for supplementary statistics CLIs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def discover_benchmarks(
    benchmarks_dir: Path,
    selected: list[str] | None,
) -> tuple[list[str], list[str]]:
    """Return ``(benchmark_names, skipped)`` for benchmark dirs with runs/."""
    found: list[str] = []
    skipped: list[str] = []
    if selected:
        names = selected
    else:
        names = sorted(path.name for path in benchmarks_dir.iterdir() if path.is_dir())
    for name in names:
        runs_dir = benchmarks_dir / name / "runs"
        if runs_dir.is_dir():
            found.append(name)
        else:
            skipped.append(name)
    return found, skipped


def dump_json(payload: dict[str, Any]) -> str:
    """Serialize supplementary JSON deterministically."""
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_json(path: Path, payload: dict[str, Any]) -> str:
    """Write supplementary JSON and return the exact written text."""
    text = dump_json(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text
