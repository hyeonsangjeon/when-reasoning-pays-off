# Polish Changelog — Full-Site Editorial Pass (5 locales)

Goal: make every language edition read as if written natively in that language — not
translated — while keeping all numbers, code, structure, verdicts, and correction
history byte-for-byte intact. Work order per article: **KO first** (the author's
original voice), then EN → JA → ZH → HI brought to KO's *meaning* in each language's
own natural structure (never copying KO/EN sentence shape). Branch
`polish/editorial-pass`, one commit per article group.

Scope: 18 article groups, 81 changed HTML files across `docs/`. Terminology frozen in
`docs/blog/glossary.md`. Every group verified: tag skeleton, numeric multiset, and
`<code>/<pre>` inner text identical to the pre-edit baseline (only prose text nodes,
plus `alt`/meta description prose, changed).

## Per-language editorial moves

- **EN** — Broke Korean-cadence run-ons; replaced em dashes with colons/periods/parentheses
  (≤1 per section); converted paired-dash appositives to comma appositives; made section
  headings informative; kept the effort-ladder notation `none low medium high xhigh`.
- **KO** — Removed 무생물 주어 ("측정은 예측을 확증했다" → "측정해 보니 예측 그대로였다"); plain words
  for Sino-Korean jargon (확증→그대로 맞음, 상쇄→서로 깎아먹음, 지문화→해시로 고정, 포화→천장에 닿다/더
  오르지 않는다); prose middots → commas; kept the "값을 하다"/floor-ceiling motifs but un-stacked them.
- **JA** — Joined staccato韓国語リズム; unified 「だ・である」body register; frozen judge term
  審判 (retired ジャッジ); 韓国式「·」→ 全角「・」.
- **ZH** — Flowed short-sentence bursts with commas; frozen judge term 评审 (retired 裁判);
  韩式「·」→「、」; broke up 「的」chains; varied "不值得" with 不划算/回不了本.
- **HI** — Reduced over-Sanskritized文語 toward natural tech-media Hinglish; kept English
  tech terms (PAYG, PTU, fixture, dry-run) and the effort enum in Latin.

## Per-article groups (all across 5 locales unless noted)

| Group | Notes |
|---|---|
| landing | Polished KO/JA/ZH/HI bodies; **EN `<main>` left byte-stable** (already native; canonical source for the landing sha256 gate). |
| articles-index | Nav/section labels and blurbs naturalized; numbered-nav middots → periods, mirrored across 5 editions. |
| when-reasoning-pays-off (flagship) | Full de-Koreanization; kicker middot → colon. Effort tier `extra-high` → `xhigh` to match its own charts. Canonical `article:source-article-sha256` recomputed and re-pinned in 4 translations + `numeric-claims.json` (release validator: 19 claims OK). |
| reasoning-effort-retrospective (hub) | Already multi-pass polished this session; applied residual glossary drift only (審判/评审/plain KO). |
| exp001-short-factual | Glossary drift only (lede 무생물 주어 fix, 확증/상쇄/지문화/포화 → plain, 審判/评审). |
| exp002-multi-step-reasoning | Glossary drift only (지문화 → 해시로 고정, 審判/评审). |
| exp003-tool-using-agent | Glossary drift only (지문화 → 해시로 고정, 審判/评审). |
| reasoning-migration-sizing | Full pass; em-dash split-verb constructions rebuilt; headings made declarative. |
| ptu-retry-after-recovery | Full pass; runbook prose naturalized per locale. |
| prompt-cache-key-bucketing | Full pass; KO/HI/ZH separators localized; policy framing simplified. |
| prompt-cache-retention | Full pass; dash density reduced; JA body register unified. |
| topics/short-factual-work | Full pass; dial/floor metaphors landed; `extra-high` → `xhigh`. |
| topics/multi-step-work | Full pass; **restored KO effort enum** 없음/낮음… → `none/low/medium/high/xhigh`; 평가→심판. |
| topics/tool-agent-ceiling-checks | Full pass; `extra-high` → `xhigh` incl. chart-axis annotation. |
| topics/invisible-reasoning-tokens | Full pass; "hidden bill" metaphor per locale; `extra-high` → `xhigh`. |
| topics/ptu-payg-planning | Full pass; "not a capacity result" boundary re-set naturally; 포화/饱和 → glossary. |
| topics/agentic-loop-budget-governance | Full pass; direct EN; JA だ・である; ZH flow. |
| topics/bridge-from-measurement-to-production | Full pass; "bridge" metaphor landed as 연결/橋渡し/衔接 per locale. |

## 판단 기록 (borderline calls — logged, not silently changed)

1. **`extra-high` → `xhigh` (normalized).** The canonical effort ladder is
   `none · low · medium · high · xhigh` — the notation the briefing pins and the label the
   chart data uses (none/xhigh appear 20× each per benchmark; `extra-high` was a 1× stray).
   Prose reading "extra-high" was inconsistent with each page's own charts, so it was
   normalized to `xhigh` site-wide (flagship, short-factual-work, invisible-reasoning-tokens,
   tool-agent-ceiling-checks). All occurrences were prose (never inside `<code>`); numbers
   unchanged.
2. **`minimal` KEPT (not changed to `none`).** Although `minimal` is a non-canonical
   bottom-tier label on a few pages, in exp001 it appears as a real `<code>minimal</code>`
   API value, and elsewhere it risks colliding with the ordinary adjective. Default =
   keep the original rather than risk altering a real reference. Left as a known
   pre-existing inconsistency.
3. **Retrospective series (hub, exp001/002/003) — light touch.** These were already
   polished multiple times this session (회고체 → easy-read → lede split). Only the residual
   frozen-glossary drift was applied (JA ジャッジ→審判, ZH 裁判→评审, KO 포화/확증/상쇄/지문화→plain);
   otherwise left intact to avoid churning good prose ("better not to fix than fix wrong").
4. **EN site landing `<main>` untouched.** Already native-quality and the canonical source
   for the landing `i18n:source-content-sha256` release gate; editing it would force a
   hash recompute across all five landings for no editorial gain. Left byte-stable.
5. **`alt` / meta description treated as prose.** Polished as reader-facing copy; the
   invariant gate masks `alt`/`content` values so structural integrity (tags, classes,
   href, ids) is still enforced while permitting these prose edits.
6. **Series metaphors preserved, not flattened.** floor/ceiling, "1 더하기 1에 회의 소집",
   "a dial wired to nothing", the restaurant/handle easy-read analogies — kept and landed
   naturally in each language rather than normalized to a plain report.
7. **Effort enum stays Latin in all locales.** Restored KO tokens that had been translated
   (없음/낮음/중간/높음 → `none/low/medium/high`) in topics/multi-step-work; never transliterated
   in HI.

## Verification (final)

- Per-group invariant gate (`/tmp/verify_polish.py`): tag skeleton + numeric multiset +
  `<code>/<pre>` text identical to baseline — **OK for all 81 HTML files**.
- `bash docs/validate.sh` — **all checks passed** (i18n metadata, leakage grep, chart-sync,
  Pages chart checker, public-surface, landing sha256).
- `scripts/check_blog_article_release.py` — **passed**, 19 numeric claims intact, canonical
  article sha re-pinned.
- `tests/test_exp002_figure_consistency.py` + `tests/test_exp003_figure_consistency.py` —
  **38 passed**.
