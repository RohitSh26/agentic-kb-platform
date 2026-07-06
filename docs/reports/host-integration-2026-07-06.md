# Host integration test — Copilot CLI + OpenCode against the Context Broker (pre-pilot gate)

> Executes `docs/runbooks/host-integration-test-plan.md` (T1–T5) on the two real host binaries,
> through the **committed** host configurations, against the built `agentic_kb_full` registry.
> Harness: `scripts/integration/` (this PR). Reporting discipline per
> `docs/architecture/evaluation-system.md`: verbatim failures, skip-with-reason, flakes counted
> separately, no waivers. Date: 2026-07-06.

**GATE VERDICT: FAIL — pilot may not start yet.** The Copilot CLI lane passed the full matrix
(13/13 after one committed-config finding was worked around at install time). The OpenCode lane
passed discovery, single-tool correctness, and governance (T2/T3/T5 = 100%) but **failed T4 agent
discipline on both Groq models tested**, with two different, well-characterized failure modes
(findings 5–6). Issues are filed below; the matrix re-runs on their fixes.

## Setup

| Component | Version / value |
|---|---|
| GitHub Copilot CLI | 1.0.63 (`gh` 2.76.1, keyring OAuth; default model `claude-haiku-4.5`) |
| OpenCode | 1.17.13 (installed via `brew install sst/tap/opencode`) |
| OpenCode models tried | `groq/openai/gpt-oss-120b`, `groq/meta-llama/llama-4-scout-17b-16e-instruct` (Groq key from repo `.env`, loaded in-process, never printed) |
| MCP server | `services/mcp-server` venv (Python 3.12.13), local-dev auth (ADR-0016), loopback :8765 |
| Registry | `agentic_kb_full`, active `kb_version = local.20260705T141922Z` |
| Baseline (T1) | retrieval_event = 48, trace_span = 198 (final: 111 / 321 — rows left in place as evidence) |
| OS / infra | macOS (Darwin 25.5.0), Postgres 16.14, uv 0.8.20, node v18.20.4, jq 1.x, shellcheck present |

Per-host ledger subjects via the documented `MCP_LOCAL_DEV_SUBJECT`: `copilot-cli` and
`opencode-cli` — every graded ledger row carries its host's identity.

## Method (what "committed config" meant in practice)

The test never hand-rolled host policy. Where a CLI could not consume the committed file in
place, its native config was **generated from** the committed one, substituting only what local
serving requires — each mechanic below is codified in `scripts/integration/`:

- **Copilot**: `~/.copilot/mcp-config.json` generated from `.copilot/mcp/repository-settings.json`
  per dev-guide 09 §3 (URL → `http://127.0.0.1:8765/mcp/`, bearer → local-dev placeholder; the
  two-tool allowlist verbatim). Agent renderings `.copilot/agents/*.agent.md` installed to
  `~/.copilot/agents/` (the discovery location `.copilot/README.md` names) with one generation
  transform forced by finding 1. Pre-existing user config backed up and restored on exit.
- **OpenCode**: the committed `.opencode/` tree is auto-discovered at the repo root. The one
  override the committed file cannot carry (the broker URL placeholder) is applied via
  `OPENCODE_CONFIG_CONTENT` — the only merge-last mechanism; a root `opencode.json` **loses** the
  merge to `.opencode/opencode.json` (verified on 1.17.13). Generated from the committed file,
  URL substitution only; the bearer stays the committed `{env:CONTEXT_BROKER_TOKEN}` reference.
- Session-scoped `--disable-mcp-server`/`--disable-builtin-mcps` flags kept Copilot's visible
  surface equal to committed policy without editing any tracked file (finding 2 explains why this
  was needed).
- **Flake policy**: one bounded retry per case, only on a machine-checkable provider/validator
  error (Groq 400s, MCP-boundary pydantic rejects) — never on a graded outcome. First attempts
  preserved verbatim in the evidence bundle.
- **Ledger accounting**: every case captures before/after `retrieval_event`/`trace_span` deltas;
  T5 re-derives the whole window from recorded phase intervals and asserts per-case deltas sum to
  it exactly (zero gaps).

## Results

### T1 — Preflight: PASS
Versions pinned, KB active, baselines snapshotted, port/PID-file hygiene verified.

### T2 — Handshake & discovery: PASS (both hosts)

- **Copilot** (mechanical + model-visible): `copilot mcp get context-broker` shows exactly the
  committed two-tool allowlist; the model, asked to list its `context-broker` tools, reported
  `get_task_context` + `kb_search` and none of the broker's ten other wire tools.
