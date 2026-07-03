#!/usr/bin/env python3
"""Build public data for one or more article topics.

This is the topic-oriented entry point for the public repo. It delegates to the
existing deterministic chart-data generators, but callers choose work by
reader-facing article topic instead of by internal release tranche.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import promote_chart_data  # noqa: E402
from scripts import promote_ptu_payg_crossover  # noqa: E402
from scripts.article_topics import manifest  # noqa: E402
from scripts.article_topics import registry  # noqa: E402

Generator = Callable[[bool], int]

GENERATORS: dict[str, Generator] = {
    "effort-evidence": promote_chart_data.generate,
    "ptu-payg-planning": promote_ptu_payg_crossover.generate,
}


def generator_keys_for_topics(topic_slugs: tuple[str, ...]) -> tuple[str, ...]:
    keys: list[str] = []
    for slug in topic_slugs:
        topic = registry.article_topic(slug)
        for key in topic.generator_keys:
            if key not in keys:
                keys.append(key)
    return tuple(keys)


def run_generators(generator_keys: tuple[str, ...], *, check: bool) -> int:
    for key in generator_keys:
        generator = GENERATORS[key]
        code = generator(check=check)
        if code != 0:
            return code
    errors = manifest.check(REPO_ROOT)
    if errors:
        for error in errors:
            print(f"FAIL {error}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--topic",
        action="append",
        choices=tuple(topic.slug for topic in registry.ARTICLE_TOPICS),
        help="Article topic slug to build. May be passed more than once.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Build every article-topic data generator.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify committed outputs instead of writing them.",
    )
    args = parser.parse_args(argv)

    if args.all:
        generator_keys = tuple(GENERATORS)
    elif args.topic:
        generator_keys = generator_keys_for_topics(tuple(args.topic))
    else:
        parser.error("pass --all or at least one --topic")

    return run_generators(generator_keys, check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
