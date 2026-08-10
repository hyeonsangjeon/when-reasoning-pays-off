# Agentic loop budget governance — thresholds, intervention, and traceability for unbounded reasoning loops

Your agent passes every demo. Then one afternoon a single support ticket sends
it into a loop — search, re-read, call another tool, think a little harder, try
again — and forty tool calls later it answers. The answer is correct. It also
cost forty times what you budgeted for that task, and no one noticed until the
monthly invoice. Nothing *failed*: no exception, no 500, no alert. That is what
makes a runaway loop dangerous — it looks like a working agent right up until
you read the bill.

A large context window and an open-ended tool loop are what make that afternoon
possible, and the instinct when an agent underperforms is the familiar one: let
it loop a little longer, let it think a little harder. This document is about
the other half of that instinct — deciding *when to stop*, and making that
decision something you own rather than something the model reaches by default.

**Companion to [`docs/04-decision-framework.md`](04-decision-framework.md) and
[`docs/09-operator-guide-one-page.md`](09-operator-guide-one-page.md).** The
decision framework routes a **single call** to a `(model, effort)` cell, and the
operator guide's levers L1–L5 each tune **one call or one deployment**. This
document covers the axis those two leave open: governing a workload that issues
**many calls in a loop** — a tool-using or agentic task whose iteration count
and context size are not fixed in advance. `docs/04` §4 already names this as out
of the per-call framework's scope ("long-form generation", "function-calling
beyond simple calculator … retries … partial-failure recovery"); what follows is
the governance design for it.

> **Claim authority (per [`docs/15-spec-vs-inference-taxonomy.md`](15-spec-vs-inference-taxonomy.md)).**
> This is a **Tier 2 — operational inference / design** document. It introduces
> **no new measured magnitudes.** Every measured number it references is cited
> to its owning `analysis.md` / `results/summary.md`, which carry their own tier
> labels. The pattern below is grounded in primitives that **already exist in
> this repo's measurement runner**, not in a new benchmark.

---

## 1. Who this is for, and why this document exists

You run a workload where a **single task spawns a loop of model calls** — plan →
call tool → observe → re-reason → … → answer — rather than one tidy
request/response. The loop is what delivers the result, and most days it
behaves. The question this document answers is the one that only shows up under
load, or on a pathological input: **when does the loop stop, and who decided?**

If you operate one of these workloads, you have probably already reached for the
per-call levers and found they don't quite bite:

- `reasoning_effort` (lever **L4**) bounds the reasoning tokens of **one call**.
  It says nothing about **how many calls** the loop makes.
- `max_output_tokens` (lever **L5**) bounds the output of **one call**. It does
  not bound the **cumulative** spend across iterations.
- Prompt-cache stability (**L3**) lowers the input cost of **one prefix**. In a
  loop the transcript grows every step, so the cacheable fraction shrinks
  exactly when the bill is largest.

None of that makes the per-call levers wrong — they are still the right first
move on any single call. They simply operate one layer below the loop. The
capability question ("can the model loop, can it hold a million tokens") is not
the hard one; the hard one is **traceability, evals, thresholds, intervention,
and governance** — the five surfaces in §3.

---

## 2. The runaway-cost failure mode (mechanism)

A loop surprises you because its bill is an **integral over steps**, not a single
number you can read off a pricing page:

```
total_cost ≈ Σ_step (  input_tokens_step      × input_rate
                     + reasoning_tokens_step   × output_rate
                     + output_tokens_step      × output_rate )
```

Three things make that sum grow faster than anyone expects:

1. **The iteration count is unbounded.** With no hard cap, a loop that fails to
   converge — a flaky tool, an ambiguous sub-goal, a "keep refining until it's
   right" instruction — just keeps issuing calls. Every call is billable, and
   nothing in the model stops it.
2. **The context grows every step.** Each iteration re-sends the prior
   transcript plus the new tool output as the next `input=`, so
   `input_tokens_step` climbs with every loop. The late steps are the most
   expensive ones, and a single byte change anywhere in the prefix flushes the
   cache (**L3**) so they bill at the full input rate.