- **OpenCode** (mechanical): `opencode debug config` resolved-config parity against the committed
  `.opencode/opencode.json` — broker URL substituted, `context-broker_*: false` global deny
  intact, all 13 agents' per-tool grants byte-equal to the committed grants; model-visible
  listing also matched.

### T3 — Single-tool correctness: PASS (both hosts)

Per host: one forced `kb_search` → real `results`/`budget_remaining`/`notice` shape, hits
resolve to real repo files, **exactly one approved ledger row**; one forced `get_task_context` →
tiered scope/blast-radius/conventions with evidence ids, one approved row, and all four node
spans in `trace_span` (`resolve_scope`, `blast_radius`, `conventions`, `similar_prior_changes`,
plus the `get_task_context` root and `synthesize`).

(OpenCode required the harness's recorded "forcing-device miss" re-seat once on gpt-oss — the
model answered without calling the tool at all on the first try; the re-run called it cleanly.)

### T4 — Agent discipline

**Copilot CLI (orchestrator rendering, `claude-haiku-4.5`): 9/9 PASS.**

- KB-first EXPLAIN 5/5: an approved broker call precedes any file access in every transcript;
  all answers cite repo sources. (explain-1 passed on a recorded bounded retry — see finding 4
  for what its first attempt exposed.)
- BUILD 2/2: `get_task_context` is the first platform call, plans cite sources.
- Fallback: turn 1 (server up) searched and answered; server killed; resumed session answered
  the next question completely from native reads, exit 0, no crash markers.
- Budget (cap `max_requests: 1`): ledger shows `approved` then `denied`; the model acknowledged
  the budget notice and completed both answers from native tools.

**OpenCode (orchestrator rendering, Groq): FAIL on both models tested.**

| T4 case | gpt-oss-120b (run 2c) | llama-4-scout (run 2d) |
|---|---|---|
| explain-1 | PASS | FAIL — no KB call |
| explain-2 | FAIL — provider 400 ×2 (discipline itself OK: KB-first + citations before the error) | FAIL — no KB call, no citation |
| explain-3 | PASS | FAIL — no KB call, no citation |
| explain-4 | PASS | FAIL — no KB call, no citation |
| explain-5 | PASS | FAIL — no KB call, no citation |
| build-1 | FAIL — called `kb_search` before `get_task_context` | FAIL — order OK, no citations |
| build-2 | PASS | FAIL — order OK, no citations |
| fallback | FAIL — turn 2 answered fully from native reads but the host exited 1 on a trailing provider 400 | turn 2 clean; FAIL — turn 1 never consulted the KB |
| budget (cap 0) | FAIL — denials fired + Q1 answered, Q2 lost to a provider 400 | PASS (denied rows, notice respected, both answers completed) |

The two failure modes, verbatim:

- **gpt-oss-120b** systematically drifts off the namespaced MCP tool name mid-session; Groq's
  strict validation rejects the generation:

  ```
  error="Tool call validation failed: tool call validation failed: attempted to call
  tool 'kb-search' which was not in request.tools"
  ```
  (variants observed: `kb_search`, `kb-search`, `exec`, `commentary` — the last being the
  model's harmony-format channel name leaking as a tool call). When this fires after the answer
  text, OpenCode still exits 1, so a completed answer is reported as a failed run.

- **llama-4-scout** is mechanically clean (T2/T3 100%, zero provider 400s in the ledgered calls)
  but does not follow the KB-first discipline for open questions — it answers from priors with
  no broker call and no citations, despite the committed orchestrator body.

### T5 — Governance evidence: PASS

- **Ledger completeness**: 28 host rows in the graded window (`copilot-cli` 21,
  `opencode-cli` 7); per-case deltas sum to the window exactly — zero gaps; only
  `kb_search`/`get_task_context` rows; all statuses ∈ approved/denied.
- **Subject attribution**: every row carries its host's subject; probe traffic (subject
  `probe-subject`) stayed outside the graded window by construction.
- **Secret scan**: zero matches for the values of `LLM_API_KEY`, `GITHUB_TOKEN`, `ADO_PAT`, the
  live `gh` token, and the `gsk_`/`gh*_`/`github_pat_`/`sk-` patterns across every transcript,
  session share, and server log (counts-only scan; values never printed).
- **Dashboard**: `make dashboard` against `agentic_kb_full` exited 0 and rendered
  `dashboard.html` + `dashboard.md` (copied into the evidence bundle, removed from the repo).

### T6 (optional) — review-panel draft smoke: SKIP
Reason: optional per the plan, and the gate already fails on T4; requires the review-panel
service running. A human can exercise it post-fix via `scripts/run_review_panel_local.sh`.

## Flake accounting

