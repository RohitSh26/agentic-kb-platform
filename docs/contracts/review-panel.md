# Contract: Review-panel draft engine (ADR-0030 §3 as amended by ADR-0031)

The review panel is a self-contained service (`services/review-panel`, package `review_panel`)
that runs as one bounded on-demand job per pull request: LangGraph fan-out of the four specialist
reviewers → deterministic reconciliation + `code_reviewer` synthesis → **one persisted draft**.
**The panel never publishes.** No review, comment, approval, or request-changes ever reaches
GitHub from this service; the developer's in-session agent pulls the draft, the developer edits
it, and publication happens only on the developer's ask under the developer's own authorization
(ADR-0031 Decision §1). This document is the only interface other parts of the platform may rely
on (ADR-0008: no shared Python packages; small DTOs are duplicated).

## Dev gate (no publish path)

- The service holds **no GitHub write credential**. Its GitHub adapter is read-only by
  construction: the only capability is fetching PR metadata + diff over GET
  (`GITHUB_TOKEN` optional, read scope; unauthenticated works for public repos).
- The graph's terminal node is `store_draft`. There is no posting node; tests assert the graph's
  node set and the read-only client surface (`tests/contract/test_dev_gate.py`,
  `tests/integration/test_graph_end_to_end.py`).
- A future MCP fetch tool (PR-41 candidate) and the in-session `code_reviewer` agent read drafts
  against this contract; in the interim the CLI below is the fetch path.

## Storage ownership

- The service owns the dedicated Postgres schema **`review_panel`** and nothing else. It contains
  the LangGraph checkpointer's tables plus the `review_draft` and `trace_span` (ADR-0032 — see
  "Tracing" below) tables, all created idempotently at startup (`CREATE SCHEMA IF NOT EXISTS` /
  `CREATE TABLE IF NOT EXISTS`; the connection's `search_path` is pinned to `review_panel`, so no
  other schema is reachable).
- kb-builder remains the sole owner of the Knowledge Registry (public schema). The review panel
  never reads or writes any registry table (`source_item`, `knowledge_artifact`,
  `knowledge_edge`, `generation_cache`, `embedding_cache`, `kb_build_run`, `retrieval_event`);
  asserted by `tests/integration/test_draft_store_schema.py`, and statically impossible — the
  service imports neither sqlalchemy/asyncpg nor any kb-builder code
  (`tests/contract/test_import_boundaries.py`).
- One env, `REVIEW_PANEL_DATABASE_URL`, backs both the checkpointer and the draft store. Unset,
  both fall back to in-memory (single-process durability only; logged plainly as
  `event=persistence_fallback`).
- **Alembic exemption (explicit).** The repo rule "every schema change is an Alembic revision
  with a downgrade" applies to the Knowledge Registry, which kb-builder owns. The `review_panel`
  schema is deliberately outside it: it holds only derived, recomputable state (checkpoints,
  drafts, and — per ADR-0032 — trace spans; never truth, never served as evidence), is
  bootstrapped idempotently at startup, and its rollback story is simply
  `DROP SCHEMA review_panel CASCADE` — nothing else references it. Growing this schema beyond
  derived state would end the exemption and require an ADR.

## Draft table

```sql
-- schema: review_panel
CREATE TABLE review_draft (
    draft_key  text PRIMARY KEY,          -- "<repo>#<pr_number>@<head_sha>"
    repo       text NOT NULL,             -- "owner/name"
    pr_number  integer NOT NULL,
    head_sha   text NOT NULL,
    draft      jsonb NOT NULL,            -- review_draft_v1 document (below)
    created_at timestamptz NOT NULL DEFAULT now()
);
```

- **Key + idempotency**: `draft_key = <repo>#<pr_number>@<head_sha>` — also the LangGraph
  checkpoint `thread_id`. At most one draft row exists per key. Writes are
  `INSERT … ON CONFLICT (draft_key) DO NOTHING` (first writer wins; a racing run reuses the
  stored row). A re-run on the same head SHA returns the stored draft without recomputing; a new
  head SHA is a new key, a new thread, and a fresh computation.
