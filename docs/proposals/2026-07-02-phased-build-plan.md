# Proposal — Phased Build Plan for the Tool-Design-First KB Architecture

## Status

Proposal, not an ADR. Task 4 of the 2026-07-02 ground-up-rebuild analysis. Companion document:
`2026-07-02-tool-design-first-kb-architecture.md` (Task 3 — the design this plan validates).

## Discipline

Prove the highest-risk, most novel assumption first, before building anything that depends on it.
Each phase below has an explicit pass/fail gate. **Failing a gate stops the plan at that phase** —
it does not mean "push through to the next phase anyway."

**This plan does not require deleting, replacing, or pausing the existing `services/kb-builder` /
`services/mcp-server` system at any phase.** Phases 0–2 validate new ideas against a read-only
corpus. Phase 3 adds one narrow, additive piece of orchestration to a process we already own. Phase
4 is a new agent, not a rewrite of an existing one. If every phase passes, the integration decision
— fold into the existing Postgres-truth system vs. something else — is a separate, later ADR, made
with evidence in hand rather than assumed up front.

---

## Phase 0 — Corpus selection

**Task.** Pick a real, messy repo to validate against. It doesn't need to be production code, just
real enough to be a fair test (per the original brief).

**Choice: this repository (`agentic-kb-platform`) itself.** It has genuine commit history, 29
ADRs recording real decisions (including reversals and amendments), multiple services with
different conventions, and known rough edges — exactly the kind of realism Phase 1's alias-mining
and Phase 2's A/B test need. Using it also means Phase 0 costs nothing beyond what's already true
today.

**Pass/fail:** N/A (setup), but the two preconditions are already confirmed as of this session:
- Git history is accessible and non-trivial (29 ADRs, dozens of PR briefs, multi-month commit
  history).
- Graphify installs and runs cleanly against this codebase: `uv sync` in `services/kb-builder`
  resolved 74 packages with no conflicts, and a code-only `graphify update --no-cluster` extraction
  produced well-formed output in this session. **Already done — Phase 0 is closed.**

---

## Phase 1 — Alias index + Graphify integration only

**Build.** Mine `alias_reference` artifacts (per the Task 3 schema) from this repo's commit
messages, PR briefs, and ADR titles. Wire up code-graph extraction over `services/` and `docs/` by
**reusing the existing `graphify_backend.GraphifyGraphifier` adapter from ADR-0012** — do not write
a second Graphify integration from scratch; extend the existing one to also surface confidence
tiers per artifact.

**Validation.**

1. **Resolution accuracy.** Hand-pick ~25 real terse phrases a developer might type, sourced from
   actual commit messages / PR brief titles in this repo (e.g. "the durable model-output cache,"
   "the incremental per-source commit fix," "the retrieval budget enforcement"). Manually determine
   the correct target file/symbol for each *before* running resolution (to avoid grading our own
   homework after the fact). Run alias resolution. **Pass:** ≥80% resolve to the correct entity on
   the first attempt; every miss is either correctly flagged as `ambiguous_candidates` (not silently
   wrong) or traceable to a mining gap, not a resolution-logic bug.