Bounded retries (one per case, provider-error signatures only, first attempts preserved):
Copilot 1/13 cases (explain-1, MCP-boundary schema reject — finding 4); OpenCode gpt-oss 3/12,
scout 3/12 — plus several in-session recoveries where the host itself repaired a failed call
without our retry (visible as `— Failed` blocks in transcripts, e.g. copilot build-1/2,
explain-5). No golden expectation was ever retried against; the one "forcing-device miss"
re-seat (T3, gpt-oss) is recorded in the case meta.

## Findings (filed; none silently fixed)

1. **Committed Copilot orchestrator rendering fails to load in Copilot CLI 1.0.63.**
   `.copilot/agents/orchestrator.agent.md`'s frontmatter `description:` is an unquoted YAML
   scalar containing `": "` — a YAML `ScannerError`; the CLI silently drops exactly this agent
   (every other rendering loaded). All run-1 T4 cases failed with
   `No such agent: orchestrator, available: your_agent_name, adr_writer_agent, ...`.
   *Workaround shipped*: the harness's install step YAML-quotes the description at generation
   time. *Proposed fix*: quote the description in the committed rendering and add a YAML
   frontmatter parse to `agents/check_parity.py` / `test_portable_agent_exports.py`.
2. **Copilot CLI treats `.mcp.json` (Claude Code build-tooling config) as workspace MCP
   config.** A dev checkout therefore exposes `postgres-dev`, `github`, and `agentic-kb`
   (ignoring its `"disabled": true`) to the Copilot host, outside the committed `.copilot`
   policy. Tests used session-scoped `--disable-mcp-server` flags. *Proposed fix*: document in
   dev-guide 09 and decide whether `.mcp.json` should carry a no-Copilot note or the entries
   move to untracked local config.
3. **Copilot CLI lists `.claude/agents/*` (build subagents) as invocable custom agents** —
   the build layer leaks into the product host surface on a dev checkout. Document, or accept.
4. **A schema-invalid tool call leaves no ledger row.** Copilot explain-1's first attempt made
   two `kb_search` calls whose malformed arguments the MCP boundary rejected
   (`1 validation error for call[kb_search] ... Input should be a valid dictionary or instance
   of KbSearchRequest`); the broker never wrote any row — the KB-first attempt is invisible to
   the ledger. Conflicts with "ledger complete by construction". *Proposed fix*: emit an `error`
   retrieval_event on schema rejects (needs an mcp-server change; not in this test's scope).
5. **OpenCode × Groq × gpt-oss-120b: systematic namespaced-tool-name drift** (verbatim above)
   — the committed rendering's discipline holds (KB-first, citations) but sessions die on
   provider 400s; OpenCode also exits 1 when the error lands after a completed answer.
   *Proposals*: reinforce exact tool names in the OpenCode rendering bodies (canon change via
   parity flow), and/or file upstream (OpenCode exit-code semantics; Groq harmony translation).
6. **OpenCode × Groq × llama-4-scout: KB-first discipline not followed** for open questions
   (mechanically clean, but answers from priors without consulting the KB or citing). Together
   with 5: **no Groq-hosted model tested drives the committed OpenCode rendering through T4** —
   the pilot's OpenCode lane needs either a stronger provider/model behind OpenCode or rendering
   reinforcement, then a T4 re-run.
7. **Environment traps now documented in the harness**: `gh auth token` returns an exported
   `GITHUB_TOKEN` (the repo `.env`'s classic PAT shadowed the keyring OAuth token — Copilot
   rejects `ghp_`); `.env`'s `LLM_MODEL` is the build-plane docify model, not an agent model;
   `OPENCODE_CONFIG_CONTENT` is the only merge-last override for the committed placeholder URL.

## Evidence bundle & reproduction

Evidence (transcripts, session shares, per-case SQL deltas, server logs, T5 dumps, first
attempts, archived prior runs) is on the test machine under the session scratchpad
`host-integration-evidence/` tree; the ledger/trace rows remain in `agentic_kb_full`
(retrieval_event 48 → 111, trace_span 198 → 321). Reproduce with:

```sh
EVIDENCE_DIR=/abs/path scripts/integration/run_all.sh          # full matrix
OPENCODE_MODEL=<groq model> ... scripts/integration/run_opencode.sh   # opencode phase only
```

**GATE VERDICT: FAIL** — Copilot CLI lane: PASS (13/13). OpenCode lane: T2/T3/T5 PASS, T4 FAIL
(findings 5–6). Pilot start blocked until findings 1, 5, 6 are fixed and the matrix re-runs
green; findings 2–4 are filed for triage and do not block the Copilot lane.
