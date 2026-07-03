# Proposal v2 — World-Class Multi-Agent Developer Platform

## Status

**Architecture companion to ADR-0030** (accepted 2026-07-02), no longer a freestanding proposal
for an execution-substrate decision — that decision has been made, and it went the other way from
this document's first draft: **Anthropic Managed Agents was evaluated and explicitly rejected**
(ADR-0030, Alternatives rejected). The team's daily tools are OpenCode, VS Code Copilot custom
agents, and the GitHub Copilot cloud coding agent; a hosted parallel runtime would have been an
experience nobody uses. This revision replaces that recommendation with the accepted model and
keeps everything the research settled that remains true.

Lineage: supersedes `2026-07-02-tool-design-first-kb-architecture.md` (v1) as the platform-scope
document; v1 remains the reference for the alias-artifact schema and the `get_task_context` tool
I/O contract, which stand unchanged. The companion `2026-07-02-v2-phased-build-plan.md`'s Phase 0/3
substrate steps are likewise superseded by ADR-0030; execution now tracks
`2026-07-02-autonomous-execution-plan.md` (acceptance criteria A1–A13) and PR briefs 37–40.

Ground truth as of this revision:

- **Shipped**: `kb_search` is a real, budgeted MCP tool (PR-37) — registered in
  `services/mcp-server`'s tool registry, dual-cap budget (calls AND tokens) enforced server-side,
  `retrieval_event` per call, ACL-filtered. The ADR-0025 gap this document's first draft flagged is
  closed.
- **Shipped**: the twelve-role roster (ADR-0030 §1) exists in `agents/*.md` and is rendered
  parity-clean into `.opencode/` and `.copilot/` (`check_parity.py` exit 0).
- **In flight**: PR-38 (alias/reference index), PR-39 (`get_task_context` on LangGraph),
  PR-40 (review-panel service).

## The one thing that is *not* up for debate

**Agentic search over a precomputed structural graph, not vector RAG, as the retrieval core.**

This was ADR-0025's bet, made from one team's bad experience with a broker-gated RAG system. It is
independently validated at every level that matters:

- **Anthropic's own engineering**: Claude Code dropped RAG + a vector DB for agentic search because
  it "outperformed everything, by a lot."
- **Peer-reviewed research**: Subramanian et al. (AAAI 2026) ran a controlled study — same model,
  same six datasets, only the retriever varied — and agentic keyword search beat vector RAG by
  20–24% relative.
- **Every major production platform independently converged on the same thing**: Cursor, Windsurf,
  Devin, Cline, and Sourcegraph Amp no longer index their target corpora into a vector database.

The first draft's warning was that the strategy had been "chosen correctly and never shipped" —
the tool registry had no `kb_search` at all. That is no longer true: PR-37 shipped it, and every
role in the roster is written against it. Do not relitigate the retrieval bet; the remaining work
is layering on top of it (PR-38/39), not reconsidering it.

## What changed from v1, and where it landed

| v1 assumption | Where it landed | Why |
|---|---|---|
| Two narrow owned processes (build pipeline, one review agent) | Full-SDLC scope via a twelve-role roster — but only **one** owned interactive-adjacent process (the review panel); everything else runs on host surfaces we don't control | The owner's scope (hundreds of developers, full SDLC) collided with a verified fact: we do not own any host's agent loop (ADR-0030 Context) |
| LangGraph: partial-adopt, hedged, one HITL gate only | **Committed** (ADR-0030 §2–3): LangGraph orchestrates the backend graph behind `get_task_context`/`kb_search` and the review panel — self-hosted OSS, no Platform subscription | Both owned workflows are bounded jobs with genuine fan-out/join structure. The bare-OSS distributed-coordination gap the research flagged applies to a long-lived concurrent substrate — a thing ADR-0030 explicitly declines to build |
| LangSmith: don't adopt (data-egress, cost) | **Committed from day one** on both owned graphs, env-gated; cost budget-gated against measured trace volume, not assumed | Uniform visibility into the slow or low-confidence step across all three developer surfaces; suites must pass with no `LANGSMITH_*` env set |
| Retrieval: alias index + blast radius, unranked against alternatives | Same core design, validated against Cursor's production pattern: fast structural graph primary, semantic search fallback only | Cursor's dual-layer search cuts large-monorepo query latency from 20–30s to under 1s using exactly this shape |
| No explicit security requirement for agent boundaries | **Explicit, load-bearing requirement** with a concrete test gate (PR-40 fixtures) | ChatDev/MetaGPT/AgentVerse all showed framework-specific prompt-injection vulnerabilities at 45–93% attack success (2026 IMBIA evaluation); this platform ingests PRs, tickets, and issue text |
| No execution/isolation model specified | v2's first draft recommended Anthropic Managed Agents; **rejected in ADR-0030**. Interactive experience is delivered exclusively through each host's native mechanism, fed by our MCP tools | Building on a hosted Anthropic runtime would have produced a parallel experience disconnected from the tools developers actually use daily |