2. **Adversarial name-collision check.** This is a direct follow-up to the confidence-tiering gap
   found during this session's audit (a Graphify call resolved to a single, confidently wrong
   target — not the ambiguous-multi-target case the existing ADR-0012 adapter already catches).
   Construct at least 3 deliberate name-collision fixtures (module-level function vs. same-named
   method, reproducing the pattern from this session's audit) and confirm the confidence-tiering
   rule in the Task 3 document actually demotes or flags every one of them — **none may reach the
   caller labeled `deterministic` without a caveat.** **Pass:** 3/3 caught.

**If this phase fails** (resolution accuracy below threshold, or any collision case slips through
unflagged): stop. The whole tool-design-first premise depends on scope resolution being reliable —
do not proceed to build `get_task_context` on top of an alias index or a blast-radius signal that
can't be trusted.

---

## Phase 2 — `get_task_context` tool only

**Build.** The tool itself, per the Task 3 input/output schema, backed by: Phase 1's alias index +
Graphify blast radius; a first-cut convention-mining pass (can be minimal at this stage — e.g. "the
dominant test-file naming pattern per directory" — sophistication is not the point of this phase);
similar-prior-changes via a simple commit-similarity lookup (reuse `evals/agent_task_cases` if it
already has usable structure, otherwise a plain embedding search over commit messages).

**Validation — the core test the original brief asked for: does this measurably beat a raw LLM
call reading files directly, on the same repo.**

Take ~15 realistic task descriptions against this repo (e.g. "add a rollback test for the
per-source commit migration," "find everywhere the generation cache key is computed"). Run each
twice:
- **(a) Raw:** an LLM call with only the task description and normal `read`/`grep`/`glob` tools,
  no `get_task_context`.
- **(b) Tooled:** an LLM call that starts from `get_task_context`'s output for the same task
  description.

Score both on: total tokens spent reaching a correct understanding of scope, wall-clock/tool-call
count, and correctness of the resulting plan (human or LLM-judge rubric against a
pre-written-by-hand "what a correct answer touches" reference, written before running either arm).

**Pass:** tooled arm beats raw arm by a defined margin on at least one axis without regressing the
others badly — e.g. ≥30% fewer tokens at equal-or-better correctness, or meaningfully better
correctness at equal token budget. **Report the numbers, don't just say "better."**

**If this phase fails:** stop. Do not build orchestration on top of a tool that doesn't actually
save the host agent anything over reading files itself — that would be exactly the "many agents
with KB access" anti-pattern this platform's whole design already rejects.

---

## Phase 3 — Orchestration layer, scoped by the Task 1 findings

**Build.** Only the piece Task 1's LangGraph analysis judged worth adopting: the Functional API
(`@entrypoint`/`@task`) plus a Postgres-backed checkpointer, applied *only* to the review-and-PR
agent's human-approval decision gate. Explicitly excluded, per Task 1: LangGraph for the build-time
pipeline (it's a DAG — no benefit), LangChain core (superseded by native structured outputs),
LangSmith (the existing structured-logging discipline already covers the diagnostic need).

**Validation — the idempotency risk Task 1 flagged has to be proven, not assumed away.**
Simulate a crash mid-decision-gate: fire the `interrupt()`, kill the process before a human
responds, resume. **Pass:** the already-fired side effect (e.g. a PR comment) is not re-triggered
on resume — confirmed by an idempotency key or dedup check on that step, not by "it happened to
work once." This directly targets the duplicate-charge failure mode cited in the Task 1 research.

**If this phase fails:** do not proceed to Phase 4 with an orchestration layer that can double-post
to a real PR on every crash-and-resume.

---

## Phase 4 — Review/PR agent, multi-agent split

**Build.** The full pipeline: diff capture → context gathering (via `get_task_context`) → review
checks → revise loop (plain retry-with-cap, per Task 1's finding that this doesn't need LangGraph's
cyclic-graph primitive) → decision gate (Phase 3) → PR posting.

**Validation.** Run against a handful of real, small PRs (this repo is a fine source — plenty of
recent small, well-scoped commits to replay as synthetic PRs). Check: diff context is complete and
correctly scoped (via Phase 2's tool), review findings hold up against a human spot-check, the
revise loop actually converges within its cap rather than looping indefinitely, the decision gate
correctly pauses and resumes, and PR posting does not double-post on a resume (this is Phase 3's
idempotency proof exercised for real, not just simulated).

**Optional overlay, not a blocker.** If Headroom's own Phase A/B gate (established during this
session's legitimacy research) has separately passed by this point, evaluate whether library-mode
compression measurably reduces this agent's token cost without degrading review quality. This is an
enhancement to Phase 4, not a prerequisite — Phase 4 should ship without it if Headroom's gate
hasn't cleared.

---

## Summary gate table

| Phase | Proves | Pass bar | Failure means |
|---|---|---|---|
| 0 | Corpus is usable | Graphify runs clean on real data | **Already passed this session** |
| 1 | Scope resolution is reliable | ≥80% alias accuracy; 3/3 collision cases caught | Stop — don't build on unreliable scope |
| 2 | The tool beats raw reading | Defined token/correctness margin, numbers reported | Stop — don't orchestrate around a tool with no proven value |
| 3 | Crash-resume is safe | No duplicate side effects on simulated crash | Stop — don't ship an agent that can double-post |
| 4 | The full agent works end to end | Real-PR spot check passes on all axes above | Scope back to whichever sub-piece didn't hold |
