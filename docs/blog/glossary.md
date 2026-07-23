# Terminology Glossary (frozen)

Frozen from the exp001-short-factual note across all five locales. **One concept =
one term per language.** When a term already drifts in the source, the row records the
single frozen choice and the variants to retire during the polish pass. Proper nouns
(PAYG, PTU, gpt-4o, gpt-5.2, SHA-256, API, GitHub) stay Latin in every locale. The
effort-enum tokens `none · low · medium · high · xhigh` stay Latin letters everywhere
(never transliterate).

| Concept (EN) | EN | KO | JA | ZH | HI |
|---|---|---|---|---|---|
| reasoning effort | reasoning effort | 추론 강도 | 推論努力度 | 推理强度 | तर्क-प्रयास (reasoning effort) |
| the effort dial (metaphor) | the (effort) dial | 손잡이 | つまみ | 旋钮 | घुंडी |
| floor (easiest benchmark) | floor | 바닥 | 床 | 底部 | तल (floor) |
| ceiling (hardest benchmark) | ceiling | 천장 | 天井 | 天花板 | सीलिंग |
| Null case (benchmark-01 name) | Null case | Null 케이스 | Null case | Null case | Null case |
| judge model | judge model | 심판 모델 | 審判モデル | 评审 모델 → **评审模型** | जज मॉडल |
| judge score | judge score | 심판 점수 | 審判スコア | 评审分数 | जज स्कोर |
| pre-registered prediction | pre-registered prediction | 사전 등록한 예측 | 事前登録した予測 | 预先登记的预测 | पूर्व-पंजीकृत पूर्वानुमान |
| fixture (placeholder value) | fixture | 픽스처 | フィクスチャ | fixture | fixture |
| measured (real run) | measured | 실측 | 実測 | 实测 | मापा गया (measured) |
| accuracy | accuracy | 정답률 | 正答率 | 正确率 | सटीकता |
| saturate / plateau | saturate | **더는 오르지 않는다** (plain) | 頭打ち | 不再上升 / 饱和 | और नहीं बढ़ता |
| passing bar | passing bar | 합격선 | 合格線 | 门槛 | उत्तीर्ण-रेखा |
| PAYG (pay-as-you-go) | PAYG | PAYG | PAYG | PAYG | PAYG |
| PTU (provisioned units) | PTU | PTU | PTU | PTU | PTU |
| pays off / worth it (motif) | pays off / worth it | 값을 하다 (2–3×만) | 値を返す / 費用に見合う | 划算 / 回本 | लागत वसूल करना |

## Frozen drift fixes (apply during the relevant article pass)

- **JA judge**: source mixes 審判 (15×) and ジャッジ (3×). Freeze **審判**; retire ジャッジ.
- **ZH judge**: source mixes 评审 (dominant) and 裁判分数 (2×). Freeze **评审(模型/分数)**;
  retire 裁判.
- **KO saturate**: retire the Sino-Korean 포화 in prose → **더는 오르지 않는다 / 천장에 닿다**.
- **KO Sino→plain** (per KO desk brief): 확증→그대로 맞음 · 상쇄→서로 깎아먹음 ·
  지문화→해시로 고정. Keep for consistency wherever they recur.
- **"·" middot**: a notation separator only inside the effort-enum ladder
  (`none · low · medium · high · xhigh`). In prose use each language's own separator:
  KO comma, JA/ZH 「・」/「、」, HI comma or और.

## First-use gloss policy
On first appearance per page, gloss hard terms once in-language: KO PAYG(사용한 만큼 과금)·
PTU(고정 용량)·심판 모델·사전 등록·픽스처; JA/ZH/HI likewise in their own register.
Do not repeat the gloss on later mentions.
