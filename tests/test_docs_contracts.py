import re
from urllib.parse import urlsplit

from scripts import check_docs_contracts


def test_public_documentation_contracts() -> None:
    check_docs_contracts.check()


def test_readme_front_loads_evidence_before_quickstart() -> None:
    readme = check_docs_contracts.README.read_text(encoding="utf-8")
    landmarks = (
        "# when-reasoning-pays-off",
        "*Same token price, different bill",
        "When teams move a workload",
        "<!-- CLAIM-INTEGRITY:START current-headlines -->",
        "GPT-5.2 short-factual cohort",
        "docs/assets/benchmark-01-cost-per-request.png",
        "docs/assets/benchmark-01-quality.png",
        "<!-- CLAIM-INTEGRITY:PAUSE current-headlines -->",
        "[![PR fast CI]",
        "](docs/assets/hero.svg)",
        "> [!TIP]",
        "**Project site:**",
        "## Run one real experiment in five minutes",
        "This repository is **offline-first, not offline-only**.",
        "<!-- CLI-CAPABILITIES:START -->",
        "<!-- CLI-CAPABILITIES:END -->",
        "**Badge scope:**",
        "## Reproducibility service objectives",
    )
    positions = [readme.index(landmark) for landmark in landmarks]
    assert positions == sorted(positions)
    assert readme.count("When teams move a workload") == 1
    for chart in ("benchmark-01-cost-per-request.png", "benchmark-01-quality.png"):
        assert readme.count(f"](docs/assets/{chart})") == 1


def test_readme_anchors_contents_and_local_images_resolve() -> None:
    readme = check_docs_contracts.README.read_text(encoding="utf-8")
    prose = re.sub(r"(?ms)^```[^\n]*\n.*?^```[^\n]*(?:\n|$)", "", readme)
    headings = {
        re.sub(r"[^\w -]", "", match[1].lower()).replace(" ", "-"): match.start()
        for match in re.finditer(r"(?m)^#{1,6} (.+)$", prose)
    }
    for anchor in re.findall(r"\]\(#([^)]+)\)", readme):
        assert anchor in headings, f"missing README anchor: {anchor}"

    contents = readme.split("## Contents\n", 1)[1].split("\n## ", 1)[0]
    positions = [
        headings[anchor] for anchor in re.findall(r"\]\(#([^)]+)\)", contents)
    ]
    assert positions == sorted(positions)
    for target in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", readme):
        parsed = urlsplit(target)
        if not parsed.scheme:
            assert (check_docs_contracts.ROOT / parsed.path).is_file(), target
