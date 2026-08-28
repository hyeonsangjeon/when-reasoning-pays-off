"""The experiment catalog: one DATA -> IN -> EXECUTE -> OUT card per config.

The catalog is *derived*, never hand-maintained. :func:`build_catalog` reads
the single source of truth in ``experiments.runner`` (its ``_FAMILIES`` table
plus ``describe``/``list_experiments``) and renders a stable, four-stage view
of every committed ``experiments/exp*.yaml`` file. That derived view is frozen
into a packaged ``resources/catalog.json`` so the installed
``reasoning-payoff experiment list`` / ``describe`` commands can render it
without needing the (clone-only) ``experiments`` package on the path.

Four stages, one card:

* DATA    — the input corpus files, their formats, and their JSON shapes.
* IN      — the model, endpoint environment variable, and authentication.
* EXECUTE — the exact command, its provider, and its cost status.
* OUT     — the artifacts a real (billed) run writes.

The catalog covers every ``exp*.yaml`` exactly once. ``_template.yaml`` is not
an ``exp*`` file and is intentionally excluded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any

CATALOG_SCHEMA_VERSION = "1.0.0"

# The Entra ID audience and environment-variable NAMES the Azure runners read.
# These are names only — never a resolved endpoint URL or a secret value.
_AZURE_ENDPOINT_ENV = "AZURE_OPENAI_FOUNDRY_ENDPOINT"
_AZURE_AUTH_MODE_ENV = "AZURE_AUTH_MODE"
_AZURE_AUDIENCE = "https://ai.azure.com/.default"


# ---------------------------------------------------------------------------
# DATA-stage shapes. Derived by inspecting the committed benchmark corpora.
# Keyed by the benchmark directory the experiment actually *reads*.
# ---------------------------------------------------------------------------
DATA_FAMILY_SHAPES: dict[str, dict[str, Any]] = {
    "01-short-factual": {
        "files": [
            {
                "path": "benchmarks/01-short-factual/dataset.json",
                "format": "json",
                "top_level": "array of 20 objects",
                "row_fields": {
                    "id": "string",
                    "input": "string",
                    "expected_output_shape": "string",
                    "quality_rubric_notes": "string",
                    "tags": "array of string",
                },
            },
            {
                "path": "benchmarks/01-short-factual/prompts/system.md",
                "format": "markdown",
                "top_level": "system prompt text",
            },
            {
                "path": "benchmarks/01-short-factual/prompts/user_template.md",
                "format": "markdown",
                "top_level": "user prompt template",
            },
        ]
    },
    "02-multi-step-reasoning": {
        "files": [
            {
                "path": "benchmarks/02-multi-step-reasoning/dataset.json",
                "format": "json",
                "top_level": "array of 20 objects",
                "row_fields": {
                    "id": "string",
                    "input": "string",
                    "expected_output_shape": "string",
                    "verifiable_answer": "string",
                    "quality_rubric_notes": "string",
                    "tags": "array of string",
                },
            },
            {
                "path": "benchmarks/02-multi-step-reasoning/prompts/system.md",
                "format": "markdown",
                "top_level": "system prompt text",
            },
        ]
    },
    "03-tool-using-agent": {
        "files": [
            {
                "path": "benchmarks/03-tool-using-agent/dataset.json",
                "format": "json",
                "top_level": "array of 20 objects",
                "row_fields": {
                    "id": "string",
                    "input": "string",
                    "expected_output_shape": "string",
                    "expected_tool_calls": "array",
                    "verifiable_answer": "string",
                    "quality_rubric_notes": "string",
                    "tags": "array of string",
                },
            },
            {
                "path": "benchmarks/03-tool-using-agent/search_kb.json",
                "format": "json",
                "top_level": "object mapping query string -> answer string",
            },
            {
                "path": "benchmarks/03-tool-using-agent/prompts/system.md",
                "format": "markdown",
                "top_level": "system prompt text (with tool instructions)",
            },
        ]
    },
    "04-spillover-simulation": {
        "files": [
            {
                "path": "benchmarks/04-spillover-simulation/system_prompt_corpus.json",
                "format": "json",
                "top_level": "array of 132 strings",
            },
            {
                "path": "benchmarks/04-spillover-simulation/user_prompts.json",
                "format": "json",
                "top_level": "array of 30 strings",
            },
        ]
    },
    "05-dual-spillover": {
        "files": [
            {
                "path": "benchmarks/05-dual-spillover/system_prompt_corpus.json",
                "format": "json",
                "top_level": "array of strings",
            },
            {
                "path": "benchmarks/05-dual-spillover/user_prompts.json",
                "format": "json",
                "top_level": "array of strings",
            },
        ]
    },
    "06-cache-key-bucketing": {
        "files": [
            {
                "path": "benchmarks/06-cache-key-bucketing/system_prompt_corpus.json",
                "format": "json",
                "top_level": "array of strings",
            },
            {
                "path": "benchmarks/06-cache-key-bucketing/user_prompts.json",
                "format": "json",
                "top_level": "array of strings",
            },
        ]
    },
}


@dataclass(frozen=True)
class CatalogEntry:
    """One experiment rendered as a DATA -> IN -> EXECUTE -> OUT card."""

    experiment_id: str
    config_path: str
    family: str
    runner_module: str
    benchmark: str
    read_benchmark: str
    question: str
    task: str
    variable: str
    data: dict[str, Any]
    in_stage: dict[str, Any]
    execute: dict[str, Any]
    out: dict[str, Any]
    live_support: str
    ollama_support: bool
    mock_support: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "config_path": self.config_path,
            "family": self.family,
            "runner_module": self.runner_module,
            "benchmark": self.benchmark,
            "read_benchmark": self.read_benchmark,
            "question": self.question,
            "task": self.task,
            "variable": self.variable,
            "data": self.data,
            "in": self.in_stage,
            "execute": self.execute,
            "out": self.out,
            "live_support": self.live_support,
            "ollama_support": self.ollama_support,
            "mock_support": self.mock_support,
        }


def _model_family(config: dict[str, Any]) -> str:
    model = config.get("model") or {}
    family = model.get("family")
    return str(family) if family else "gpt-5.2"


def _endpoint_env(config: dict[str, Any]) -> str:
    model = config.get("model") or {}
    env = model.get("endpoint_env")
    return str(env) if env else _AZURE_ENDPOINT_ENV


def build_catalog(repo_root: Any | None = None) -> dict[str, Any]:
    """Build the catalog from ``experiments.runner`` (clone-only, no network).

    Imports the ``experiments`` package lazily so this generator runs from a
    clone; the installed CLI never calls it (it reads the packaged JSON).
    """
    import pathlib  # noqa: PLC0415
    import sys  # noqa: PLC0415

    if repo_root is None:
        # <repo>/batch-runner/batch_runner/experiment/catalog.py -> <repo>
        repo_root = pathlib.Path(__file__).resolve().parents[3]
    repo_root = str(repo_root)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    import experiments  # noqa: PLC0415 - clone-only source of truth
    from experiments.runner import (  # noqa: PLC0415
        _FAMILIES,
        _family_of,
        _read_yaml,
        _resolve_config_path,
    )

    entries: list[dict[str, Any]] = []
    for spec in experiments.list_experiments():
        family_key = _family_of(spec.experiment_id)
        fam = _FAMILIES[family_key]
        read_bench = fam.input_benchmark or fam.benchmark
        config = _read_yaml(_resolve_config_path(spec.config_path))
        model_family = _model_family(config)
        endpoint_env = _endpoint_env(config)

        data_stage = DATA_FAMILY_SHAPES.get(read_bench, {"files": []})
        in_stage = {
            "provider": "azure",
            "model": model_family,
            "endpoint_env": endpoint_env,
            "auth_mode": "entra",
            "auth_mode_env": _AZURE_AUTH_MODE_ENV,
            "audience": _AZURE_AUDIENCE,
            "variable": spec.variable,
        }
        execute = {
            "command": spec.command,
            "provider": "azure",
            "runner_module": spec.runner_module,
            "cost_status": "billed — Azure OpenAI live calls; needs confirmation and budget",
        }
        out = {"artifacts": list(spec.outputs), "output_dir": spec.output_dir}

        entry = CatalogEntry(
            experiment_id=spec.experiment_id,
            config_path=spec.config_path,
            family=family_key,
            runner_module=spec.runner_module,
            benchmark=fam.benchmark,
            read_benchmark=read_bench,
            question=spec.intent,
            task=spec.task,
            variable=spec.variable,
            data=data_stage,
            in_stage=in_stage,
            execute=execute,
            out=out,
            live_support="azure",
            ollama_support=False,
            mock_support=False,
        )
        entries.append(entry.to_json())

    entries.sort(key=lambda e: e["experiment_id"])
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "experiment_count": len(entries),
        "experiments": entries,
    }


def load_packaged_catalog() -> dict[str, Any]:
    """Load the frozen catalog shipped inside the installed wheel."""
    text = (
        resources.files("batch_runner.experiment.resources")
        .joinpath("catalog.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(text)


def render_entry(entry: dict[str, Any]) -> str:
    """Render one catalog entry as a stable DATA/IN/EXECUTE/OUT block."""
    lines: list[str] = []
    lines.append(f"experiment : {entry['experiment_id']}  [{entry['family']}]")
    lines.append(f"config     : {entry['config_path']}")
    lines.append(f"question   : {entry['question']}")
    lines.append("")
    lines.append("DATA (what goes in)")
    for f in entry["data"].get("files", []):
        lines.append(f"  - {f['path']}  [{f['format']}]")
        lines.append(f"      shape: {f.get('top_level', '(text)')}")
        row_fields = f.get("row_fields")
        if row_fields:
            fields = ", ".join(f"{k}:{v}" for k, v in row_fields.items())
            lines.append(f"      row fields: {fields}")
    lines.append("")
    lines.append("IN (model + endpoint + auth)")
    in_stage = entry["in"]
    lines.append(f"  provider     : {in_stage['provider']}")
    lines.append(f"  model        : {in_stage['model']}")
    lines.append(f"  endpoint env : {in_stage['endpoint_env']} (name only)")
    lines.append(
        f"  auth         : {in_stage['auth_mode']} "
        f"(audience {in_stage['audience']})"
    )
    lines.append(f"  variable     : {in_stage['variable']}")
    lines.append("")
    lines.append("EXECUTE (command + cost)")
    execute = entry["execute"]
    lines.append(f"  command : {execute['command']}")
    lines.append(f"  provider: {execute['provider']}")
    lines.append(f"  cost    : {execute['cost_status']}")
    lines.append("")
    lines.append("OUT (artifacts a real run writes)")
    for artifact in entry["out"]["artifacts"]:
        lines.append(f"  - {artifact}")
    support = []
    support.append(f"live={entry['live_support']}")
    support.append(f"ollama={'yes' if entry['ollama_support'] else 'no'}")
    support.append(f"mock={'yes' if entry['mock_support'] else 'no'}")
    lines.append("")
    lines.append("support    : " + ", ".join(support))
    return "\n".join(lines)


def render_list(catalog: dict[str, Any]) -> str:
    """Render a one-line-per-experiment index."""
    lines = [
        f"{catalog['experiment_count']} experiments "
        f"(catalog schema {catalog['schema_version']})",
        "",
    ]
    for entry in catalog["experiments"]:
        lines.append(
            f"  {entry['experiment_id']:<38} "
            f"{entry['family']:<3} "
            f"{entry['execute']['provider']:<6} "
            f"reads {entry['read_benchmark']}"
        )
    lines.append("")
    lines.append(
        "All experiments are billed Azure OpenAI runs. For a free local "
        "preview, use `reasoning-payoff sample run` instead."
    )
    return "\n".join(lines)


def find_entry(catalog: dict[str, Any], key: str) -> dict[str, Any] | None:
    """Find an entry by experiment id or config path (bare filename allowed)."""
    stem = key
    if stem.endswith(".yaml"):
        stem = stem[: -len(".yaml")]
    for entry in catalog["experiments"]:
        if entry["experiment_id"] == key:
            return entry
        cfg = entry["config_path"]
        if cfg == key or cfg.endswith("/" + key) or cfg.endswith("/" + stem + ".yaml"):
            return entry
        if entry["experiment_id"] == stem:
            return entry
    return None


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "CatalogEntry",
    "DATA_FAMILY_SHAPES",
    "build_catalog",
    "find_entry",
    "load_packaged_catalog",
    "render_entry",
    "render_list",
]
