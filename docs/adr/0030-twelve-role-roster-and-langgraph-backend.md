# ADR-0030 — Twelve-role agent roster, with LangGraph orchestrating MCP tool responses and the review panel

## Status

Accepted (2026-07-02). Depends on ADR-0025 (kb-first, budgeted retrieval) and ADR-0009 (portable
agent framework); does not amend either. Unblocks rendering the six roles drafted in
`docs/proposals/2026-07-02-v2-world-class-platform-architecture.md`, which stays a proposal
document — this ADR is what makes its roster decision durable.

## Context

The product goal, stated plainly by the owner: a developer opens VS Code, Copilot CLI, or the
OpenCode CLI, types a question or a task, and gets a great answer — with the knowledge graph, the
MCP tool surface, and whatever orchestration produces that answer entirely invisible. Same quality
of experience regardless of entry point.

Two things this session established as fact, not opinion, bound how that goal can be reached:

1. **We do not own the interactive agent loop.** OpenCode, VS Code Copilot's custom-agent surface,
   and the separate, async GitHub Copilot cloud coding agent are three distinct, closed
   implementations. Nothing we build swaps out or runs inside any of their loops. Our reach into
   the interactive experience is exactly two things: what we precompute, and how good a single MCP
   tool response is. (Confirmed via direct research into all three surfaces' current 2026-07-02
   capabilities — OpenCode has native subagent auto-invoke plus a new subagent-to-subagent
   delegation mechanism; VS Code Copilot supports `handoffs` — agent-to-agent chaining with
   pre-filled context; the GitHub Copilot cloud agent is MCP-tools-only, explicitly does not support
   `handoffs`, and runs one bounded task per session.) Anthropic Managed Agents was evaluated and
   explicitly rejected as an execution substrate for exactly this reason — it would have meant
   hosting a parallel, disconnected experience nobody on the team actually uses.

2. **ADR-0025's retrieval bet was correct and never shipped.** `scripts/kb_agent.py` proves the
   `kb_search`-first, budgeted, native-tools-kept pattern works (`docs/reports/kb-benefit-2026-06-18.md`:
   3/3 correct and cited vs. 0/3, all hallucinated, on a baseline with no KB). Independent
   confirmation arrived from outside this repo, not just from the original owner complaint: Anthropic's
   own Claude Code, Cursor, Devin, Windsurf, and Sourcegraph Amp have all independently converged on
   agentic search over a precomputed structural graph, over vector RAG; a controlled AAAI 2026 study
   (Subramanian et al.) found agentic keyword search beating vector retrieval by 20–24%. But the six
   canonical manifests in `agents/*.md`, and `services/mcp-server`'s actual tool registry, were still
   wired to the pre-ADR-0025 `context.create_pack`/`context.request_more` broker flow as of this
   session — the fix existed only in the prototype script.

Two further gaps surfaced once the roster itself was scrutinized against the owner's actual stated
scope (asking questions, code implementation, code design, ADR writing, unit tests, infrastructure
code, PR review) rather than assumed from the pre-existing six-role set:

- A 2026 independent benchmark (Qodo Merge vs. seven other review tools) found a panel of
  specialist reviewers running in parallel — bug, security, quality, test-coverage — measurably
  beats a single generalist reviewer on real defect-finding (F1 60.1%, best of the field). The
  existing single `code_reviewer_agent` did not reflect this.
- ADR writing and infrastructure-code writing had no corresponding role at all in the original six.

## Decision

1. **The canonical roster is twelve roles**, not six: `orchestrator`, `implementation_agent`,
   `test_layer_agent`, `delivery_planner_agent`, `pr_planner_agent`, `adr_writer_agent`,
   `infra_code_agent`, and a five-agent review group — `bug_reviewer_agent`,
   `security_reviewer_agent`, `quality_reviewer_agent`, and `test_coverage_reviewer_agent` running
   as independent parallel lenses, reconciled by `code_reviewer_agent` acting as a synthesizer
   (merge duplicates, surface disagreement rather than silently resolving it, rank by real
   severity — never review the code itself). Every role uses the ADR-0025 pattern: the budgeted
   `kb_search` tool plus whichever native tools the role needs, never the retired broker flow.

2. **LangGraph orchestrates the backend processing behind MCP tool responses.** `get_task_context`
   and `kb_search` are not flat function calls on our side — resolving scope, walking the
   blast-radius graph, pulling conventions, and finding similar prior changes run as parallel graph
   nodes, reconciled by a synthesis node, with a retry path if confidence comes back too low to
   answer. This runs entirely server-side, inside a single MCP tool call; the developer in VS Code,
   Copilot CLI, or OpenCode never sees it — they see one fast, well-grounded answer, identically
   regardless of which of the three surfaces they're on.