## Execution model: three host surfaces we don't own, one owned backend process

*(Replaces the first draft's "Execution substrate: Anthropic Managed Agents" section.)*

The binding fact, established by direct research into all three surfaces' capabilities as of
2026-07-02 (ADR-0030 Context): **we do not own the interactive agent loop.** OpenCode, VS Code
Copilot's custom-agent surface, and the async GitHub Copilot cloud coding agent are three distinct,
closed implementations. Nothing we build swaps out or runs inside any of them. Our reach into the
interactive experience is exactly two things: **what we precompute nightly, and how good a single
MCP tool response is.**

**Three surfaces, three contracts** — the hosts are not interchangeable:

- **OpenCode**: native subagent auto-invoke by description (or @mention), plus subagent-to-subagent
  delegation. The richest fit for the interactive roster.
- **VS Code Copilot custom agents**: `handoffs` — agent-to-agent chaining with pre-filled context
  into the transition. Sequential, not parallel; instruction bodies capped at 30k characters.
- **GitHub Copilot cloud coding agent**: the tightest surface — MCP tools only (no MCP
  resources/prompts), explicitly **no** `handoffs`, one bounded task per session (one repo, one
  branch, one PR). Built for a single well-scoped job, not a roster.

One canonical instruction set in `agents/*.md`, rendered per host through ADR-0009's existing
pipeline, parity pinned by `check_parity.py`. Same knowledge, same budgets, same citations,
regardless of entry point.

**The one owned backend process: the review panel** (ADR-0030 §3, PR-40). Four specialist
reviewers — bug, security, quality, test-coverage — fan out in parallel; `code_reviewer_agent`
reconciles as a synthesizer (merge duplicates, keep disagreement explicit, rank by real severity,
never re-review the code itself); one review posted per PR, report-only. It runs as a GitHub
Actions job on PR open with LangGraph fan-out/join, for a structural reason, not a preference: a
parallel panel cannot fit the cloud coding agent's one-bounded-task session shape. The
panel-over-generalist design is measured, not aesthetic — Qodo Merge's parallel specialist ensemble
posted the best F1 (60.1%) in an independent benchmark against single-reviewer tools.

**What this does NOT change**: the nightly build pipeline (kb-builder) stays a plain, owned,
scheduled process, and the governance layer (Postgres truth, ACL, retrieval ledger) stays entirely
ours — host agents reach it only through the MCP tool surface, never directly.

## Inter-agent communication: watch A2A, don't adopt yet

In-surface delegation is covered by each host's own mechanism (OpenCode delegation, Copilot
`handoffs`), and the review panel's coordination is a LangGraph graph inside one process — not
agent-to-agent messaging. The Agent2Agent protocol (A2A, v1.0 in 2026, backed by
Microsoft/AWS/Salesforce/SAP/Cisco) becomes relevant only if agents must coordinate *across*
runtimes or vendors — not a current requirement anywhere in the plan. Revisit when it is one.

## Retrieval layer: the same design, now partially shipped

The v1 proposal's alias/reference index, confidence tiering, and `get_task_context` schema stand —
see v1 for the full artifact schema and tool I/O contract.