- **Crash-resume**: a run killed after reviewer nodes complete resumes its checkpointed thread —
  the reviewer LLM calls are not re-executed and exactly one draft row lands.

## `review_draft_v1` (the `draft` jsonb document)

```json
{
  "schema_version": "1.0.0",
  "draft_key": "acme/platform#7@<head_sha>",
  "repo": "acme/platform",
  "pr_number": 7,
  "head_sha": "<head_sha>",
  "generated_at": "2026-07-03T12:00:00Z",
  "advisory_verdict": "approve | request_changes",
  "lens_verdicts": {"bug": "…", "security": "…", "quality": "…", "test_coverage": "…"},
  "findings": [
    {
      "severity": "blocker | major | minor | note",
      "finding": "<what and where>",
      "evidence_ids": ["<file path or diff hunk>"],
      "lenses": ["bug", "security"],
      "disagreement": "severity disputed (bug=major, security=minor); highest kept | null",
      "suggested_comment": "<markdown comment body the developer can edit and post>"
    }
  ],
  "open_questions": ["<panel + synthesizer questions, deduped>"],
  "synthesis": { "…the synthesizer's own review_findings_v1 output…" },
  "summary_markdown": "<one editable overall review body, draft-labelled>",
  "provenance": {
    "engine": "review-panel",
    "engine_version": "<service version>",
    "model": "<provider>:<model id>",
    "lenses": ["bug", "security", "quality", "test_coverage"],
    "kb_used": false
  }
}
```

- `findings[]` is the **deterministic** reconciliation (merge duplicates, keep disagreements
  explicit, rank by severity — `review_panel/domain/reconcile.py`); the synthesizer's model call
  layers `advisory_verdict` + `synthesis` on top and can never drop a panelist finding.
- `suggested_comment` and `summary_markdown` are rendered by service code, never by the model,
  and `summary_markdown` always carries the draft disclaimer (never-published, developer edits
  and publishes). The verdict is advisory only — nothing in this document triggers any publish.

## Draft retrieval (v1: CLI)

```
uv run review-panel draft <owner/repo> <pr-number>
```

- If a draft exists for the PR's **current** head SHA → prints the stored `review_draft_v1` JSON
  on stdout (no model calls, no LLM credentials needed).
- Else → computes, stores, and prints it (LLM credentials required for this path only).
- stdout carries ONLY the JSON document; structured logs go to stderr. Exit 0 on success, 1 on
  failure. `scripts/run_review_panel_local.sh` wraps this command. There is **no auto-triggering
  GitHub Actions workflow** (ADR-0031: trigger is on-demand; a non-posting CI precompute is a
  later decision). The MCP fetch tool is PR-41, in mcp-server (below).

## Fetching drafts over MCP (PR-41)

