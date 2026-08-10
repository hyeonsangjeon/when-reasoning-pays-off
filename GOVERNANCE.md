# Governance

This repository follows a **single-owner ("BDFL") model with documented
escalation** for changes to frozen artifacts. The model is intentionally
small because this is a personally maintained research artifact, not a
multi-org project.

## Owner

`@hyeonsangjeon` is the sole maintainer. The owner has final decision
authority on:

- Methodology — `docs/05-methodology.md` (frozen; see Escalation below).
- Release policy — `docs/16-release-tiers-and-redaction-policy.md`.
- Foundry packaging relationship — `docs/17-foundry-packaging-relationship.md`.
- Repository licensing and any Track B / Foundry-sample derivative,
  if one is created.
- Public-flip decisions (visibility, mirrors, and the conditional
  policy for external publication channels such as GitHub Pages,
  Medium, and arXiv).

## Decision model

1. **Day-to-day code and docs changes.** Owner reviews and merges PRs.
   CI must pass. Reviewers (when present) leave non-blocking comments;
   owner has the final approval bit.
2. **Frozen artifacts.** Some files are frozen because they are the
   reproducibility contract. They are listed in `.github/CODEOWNERS` as
   owner-only. They are:
   - `docs/05-methodology.md` (the reproducibility contract — frozen
     since Task 001).
   - `docs/15-spec-vs-inference-taxonomy.md` (the citation tier
     convention used across all docs).
   - `docs/16-release-tiers-and-redaction-policy.md` (the public-release
     governance).
   - `docs/17-foundry-packaging-relationship.md` (the Track A ↔ Track B
     relationship contract).
   - Every `benchmarks/*/analysis.md` that has been approved (immutable
     post-APPROVE per `docs/16` §6).
3. **Release tiers.** Every artifact published to the public tree must
   carry a `SANITIZED_PUBLIC` or `AGGREGATE_AZURE_SAMPLE` tier label.
   `RAW_PRIVATE` content is **forbidden** in the public tree.
4. **No force-push.** Hard rule. Branch history rewrites are not
   performed on the public remote. The fresh-start option (publishing
   a new clean mirror, abandoning history on the public side while
   keeping the private working tree intact) is the documented lower-risk
   default per `docs/16` §6 and the redaction policy.

## Escalation: changing a frozen artifact

If a frozen artifact must change (for example, a methodology bug is
found that invalidates a published claim):

1. Open an issue using the "Hypothesis proposal" or "Reproducibility
   report" template, with a clear description of the defect and the
   public claims affected.
2. The owner decides whether to (a) accept the change, (b) reject it,
   or (c) require a separate replacement document that supersedes
   rather than mutates the frozen file. The frozen file is rarely
   edited in place; the usual remedy is to ship a successor document
   and add a "Superseded by" header to the original.
3. If accepted, the change lands on a branch named
   `frozen-amend/<short-description>` and is reviewed by the owner
   end-to-end. The changelog entry explicitly names the defect, the
   amendment, and the public claims revised.

## Reviewer roles

This repository has historically been developed with the help of a
multi-worker review pipeline (drafting, first review, code review,
methodology audit, release gating). The internal worker prompts that
formalized that pipeline are not part of the public surface and are not
required to use this repository. Public contributors interact only with
the owner via GitHub issues and pull requests.

## Continuity

The owner maintains a `LICENSE` file (MIT) that grants permissive reuse
of the published code and documentation in this repository. If the
repository becomes unmaintained, downstream users may fork under the
same license. See `LICENSE` for full terms.
