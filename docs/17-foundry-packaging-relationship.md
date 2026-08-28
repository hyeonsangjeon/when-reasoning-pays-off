# Microsoft Foundry packaging relationship — public research repo, Pages, and the downstream Microsoft Foundry sample

![Track A research repo and its surfaces bridged to the downstream Track B Microsoft Foundry sample.](assets/foundry-track-a-track-b-bridge.svg)

**Companion to `docs/16-release-tiers-and-redaction-policy.md`.** That
document defines the three release tiers (`RAW_PRIVATE`,
`SANITIZED_PUBLIC`, `AGGREGATE_AZURE_SAMPLE`), the redaction rules,
the raw-data preservation rule, and the public-surface tier permission
matrix. This document defines the public channel relationship that
remains in this mirror: how the public research repo, the GitHub Pages
dashboard / blog, and the downstream Microsoft Foundry sample repo relate
to each other; which surface is canonical for which kind of content; and
the acceptance bar each surface inherits.

External syndication or scholarly-submission planning is intentionally
out of scope for the public mirror. Keep that planning in the private
release workspace until a separate publication surface is explicitly
approved.

---

## 1. Two tracks, three public surfaces

Two downstream tracks remain public here: **Track A — public research
repo** (this repo, made public; methodology, hypothesis ledger,
decision tools, citation taxonomy, sanitized benchmark slices) and
**Track B — downstream Microsoft Foundry sample repo** (separately named,
separately governed; Microsoft Foundry-conformant structure, aggregate-only data,
runnable notebooks cross-linked to public Azure documentation).

Track A has two public surfaces in this mirror: the repository itself
and the **GitHub Pages dashboard / blog**. Track B is the downstream
operator-facing Microsoft Foundry sample. Each surface has a defined tier allowance
(`docs/16-release-tiers-and-redaction-policy.md` §4) and a defined
channel role here.

```
   private working tree (raw archive + redaction rules)
            │                              │
   SANITIZED│                              │AGGREGATE
            ▼                              ▼
   Track A — public          Track B — Microsoft Foundry
   research repo             sample repo (downstream)
       │ canonical                ▲ cites
       ▼                          │
   GitHub Pages dashboard / blog ─┘
```

---

## 2. Track A ↔ Track B invariants

1. **Track B never sources from the private working tree.** It sources
   from Track A (`SANITIZED_PUBLIC` re-aggregated to
   `AGGREGATE_AZURE_SAMPLE`) or from a designated
   `AGGREGATE_AZURE_SAMPLE` export first published on Track A.
2. **Track A is the source of methodological truth.** Methodology,
   hypothesis ledger, decision tools, citation taxonomy, and
   per-request evidence live on Track A; Track B packages decisions and
   aggregates for operators and cross-links to Track A for depth.
3. **One-way propagation.** Methodology changes land on the private
   working tree → Track A → Track B (never reversed). Track B may evolve
   its Microsoft Foundry packaging independently as long as no methodology drift is
   introduced.
4. **Bidirectional citation cross-link.** Track A's public-facing pages
   name the Microsoft Foundry sample repo as the operator-facing downstream
   product; Track B's `README.md` and `PROVENANCE.md` name the Track A
   repo and the pinned upstream commit SHA.

---

## 3. Track B — Microsoft Foundry sample acceptance bar

- Microsoft sample-repo `LICENSE` (verified with the Microsoft Foundry samples
  program at packaging time); Microsoft Open Source `CODE_OF_CONDUCT.md`;
  `SECURITY.md` pointing at the Microsoft MSRC vulnerability disclosure
  flow.
- `README.md` structured per the Microsoft Foundry sample template (overview →
  prerequisites → run → expected outputs → cleanup → next steps → links
  to public Azure documentation).
- Notebook(s) runnable end-to-end on Microsoft Foundry with a pinned
  dependency manifest; no undeclared local-toolchain assumptions.
- Per-file `SPDX-License-Identifier` headers; no reference to private
  customer engagements, private communication channels, or private task
  identifiers.
- `AGGREGATE_AZURE_SAMPLE` data only; per-request rows are forbidden.
- `PROVENANCE.md` linking back to the Track A public research repo at the
  pinned upstream commit SHA.

Track A and Track B licenses are independently chosen but MUST be
mutually compatible; license selection happens before Track B packaging
starts.

