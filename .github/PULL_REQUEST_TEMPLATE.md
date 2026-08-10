<!--
Thanks for opening a pull request to this research repository.

Please complete the checklist below. PRs that omit the checklist may be
asked to fill it in before review.
-->

## Summary

<!-- One or two sentences: what changes and why. -->

## Related issue / task

<!-- Link to the issue this PR addresses, or "n/a" for a drive-by fix. -->

## Release-tier label (required for any published artifact)

If this PR adds or modifies a file under `docs/`, `benchmarks/`,
`results/`, `pricing/`, or `schemas/`, declare the tier per
[`docs/16-release-tiers-and-redaction-policy.md`](../docs/16-release-tiers-and-redaction-policy.md):

- [ ] `SANITIZED_PUBLIC` (per-request rows with redactions applied)
- [ ] `AGGREGATE_AZURE_SAMPLE` (aggregate-only; no per-request rows)
- [ ] Not applicable — this PR touches only code, tests, or governance files.

`RAW_PRIVATE` content is **forbidden** in the public tree. If you are
unsure whether a file qualifies as `RAW_PRIVATE`, treat it as if it does
and open an issue before pushing.

## Redaction check

- [ ] No endpoint hostnames, deployment names, request IDs, region
      identifiers, customer-shape fingerprints, or secret patterns
      appear in any added or modified file.
- [ ] `bash scripts/check_public_surface.sh` passes locally on my branch
      (or I have documented why it cannot).

## Citation tier (required for any new claim about Azure / OpenAI / Foundry behaviour)

Per [`docs/15-spec-vs-inference-taxonomy.md`](../docs/15-spec-vs-inference-taxonomy.md):

- [ ] Any new vendor-behaviour claim carries an `OFFICIAL_SPEC`
      (Tier 1) citation (vendor URL + ISO access date or a pinned SDK
      source identifier).
- [ ] Any new operationally-inferred claim carries an
      `OPERATIONAL_INFERENCE` (Tier 2) label with a rationale
      (≥ 20 chars) and in-repo evidence.
- [ ] Not applicable — this PR adds no vendor-behaviour claims.

## Tests

- [ ] `ruff check .` passes on changed Python files.
- [ ] `pytest -q -m "not adaptive_calibration" batch-runner/tests/` passes
      locally.
- [ ] If this PR adds new code, it is covered by new or updated tests
      under `batch-runner/tests/` or `tests/`.

## Frozen files

- [ ] This PR does **not** modify `docs/05-methodology.md`.
- [ ] This PR does **not** modify any approved `benchmarks/*/analysis.md`.

If either of the above is intentionally false, see
[`GOVERNANCE.md`](../GOVERNANCE.md) "Escalation: changing a frozen
artifact".

## Notes for reviewers

<!-- Anything reviewers should know. Out-of-scope changes, follow-ups, etc. -->
