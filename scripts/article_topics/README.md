# Article-topic data builders

This directory is the public mirror's routing layer between reader-facing
article topics and the governed data generators that feed those articles.

The Pages site is organized by article topic. The measured data underneath it
is organized by benchmark slice and chart family. The files here keep that
translation explicit so a maintainer can answer four questions before changing
published evidence:

- Which article topic owns or cites this data?
- Which generator creates it?
- Which committed inputs does the generator read?
- What output contract must stay stable for Pages and reviewers?

## Files

| File | Role |
| --- | --- |
| `registry.py` | Canonical topic registry: benchmark slices, chart families, article topics, and generator specs. |
| `manifest.py` | Builds and checks a machine-readable manifest from the registry and `release/public_chart_candidates.json`. |
| `publication.py` | Shared publication helper for deterministic JSON, chart-candidate manifest upserts, and the public sanitized-manifest ledger. |
| `../build_article_topic_data.py` | Topic-oriented CLI that runs the right generator by article topic instead of by internal release tranche. |

## Generator specs

`registry.GENERATOR_SPECS` is the maintainer-facing contract for public data
generation. Each spec records:

- `command`: command that writes or refreshes the output.
- `check_command`: command that verifies committed output without rewriting it.
- `work`: what the generator does in article terms.
- `input_paths`: public inputs used by the generator.
- `sample_unit`: what one row or cell represents.
- `output_paths`: committed files produced or checked.
- `result_contract`: schema, tier, redaction, and interpretation constraints.

Current generators:

| Generator | What it does | Main outputs |
| --- | --- | --- |
| `effort-evidence` | Converts benchmark cost, latency, quality, throughput, and token-composition CSVs into numeric JSON for overview/evidence articles. | `results/public/chart-data/cost-curves-effort/**`, `results/public/chart-data/token-composition/**` |
| `ptu-payg-planning` | Builds modeled PTU/PAYG crossover data from public PAYG chart data plus pinned PTU/PAYG pricing snapshots. | `results/public/chart-data/ptu-payg-crossover/**` |
| `supplementary-stats` | Regenerates descriptive bootstrap/effect-size/inter-rater JSON and byte-checks it against committed supplementary artifacts. | `results/supplementary/**` |

## Common commands

```bash
python3 scripts/build_article_topic_data.py --all --check
python3 scripts/build_article_topic_data.py --topic invisible-reasoning-tokens --check
python3 scripts/build_article_topic_data.py --topic ptu-payg-planning --check
python3 scripts/article_topics/manifest.py --check
python3 scripts/article_topics/manifest.py --write docs/blog/data/article-topic-evidence-manifest.json
```

Use `--check` in CI and review. Omit it only when deliberately refreshing
committed public outputs.

## Boundaries

- This layer does not call Azure OpenAI or generate new model responses.
- This layer does not read private raw archives.
- Chart-data outputs must stay `SANITIZED_PUBLIC` and locale-agnostic.
- `ptu-payg-planning` emits a modeled planning lens, not measured PTU load-test
  throughput.
- The Foundry sample repo is downstream. It may consume aggregate outputs from
  this repo, but it is not the source of measurement methodology or evidence.
