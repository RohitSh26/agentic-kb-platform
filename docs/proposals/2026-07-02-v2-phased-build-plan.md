# Proposal v2 — Phased Build Plan for the World-Class Platform

> **Status: superseded by ADR-0030 (accepted 2026-07-02).** Historical design record — do not use
> for onboarding. Phases 0 and 3 below are built on provisioning an Anthropic Managed Agents
> execution substrate, which ADR-0030 evaluated and explicitly rejected (see its Alternatives
> rejected). Current references: ADR-0030, ADR-0031, ADR-0032, `docs/contracts/`.

## Status

Proposal, not an ADR. Supersedes `2026-07-02-phased-build-plan.md` (v1). Companion:
`2026-07-02-v2-world-class-platform-architecture.md` — read that first, this plan validates it.

## Discipline, unchanged from v1

Prove the highest-risk, most novel assumption first. Each phase has an explicit pass/fail gate.
Failing a gate stops the plan there — it does not mean push through anyway.

**Note on the rewrite mandate:** nothing below requires keeping any specific piece of existing
code, and the owner has been explicit that code disposition is not the concern — quality,
robustness, and efficiency are. In practice that means: where something already demonstrably works
(the Postgres schema, the `GraphifyGraphifier` adapter, the connectors, ADR-0025's proven KB-first
*pattern*), reusing it is the fast path to quality, not a compromise of it. Where something doesn't
hold up under this plan's own validation gates, it gets replaced without hesitation. The safety tag
(`pre-rewrite-2026-07-02`) means nothing is lost either way.

---

## Phase 0 — Corpus and substrate check

**Task.** Confirm both foundations before building anything on top of them.

1. Corpus: this repo, as in v1 — real commit/ADR/PR history, Graphify already confirmed to install
   and run clean (`uv sync`: 74 packages, no conflicts; a code-only extraction run produced
   well-formed output in this session).
2. **New — execution substrate.** Provision a minimal Anthropic Managed Agents environment + a
   single trivial agent (no roster yet). Confirm: agent creation, session creation, a basic tool
   call round-trip, and that session events stream correctly.

**Pass/fail:** corpus check already passed this session. Substrate check passes if the trivial
agent completes a basic task end-to-end with no infrastructure surprises. **If Managed Agents
fails this basic check** (beta instability, an account/access blocker, anything structural): fall
back to LangGraph Platform as the execution substrate and re-scope Phase 3 accordingly — don't
discover this incompatibility mid-build.

---

## Phase 1 — Close the ADR-0025 gap, for real this time

**Task.** This is the most important phase in the whole plan and the one most likely to be
under-weighted: `scripts/kb_agent.py` already proves the KB-first pattern works
(`docs/reports/kb-benefit-2026-06-18.md`: 3/3 correct + cited vs. 0/3, all hallucinated). It was
never shipped to the actual product. Ship it.

**Build.**
1. A real, budgeted `kb_search` MCP tool schema + handler in `services/mcp-server`, porting
   `kb_agent.py`'s proven design (call-count + token-budget cap enforced in the tool, not the
   prompt).
2. The alias/reference index (mined from commits, PR titles, ADR titles — reusing the existing
   `git_metadata` and `ado_card` connector artifacts, per v1's schema).
3. Two-tier resolution: alias index + Graphify's re-normalized structural graph (via the *existing*
   `GraphifyGraphifier` adapter, extended with this session's confidence-tiering fix) as the
   **primary** path; semantic/embedding search only as **fallback** when structural resolution
   comes up empty — this is Cursor's production pattern, not a new invention.

**Validation.**
1. **The tool is actually callable over MCP**, not just importable as a Python module — connect a
   real MCP client (even a minimal test harness) and confirm `kb_search` round-trips correctly.
   This specifically closes the gap found this session (the tool registry currently has no
   `kb_search` entry at all).
2. **Resolution accuracy**: ≥80% on ~25 real terse phrases hand-verified against this repo's
   history (v1's methodology, unchanged).
3. **Adversarial name-collision check**: 3/3 deliberate collision fixtures correctly demoted to
   `interpreted` tier or flagged, none reaching the caller labeled `deterministic` — the exact test
   this session's audit showed was necessary (a Graphify call resolved to a single, confidently
   wrong target, a failure shape the *original* ADR-0012 adapter didn't catch).

**If this phase fails:** stop. Nothing downstream matters if the foundational retrieval claim
(proven in a script, unproven in the product) doesn't actually hold once shipped for real.

---

## Phase 2 — `get_task_context`, benchmarked properly

**Build.** The full tool per the v1/v2 schema, backed by Phase 1's retrieval, a first-cut
convention-mining pass, and similar-prior-changes lookup.

**Validation.** Same A/B methodology as v1 (tooled vs. raw file-reading, on real task
descriptions from this repo, scored on tokens + correctness against a pre-written reference) —
**plus**, if time allows, sanity-check the methodology against the gold-context evaluation approach
from SWE-ContextBench/ContextBench (2026) rather than relying solely on a hand-picked task list.
**Pass:** tooled arm beats raw by a defined, reported margin (v1's bar: ≥30% fewer tokens at
equal-or-better correctness, or better correctness at equal budget).

**If this phase fails:** stop. Do not build a multi-agent roster around a context tool that
doesn't measurably help.

---

## Phase 3 — Multi-agent substrate, small roster, native HITL

**Build.** Stand up the Managed Agents multiagent coordinator with a **small** roster — one
implementer agent, one reviewer agent — both wired to this platform's own MCP server (Phase 1/2's
tools) via the MCP connector + a vault credential. Use `permission_policy: {type: "always_ask"}` on
the decision-gate tool call, handled via `user.tool_confirmation` (the native equivalent of the
LangGraph `interrupt()` pattern from Task 1's original analysis — no custom checkpoint code needed
if the substrate check in Phase 0 passed).

**Validation.**
1. **Crash-resume safety**, same requirement as v1's Phase 3: kill the process mid-decision-gate,
   confirm resume doesn't re-trigger the already-fired side effect. On the Managed Agents substrate
   this is largely the platform's responsibility — the test is to confirm that's actually true in
   practice, not assume it from the docs.
2. **Rate-limit reality check**: simulate concurrent session creation against the documented 300
   RPM (create) / 600 RPM (other) per-org ceilings. This validates or invalidates the "hundreds of
   developers" capacity assumption empirically, before it's load-bearing.

**If this phase fails:** if it's a Managed Agents substrate problem, fall back to the LangGraph
Platform contingency flagged in Phase 0. If it's a rate-limit problem, that's a capacity-planning
finding to fold into Phase 6, not necessarily a stop — but don't proceed to a wide roster (Phase 5)
without knowing the real ceiling.

---

## Phase 4 — Security hardening (hard gate, before widening the roster)

**Task.** The research finding that must not be skipped: ChatDev/MetaGPT/AgentVerse — the
frameworks closest in shape to what's being built here — all have framework-specific
prompt-injection vulnerabilities, 45–93% attack success rates. This platform ingests PR text,
tickets, and issue descriptions as a matter of course.

**Validation.** Build an adversarial test suite covering every agent-to-agent handoff and every
point where retrieved/ingested content reaches an agent's context (mirroring the technique class
the 2026 IMBIA evaluation used against the comparable frameworks): attempt to override tool policy,
bypass ACL, alter another agent's instructions, or exfiltrate a vaulted credential via crafted
PR/ticket/commit-message content. **Pass:** zero successful policy override, ACL bypass, or
credential exfiltration across the suite. Managed Agents' vault model (credentials never enter the
sandbox) should make credential exfiltration structurally hard to achieve — confirm this holds
under actual adversarial input, don't just trust the architecture description.

