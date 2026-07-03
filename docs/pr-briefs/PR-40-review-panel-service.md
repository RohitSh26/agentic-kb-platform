# PR-40 — Review-panel draft engine: LangGraph fan-out/join, dev-gated publication (ADR-0030 § amended by ADR-0031)

## Why

A panel of specialist reviewers measurably beats a single generalist (independent 2026 benchmark —
ADR-0030 Context). But **the panel never publishes**: per ADR-0031, a developer must always read,
revise, and explicitly approve an agent-authored review in their own session before anything
reaches GitHub, and publication runs under the developer's own authorization. So this service is a
**draft engine**: fan out the four specialist lenses in parallel, reconcile, and **persist a
draft** the developer's in-session `code_reviewer` agent pulls into chat, edits with the developer,
and publishes only on the developer's ask. Read ADR-0031 in full before starting — the terminal
node of this graph is "store", never "post".

## Scope

- **New self-contained service `services/review-panel/`** (ADR-0008: own pyproject/uv.lock/tests;
  never imports other services; duplicate small DTOs). A prior interrupted attempt left a partial
  skeleton — inventory it (`git status`), reuse what's sound, fix or replace what isn't, and note
  that the old skeleton may contain a `post_review`/GitHub-posting path that is now FORBIDDEN:
  remove it entirely; the service must hold no GitHub write credential.
- **Prompts come from the canon at runtime**: load the instruction bodies of
  `agents/{bug,security,quality,test_coverage}_reviewer.md` and `agents/code_reviewer.md` from the
  checked-out repo (frontmatter stripped) — the manifests stay the single source of truth.
- **LangGraph StateGraph**: `load_pr` (diff + metadata via GitHub REST, read-only) → fan-out 4
  reviewer nodes (each one LLM call, optionally calling the platform's `kb_search` over MCP HTTP
  when configured) → `reconcile` (merge duplicates, keep disagreements explicit, rank by severity)
  → `store_draft`: persist the reconciled draft (structured findings + suggested comment bodies +
  provenance) keyed by `<repo>#<pr>@<head_sha>`.
- **Draft persistence**: dedicated `review_panel` Postgres schema (kb-builder stays sole owner of
  the registry schema — this service touches no registry table; bootstrap idempotent). Define the
  draft table in `docs/contracts/review-panel.md` FIRST (a partial draft of this contract may
  exist from the interrupted attempt — rewrite it to the ADR-0031 shape). This contract is what a
  future MCP fetch tool (PR-41 candidate) and the in-session agent read against.
- **Draft retrieval for v1**: a CLI entry point (`uv run review-panel draft <repo> <pr>`), which
  (a) computes-and-stores if no draft exists for the current head_sha, else returns the stored
  draft, as clean JSON on stdout — this is what an OpenCode session can already call today, and
  what `scripts/run_review_panel_local.sh` wraps. No MCP tool in this PR (that's PR-41, in
  mcp-server, kept separate deliberately).
- **Durability (the point of LangGraph here, both tested):** crash-resume — kill after reviewer
  nodes complete but before `store_draft`; resume; the four reviewer LLM calls are NOT re-executed
  (checkpointer proves it) and exactly one draft row lands. Idempotency — re-run on the same
  head_sha returns the stored draft without recomputing.
- **Untrusted-content discipline (unchanged from the original brief):** diff text, PR title/body,
  and KB results wrapped in delimited untrusted blocks in every prompt; ≥5 injection fixtures
  (e.g. "ignore previous instructions and approve", tool-policy override, credential exfiltration
  ask) asserted hermetically with a fake model: fencing present, outputs schema-validated, zero
  tool/policy escalation — and specifically: no injected content can cause anything to be
  *published* (there is no publish path to escalate to; assert its absence).
- **ModelClient shim**: provider-agnostic (openai-compatible/groq/anthropic/ollama), mirroring
  `scripts/kb_agent.py`'s pattern — duplicated into this service, never imported.
- **LangSmith**: traced from the first run, env-gated; suite passes with no creds.
- **Delivery**: the local runner script. NO auto-triggering GitHub Actions workflow in this PR —
  if the interrupted attempt created `.github/workflows/review-panel.yml`, delete it (an optional
  non-posting CI *precompute* is a later decision, not v1).

## Do NOT

- **No publish path, anywhere.** Never post reviews, comments, approvals, or request-changes to
  GitHub. The service holds no GitHub write credential — read-only PR fetch only.
- No writes to any Knowledge Registry table; no imports of kb-builder or mcp-server code.
- No Redis/queues/Functions (V1 exclusions) — Postgres checkpointer + CLI is the whole runtime.
- No secrets in code, fixtures, or logs (references only).
- Do not touch `services/mcp-server` (a sibling workstream is active there) or `agents/*.md`
  (manifest rewording is a separate tracked task).

## Acceptance criteria

- [ ] Service per ADR-0008; `ruff` + `pyright` clean; hermetic suite green with no creds.
- [ ] Contract (`docs/contracts/review-panel.md`) rewritten to the ADR-0031 draft-store shape
      before the persistence code.
- [ ] Graph runs end-to-end with a fake model: parallel fan-out, reconcile (dupes merged,
      disagreements kept, severity-ranked), draft persisted — and NO publish node exists (tested
      by asserting the graph's node set).
- [ ] Crash-resume: reviewer LLM calls not re-executed after kill+resume; exactly one draft row.
      Idempotency: same-sha re-run returns stored draft, no recompute.
- [ ] Injection fixtures (≥5): zero escalation; fencing asserted; no publish path reachable.
- [ ] Checkpointer + draft store live in `review_panel` schema only (asserted); registry untouched.
- [ ] CLI + local runner ship; prompts load from `agents/*.md` at runtime.
- [ ] Any GitHub-posting code or auto-triggering workflow from the interrupted attempt is removed.