---

## 4. Track A — GitHub Pages dashboard / blog (canonical channel)

The Pages site is the **canonical public source of truth** for every
public-facing essay, analysis writeup, chart series, and numeric claim
attached to Track A. Updates to a public claim land on Pages and the
public research repo first; the downstream Microsoft Foundry sample follows only
when the claim has an aggregate operator-facing form.

### 4.1 Tier boundary

Consumes **only** `SANITIZED_PUBLIC` and `AGGREGATE_AZURE_SAMPLE`;
`RAW_PRIVATE` MUST NOT appear in any page, chart series, shipped data
file, analytics payload, embedded JSON, or source map. The build MUST
fail if any input lacks a tier label, carries `RAW_PRIVATE`, or fails
the redaction-detector continuous integration (CI) job. The site MUST NOT introduce a new tier,
relabel artifacts, or bypass the redaction rules.

### 4.2 i18n-first requirements

- **Locales (initial set):** `ko`, `en`, `ja`, `zh-CN`, `hi`. "Indian
  language" is represented by **Hindi (`hi`)**; additional Indian
  languages are added later only by explicit owner decision through a
  separate task.
- **Locale-stable routes.** Every page is reachable at a locale-prefixed
  path (`/en/...`, `/ko/...`, `/ja/...`, `/zh-cn/...`, `/hi/...`); the
  locale prefix is part of the canonical URL. The unprefixed path either
  redirects to a chosen default locale or renders a locale picker, but
  does not serve translated content from an unprefixed route.
- **`hreflang` and canonical metadata.** Every page emits
  `<link rel="alternate" hreflang="…">` for every available locale,
  plus `hreflang="x-default"`, plus `<link rel="canonical">` to the
  current locale's URL; build fails on missing entries.
- **Translation status metadata.** Each translation carries status
  (`translated`, `machine_translated`, `stale`,
  `untranslated_fallback_to_en`) and a `last_translated_at` timestamp;
  status is surfaced visually (banner) when not `translated`.
- **Source-content hash (drift guard).** Each canonical content unit
  carries a stable content hash recorded at translation time; build CI
  compares the live source hash to the stored hash and marks the
  translation `stale` on mismatch.
- **Shared chart data, localized labels.** Chart series data files are
  **locale-agnostic** (numeric series + metric / dimension keys);
  per-locale label bundles supply axis titles, legends, tooltips, units,
  number / date formatting. One dataset, N label bundles.
- **Glossary (terminology lock).** The site ships a glossary covering at
  minimum **Foundry**, **PTU**, **PAYG**, **reasoning**, **cache**, **429**,
  with a fixed per-locale translation; in-text term renderings link to
  glossary entries.

---

## 5. Channel-relationship invariants (summary)

1. **Pages is canonical for public narrative.** Public-facing essays,
   analyses, chart series, and numeric claims land on Pages first (and
   on the public research repo first for code / data).
2. **The public research repo is canonical for evidence.** Methodology,
   schemas, scripts, sanitized records, aggregate summaries, and release
   manifests live here.
3. **Microsoft Foundry sample is the operator-facing downstream product.** It is
   `AGGREGATE_AZURE_SAMPLE` only and receives one-way propagation from
   the public research repo to the sample repo.
4. **All public surfaces inherit** the tier permissions and redaction
   rules defined in `docs/16-release-tiers-and-redaction-policy.md`.

---

## 6. Citation and archive plan (at minimum)

For each public publication of substance, the implementing task names a
citation plan covering: the public research repo by URL + commit SHA;
the Pages article by its locale URL; an archived-release DOI path when a
stable external citation target is required; and the Microsoft Foundry sample repo
by URL + commit SHA where referenced.

---

## 7. Relationship to other documents

- `docs/16-release-tiers-and-redaction-policy.md` — tier definitions,
  redaction rules, raw-data preservation, per-surface tier permission
  matrix, governance / readiness checklist.
- `docs/05-methodology.md` (frozen) — not modified; Track B does not
  author methodology and cites the public research repo for depth.
- `docs/14-observability-schema.md` — record fields whose tier
  classification lives in `docs/16-release-tiers-and-redaction-policy.md` §3.
- `docs/15-spec-vs-inference-taxonomy.md` — every published artifact
  carries both a claim-authority label and a release tier label.