**This is a hard gate.** Do not proceed to Phase 5 on a failed or skipped security pass — a wider
roster increases the attack surface, not just the capability surface.

---

## Phase 5 — Widen the roster to the real target

**Build.** Grow the coordinator roster to cover the stated SDLC scope: implementer, ADR writer,
test writer, infra-code writer, and — per the Qodo Merge-validated pattern (a panel beat a single
generalist reviewer on an independent benchmark) — **a review panel**, not one review agent:
separate bug/security/quality/test-coverage reviewer agents running in parallel, coordinated the
same way the roster already coordinates.

**Validation.** Run against real, small PRs/tasks on this repo (v1's Phase 4 methodology).
Additionally: confirm the review panel's outputs are reconciled coherently (no contradictory
findings surfaced to a human without resolution), and re-run a subset of Phase 4's security suite
with the full roster active — a wider roster is a bigger attack surface even if each individual
agent was clean in isolation.

**If this phase fails:** scope back to whichever sub-agent or panel member didn't hold up; the
roster model means this is additive/subtractive, not all-or-nothing.

---

## Phase 6 — Scale validation

**Task.** Everything in the architecture doc's "Operational scale requirements" section, load-tested
before a real hundreds-of-developers rollout, not discovered during one.

**Validation.**
1. **Cost**: simulate realistic concurrent usage and confirm per-developer/per-team spend stays
   within a defined ceiling (tie this into the existing Dashboard initiative's planned
   observability work — this is where it becomes load-bearing, not optional).
2. **Prompt caching**: confirm the shared-prefix design (system instructions + fixed MCP tool
   schemas, byte-identical across sessions) actually produces a measurably nonzero cache-hit rate
   under simulated multi-developer load — this was explicitly *not* demonstrated in the n=3
   prototype and needs its own proof, not an assumption that "designing for it" was sufficient.
3. **Postgres**: confirm transaction-mode connection pooling holds under simulated concurrent
   `kb_search`/graph-traversal load without connection exhaustion; confirm this system doesn't
   depend on prepared statements/advisory locks/LISTEN-NOTIFY (if it does, budget for session-mode
   pooling instead and re-test).
4. **MCP server**: confirm the stateless-per-call design (session state in Postgres) actually
   survives horizontal scaling — spin up more than one server instance and confirm session
   continuity isn't broken.

**Pass:** all four hold under simulated load equivalent to a meaningful fraction of the real target
(exact number to be set once Phase 3's rate-limit findings are in). **If this phase fails on any
axis:** that axis blocks a real rollout specifically — the other three can still graduate
independently.

---

## Summary gate table

| Phase | Proves | Pass bar | Hard gate? |
|---|---|---|---|
| 0 | Corpus + execution substrate are usable | Trivial Managed Agents task completes cleanly | Fallback exists (LangGraph Platform) |
| 1 | The already-proven KB-first pattern actually works when shipped to the real product | Real MCP tool callable; ≥80% alias accuracy; 3/3 collision cases caught | Yes — stop if it fails |
| 2 | The context tool beats raw reading | Defined token/correctness margin, reported | Yes — stop if it fails |
| 3 | Multi-agent substrate + HITL are safe and sized right | Crash-resume holds; rate limits characterized | Fallback exists; capacity finding feeds Phase 6 |
| 4 | The system resists the exact attack class the research found in comparable frameworks | Zero successful injection/exfiltration in adversarial suite | **Yes — do not widen the roster on a failed pass** |
| 5 | The full SDLC roster works end to end | Real-PR spot check + panel coherence + re-run security subset | Scope back to the failing agent, not all-or-nothing |
| 6 | It survives hundreds-of-developers load | Cost, cache, Postgres, MCP all hold under simulated load | Per-axis — a failing axis blocks rollout on that axis specifically |