3. **LangGraph orchestrates the review panel**, the one other process this platform fully owns end
   to end: the four specialist reviewers run in parallel (fan-out), `code_reviewer_agent` reconciles
   (join), triggered by a GitHub Actions workflow on PR open. Checkpointing means a killed CI runner
   doesn't re-run all four reviews from scratch. Runs as one bounded job per PR, not a long-lived
   concurrent service, so self-hosted OSS LangGraph is the right shape — no LangGraph Platform
   subscription required for this specific workflow.

4. **LangSmith traces both of the above from day one** — the MCP tool response graph and the review
   panel — not deferred until logging proves insufficient. This is what lets us find the slow or
   low-confidence step across all three developer-facing surfaces uniformly, instead of guessing
   per-surface.

5. **The nightly build pipeline (`kb-builder`) keeps its own cache, not LangGraph's checkpointer.**
   It already has a working, crash-durable, content-hash-keyed cache (`generation_cache`/
   `embedding_cache`) that resumes more precisely than a generic step checkpoint would. This is a
   deliberate exception with a specific reason, not an oversight, and not "avoid LangGraph by
   default" applied inconsistently.

6. **Explicitly not decided here**: hosting any agent execution substrate ourselves for the
   interactive developer experience. The interactive experience is delivered exclusively through
   each host's own native mechanism (OpenCode's subagent delegation, Copilot's `handoffs`, or a
   single self-contained task on the GitHub Copilot cloud agent), fed by the MCP tool surface this
   ADR governs the backend of.

## Consequences

- One knowledge/orchestration layer serves all three entry points identically — the goal this ADR
  exists to serve.
- Review quality improves on a measured basis (panel vs. generalist), and two previously-missing
  capabilities (ADR writing, infra code) now have a home.
- New dependencies: `langgraph` and `langchain`'s core runnable abstractions (MIT, open source,
  used regardless — LangGraph is built on them) in `services/mcp-server` and the review-panel
  workflow; `langsmith` (SDK is open, hosted tracing has a real cost dimension at volume — budget it
  against actual trace counts once running, per the earlier scale research this session, rather than
  assume it stays free).
- This ADR accepts the *design*; it does not claim the implementation exists yet. Every role still
  depends on `services/mcp-server` actually shipping the real `kb_search` tool (tracked separately —
  the tool registry has none today) and on the `get_task_context`/review-panel LangGraph graphs
  being built. Accepting this ADR unblocks rendering the six new roles into `.opencode/`/`.copilot/`
  per ADR-0009's pipeline; it does not make them runnable by itself.
- Explicitly does **not** commit this platform to hosting an execution substrate (Managed Agents or
  equivalent) for the interactive experience, and does not and cannot modify OpenCode's or
  Copilot's own agent loop — out of scope by protocol, not by choice.

## Alternatives rejected

- **Single generalist reviewer (status quo).** Rejected on measured evidence: a specialist panel
  beats one generalist on real defect-finding benchmarks.
- **Anthropic Managed Agents as the execution substrate.** Rejected — the team's actual daily tools
  are OpenCode, VS Code Copilot, and the GitHub Copilot cloud agent, not a hosted Anthropic runtime;
  building on Managed Agents would have produced a parallel experience disconnected from what
  developers actually use.
- **Hand-rolled orchestration instead of LangGraph, for the MCP tool backend and the review panel.**
  Rejected as the default for these two specifically: both have genuine parallel/branching
  structure (fan-out resolution nodes with a low-confidence retry; fan-out review with reconciliation)
  where LangGraph's model and checkpointing add real value over a flat script. Not applied
  uniformly — the build pipeline is the deliberate exception (see Decision §5).
- **Folding ADR-writing and infra-code into existing roles.** Rejected — distinct enough domains
  (architecture-decision justification and prior-ADR conflict-checking vs. implementation planning;
  infra blast-radius and reversibility discipline vs. application code) to warrant separate
  instruction sets and budgets rather than overloading `implementation_agent`.

## Follow-ups

- Ship the real, budgeted `kb_search` MCP tool into `services/mcp-server` — nothing in this ADR runs
  without it; tracked as its own item, already corroborated by a pre-existing failing test
  (`test_agent_manifests.py`) that pins the current, unmigrated tool registry.
- Build the LangGraph graph behind `get_task_context`/`kb_search` (parallel resolution nodes,
  synthesis, low-confidence retry).
- Build the GitHub Actions review-panel workflow on LangGraph, with LangSmith tracing wired in from
  the first run.
- Render the six new roles (`adr_writer_agent`, `infra_code_agent`, the four specialist reviewers)
  into `.opencode/` and `.copilot/` per ADR-0009's pipeline; run `check_parity.py` to a clean exit.
- Register the `adr_draft_v1` output schema in `services/mcp-server/src/agentic_mcp_server/agent_output_schemas/`
  — `adr_writer_agent` references it and it does not exist yet.
- Wire `agents/orchestrator.md` to actually hand off to the new roles — it does not invoke any of
  them today.
- Budget-gate LangSmith against real trace volume once the two graphs above are running, rather than
  assume the free tier holds indefinitely.
