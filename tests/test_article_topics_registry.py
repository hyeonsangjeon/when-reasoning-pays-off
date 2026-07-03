from __future__ import annotations

import json
from pathlib import Path

from scripts.article_topics import manifest
from scripts.article_topics import publication
from scripts.article_topics import registry

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_registry_paths_exist() -> None:
    errors = manifest.check(REPO_ROOT)
    assert errors == []


def test_chart_family_slugs_match_candidate_manifest() -> None:
    data = json.loads(
        (REPO_ROOT / "release" / "public_chart_candidates.json").read_text(
            encoding="utf-8"
        )
    )
    for candidate in data["candidates"]:
        assert candidate["target_topic_slug"] == registry.target_topic_slug(
            candidate["family_key"]
        )


def test_generator_family_boundaries_are_topic_named() -> None:
    assert registry.generator_family_keys("effort-evidence") == frozenset(
        {"cost-curves-effort", "token-composition"}
    )
    assert registry.generator_family_keys("ptu-payg-planning") == frozenset(
        {"ptu-payg-crossover"}
    )


def test_topic_manifest_resolves_overview_article_data() -> None:
    data = manifest.build_manifest(REPO_ROOT)
    by_slug = {topic["slug"]: topic for topic in data["topics"]}

    overview = by_slug["when-reasoning-pays-off"]
    assert {
        candidate["family_key"] for candidate in overview["chart_candidates"]
    } == {"cost-curves-effort", "token-composition", "ptu-payg-crossover"}
    assert len(overview["chart_candidates"]) == 18

    ptu = by_slug["ptu-payg-planning"]
    assert {candidate["family_key"] for candidate in ptu["chart_candidates"]} == {
        "cost-curves-effort",
        "ptu-payg-crossover",
    }


def test_manifest_exposes_generator_contracts() -> None:
    data = manifest.build_manifest(REPO_ROOT)
    generators = {generator["key"]: generator for generator in data["generators"]}

    assert set(generators) == {
        "effort-evidence",
        "ptu-payg-planning",
        "supplementary-stats",
    }
    for generator in generators.values():
        assert generator["command"].startswith("python3 ")
        assert generator["check_command"].startswith("python3 ")
        assert generator["work"]
        assert generator["input_paths"]
        assert generator["sample_unit"]
        assert generator["output_paths"]
        assert generator["result_contract"]


def test_publication_candidate_merge_is_family_scoped() -> None:
    old_owned = {
        "chart_data_path": "results/public/chart-data/owned/old.json",
        "family_key": "owned",
    }
    preserved_other = {
        "chart_data_path": "results/public/chart-data/other/keep.json",
        "family_key": "other",
    }
    emitted = publication.PublishedArtifact(
        relpath="results/public/chart-data/owned/new.json",
        payload={"rows": []},
        source_raw_sha="0" * 64,
        candidate={
            "chart_data_path": "results/public/chart-data/owned/new.json",
            "family_key": "owned",
        },
    )

    merged = publication.merged_candidate_manifest(
        [emitted],
        owned_family_keys=frozenset({"owned"}),
        existing_candidates=[old_owned, preserved_other],
    )

    paths = [candidate["chart_data_path"] for candidate in merged["candidates"]]
    assert paths == [
        "results/public/chart-data/other/keep.json",
        "results/public/chart-data/owned/new.json",
    ]


def test_scripts_readme_lists_public_pipeline_entrypoints() -> None:
    text = (REPO_ROOT / "scripts" / "README.md").read_text(encoding="utf-8")
    for entrypoint in (
        "build_article_topic_data.py",
        "promote_chart_data.py",
        "promote_ptu_payg_crossover.py",
        "sync_pages_chart_data.py",
        "check_promotion_set.py",
        "stats/check_repro.py",
    ):
        assert entrypoint in text
