from __future__ import annotations

from scripts import build_article_topic_data as batd


def test_generator_keys_for_single_topic() -> None:
    assert batd.generator_keys_for_topics(("invisible-reasoning-tokens",)) == (
        "effort-evidence",
    )
    assert batd.generator_keys_for_topics(("ptu-payg-planning",)) == (
        "ptu-payg-planning",
    )


def test_generator_keys_are_deduplicated_in_topic_order() -> None:
    assert batd.generator_keys_for_topics(
        ("short-factual-work", "invisible-reasoning-tokens", "ptu-payg-planning")
    ) == ("effort-evidence", "ptu-payg-planning")


def test_run_generators_calls_requested_generators(monkeypatch) -> None:
    seen: list[tuple[str, bool]] = []

    monkeypatch.setitem(
        batd.GENERATORS,
        "effort-evidence",
        lambda check: seen.append(("effort-evidence", check)) or 0,
    )
    monkeypatch.setitem(
        batd.GENERATORS,
        "ptu-payg-planning",
        lambda check: seen.append(("ptu-payg-planning", check)) or 0,
    )
    monkeypatch.setattr(batd.manifest, "check", lambda repo_root: [])

    assert batd.run_generators(
        ("effort-evidence", "ptu-payg-planning"),
        check=True,
    ) == 0
    assert seen == [("effort-evidence", True), ("ptu-payg-planning", True)]
