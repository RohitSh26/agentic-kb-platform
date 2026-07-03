# Proposal v2 — World-Class Multi-Agent Developer Platform

## Status

Proposal, not an ADR. Supersedes `2026-07-02-tool-design-first-kb-architecture.md` (v1), which was
scoped to two narrow owned processes. This version is scoped to the real target: **hundreds of
developers daily, across the full SDLC** — asking questions, code implementation, code design,
ADR writing, unit test writing, infrastructure code, and PR review (local and on GitHub).

This document is the synthesis of four research threads run against that target: production
multi-agent coding platforms, the academic/industry literature on agentic retrieval and multi-agent
SDLC, a scale-aware re-evaluation of LangChain/LangGraph/LangSmith, and what breaks operationally
going from this repo's prototype (ADR-0025, n=3) to real concurrent load. Every claim below is
either grounded in that research (cited) or in this session's own direct, hands-on verification
against this repo's code.

## The one thing that is *not* up for debate

**Agentic search over a precomputed structural graph, not vector RAG, as the retrieval core.**

This was ADR-0025's bet, made from one team's bad experience with a broker-gated RAG system. It is
now independently validated at every level that matters:

- **Anthropic's own engineering**: Claude Code dropped RAG + a vector DB for agentic search because
  it "outperformed everything, by a lot."
- **Peer-reviewed research**: Subramanian et al. (AAAI 2026) ran a controlled study — same model,
  same six datasets, only the retriever varied — and agentic keyword search beat vector RAG by
  20–24% relative.
- **Every major production platform independently converged on the same thing**: Cursor, Windsurf,
  Devin, Cline, and Sourcegraph Amp no longer index their target corpora into a vector database.

**Do not relitigate this in the rewrite.** The mistake to avoid is not "we chose the wrong
retrieval strategy" — it's "the strategy was chosen correctly and never shipped to the real
product" (see the ADR-0025 gap finding earlier in this session: `services/mcp-server`'s tool
registry still only exposes the old broker schemas, no `kb_search` tool exists there today).

## What changed from v1, and why

| v1 assumption | v2 correction | Why |
|---|---|---|
| Two narrow owned processes (build pipeline, one review agent) | Many distinct owned agent workflows: Q&A, implementation, design, ADR writing, test writing, infra code, PR review | The owner's explicit scope: hundreds of developers, full SDLC |
| LangGraph: partial-adopt, one HITL gate only, self-hosted OSS + Postgres checkpointer | LangGraph's *case* is real at this scope — but self-hosted OSS has a documented gap (single-process, no distributed coordination, race risk on concurrent thread resumption) that only shows up once concurrency is real | Production LangGraph deployments at this shape (Uber, LinkedIn, Klarna — treat the specific numbers as vendor-sourced, not audited) run on **LangGraph Platform**, not bare OSS |
| LangSmith: don't adopt (new SaaS dependency, data-egress concern) | Reconsidered — Enterprise tier supports self-hosting on your own Kubernetes cluster, which answers the data-egress objection directly | Cost still scales with volume ($1.2K–5K/month in overages at 500K–2M traces) — budget-gate on measured volume, don't buy speculatively |
| Retrieval: alias index + Graphify blast radius, unranked against alternatives | Same core design, explicitly validated against Cursor's production pattern: **fast structural graph as primary, semantic/embedding search only as fallback** — this is the answer to v1's open question about very-large-monorepo filesystem-walk cost | Cursor's dual-layer search cuts large-monorepo query latency from 20–30s to under 1s using exactly this shape |
| No explicit security requirement for agent-to-agent boundaries | **Explicit, load-bearing requirement**: multi-agent frameworks are not adversarially hardened by default | A 2026 evaluation found ChatDev/MetaGPT/AgentVerse all have framework-specific prompt-injection vulnerabilities, 45–93% attack success rates. This platform ingests PRs, tickets, and issue text — untrusted content by CLAUDE.md's own existing rule. |
| No execution/isolation model specified | **Anthropic Managed Agents** as the primary execution substrate (see below) | Production systems at this scale converge on per-execution isolation (Cursor: isolated VMs at $2B ARR; Northflank: microVM isolation for 10–100x volume). Managed Agents is Anthropic's own purpose-built answer to exactly this, and this repo already speaks its native integration protocol (MCP). |

## Execution substrate: Anthropic Managed Agents, not hand-rolled orchestration infrastructure

This is the single highest-leverage decision in this document, and it's one the research forks
couldn't fully make because it requires API-level knowledge of a product surface, not web search.

**The requirement, restated from the research:** many distinct specialized agents (implementation,
test-writing, ADR-writing, infra-code, code review) that need per-execution isolation, the ability
to delegate to and coordinate with each other, durable human-approval gates, and native tool
integration — at hundreds-of-developers concurrency.

