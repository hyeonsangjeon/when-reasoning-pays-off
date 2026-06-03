# Support

This repository is a research artifact maintained by a single owner. It is
not a hosted product and does not provide commercial support. The channels
below are best-effort.

## Where to go

| You want to … | Use … |
| --- | --- |
| Report a security or data-leakage issue | [`SECURITY.md`](SECURITY.md) — **do not** file a public issue. |
| Report a reproducibility problem (a script, benchmark, or result you cannot reproduce) | GitHub Issues → "Reproducibility report" template (`.github/ISSUE_TEMPLATE/reproducibility_report.yml`). |
| Propose a new hypothesis or extension to the cache-hit / PTU / cost-model framework | GitHub Issues → "Hypothesis proposal" template. |
| File a bug in code under `batch-runner/`, `scripts/`, or `tests/` | GitHub Issues → "Bug report" template. |
| Ask a methodology question | Open a GitHub Discussion (if enabled) or a low-priority Issue with the `question` label. Methodology is frozen in `docs/05-methodology.md`; changes go through the process in [`GOVERNANCE.md`](GOVERNANCE.md). |
| Cite this work | See the README's "Citation" section (when published) and [`docs/15-spec-vs-inference-taxonomy.md`](docs/15-spec-vs-inference-taxonomy.md) for the citation tier convention used throughout the docs. |

## What this repository is not

- Not a hosted service.
- Not a managed library with SemVer release commitments. Public Python
  modules under `batch-runner/batch_runner/` are stable in intent but may
  evolve as new measurements land.
- Not an official Microsoft / Azure / OpenAI product. If a separate Azure
  AI Foundry sample repository is created, it packages decision artifacts
  from this work under separate downstream governance; see
  [`docs/17-foundry-packaging-relationship.md`](docs/17-foundry-packaging-relationship.md).

## Response expectations

This is a personal research repository. Response times for non-security
issues are best-effort and may be measured in weeks rather than days.
Security issues are prioritized per [`SECURITY.md`](SECURITY.md).
