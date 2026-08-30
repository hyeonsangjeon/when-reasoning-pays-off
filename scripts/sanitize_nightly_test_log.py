"""Reduce pytest output to a secret-safe nightly failure summary."""

from __future__ import annotations

import re
import sys
from pathlib import Path


_RESULT_RE = re.compile(
    r"^(?P<count>\d+) (?P<result>passed|failed|error|errors|skipped|warnings?)\b"
)
_NODE_RE = re.compile(r"^(FAILED|ERROR) (?P<node>tests/|batch-runner/tests/)(?P<body>.+)")


def _sanitize_node(line: str) -> str:
    prefix, node_and_detail = line.split(" ", 1)
    node = node_and_detail.split(" - ", 1)[0]
    node = re.sub(r"\[[^\]]*\]", "[PARAMETERS_REDACTED]", node)
    return f"{prefix} {node}"


def sanitize(raw_path: Path, output_path: Path) -> None:
    safe: list[str] = []
    for line in raw_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if _NODE_RE.match(stripped):
            safe.append(_sanitize_node(stripped))
        elif _RESULT_RE.match(stripped):
            safe.append(stripped)
        elif "collected in " in stripped and stripped[:1].isdigit():
            safe.append(stripped)
    if not safe:
        safe.append("pytest exited without a publishable test summary")
    output_path.write_text("\n".join(safe) + "\n", encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: sanitize_nightly_test_log.py RAW OUTPUT")
    sanitize(Path(sys.argv[1]), Path(sys.argv[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
