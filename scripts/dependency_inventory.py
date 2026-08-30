#!/usr/bin/env python3
"""Generate or verify the deterministic release dependency inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "batch-runner/batch_runner/data/dependencies"
LOCK = DATA / "release-py311-linux-x86_64.txt"
INVENTORY = DATA / "release-py311-linux-x86_64.inventory.json"
SCOPE = "CPython 3.11 on Linux x86_64 (manylinux_2_17 wheels)"
LOCK_NAME = LOCK.name
_PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pins(path: Path) -> list[dict[str, str]]:
    packages: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _PIN_RE.match(line)
        if match:
            packages.append(
                {"name": match.group(1).lower().replace("_", "-"), "version": match.group(2)}
            )
    if not packages:
        raise ValueError("release constraints contain no exact package pins")
    return sorted(packages, key=lambda item: item["name"])


def build_inventory() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "scope": SCOPE,
        "source": {
            "file": LOCK_NAME,
            "sha256": _sha256(LOCK),
            "index": "https://pypi.org/simple",
        },
        "generator": {
            "name": "uv pip compile",
            "command": (
                "uv pip compile pyproject.toml --extra all --generate-hashes "
                "--python-platform x86_64-manylinux_2_17 --python-version 3.11 "
                "--no-emit-package when-reasoning-pays-off"
            ),
        },
        "packages": _pins(LOCK),
    }


def write_inventory() -> None:
    payload = build_inventory()
    INVENTORY.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def verify_inventory(*, installed: bool) -> None:
    expected = build_inventory()
    actual = json.loads(INVENTORY.read_text(encoding="utf-8"))
    if actual != expected:
        raise SystemExit(
            "dependency inventory is stale; run "
            "`python scripts/dependency_inventory.py generate`"
        )
    if installed:
        mismatches = []
        for package in expected["packages"]:
            name = package["name"]
            wanted = package["version"]
            try:
                found = metadata.version(name)
            except metadata.PackageNotFoundError:
                found = "missing"
            if found != wanted:
                mismatches.append(f"{name}: expected {wanted}, found {found}")
        if mismatches:
            raise SystemExit("locked environment mismatch:\n" + "\n".join(mismatches))
    print(
        f"dependency inventory verified: {len(expected['packages'])} packages, "
        f"lock sha256={expected['source']['sha256']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("generate", "verify"))
    parser.add_argument(
        "--installed",
        action="store_true",
        help="also require every locked package version in this interpreter",
    )
    args = parser.parse_args()
    if args.command == "generate":
        write_inventory()
    verify_inventory(installed=args.installed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