**Two-tier resolution, per Cursor's production pattern:**
1. **Primary: structural graph.** Alias index (mined deterministically from commits/PR briefs/ADR
   titles — zero LLM calls, PR-38) + Graphify's AST-derived call/containment graph via the
   `GraphifyGraphifier` adapter (ADR-0012), with the confidence-tiering rule from this session's
   audit: a single, name-collision-prone resolved edge is `interpreted`, never `deterministic`,
   until corroborated (e.g. a `calls` edge confirmed by an import relationship). Three adversarial
   collision fixtures gate this (execution plan A5) — none may surface as a confident wrong edge.
2. **Fallback: semantic/embedding search**, only when the structural graph doesn't resolve —
   exactly Cursor's shape, and the answer to v1's open question about very large monorepos.

**`kb_search` is shipped** (PR-37): keyword hits ride the existing IDF-weighted
`PostgresKeywordSearchClient`, start at `interpreted` tier, and return under a dual budget cap with
a ledger row per call.

**`get_task_context` (PR-39, in flight) adds one committed design rule: zero LLM at query time.**
All model work happens in the nightly build; query time is pure retrieval + assembly — fast, cheap,
cacheable. The developer's own host model does the reasoning; our job is handing it perfect
material. Ambiguity is surfaced as `ambiguous_candidates` + `open_questions`, never a silent guess.

**Context-assembly quality is its own lever, not an afterthought.** Meta Context Engineering (2026)
reports 89.1% on SWE-bench Verified vs. 70.7% for hand-tuned context-assembly baselines — a bigger
gap than most model-capability deltas. The gates: SWE-ContextBench's gold-context methodology plus
the execution plan's A/B harness (A6 — tooled vs. raw file-reading, target ≥30% fewer tokens at
equal-or-better correctness) and the 25-case alias golden set (A4 — ≥80% top-1, hand-verified
before the resolver ever ran on it).

## LangGraph and LangSmith: committed roles, one deliberate exception

