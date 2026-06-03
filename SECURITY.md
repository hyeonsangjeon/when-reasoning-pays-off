# Security Policy

## Scope

This repository is a **research artifact**, not a production service. It
publishes reproducible benchmarks, methodology documents, decision tools,
and sanitized data slices for the question of when GPT-5.2-class reasoning
models earn their cost. Nothing in this repository is intended to run as a
hosted service or to ingest untrusted input from end users.

Two classes of issue are in scope for this policy:

1. **Data-leakage vulnerabilities** — anything in the public tree that
   leaks raw experiment data, endpoint hostnames, deployment names,
   request IDs, regions, customer-shape fingerprints, or any value that
   the redaction policy in
   [`docs/16-release-tiers-and-redaction-policy.md`](docs/16-release-tiers-and-redaction-policy.md)
   classifies as `RAW_PRIVATE`.
2. **Supply-chain or CI vulnerabilities** — anything in
   `.github/workflows/`, `pyproject.toml`, `.github/dependabot.yml`, or
   `scripts/` that could be exploited to exfiltrate secrets, run
   attacker-controlled code, or bypass the read-only CI permissions
   declared in `.github/workflows/ci.yml`.

Methodology disagreements, reproducibility concerns, and proposed new
hypotheses are not security issues. File those as ordinary GitHub issues
using the templates in `.github/ISSUE_TEMPLATE/`.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security reports.

Use GitHub's private vulnerability reporting feature for this repository
(Security tab → Report a vulnerability) if it is enabled, or open a private
report by emailing the maintainer (see GitHub profile contact for
`@hyeonsangjeon`).

Please include:

- A description of the issue and the public-tree path(s) involved.
- Steps to reproduce, if applicable.
- The commit SHA you observed the issue at.
- Whether the issue involves data leakage, CI exposure, or both.

## Response Targets

This is a personally maintained research repository. Response targets are
best-effort:

- **Acknowledgement of report:** within 7 calendar days.
- **Initial assessment:** within 14 calendar days.
- **Fix or mitigation for confirmed data-leakage issues:** prioritized
  ahead of any non-security work, with the redaction sweep workflow
  defined in `docs/16-release-tiers-and-redaction-policy.md` §3 used to
  produce the fix.

There is no paid bounty program. Public credit is offered (with
reporter permission) in the `CHANGELOG.md` entry that lands the fix.

## Out of Scope

- Vulnerabilities in third-party services this repository documents but
  does not control (Azure OpenAI, Microsoft Foundry, GitHub Actions,
  the GitHub web UI). Report those to the relevant vendor.
- Vulnerabilities in dependencies whose CVE is already tracked by GitHub
  Dependabot for this repository. Those are handled by the Dependabot
  update flow defined in `.github/dependabot.yml`.
- Issues in any downstream Track B Azure AI Foundry sample repository
  derived from this repository (see
  [`docs/17-foundry-packaging-relationship.md`](docs/17-foundry-packaging-relationship.md)).
  Those follow Microsoft's MSRC disclosure flow, not this policy.