3. **Effort is multiplied inside the loop.** Setting `reasoning_effort` high
   *inside* a loop multiplies the per-step reasoning tokens by the step count.
   The repo's headline finding — quality saturates below `high`/`xhigh`
   ([`results/summary.md`](../results/summary.md) §3) — applies **per step**, so
   a high default is the most expensive place to be wrong.

The per-call levers each attack one term of that sum. What they cannot do is
bound the **number of terms** or the **running total** — and that is precisely
the gap a loop opens.

---

## 3. The five governance surfaces

Each surface below is written in the operator-guide idiom — **Mechanism /
Action / In-repo evidence** — and opens with the moment you actually hit it.
**This repository's own measurement runner already implements all five**,
because a benchmark batch is itself an unbounded-cost loop that must not run
away. The production pattern comes directly from the runner's guard.

### 3.1 Thresholds — a step cap and a cumulative cost ceiling

*You hit this the first time one task quietly costs 40× its neighbors.*

**Mechanism.** A loop needs **two** bounds a single call never does: a maximum
iteration count, and a cumulative token/cost ceiling for the whole task. Either
one alone leaks — a step cap still allows an expensive runaway if each step is
huge; a cost ceiling alone still allows an endless cheap spin.

**Action.** Declare, per task class, a `max_iterations` step cap and a
`hard_ceiling_usd` cumulative ceiling. Derive the ceiling from the p99 of a
representative sample (step count and per-task cost), not from a round number
that feels safe. Set both conservatively and raise them only with evidence
(§3.5).

**In-repo evidence.** [`scripts/run_benchmark.py`](../scripts/run_benchmark.py)
already does exactly this: a pre-run estimate (`estimate_experiment_cost_usd`)
gated by `MAX_COST_PER_BENCHMARK_USD`, a mid-run `hard_ceiling_usd` tracked by
the `BudgetTracker` dataclass, and a tool-loop `max_iterations` cap
(`agent.max_tool_iterations`). [`docs/05-methodology.md`](05-methodology.md) §6
"Budget guards" treats this as **methodology, not hygiene**: "a silent budget
overrun is also a silent change in experiment scope." The same sentence holds in
production — a silent loop overrun is a silent change in what the task was
allowed to cost.

**Azure docs.** <https://learn.microsoft.com/en-us/azure/ai-foundry/openai/concepts/provisioned-throughput>

### 3.2 Intervention — a circuit-breaker that fails closed

*You meet this the first time a loop spins all night instead of erroring out.*

**Mechanism.** When a threshold is crossed mid-loop, the loop has to stop
**deterministically and visibly** — return a best-effort partial answer plus a
typed "budget exceeded" signal — never silently keep going, and never silently
truncate. That choice is the whole difference between a bounded cost and a
runaway.

**Action.** Build a circuit-breaker that aborts the loop on **any** of: the step
cap is reached, cumulative cost ≥ `hard_ceiling_usd`, or a no-progress detector
fires (for example, the same tool call repeating). On abort, emit a **typed
terminal record** with the termination reason, then make one final answer-only
call so the caller still gets something usable. **Fail closed:** when in doubt,
the default is *stop*, not *continue*.

