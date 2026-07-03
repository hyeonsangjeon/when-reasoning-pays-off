"""Public article-topic registry.

The public Pages site is organized around reader-facing article topics, while
the measured evidence is stored in benchmark- and chart-family-shaped files.
This registry is the small translation layer between those two worlds.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BenchmarkSlice:
    """One authored benchmark slice used by the overview evidence topics."""

    number: str
    key: str
    article_topic_slug: str
    benchmark_dir: str
    cost_curve_prefix: str
    token_composition_csv: str
    supplementary_dir: str


@dataclass(frozen=True)
class ChartFamily:
    """A governed numeric chart-data family emitted for public articles."""

    family_key: str
    evidence_topic_slug: str
    owner_topic_slug: str
    generator_key: str
    benchmark_numbers: tuple[str, ...]


@dataclass(frozen=True)
class ArticleTopic:
    """Reader-facing article topic and the public evidence it owns or cites."""

    slug: str
    article_paths: tuple[str, ...]
    benchmark_numbers: tuple[str, ...] = ()
    chart_family_keys: tuple[str, ...] = ()
    source_paths: tuple[str, ...] = ()
    generator_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class GeneratorSpec:
    """Public data generator, its input evidence, and its output contract."""

    key: str
    command: str
    check_command: str
    work: str
    input_paths: tuple[str, ...]
    sample_unit: str
    output_paths: tuple[str, ...]
    result_contract: str


BENCHMARK_SLICES: tuple[BenchmarkSlice, ...] = (
    BenchmarkSlice(
        number="01",
        key="benchmark-01",
        article_topic_slug="short-factual-work",
        benchmark_dir="benchmarks/01-short-factual",
        cost_curve_prefix="results/cost-curves/benchmark-01",
        token_composition_csv="results/token-composition/benchmark-01-tokens.csv",
        supplementary_dir="results/supplementary/01-short-factual",
    ),
    BenchmarkSlice(
        number="02",
        key="benchmark-02",
        article_topic_slug="multi-step-work",
        benchmark_dir="benchmarks/02-multi-step-reasoning",
        cost_curve_prefix="results/cost-curves/benchmark-02",
        token_composition_csv="results/token-composition/benchmark-02-tokens.csv",
        supplementary_dir="results/supplementary/02-multi-step-reasoning",
    ),
    BenchmarkSlice(
        number="03",
        key="benchmark-03",
        article_topic_slug="tool-agent-ceiling-checks",
        benchmark_dir="benchmarks/03-tool-using-agent",
        cost_curve_prefix="results/cost-curves/benchmark-03",
        token_composition_csv="results/token-composition/benchmark-03-tokens.csv",
        supplementary_dir="results/supplementary/03-tool-using-agent",
    ),
)


CHART_FAMILIES: tuple[ChartFamily, ...] = (
    ChartFamily(
        family_key="cost-curves-effort",
        evidence_topic_slug="reasoning-cost-quality-latency-by-effort",
        owner_topic_slug="when-reasoning-pays-off",
        generator_key="effort-evidence",
        benchmark_numbers=("01", "02", "03"),
    ),
    ChartFamily(
        family_key="token-composition",
        evidence_topic_slug="token-composition-under-reasoning",
        owner_topic_slug="invisible-reasoning-tokens",
        generator_key="effort-evidence",
        benchmark_numbers=("01", "02", "03"),
    ),
    ChartFamily(
        family_key="ptu-payg-crossover",
        evidence_topic_slug="reasoning-ptu-payg-crossover",
        owner_topic_slug="ptu-payg-planning",
        generator_key="ptu-payg-planning",
        benchmark_numbers=("01", "02", "03"),
    ),
)


GENERATOR_SPECS: tuple[GeneratorSpec, ...] = (
    GeneratorSpec(
        key="effort-evidence",
        command="python3 scripts/build_article_topic_data.py --topic invisible-reasoning-tokens",
        check_command="python3 scripts/build_article_topic_data.py --topic invisible-reasoning-tokens --check",
        work=(
            "Converts benchmark cost, latency, quality, throughput, and token "
            "composition CSVs into locale-agnostic numeric JSON for overview "
            "and evidence-topic articles."
        ),
        input_paths=(
            "results/cost-curves/benchmark-0{1,2,3}-{cost-per-request,latency,quality,throughput-gain}.csv",
            "results/token-composition/benchmark-0{1,2,3}-tokens.csv",
        ),
        sample_unit=(
            "One row per (model, effort) cell after the canonical benchmark "
            "analysis has joined N=20 authored samples with R=3 repeats and "
            "the judge scores."
        ),
        output_paths=(
            "results/public/chart-data/cost-curves-effort/benchmark-*/{cost-per-request,latency,quality,throughput-gain}.json",
            "results/public/chart-data/token-composition/benchmark-*/tokens.json",
            "release/public_chart_candidates.json",
        ),
        result_contract=(
            "Every payload is SANITIZED_PUBLIC wrpo.chart_data JSON with stable "
            "dimension keys, numeric series only, source_sanitized_sha256 pins, "
            "and no prose labels, endpoint names, request IDs, or pricing URLs."
        ),
    ),
    GeneratorSpec(
        key="ptu-payg-planning",
        command="python3 scripts/build_article_topic_data.py --topic ptu-payg-planning",
        check_command="python3 scripts/build_article_topic_data.py --topic ptu-payg-planning --check",
        work=(
            "Builds the modeled PTU/PAYG crossover companion data used by the "
            "PTU planning topic. It carries PAYG cost through from public "
            "chart-data, joins throughput and quality guardrails, and applies "
            "the pinned PTU hourly-rate/minimum-PTU snapshot."
        ),
        input_paths=(
            "results/public/chart-data/cost-curves-effort/benchmark-*/cost-per-request.json",
            "results/public/chart-data/cost-curves-effort/benchmark-*/throughput-gain.json",
            "results/public/chart-data/cost-curves-effort/benchmark-*/quality.json",
            "results/public/chart-data/token-composition/benchmark-*/tokens.json",
            "pricing/azure-openai-ptu-2026-05.yaml",
            "pricing/azure-openai-payg-2026-05.yaml",
        ),
        sample_unit=(
            "One row per modeled (model, effort) cell. PAYG USD/request and "
            "throughput-gain values are exact carry-throughs from the governed "
            "public chart-data rows."
        ),
        output_paths=(
            "results/public/chart-data/ptu-payg-crossover/benchmark-*/crossover.json",
            "release/public_chart_candidates.json",
        ),
        result_contract=(
            "Outputs a modeled_break_even_rpm lens labeled "
            "throughput_gain_hypothesis. It is not measured PTU throughput and "
            "must stay paired with the matching quality series."
        ),
    ),
    GeneratorSpec(
        key="supplementary-stats",
        command="python3 scripts/stats/check_repro.py",
        check_command="python3 scripts/stats/check_repro.py",
        work=(
            "Regenerates descriptive supplementary statistics for the three "
            "overview benchmark slices into a temporary directory and byte-diffs "
            "them against the committed results/supplementary artifacts."
        ),
        input_paths=(
            "benchmarks/01-short-factual/runs/*.json",
            "benchmarks/02-multi-step-reasoning/runs/*.json",
            "benchmarks/03-tool-using-agent/runs/*.json",
            "benchmarks/*/judge_runs/*.json",
            "pricing/azure-openai-payg-2026-05.yaml",
        ),
        sample_unit=(
            "Raw per-call Responses API records joined to judge rows, grouped "
            "by benchmark, model, effort, authored sample, and repeat."
        ),
        output_paths=(
            "results/supplementary/*/bootstrap_ci.json",
            "results/supplementary/*/cohens_d.json",
            "results/supplementary/*/inter_rater.json",
        ),
        result_contract=(
            "Deterministic descriptive JSON: no timestamps, sorted keys, "
            "explicit empty-cell skips, and honest manual_scores_missing status "
            "when no human spot checks are committed."
        ),
    ),
)


ARTICLE_TOPICS: tuple[ArticleTopic, ...] = (
    ArticleTopic(
        slug="when-reasoning-pays-off",
        article_paths=("docs/blog/articles/when-reasoning-pays-off/index.html",),
        benchmark_numbers=("01", "02", "03"),
        chart_family_keys=("cost-curves-effort", "token-composition", "ptu-payg-crossover"),
        source_paths=("docs/blog/articles/when-reasoning-pays-off/numeric-claims.json",),
        generator_keys=("effort-evidence", "ptu-payg-planning"),
    ),
    ArticleTopic(
        slug="short-factual-work",
        article_paths=("docs/blog/articles/when-reasoning-pays-off/topics/short-factual-work/index.html",),
        benchmark_numbers=("01",),
        chart_family_keys=("cost-curves-effort", "token-composition"),
        source_paths=(
            "benchmarks/01-short-factual/analysis.json",
            "results/supplementary/01-short-factual",
        ),
        generator_keys=("effort-evidence",),
    ),
    ArticleTopic(
        slug="invisible-reasoning-tokens",
        article_paths=("docs/blog/articles/when-reasoning-pays-off/topics/invisible-reasoning-tokens/index.html",),
        benchmark_numbers=("01", "02", "03"),
        chart_family_keys=("token-composition",),
        source_paths=("pricing/azure-openai-payg-2026-05.yaml",),
        generator_keys=("effort-evidence",),
    ),
    ArticleTopic(
        slug="multi-step-work",
        article_paths=("docs/blog/articles/when-reasoning-pays-off/topics/multi-step-work/index.html",),
        benchmark_numbers=("02",),
        chart_family_keys=("cost-curves-effort", "token-composition"),
        source_paths=(
            "benchmarks/02-multi-step-reasoning/analysis.json",
            "results/supplementary/02-multi-step-reasoning",
        ),
        generator_keys=("effort-evidence",),
    ),
    ArticleTopic(
        slug="tool-agent-ceiling-checks",
        article_paths=("docs/blog/articles/when-reasoning-pays-off/topics/tool-agent-ceiling-checks/index.html",),
        benchmark_numbers=("03",),
        chart_family_keys=("cost-curves-effort", "token-composition"),
        source_paths=(
            "benchmarks/03-tool-using-agent/analysis.json",
            "results/supplementary/03-tool-using-agent",
        ),
        generator_keys=("effort-evidence",),
    ),
    ArticleTopic(
        slug="ptu-payg-planning",
        article_paths=("docs/blog/articles/when-reasoning-pays-off/topics/ptu-payg-planning/index.html",),
        benchmark_numbers=("01", "02", "03"),
        chart_family_keys=("ptu-payg-crossover", "cost-curves-effort"),
        source_paths=(
            "docs/13-ptu-vs-payg-decision-runbook.md",
            "pricing/azure-openai-payg-2026-05.yaml",
            "pricing/azure-openai-ptu-2026-05.yaml",
            "pricing/ptu-density-2026-05.yaml",
        ),
        generator_keys=("ptu-payg-planning",),
    ),
    ArticleTopic(
        slug="bridge-from-measurement-to-production",
        article_paths=("docs/blog/articles/when-reasoning-pays-off/topics/bridge-from-measurement-to-production/index.html",),
        benchmark_numbers=("01", "02", "03"),
        chart_family_keys=("cost-curves-effort", "token-composition", "ptu-payg-crossover"),
        source_paths=("docs/09-operator-guide-one-page.md", "docs/13-ptu-vs-payg-decision-runbook.md"),
        generator_keys=("effort-evidence", "ptu-payg-planning"),
    ),
    ArticleTopic(
        slug="ptu-retry-after-recovery",
        article_paths=("docs/blog/articles/ptu-retry-after-recovery/index.html",),
        source_paths=(
            "benchmarks/08-retry-after-characterization/analysis.json",
            "results/retry-after-characterization/retry_after_ms_percentiles.csv",
            "results/retry-after-characterization/retry_after_ms_events.csv",
        ),
    ),
    ArticleTopic(
        slug="prompt-cache-key-bucketing",
        article_paths=("docs/blog/articles/prompt-cache-key-bucketing/index.html",),
        source_paths=(
            "results/cache-key-bucketing/cache_hit_ratio_vs_cardinality.csv",
            "results/cache-key-bucketing/ttft_p95_vs_cardinality.csv",
            "benchmarks/06-cache-key-bucketing/analysis.md",
        ),
    ),
    ArticleTopic(
        slug="prompt-cache-retention",
        article_paths=("docs/blog/articles/prompt-cache-retention/index.html",),
        source_paths=(
            "results/cache-key-bucketing/cache_hit_ratio_vs_cardinality.csv",
            "results/cache-key-bucketing/ttft_p95_vs_cardinality.csv",
            "docs/12-prompt-cache-key-policy.md",
        ),
    ),
    ArticleTopic(
        slug="reasoning-migration-sizing",
        article_paths=("docs/blog/articles/reasoning-migration-sizing/index.html",),
        source_paths=(
            "scripts/ptu_sizing.py",
            "pricing/ptu-density-2026-05.yaml",
            "docs/13-ptu-vs-payg-decision-runbook.md",
        ),
    ),
)


def benchmark_numbers() -> tuple[str, ...]:
    return tuple(slice_.number for slice_ in BENCHMARK_SLICES)


def benchmark_slice(number: str) -> BenchmarkSlice:
    for slice_ in BENCHMARK_SLICES:
        if slice_.number == number:
            return slice_
    raise KeyError(f"unknown benchmark slice: {number}")


def chart_family(family_key: str) -> ChartFamily:
    for family in CHART_FAMILIES:
        if family.family_key == family_key:
            return family
    raise KeyError(f"unknown chart family: {family_key}")


def target_topic_slug(family_key: str) -> str:
    return chart_family(family_key).evidence_topic_slug


def generator_family_keys(generator_key: str) -> frozenset[str]:
    return frozenset(
        family.family_key for family in CHART_FAMILIES if family.generator_key == generator_key
    )


def chart_benchmark_numbers(family_key: str) -> tuple[str, ...]:
    return chart_family(family_key).benchmark_numbers


def article_topic(slug: str) -> ArticleTopic:
    for topic in ARTICLE_TOPICS:
        if topic.slug == slug:
            return topic
    raise KeyError(f"unknown article topic: {slug}")


def generator_spec(key: str) -> GeneratorSpec:
    for spec in GENERATOR_SPECS:
        if spec.key == key:
            return spec
    raise KeyError(f"unknown generator spec: {key}")