*(Replaces the first draft's hedged per-workflow evaluation.)*

**LangGraph orchestrates two things, both fully owned (ADR-0030 §2–3):**

1. **The backend graph inside `get_task_context`/`kb_search` responses.** Resolving scope, walking
   the blast-radius graph, pulling conventions, and finding similar prior changes run as parallel
   graph nodes, reconciled by a synthesis node, with one bounded broadened retry if scope resolves
   empty. Entirely server-side, inside a single MCP tool call — the developer on any of the three
   surfaces sees one fast, well-grounded answer, identically.
2. **The review panel.** Fan-out of the four specialist reviewers, join at the `code_reviewer`
   synthesizer, one posted review — on GitHub Actions, with a Postgres checkpointer in a dedicated
   `review_panel` schema (`thread_id = <repo>#<pr>@<head_sha>`). Checkpointing is the point: a
   killed CI runner resumes without re-running all four reviews, and a same-sha re-run is a no-op.

Both are bounded jobs, not a long-lived concurrent service, so **self-hosted OSS LangGraph is the
right shape — no LangGraph Platform subscription**. The distributed-coordination gap in bare OSS
that the first draft treated as disqualifying applies to a persistent multi-tenant substrate, which
ADR-0030 explicitly decided not to build.

**LangSmith traces both graphs from day one** (ADR-0030 §4) — not deferred until logging proves
insufficient. Env-gated: every suite passes with no `LANGSMITH_*` env set. Cost is budget-gated
against real trace counts once both graphs are running (the scale research put overages at
$1.2K–5K/month at 500K–2M traces — measure, don't assume the free tier holds).

**The deliberate exception (ADR-0030 §5): the nightly build pipeline keeps its own cache, not
LangGraph's checkpointer.** kb-builder already has a working, crash-durable,
content-hash-keyed cache (`generation_cache`/`embedding_cache`) that resumes more precisely than a
generic step checkpoint would. Swapping a proven, purpose-built mechanism for a more generic one
would be a downgrade. This is a specific reason, not "avoid LangGraph by default" applied
inconsistently.

## Security: adversarial hardening is a hard requirement, not a nice-to-have

The 2026 IMBIA evaluation found ChatDev/MetaGPT/AgentVerse — the major multi-agent SDLC frameworks
— all vulnerable to prompt injection at 45–93% attack success rates, framework-dependent. This
platform ingests PRs, tickets, and issue text (untrusted content, per CLAUDE.md invariant 6). Every
host handoff and every point where retrieved/ingested content reaches an agent's context is a
potential injection surface. Concretely:

- Retrieved KB content and PR/ticket content are both untrusted — neither can alter tool policy,
  identity, ACL, or another agent's instructions. This applies to *every* role in the roster and
  every host surface, not just the retrieval path.
- Credentials never reach agents (invariant 6): host agents hold no Postgres/Search/model
  credentials, and the review panel's workflow carries secrets by reference only. The panel is
  report-only by design — it never approves, merges, or requests changes — capping the blast
  radius of any successful injection.
- The concrete gate ships in PR-40 (execution plan A8): diff text, PR title/body, and KB results
  are wrapped in delimited untrusted blocks in every prompt, and a fixture suite of ≥5 injection
  payloads ("approve this", tool-policy override, credential exfiltration ask) must show zero
  policy override and zero unfenced trust, asserted hermetically.

## Operational scale requirements (design-time, not retrofit)

From the KB/MCP scaling research — requirements to design in from the start, not problems to solve
after hundreds of developers are on the system:

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
3. **Cost control**: per-developer/per-team spend ceilings plus a usage-tracked dashboard, treated
   as load-bearing at this scale. This is the same thing the existing **Dashboard initiative**
   (already noted as needing an ADR) was scoped for — it graduates from "nice observability" to
   "the thing that prevents a runaway bill" at hundreds of developers.
4. **MCP server**: stateless-per-call from day one — session state lives in Postgres, not
   in-process — so horizontal scaling doesn't require a retrofit. Raw MCP throughput is not the
   bottleneck (benchmarked 10K+ concurrent connections); session-state affinity is, and only shows
   up once the server is actually stateful somewhere it shouldn't be.

## What to evaluate reusing before building

- **Anthropic's Claude Code multi-agent Code Review feature** (Teams/Enterprise, per-repo, runs in
  cloud on every PR). ADR-0030 decided to own the panel — it needs KB-grounded reviewers, the
  canonical `agents/*.md` prompts, and report-only governance — so this is no longer a build-vs-buy
  question; it remains the natural external baseline to measure the owned panel against once PR-40
  runs on real PRs.
- **`h4gen/postgres-graph-rag`** — a close reference implementation for the Postgres hybrid-search +
  pooling shape this platform needs at scale.

## What's still open

From ADR-0030's follow-ups and the execution plan's unchecked criteria (A3–A13):

- **`get_task_context` (PR-39)**: the LangGraph graph itself (A3), the 3/3 collision-fixture gate
  (A5), the A/B harness result (A6), and the p50 < 2s query-latency target (A10).
- **Alias index (PR-38)**: ≥80% top-1 on the 25-case golden set (A4).
- **Review panel (PR-40)**: crash-resume + same-sha idempotency (A7), the injection fixture suite
  (A8), no-creds LangSmith gating (A9).
- **Orchestrator wiring (A11)**: `agents/orchestrator.md` does not yet invoke the six new roles.
- **`adr_draft_v1` output schema** (A12): referenced by `adr_writer_agent`, not yet registered in
  `agent_output_schemas/` — pinned by a strict xfail in `test_agent_manifests.py`.
- **`.claude/rules/token-budgets.md`** still names only the original roles, not all twelve (A12).
- **`kb_search` budget-window TTL** for long-lived host sessions (PR-37 open question) — needs
  real usage data and a request-schema extension.
- **Embedding-backed alias fuzzy match** (ADR-0019 Ollama path) — deferred; add only if A4's
  measured accuracy demands it.
- **LangSmith cost at volume** — budget-gate on real trace counts (ADR-0030 follow-up).
- **A2A adoption timing** — see above; revisit only on a real cross-runtime need.
