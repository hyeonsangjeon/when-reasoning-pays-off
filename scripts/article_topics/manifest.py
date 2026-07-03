"""Build and validate an article-topic evidence manifest."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.article_topics import registry  # noqa: E402

CANDIDATE_MANIFEST = REPO_ROOT / "release" / "public_chart_candidates.json"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump_json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _repo_rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _candidate_chart_paths(repo_root: Path) -> dict[str, list[dict[str, Any]]]:
    path = repo_root / "release" / "public_chart_candidates.json"
    if not path.is_file():
        return {}
    data = _load_json(path)
    by_family: dict[str, list[dict[str, Any]]] = {}
    for candidate in data.get("candidates", []):
        by_family.setdefault(candidate["family_key"], []).append(candidate)
    return by_family


def _topic_chart_candidates(
    topic: registry.ArticleTopic,
    candidates_by_family: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    benchmark_keys = {f"benchmark-{number}" for number in topic.benchmark_numbers}
    for family_key in topic.chart_family_keys:
        for candidate in candidates_by_family.get(family_key, []):
            parts = Path(candidate["chart_data_path"]).parts
            candidate_benchmark = next(
                (part for part in parts if part.startswith("benchmark-")),
                None,
            )
            if benchmark_keys and candidate_benchmark not in benchmark_keys:
                continue
            out.append(
                {
                    "chart_data_path": candidate["chart_data_path"],
                    "evidence_topic_slug": candidate["target_topic_slug"],
                    "family_key": candidate["family_key"],
                    "schema_semver": candidate["schema_semver"],
                    "tier": candidate["tier"],
                }
            )
    return sorted(out, key=lambda item: item["chart_data_path"])


def build_manifest(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    candidates_by_family = _candidate_chart_paths(repo_root)
    generators = []
    for spec in registry.GENERATOR_SPECS:
        generators.append(
            {
                "check_command": spec.check_command,
                "command": spec.command,
                "input_paths": list(spec.input_paths),
                "key": spec.key,
                "output_paths": list(spec.output_paths),
                "result_contract": spec.result_contract,
                "sample_unit": spec.sample_unit,
                "work": spec.work,
            }
        )
    topics = []
    for topic in registry.ARTICLE_TOPICS:
        topics.append(
            {
                "article_paths": list(topic.article_paths),
                "benchmark_numbers": list(topic.benchmark_numbers),
                "chart_candidates": _topic_chart_candidates(topic, candidates_by_family),
                "chart_family_keys": list(topic.chart_family_keys),
                "generator_keys": list(topic.generator_keys),
                "slug": topic.slug,
                "source_paths": list(topic.source_paths),
            }
        )
    return {
        "generators": generators,
        "schema": "wrpo.article_topic_evidence_manifest",
        "schema_semver": "0.1.0",
        "topics": topics,
    }


def check(repo_root: Path = REPO_ROOT) -> list[str]:
    errors: list[str] = []
    manifest = build_manifest(repo_root)
    for topic in manifest["topics"]:
        for key in ("article_paths", "source_paths"):
            for relpath in topic[key]:
                if not (repo_root / relpath).exists():
                    errors.append(f"{topic['slug']}: missing {key[:-1]} {relpath}")
        for candidate in topic["chart_candidates"]:
            relpath = candidate["chart_data_path"]
            if not (repo_root / relpath).is_file():
                errors.append(f"{topic['slug']}: missing chart data {relpath}")
        if topic["chart_family_keys"] and not topic["chart_candidates"]:
            errors.append(f"{topic['slug']}: no chart candidates resolved")
    return errors


def write_manifest(path: Path, repo_root: Path = REPO_ROOT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump_json(build_manifest(repo_root)), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write", type=Path)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    if args.check:
        errors = check(repo_root)
        if errors:
            for error in errors:
                print(f"FAIL {error}")
            return 1
        print("check passed: article topic evidence registry")
    if args.write:
        write_manifest(args.write, repo_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
