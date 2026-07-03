# ADR-0031 — Dev-gated review publication: the panel drafts, the developer publishes

## Status

Accepted (2026-07-03). Amends ADR-0030 Decision §3 (which had the review panel auto-posting one
review per PR from a GitHub Actions trigger). Everything else in ADR-0030 stands.

## Context

The owner described how teams actually review PRs (2026-07-03): a developer in VS Code opens the
PR/review extension, goes to Copilot chat, asks the reviewer agent to review a tagged PR; the agent
reviews it according to the team's own agent description and returns the review in the team's
format, **in chat**; the developer reads it, asks for corrections ("update these comments"), and
only then pushes the review to GitHub. The developer must always get the chance to read and revise
an agent-authored review before it is published — ADR-0030 §3's auto-post-on-PR-open flow removed
exactly that chance, publishing before developer sight.

This also aligns with two standing platform positions: teams own their agents' descriptions and
output formats (ADR-0009 — the framework ships the skeleton, not the voice), and the platform's
leverage is precompute + tool quality, not seizing control of the workflow.

## Decision

1. **No agent-authored review is ever published without a developer having seen and approved it
   in-session.** Publication is developer-triggered and runs under the developer's own
   authorization (host-native: the Copilot PR integration, or `gh` in the workspace) — the review
   lands on GitHub attributable to the developer who approved it. There is no server-held GitHub
   write credential for reviews.
2. **The primary review flow is interactive and host-side.** The team's `code_reviewer` agent
   (canonical skeleton + the team's own description slot) reviews in-session: KB-grounded via
   `kb_search`/`get_task_context`, draft presented in chat, iterated with the developer, published
   on the developer's ask. Works today on all three host surfaces with nothing but the MCP tools.
3. **The LangGraph panel is retained as a server-side *draft engine* — it never posts.** The
   fan-out/join from ADR-0030 §3 (four specialist lenses in parallel → reconcile) survives intact,
   but its terminal node changes from "post review" to "persist draft" (dedicated `review_panel`
   schema, contract-first). Drafts exist so the dev's agent can start from a thorough,
   checkpointed, LangSmith-traced multi-lens draft instead of reviewing cold — fetched
   into the session, edited with the developer, and published only by the developer. Trigger is
   on-demand (local runner; a CI *precompute* that stores-but-never-posts may be added later).
4. **`code_reviewer`'s role is redefined** from "backend synthesizer the orchestrator never
   touches" to: the in-session reviewer/presenter that works WITH the developer — pull the panel
   draft when one exists (else review directly through the four lenses), reconcile, present in the
   team's format, revise on feedback, publish on request. The four specialist lens roles are
   unchanged; they run server-side in the draft engine (or as parallel subagents on hosts that
   support it, e.g. OpenCode).

## Consequences

- `agents/code_reviewer.md` and `agents/orchestrator.md` need rewording (review is no longer
  "backend-only, never in-session" — it is in-session BY DEFAULT, with the backend engine demoted
  to draft preparation), then re-rendering + parity.
- PR-40 is rescoped: `post_review` node removed; draft persistence + a fetch path added; the
  GitHub Actions workflow becomes an optional, non-posting precompute (or is dropped from v1).
- Execution-plan criterion A7 adjusts: crash-resume/idempotency now protect the *draft
  computation* (still real — a killed run must not re-pay four LLM reviews); a new criterion
  covers the dev-gate itself (no publish path exists server-side).
- A follow-up (PR-41 candidate): expose draft fetch via an MCP tool for hosts without shell
  access; on OpenCode the agent can read the draft via the local CLI in the interim.

## Alternatives rejected

- **Auto-post on PR open (ADR-0030 §3 as written).** Rejected by the owner on workflow grounds:
  developers must review the review before GitHub sees it; post-hoc correction of a published bad
  review is not acceptable.
- **Kill the panel entirely and rely only on in-session review.** Rejected: loses the
  benchmark-backed multi-lens quality and the platform's precompute leverage; the panel's problem
  was *who publishes*, not *how it reviews*.
