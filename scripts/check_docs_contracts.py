#!/usr/bin/env python3
"""Validate public reproducibility and CLI network-boundary documentation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "batch-runner")]
CAPABILITIES = ROOT / "batch-runner/batch_runner/data/cli_capabilities.v1.json"
README = ROOT / "README.md"
METHODOLOGY = ROOT / "docs/05-methodology.md"
RELEASE_POLICY = ROOT / "docs/16-release-tiers-and-redaction-policy.md"
SAMPLE_GUIDE = ROOT / "docs/20-five-minute-experiment-run.md"
README_START = "<!-- CLI-CAPABILITIES:START -->"
README_END = "<!-- CLI-CAPABILITIES:END -->"
METHODOLOGY_URL = (
    "https://github.com/hyeonsangjeon/when-reasoning-pays-off/"
    "blob/main/docs/05-methodology.md"
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {message}")


def load_manifest() -> dict[str, object]:
    manifest = json.loads(CAPABILITIES.read_text(encoding="utf-8"))
    require(manifest.get("schema_version") == "1.1.0", "unsupported capability schema")
    commands = manifest.get("commands")
    require(isinstance(commands, list) and commands, "capability commands must be a list")
    return manifest


def parser_leaf_paths() -> set[str]:
    from batch_runner.cli import _parser

    def walk(parser: argparse.ArgumentParser, prefix: tuple[str, ...]) -> set[str]:
        subparsers = [
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        if not subparsers:
            return {"reasoning-payoff " + " ".join(prefix)}
        require(len(subparsers) == 1, f"multiple subparser actions at {' '.join(prefix)}")
        leaves: set[str] = set()
        for name, child in subparsers[0].choices.items():
            leaves.update(walk(child, (*prefix, name)))
        return leaves

    return walk(_parser(), ())


def sample_init_providers() -> set[str]:
    from batch_runner.cli import _parser

    parser = _parser()
    top = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    sample = top.choices["sample"]
    sample_sub = next(
        action
        for action in sample._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    init = sample_sub.choices["init"]
    provider = next(action for action in init._actions if action.dest == "provider")
    return set(provider.choices or ())


def readme_rows() -> dict[str, list[str]]:
    text = README.read_text(encoding="utf-8")
    require(text.count(README_START) == 1, "README capability start marker missing")
    require(text.count(README_END) == 1, "README capability end marker missing")
    block = text.split(README_START, 1)[1].split(README_END, 1)[0]
    rows: dict[str, list[str]] = {}
    for line in block.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        claim_id = cells[0].strip("`")
        require(claim_id not in rows, f"duplicate README capability row: {claim_id}")
        rows[claim_id] = cells
    return rows


def public_result_pages() -> list[Path]:
    retrospective = ROOT / "docs/blog/articles/reasoning-effort-retrospective"
    pages = [
        ROOT / "docs/blog/charts/index.html",
        retrospective / "index.html",
        retrospective / "ko/index.html",
        retrospective / "ja/index.html",
        retrospective / "zh-CN/index.html",
        retrospective / "hi/index.html",
    ]
    pages.extend(sorted((retrospective / "experiments").rglob("index.html")))
    return pages


def check() -> None:
    manifest = load_manifest()
    commands = manifest["commands"]
    assert isinstance(commands, list)

    manifest_paths = {item["cli_path"] for item in commands}
    require(
        manifest_paths == parser_leaf_paths(),
        "capability manifest does not cover exactly the installed CLI leaf commands",
    )

    sample_init = next(item for item in commands if item["id"] == "sample-init")
    require(
        set(sample_init["providers"]) == sample_init_providers(),
        "sample-init provider choices drifted from the capability manifest",
    )
    run_providers = {
        item["provider"]
        for item in commands
        if item["cli_path"] == "reasoning-payoff sample run" and "provider" in item
    }
    require(
        run_providers == sample_init_providers(),
        "sample-run provider coverage drifted from sample-init choices",
    )

    documented = readme_rows()
    expected = {item["id"]: item["readme_row"] for item in commands}
    require(documented == expected, "README capability table drifted from the manifest")

    python_support = manifest.get("python_support")
    require(
        python_support
        == {
            "implementation": "CPython",
            "minimum": "3.11",
            "maximum_exclusive": "3.14",
            "ci_endpoints": ["3.11", "3.13"],
        },
        "bounded Python support contract drifted",
    )
    contracts = manifest.get("reproducibility_contracts")
    require(isinstance(contracts, list), "reproducibility contracts must be a list")
    require(
        [item["id"] for item in contracts]
        == ["cold-mock", "warm-ollama", "full-research-rerun"],
        "reproducibility contract IDs or order drifted",
    )
    require(
        contracts[0]["threshold_seconds"] == 300
        and contracts[1]["threshold_seconds"] == 300
        and contracts[2]["threshold_seconds"] is None,
        "reproducibility thresholds drifted",
    )
    automations = manifest.get("automation_contracts")
    require(
        isinstance(automations, list)
        and [item.get("id") for item in automations] == ["protected-azure-smoke"],
        "protected automation contract drifted",
    )
    protected = automations[0]
    require(
        protected.get("public_pr_ci") is False
        and protected.get("offline_fake_in_pr_ci") is True
        and protected.get("network") == "azure-https",
        "protected smoke CI boundary drifted",
    )

    for path in public_result_pages():
        require(path.is_file(), f"missing public result surface: {path.relative_to(ROOT)}")
        require(
            METHODOLOGY_URL in path.read_text(encoding="utf-8"),
            f"public result surface lacks reproducibility scope link: {path.relative_to(ROOT)}",
        )

    for path in (README, METHODOLOGY, RELEASE_POLICY):
        text = path.read_text(encoding="utf-8")
        for term in (
            "Public evidence verification",
            "Same-method rerun on a new environment",
            "Exact original raw reproduction",
            "owner-auditable",
            "source_raw_sha256",
        ):
            require(term in text, f"{path.relative_to(ROOT)} missing contract term: {term}")

    for path in (README, METHODOLOGY, SAMPLE_GUIDE):
        text = path.read_text(encoding="utf-8")
        for term in (
            "Cold Mock",
            "Warm Ollama",
            "Full research rerun",
            "300",
            "CPython 3.11",
            "Windows",
            "fcntl",
        ):
            require(
                term in text,
                f"{path.relative_to(ROOT)} missing SLO/platform term: {term}",
            )

    print(
        "docs contracts: "
        f"{len(commands)} CLI capability rows and "
        f"{len(public_result_pages())} public result surfaces checked"
    )


if __name__ == "__main__":
    check()
