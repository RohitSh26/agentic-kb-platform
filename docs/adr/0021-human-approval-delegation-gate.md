# ADR-0021 — Human-approval gate at every agent delegation

- Status: Accepted
- Date: 2026-06-16
- Deciders: Rohit Sharma
- Related: `agents/orchestrator.md` + subagents, ADR-0009 (portable agent framework),
  the MCP retrieval ledger, docs/dev-guide/reference/tools.md

## Context

The orchestrator delegates to specialist subagents (delivery_planner, pr_planner,
implementation, test_layer, code_reviewer), each drawing from one broker-governed Evidence
Pack. The agent manifests are human-approved at authoring time, but at **runtime** a
multi-step feature could hand off 5+ times with no human in the loop — an autonomous chain
the operator cannot steer or correct mid-flight. The operator must stay in control: approve
the plan, see each proposed hand-off, and be able to change it before work proceeds.

## Decision

**The orchestrator routes by need, and every delegation it makes is human-gated.** Not every
request fans out to all five subagents: the orchestrator first proposes a **plan of action**
(answer directly, or a specific chain of subagents), and the **number of gates equals the
number of delegations** — a simple question ("explain how X works", "what calls Y") ends at
one gate; a feature ("build X as phased PRs") gates each hand-off.

- **Gate 1 (always): approve the plan of action.** Before doing anything, the orchestrator
  surfaces *what it intends to do*; the human approves/edits/rejects that routing. A direct
  answer therefore still has exactly one gate; a delegated workflow continues from here.
- **Then one gate per delegation.** Each agent→agent hand-off is gated, always-on for V1, no
  skip.

The gate is **enforced by the orchestration runner, not by prompt instructions**, so a weak
model cannot bypass it.

At each gate the runner surfaces (a) the current agent's output/plan and (b) the proposed
delegation (which subagent, with what instructions), then **blocks until the human chooses**:

- **Approve** — proceed as proposed.
- **Edit then approve** — the human modifies the plan / the next agent's instructions; the
  edits become the delegated input.
- **Reject with feedback** — control returns to the current agent to redo, with the feedback.
- **Abort** — stop the run cleanly; everything done so far stays recorded.

Every checkpoint (from-agent, to-agent, plan summary, decision, any edits, timestamp) is
**recorded in the observability action stream / ledger**, so a run can be replayed showing
exactly where the human approved, edited, or rejected.

## How it threads through the build

- **Enforced** in the agent runner (the orchestration layer; first the Groq test runner, later
  carried into the Copilot/VS Code runtime where the orchestrator surfaces the gate in chat).
- **Declared** in the orchestrator/subagent manifests (so the behavior is part of the portable
  agent contract, not just one runner's code).
- **Recorded** as a first-class event in the MCP observability stream (the foundation work),
  alongside the retrieval/expand/verify events — the gate is part of what you replay.

## Consequences

- The human stays in control of every hand-off; nothing autonomous runs unchecked. This is the
  point — control is preferred over speed for V1.
- More interaction per feature (one approval per delegation). Accepted; a lower-friction mode
  (key-gates-only / "approve the rest") can be added later via an ADR if the every-gate cadence
  proves heavy — but V1 is always-on by explicit decision.
- Adds a checkpoint event type to the observability/ledger surface.

## Alternatives considered

- **Prompt-only ("please ask for approval")**: rejected — skippable; a model can forget. The
  gate must be enforced by the runner.
- **Key gates only (plan + before-build)**: rejected for V1 — the operator wants every
  delegation gated. Kept as a possible later mode.
- **Fully autonomous**: rejected — removes the human control this ADR exists to guarantee.