Hosts without a shell (VS Code Copilot chat) fetch a stored draft through the Context Broker's
`get_review_draft` MCP tool instead of the CLI — full request/response contract:
`docs/contracts/mcp-tools-contract.md`. The two paths answer the SAME question two different
ways and intentionally diverge on one point: the CLI resolves the PR's *current* head SHA via a
live (read-only) GitHub call before looking up the draft; `get_review_draft` never calls GitHub
at all (mcp-server holds no GitHub credential and this tool's whole point is to stay Postgres-only)
— an omitted `head_sha` instead returns the **newest stored** draft for `(repo, pr_number)`, which
is the current head's draft whenever the panel has already run for it, and a slightly stale one
only if a newer commit landed after the last draft computation.

- **Read-only, compute-never (the same dev gate, from the other side).** `get_review_draft`
  `SELECT`s the `review_panel.review_draft` table this contract owns; it never inserts, updates,
  or triggers `compute_draft`. review-panel remains the schema's sole writer. This is the SAME
  cross-schema READ posture mcp-server already has on the kb-builder-owned Knowledge Registry
  (a reader service that does not own the schema it reads) — mcp-server duplicates only the tiny
  row shape from the Draft table above (`draft_key`, `repo`, `pr_number`, `head_sha`, `draft`,
  `created_at`) as a plain dataclass; it does not import `review_panel` Python code or its
  `ReviewDraft` pydantic model (ADR-0008). The inner `draft` jsonb document is returned to the MCP
  caller **verbatim** — mcp-server does not parse, validate, or reshape the `review_draft_v1`
  shape; that schema stays owned here.
- **No draft yet is not an error.** `{found: false}` when no row matches `(repo, pr_number[,
  head_sha])` — the developer's agent falls back to reviewing cold (ADR-0031 Decision §4). A
  genuinely unexpected read failure (most likely: `DATABASE_URL` and `REVIEW_PANEL_DATABASE_URL`
  point at different databases, so the `review_panel` schema/table is simply absent from
  mcp-server's connection — see `docs/dev-guide/05-database-operations.md` §"review_panel drafts
  list") is a distinct case: a tool error, ledgered `status="error"`.
- **No `kb_search` budget charge.** Fetching an already-computed draft is not knowledge retrieval
  — recorded explicitly so a future reader does not "fix" this into the budgeted path by analogy
  with `kb_search`/`get_task_context`. The only gates are authentication (every MCP tool requires
  an authenticated session) and the client-scope check (`context.read`, additive, opt-in per
  client — `docs/contracts/mcp-tools-contract.md`).
- **Authorization (v1 scope).** Any authenticated requester may fetch any draft in v1 — this
  platform instance is single-team/local scope, so there is no per-repo or per-team ACL on
  `review_draft` rows the way `knowledge_artifact.acl_teams` gates registry artifacts. A
  multi-team deployment sharing one `review_panel` schema across repos with different visibility
  needs a draft-level ACL; that is a recorded **layer-2 item**, not solved by PR-41.

## Panelist output schema

Each reviewer node and the synthesizer must return `review_findings_v1` JSON
(`docs/contracts/agent-output-contracts.md`): `schema_version "1.0.0"`, `verdict`
(`approve | request_changes` — advisory only), `findings[]` of
`{severity: blocker|major|minor|note, finding, evidence_ids[]}`, and `open_questions[]`. The DTO
is deliberately duplicated in `review_panel/domain/findings.py` (canonical shape:
`services/mcp-server/src/agentic_mcp_server/agent_output_schemas/review_findings_v1.py`);
keep the two in sync through this contract, never by import.

## Prompt source

Prompts are loaded at RUNTIME from the checked-out repo's canonical manifests —
`agents/{bug,security,quality,test_coverage}_reviewer.md` and `agents/code_reviewer.md` — with
YAML frontmatter stripped and only the instruction body used. No prompt text is copied into the
service, and no test pins manifest wording (teams own their agents' voice — ADR-0009).
`REVIEW_PANEL_AGENTS_DIR` points at the `agents/` directory (defaults to `../../agents` relative
to the service, i.e. the repo checkout).

## Untrusted-content discipline

PR title, PR body, diff text, and any KB results are untrusted content. Every prompt wraps them
in delimited blocks (`<<<UNTRUSTED_CONTENT_BEGIN>>> <label>` … `<<<UNTRUSTED_CONTENT_END>>>
<label>`) behind a fixed preamble stating the content is data, never instructions. Delimiter
sequences occurring *inside* untrusted content are neutralized before fencing so a payload can
never close a fence early. Model outputs are schema-validated; on a schema-validation failure the
node retries **once**, feeding the verbatim validator error back as a fenced block (the error can
embed fragments of the model's own untrusted-derived output, so it gets the same fencing PR/KB
text does — never trusted as an instruction) — this is the "adopted" bounded runtime retry against
a machine-checkable validator (`docs/architecture/evaluation-system.md` §2), not an
iterate-until-pass loop. A **second** consecutive failure fails the node (and therefore the run)
exactly as before — nothing is ever stored from unvalidated output. Nothing in untrusted content
can add tools, alter the draft key, or cause anything to be published — there is no publish path
to escalate to (asserted by `tests/integration/test_injection.py`).

## Tracing (ADR-0032)

Per-step tracing lives in this service's own `trace_span` table (`review_panel` schema,
bootstrapped idempotently alongside `review_draft`), behind a `TraceSink` port duplicated from
mcp-server's (never shared, ADR-0008). Full span shape + fail-soft rule: `docs/contracts/tracing.md`.

- **Root span**: one per `compute_draft` attempt (an initial run or a crash-resume), name
  `review_panel.draft_run`. **Node spans**: `load_pr`, `review_bug`/`review_security`/
  `review_quality`/`review_test_coverage`, `reconcile`, `store_draft` — one per node that actually
  executes in that attempt (a resumed run's already-completed nodes do not re-emit).
- **`trace_id` = the draft's key** (`<repo>#<pr>@<head_sha>`) — deterministic and stable across a
  crash + resume, so a resumed attempt's spans correlate with the interrupted attempt's under the
  same trace, unlike a fresh random id.
- **Never checkpointed.** Spans are emitted directly from each node closure via
  `PanelDependencies.trace_sink` and never enter `PanelState` — the LangGraph checkpointer persists
  only graph state, never trace data, so tracing cannot affect or be affected by crash-resume
  semantics.
- **Env**: `TRACE_SINK=postgres|none` (default `postgres` when `REVIEW_PANEL_DATABASE_URL` is set,
  else `NullTraceSink`, exactly like the checkpointer/draft-store fallback).
- LangChain's native `LANGSMITH_*` env instrumentation remains inert (surfaced only in the
  `panel_start` boot log) and is not part of the tracing story — see ADR-0032, which withdraws the
  LangSmith commitment made on paper by ADR-0030 §4 before ever activating it.
- **No-content rule, service-specific additions**: on top of `docs/contracts/tracing.md`'s shared
  forbidden-key set, this service's `Span.__post_init__` also rejects `diff`, `pr_body`,
  `pr_title`, and `kb_context` — the literal PR/KB fields a node closure has in scope and could
  otherwise pass into `attributes` by mistake.

## Optional KB access

When `REVIEW_PANEL_MCP_URL` is set, the service performs one `kb_search` MCP tools/call over
streamable HTTP (bearer token from `REVIEW_PANEL_MCP_TOKEN`, by reference) during `load_pr` and
shares the fenced result with all four reviewers — each reviewer stays a single LLM call. Unset
(the default, and the hermetic-test configuration) the panel runs with no KB access. KB failures
are fail-soft: the panel logs and reviews without KB context.

## Configuration (identifiers and references only — no secret values)

| Env | Meaning |
| --- | --- |
| `GITHUB_TOKEN` | Optional read-only GitHub token (private repos / rate limits). Never a write credential. |
| `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY` / `LLM_BASE_URL` | ModelClient shim, provider-agnostic (`groq`, `openai`, `openai_compatible`, `ollama`, `anthropic`) — mirrors `scripts/kb_agent.py`. Needed only when computing a new draft. |
| `REVIEW_PANEL_DATABASE_URL` | Postgres URL for checkpointer + draft store. Unset ⇒ in-memory fallback (logged plainly). |
| `REVIEW_PANEL_AGENTS_DIR` | Path to the canonical `agents/` directory |
| `REVIEW_PANEL_MCP_URL` / `REVIEW_PANEL_MCP_TOKEN` | Optional MCP endpoint for `kb_search` |
| `TRACE_SINK` | `postgres` (default when `REVIEW_PANEL_DATABASE_URL` is set) or `none` — per-step tracing (ADR-0032) |
| `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` | Inert — LangChain's native env instrumentation, surfaced only in the boot log; not the tracing story (ADR-0032 withdraws ADR-0030 §4's LangSmith commitment). The suite passes with neither set |

Delivery: the CLI + `scripts/run_review_panel_local.sh`. Tests follow mcp-server's
`TEST_DATABASE_URL` convention: DB-backed tests skip without it; everything else is hermetic.
