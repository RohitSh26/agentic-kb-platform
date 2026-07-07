# Host integration test plan — OpenCode + Copilot CLI (pre-pilot gate)

> Requested by the architects (2026-07-06): before the pilot, prove the agentic system through the
> two **real host binaries** — not simulated clients. Everything below produces evidence (ledger
> rows, trace spans, transcripts), graded PASS/FAIL with verbatim failures, in the same discipline
> as `docs/architecture/evaluation-system.md`. Status: **plan — awaiting architect approval;
> executable immediately on approval.**

## 1. What is already proven vs. what this test adds

| Already proven (automated, committed) | NOT yet proven — this test's job |
|---|---|
| Server-side tools, budgets, ledger, ACL (1,368+ tests) | Real host processes connecting over the real transport |
| FastMCP in-process client calls (integration suites) | Tool **discovery** through each host's committed config |
| Manifests/renderings parity (12 roles, exit 0) | Whether the hosts actually **load** those manifests/skills and obey them |
| A/B agent behavior in our own harness (kb_agent) | Agent discipline inside the hosts' **own** loops (KB-first, fallback, budgets) |
| Bootstrap on a clean clone (twice) | Failure UX end-to-end as a developer would experience it |

## 2. Environment readiness (preflighted 2026-07-06 on the dev machine)

| Component | State | Action needed |
|---|---|---|
| Copilot CLI | **installed** (1.0.63), `gh` authed (account live-verified against this server in June) | none — runnable today |
| OpenCode | not installed | one install command + Groq provider config (key already in `.env`, per standing directive) |
| MCP server + active KB | ready (`agentic_kb_full`, clean, p50 0.93s) | start server per docs/dev-guide/getting-started.md |
| Committed host configs | `.copilot/mcp/repository-settings.json` = `[get_task_context, kb_search]`; `.opencode/` full rendering | none — the test MUST use these, never ad-hoc config |

## 3. Test matrix — two hosts × five layers

**T1 — Preflight (automated).** Binaries + versions pinned; server healthy; KB active; ledger/trace
row counts snapshotted.

**T2 — Handshake & discovery (automated).** Host connects using ONLY the committed config. PASS =
the host lists exactly the expected tool surface (Copilot: the two-tool allowlist; OpenCode:
per-agent grants) and a first call authenticates with the right subject attribution in the ledger.

**T3 — Single-tool correctness (automated).** Scripted non-interactive prompts (`copilot -p …`,
`opencode run …`) engineered to force exactly one tool call each:
- one `kb_search` → response carries `results/budget_remaining/notice`; exactly one `approved`
  ledger row; hits are real artifacts.
- one `get_task_context` → resolved scope + blast radius present, tiered; one ledger row; trace
  spans for all four nodes visible in `trace_span`.

**T4 — Agent discipline (behavioral, automated with human review of transcripts).**
- *KB-first:* 5 scripted EXPLAIN-style questions → ledger shows `kb_search`/`get_task_context`
  BEFORE any file access each time; answers carry citations. PASS = 5/5 (model flakes counted and
  reported separately, per the eval system's flake discipline).
- *BUILD-lane:* 2 scripted task prompts → `get_task_context` is the first platform call.
- *Fallback (the answer-completely rule):* server stopped mid-session → the agent still answers
  from native reads, no visible crash; ledger shows the error row from the last attempt.
- *Budget exhaustion:* server started with a tiny `MCP_AGENT_ALLOWANCES` cap → budget notice
  respected, agent degrades to native tools, completes the answer.

**T5 — Governance evidence (automated).** For the whole session: one ledger row per call, zero
gaps; subject attribution matches the host identity; transcript + server logs grepped for secrets
(zero tolerance); `make dashboard` renders the session's numbers.

*(Optional T6, OpenCode only: `review-panel draft` smoke from the host's terminal — exercises the
dev-gated review path a pilot developer would use.)*

## 4. Method

- **Harness:** `scripts/integration/` — one runner per host + a shared grader. Uses each CLI's
  non-interactive mode; captures transcript, exit code, and before/after SQL snapshots
  (`retrieval_event`, `trace_span`) into an evidence directory; grader emits a markdown report with
  verbatim failures (same posture as `evals/run_all.py`). Auth-gated or missing-binary cases SKIP
  with a stated reason — never fabricated.
- **Copilot CLI caveat:** requires a Copilot-entitled GitHub account — automated here because this
  machine is authed; on any other machine it's a 15-minute human step (documented inline).
- **OpenCode provider:** Groq (`.env`), consistent with the platform's provider directive.
- **Version pinning:** the report records exact host versions; host CLIs move fast, so the harness
  asserts behavior, not UI text.

## 5. Pass/fail — the gate to the pilot

- T1–T3, T5: **100% of automated cases pass on both hosts**, or a failure is filed → fixed →
  matrix re-run (no waivers; a host-side hard limitation must be documented with a workaround and
  architect sign-off).
- T4: 5/5 KB-first on clean runs (flakes reported, retried per the bounded-retry policy, and if
  flakes dominate, that is itself a finding); fallback and budget cases end in complete answers
  with zero uncaught errors.
- Evidence bundle (report + transcripts + SQL snapshots) committed to `docs/reports/
  host-integration-<date>.md` — the artifact the architects review.

## 6. Risks & knowns

- **Host version drift** since the June live-verification — mitigated by preflight pinning and
  behavior-level assertions.
- **Host model quality** affects tool-use reliability — flake accounting + the provider-400 retry
  already shipped; numbers get reported, not hidden.
- **OpenCode manifest semantics** (auto-invoke by description, subagent permissions) are the least
  previously-exercised surface — expect the fix loop to land there if anywhere.
- Copilot CLI entitlement on other machines is human-gated — the pilot checklist already covers it.

## 7. Effort

| Step | Estimate |
|---|---|
| Harness + runbook scripts (agent-built, verified) | one focused session |
| OpenCode install + provider config | minutes |
| Full automated matrix run (both hosts) | ~30–45 min |
| Fix loop | evidence-driven; unknown until run 1 |
| Architect review of the evidence bundle | their call |
