# Polish Progress

Editorial polish pass so each language edition reads as if written natively in that
language, not translated. Unit = one article = 5 locale editions (EN + ko/ja/hi/zh-CN).
Order per article: KO → EN → JA → ZH → HI. Work on branch `polish/editorial-pass`,
one commit per article, single PR at the end.

## 아티클 단위 (한 아티클 = 5개 언어판 한 묶음)

- [ ] landing — docs/{en,ko,ja,hi,zh-CN}/index.html
- [ ] articles-index — docs/blog/articles/{index.html,ko,ja,hi,zh-CN}/index.html
- [ ] when-reasoning-pays-off — docs/blog/articles/when-reasoning-pays-off/(en+ko+ja+hi+zh-CN)
- [ ] reasoning-effort-retrospective — docs/blog/articles/reasoning-effort-retrospective/(5)
- [ ] exp001-short-factual — .../reasoning-effort-retrospective/experiments/exp001-short-factual/(5)
- [ ] exp002-multi-step-reasoning — .../experiments/exp002-multi-step-reasoning/(5)
- [ ] exp003-tool-using-agent — .../experiments/exp003-tool-using-agent/(5)
- [ ] reasoning-migration-sizing — docs/blog/articles/reasoning-migration-sizing/(5)
- [ ] prompt-cache-key-bucketing — docs/blog/articles/prompt-cache-key-bucketing/(5)
- [ ] prompt-cache-retention — docs/blog/articles/prompt-cache-retention/(5)
- [ ] ptu-retry-after-recovery — docs/blog/articles/ptu-retry-after-recovery/(5)
- [ ] topic-agentic-loop-budget-governance — when-reasoning-pays-off/topics/agentic-loop-budget-governance/(5)
- [ ] topic-bridge-from-measurement-to-production — .../topics/bridge-from-measurement-to-production/(5)
- [ ] topic-invisible-reasoning-tokens — .../topics/invisible-reasoning-tokens/(5)
- [ ] topic-multi-step-work — .../topics/multi-step-work/(5)
- [ ] topic-ptu-payg-planning — .../topics/ptu-payg-planning/(5)
- [ ] topic-short-factual-work — .../topics/short-factual-work/(5)
- [ ] topic-tool-agent-ceiling-checks — .../topics/tool-agent-ceiling-checks/(5)

## Scope notes
- Excluded: docs/assets, docs/blog/charts, docs/blog/data, *.json/css/js,
  validate.sh, repo-root README/CONTRIBUTING, and docs/NN-*.md numbered content docs
  (owned/allowlisted engineering docs, not the public site prose surface).
- Included prose-bearing surfaces only: language landings, blog article/topic/experiment
  HTML, and the articles index. `<title>`, meta description, and image `alt` are prose.
- Hard invariants: numbers, table structure, code/paths/commands, model names, experiment
  IDs, conclusions/verdicts, section count & order, correction history. Text nodes only.