**Managed Agents matches this requirement almost exactly, out of the box:**

- **Per-session containers** — Anthropic provisions and isolates the execution environment per
  session. This is the "isolated VM per execution" pattern the research found in production systems
  (Cursor, Northflank), without building or owning container orchestration.
- **Native multiagent coordinator pattern** — an agent's `multiagent: {type: "coordinator", agents:
  [...]}` field lets one orchestrator delegate to a roster of up to 20 specialized agents (test
  writer, ADR writer, reviewer, infra-code writer, each its own persisted, versioned Agent object),
  each running in its own thread, sharing the session's filesystem. This is exactly the shape MetaGPT's
  structured-artifact approach (which beat ChatDev's chat-based approach on real measurements) calls
  for, and it's a supported product feature, not something to build.
- **Native MCP integration** — this repo already speaks MCP. Managed Agents' `mcp_servers` +
  `mcp_toolset` + vault-based credential model means the specialized agents call this platform's own
  `kb_search`/`get_task_context` tools the same way any MCP client would, once
  `services/mcp-server` actually implements ADR-0025 (see Retrieval Layer below).
- **Native human-approval gates** — `permission_policy: {type: "always_ask"}` + the
  `user.tool_confirmation` event is a durable, resumable HITL gate, the same requirement Task 1's
  LangGraph analysis flagged as the one orchestration piece worth having. No custom
  interrupt/checkpoint code to write.
- **Sidesteps the exact gap the research found in bare OSS LangGraph** — "no distributed execution,
  no task queue, no worker pool," race risk when two processes resume the same session concurrently.
  Managed Agents' session/thread model is server-managed; that failure class doesn't exist on this
  path.
- **Concrete, checkable scale numbers, not marketing**: Sessions/Agents/Vaults create operations are
  rate-limited at 300 RPM per organization, other operations at 600 RPM — real headroom for hundreds
  of developers unless usage is extremely bursty, and a number to actually plan capacity against
  rather than assume.

**What this does NOT replace**: the nightly build-time pipeline (kb-builder) stays a plain,
owned, scheduled process — Managed Agents is for interactive/agentic SDLC work, not batch
extraction. It also doesn't replace this platform's own governance layer (Postgres truth, ACL,
retrieval ledger) — Managed Agents' vaults and session resources are how agents *reach* that layer
via MCP, not a substitute for it.

**Where LangGraph still has a real role**: if a specific workflow genuinely needs cross-provider
model flexibility (not just Claude) or must run fully self-hosted with no dependency on Anthropic's
infrastructure, LangGraph Platform (paid, managed, or self-hosted hybrid) is the fallback — but
default to Managed Agents first, since it requires building the least new infrastructure and this
repo already has the MCP investment to plug into it directly. Treat "do we need LangGraph anywhere"
as a per-workflow question to answer during Phase 3 of the build plan, not a platform-wide default.

## Inter-agent communication: watch A2A, don't adopt yet

Once there are genuinely multiple specialized agents (test writer, ADR writer, reviewer) that need
to talk to *each other* — not just call tools — that's a different protocol concern than MCP
(agent-to-tool). The Agent2Agent protocol (A2A, v1.0 in 2026, backed by Microsoft/AWS/Salesforce/
SAP/Cisco) is emerging specifically for this. Managed Agents' native multiagent coordinator pattern
already covers the in-container delegation case above without needing A2A. A2A becomes relevant if
agents need to coordinate *across* separate sessions/containers/vendors — not a Phase 1–3 need.
Revisit once the specialized-agent roster is real and cross-session coordination is an actual
requirement, not a hypothetical one.

## Retrieval layer: the same design, ranked and hardened by the research

The v1 proposal's alias/reference index, confidence tiering, and `get_task_context` schema stand —
this section only adds what the research changed or hardened. See v1 for the full artifact schema
and tool I/O contract; this is not repeated here.

**Two-tier resolution, per Cursor's production pattern:**
1. **Primary: structural graph.** Alias index (mined from commits/PRs/tickets) + Graphify's
   AST-derived call/containment graph, re-normalized through the existing `GraphifyGraphifier`
   adapter (ADR-0012) with the confidence-tiering rule this session's audit already established
   (a single, name-collision-prone resolved edge is `interpreted` tier, not `deterministic`, until
   corroborated).
2. **Fallback: semantic/embedding search**, only when the structural graph doesn't resolve —
   exactly Cursor's shape, and the answer to v1's open question about whether agentic search holds
   up on very large monorepos (it does, as long as the *fast* structural path is tried first).

**Context-assembly quality is now its own lever, not an afterthought.** Meta Context Engineering
(2026) reports 89.1% on SWE-bench Verified vs. 70.7% for hand-tuned context-assembly baselines —
a bigger gap than most model-capability deltas. `get_task_context`'s design (one call, tiered
confidence, explicit `open_questions` rather than silent guessing) should be evaluated against
SWE-ContextBench/ContextBench's gold-context methodology during Phase 2's validation, not just
against a hand-picked task list.

**Review agents: a panel of specialists, not one generalist.** Qodo Merge's parallel
bug/security/quality/test-coverage reviewer ensemble posted the best F1 in an independent benchmark
against single-reviewer tools. Phase 4's review-and-PR agent should be a Managed Agents
multiagent-coordinator roster (one reviewer agent per concern), not one agent asked to check
everything — this is a design change from v1's build plan, which assumed a single review step.

## Security: adversarial hardening is a hard requirement, not a nice-to-have

The 2026 IMBIA evaluation found ChatDev/MetaGPT/AgentVerse — the major multi-agent SDLC frameworks
— are all vulnerable to prompt injection at 45–93% attack success rates, framework-dependent. This
platform ingests PRs, tickets, and issue text (untrusted content, per CLAUDE.md invariant 6). Every
agent-to-agent handoff and every point where retrieved/ingested content reaches an agent's context
must be treated as a potential injection surface. Concretely for this platform:

- Retrieved KB content (invariant 6, already stated) and PR/ticket content ingested by review agents
  are both untrusted — neither can alter tool policy, identity, ACL, or another agent's
  instructions. This is not new, but it now has teeth: it applies to *every* specialized agent in
  the coordinator roster, not just the retrieval path.
- Managed Agents' vault-based credential model helps here structurally — credentials never enter
  the sandbox the agent's tools execute in, so even a successfully injected agent can't exfiltrate a
  vaulted secret directly.
- This needs its own explicit test pass before Phase 4 ships (see the build plan).

## Operational scale requirements (design-time, not retrofit)

From the KB/MCP scaling research — these are requirements to design in from Phase 1, not problems
to solve after hundreds of developers are already on the system:

1. **Postgres**: transaction-mode connection pooling first (20–50 backend connections serving
   thousands of clients); confirm this system doesn't depend on prepared statements, advisory
   locks, or LISTEN/NOTIFY before committing to it (session-mode pooling is the fallback if it
   does). Read replicas come after pooling, for `kb_search`/graph-traversal reads specifically —
   writes stay on primary. Evaluate `h4gen/postgres-graph-rag` as a reference implementation of
   nearly this exact shape before building the pooling layer from scratch.
2. **Prompt caching**: design a byte-identical shared prefix (system instructions + the fixed MCP
   tool schema set) across every session, with volatile per-task content placed after the cache
   breakpoint. Per-task content will never cache well across hundreds of different developers'
   different tasks — the win has to come from the shared part, and that only works if it's designed
   in, not retrofitted.
3. **Cost control**: per-developer/per-team spend ceilings (Anthropic Console natively supports
   this) plus a usage-tracked dashboard, treated as load-bearing at this scale. This is the same
   thing the existing **Dashboard initiative** (already noted as needing an ADR) was scoped for —
   it graduates from "nice observability" to "the thing that prevents a runaway bill" at hundreds of
   developers.
4. **MCP server**: design stateless-per-call from day one — session state lives in Postgres, not
   in-process — so horizontal scaling doesn't require a retrofit. Raw MCP throughput is not the
   bottleneck (benchmarked 10K+ concurrent connections); session-state affinity is, and only shows
   up once the server is actually stateful somewhere it shouldn't be.

## What to evaluate reusing before building

- **Anthropic's own Claude Code multi-agent Code Review feature** (Teams/Enterprise, per-repo,
  runs in cloud on every PR) — one of the platform's target use cases ("reviewing the PR... on
  GitHub") may already exist as a product. Evaluate whether to integrate/extend it before building
  a parallel review agent from scratch.
- **`h4gen/postgres-graph-rag`** — a close reference implementation for the Postgres hybrid-search +
  pooling shape this platform needs at scale.
- **Anthropic Managed Agents** (see above) — the primary execution substrate recommendation, not
  just a reference.

## Open questions carried into the build plan

- Managed Agents is currently beta — confirm its production readiness bar and SLA before committing
  irreversibly; the fallback (LangGraph Platform, or self-hosted LangGraph OSS with hand-built
  distributed coordination) should be scoped as a real contingency, not a footnote.
- Whether the per-workspace RPM limits (300 create / 600 other) hold up under real hundreds-of-
  developers load is a Phase 1/2 empirical question, not something to assume from the documented
  ceiling.
- A2A adoption timing — revisit once the specialized-agent roster is real (see above).