**In-repo evidence.** The runner raises a typed `BudgetExceededError` ("Raised
when pre-run estimate or running total breaches a budget guard"), and
`BudgetTracker.is_halted` enforces a halt **before the next call** once the total
crosses the ceiling; the process exits with a dedicated `EXIT_BUDGET` code. The
tool loop is a clean model of the same idea: on `iterations >= max_iterations` it
"fire[s] one final call WITHOUT `tools=` so the model is forced to emit a final
answer", records `tool_loop_terminated="iteration_cap"`, and appends the recovery
leg to the trajectory. That is a fail-closed breaker that leaves a reason
behind — lift it into production as-is.

**Azure docs.** <https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/reasoning>

### 3.3 Evals — escalate only where it pays

*This one arrives the day "just let it think harder" stops helping but keeps
billing.*

**Mechanism.** The repo's central result is that effort above the mid-range
raises **cost without improving quality.** On benchmark-03, quality held to a
ceiling near 1.97–2.00
across the whole effort ladder while per-request cost rose from $0.002762 to
$0.003499 and latency from 2.9 s to 3.8 s
([`results/summary.md`](../results/summary.md) §3;
[`benchmarks/03-tool-using-agent/analysis.md`](../benchmarks/03-tool-using-agent/analysis.md)).
In a loop the same finding governs the *continuation* decision: another step, or
a higher effort on the next call, should have to earn its place.

**Action.** Gate escalation — one more iteration, or a higher `reasoning_effort`
on the next call — behind a **cheap eval**: a rubric score, a self-consistency or
confidence signal, an explicit stop check. Default to **stop**, and make the
eval argue for continuing. That turns an open-ended "loop until done" into a
bounded "loop until the eval stops paying", which is just the decision-framework
routing tree (`docs/04` §2) in loop form.

**In-repo evidence.** [`scripts/run_judge.py`](../scripts/run_judge.py) is the
rubric judge already scoring quality in every benchmark; the same shape is the
natural continuation gate. The 1.97–2.00 ceiling is the evidence that an ungated
"think harder, loop again" default keeps spending well past the point of return.

### 3.4 Traceability — attribute cost per loop, per step

*This shows up the first time you face a $40 task and can't say which step
caused it.*

**Mechanism.** You cannot govern what you cannot attribute. A single
end-of-task number hides **which step** ran away and **why**. Governance needs
per-step records keyed to a stable loop id, each carrying the step's usage, the
running cumulative cost, and the budget headroom left.

**Action.** Extend the per-request capture in
[`docs/14-observability-schema.md`](14-observability-schema.md) with
**loop-scoped fields**: a stable `loop_id` / task id, a monotonic `step_index`,
the per-step `usage` block (already captured — `reasoning_tokens`,
`cached_tokens`, …), a `cumulative_cost_usd`, and a `budget_remaining_usd`. Emit
one JSONL row per step; the loop's bill is the sum, and the row where
`budget_remaining_usd` first goes non-positive is your intervention point. Alarm
on the **distribution** of `step_index` at termination, not just its mean — the
tail is where the runaways live.

**Scope note.** Shipping those fields is the **next increment**, not this
document; option 1 here is design only. They slot onto the existing
`PTURequestRecord` (which already carries `request_idx` and the full `usage`
block) and `schemas/ptu_request_record.schema.json`, and writing them is the
natural follow-on task.

**In-repo evidence.** The runner already **sums the per-iteration `usage` objects
into a single cell-level `usage` dict** and records a per-iteration trajectory;
loop traceability is that roll-up made first-class — keep the per-step rows,
don't only keep the sum.

### 3.5 Governance — the budget is an owned contract

*And this one lands when someone bumps a ceiling in a hotfix and the bill moves
with no paper trail.*

**Mechanism.** Step caps and cost ceilings are **policy**, not magic numbers.
They have to be owned, version-controlled, and changed through review — like any
other contract that decides what the system is allowed to do. A ceiling raise is
a scope change, and it should feel like one.

**Action.** Declare per-task-class budget policy in **committed config**, not in
constants scattered across services. Review every ceiling change; track the
**overrun rate** (how often the breaker fires) as a first-class SLO; and give a
ceiling raise the same ceremony as a methodology change
([`GOVERNANCE.md`](../GOVERNANCE.md); the "silent overrun = silent scope change"
rule from `docs/05` §6). The release-governance discipline in
[`docs/16-release-tiers-and-redaction-policy.md`](16-release-tiers-and-redaction-policy.md)
is the same shape applied to published data; this applies it to spend.

---

## 4. Operator lever L6 — loop / budget governance (one-line summary)

`docs/09` defines five levers that each tune **one call or one deployment**.
**L6 is the first that governs the *loop itself*:**

> **L6. Loop / budget governance.** Wrap any multi-call (tool-using / agentic)
> task in a per-task **step cap** and **cumulative cost ceiling**, a
> **fail-closed circuit-breaker** that records why it fired, **eval-gated**
> continuation, and **per-step cost traceability** — owned as committed,
> reviewed policy. L1–L5 bound each call; **L6 bounds the loop.**

The mechanism, action, and in-repo evidence for each piece are §3 above.

---

## 5. A worked decision (no new numbers)

Take Example B from `docs/04` §3 — a multi-tool workflow agent (retrieve CRM data
→ do arithmetic on it → draft a follow-up email, 2–4 tools per task). The
per-call answer doesn't change: **gpt-5.2 `low`** is still the defensible cell
([`docs/04`](04-decision-framework.md) §3, from
`benchmarks/03-tool-using-agent/analysis.md`). L6 simply wraps a bound around the
loop that framework already routed:

1. **Thresholds.** `max_iterations` = the p99 tool-loop length on a
   representative sample; `hard_ceiling_usd` = p99 per-task cost at the gpt-5.2
   `low` per-correct figure already published in benchmark-03 (cited, not
   re-derived).
2. **Intervention.** A breaker that aborts on cap / ceiling / no-progress and
   hands back the best partial draft with a typed reason.
3. **Evals.** Continue to another tool iteration only when the `run_judge`-style
   rubric says the draft isn't acceptable yet — otherwise stop.
4. **Traceability.** One per-step row with `loop_id`, `step_index`,
   `cumulative_cost_usd`, `budget_remaining_usd`.
5. **Governance.** Both thresholds live in committed config; raising them is a
   reviewed change.

No measured magnitude moves — the per-correct numbers come straight from the
existing analysis. L6 only adds the **bound** around the loop.

---

## 6. When this does NOT apply / open questions

This is **operational design (Tier 2)**, not a measurement. The following are
**unmeasured in this repo**, and they are stated as honest gaps rather than quiet
claims:

- **How often loops actually run away**, and the **distribution of step counts**
  by task shape. A future *unbounded-loop runaway characterization* benchmark
  would measure these; it does not exist yet.
- **The long-context cost-scaling curve** (cost vs. context length within a
  loop). `docs/04` §4 already records that no benchmark here measures
  long-output / long-context workloads.
- **No-progress detection heuristics.** §3.2 names the mechanism; the right
  detector — repeat-call hashing, embedding-distance stall, rubric plateau — is
  workload-specific and unmeasured here.

If you want measured magnitudes for any of these, treat this document as the
**design contract** that a measurement would validate — the same relationship
`docs/07` (hypotheses) has to the benchmarks that test them.

---

## 7. Relationship to other documents

- [`docs/04-decision-framework.md`](04-decision-framework.md) — routes a single
  call to a `(model, effort)` cell; L6 wraps the loop around that cell.
- [`docs/05-methodology.md`](05-methodology.md) §6 "Budget guards" — the pre-run
  estimate + mid-run hard-ceiling abort this document generalizes.
- [`docs/09-operator-guide-one-page.md`](09-operator-guide-one-page.md) — levers
  L1–L5 (per call / per deployment); L6 is introduced there as a pointer here.
- [`docs/14-observability-schema.md`](14-observability-schema.md) — the
  per-request record the per-step traceability fields (§3.4) extend.
- [`docs/15-spec-vs-inference-taxonomy.md`](15-spec-vs-inference-taxonomy.md) —
  this document is Tier 2; every measured number it cites carries its own tier.
- [`docs/16-release-tiers-and-redaction-policy.md`](16-release-tiers-and-redaction-policy.md)
  — the governance discipline in §3.5, applied to spend instead of data.
- [`GOVERNANCE.md`](../GOVERNANCE.md) — the owned-contract change-control model
  budget policy follows.
