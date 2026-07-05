# review-panel

LangGraph draft engine (ADR-0030 §3 as amended by ADR-0031): the four specialist reviewers
(`bug`, `security`, `quality`, `test_coverage`) run in parallel, the `code_reviewer` synthesizer
reconciles, and ONE draft is persisted per `<repo>#<pr>@<head_sha>`. **The panel never
publishes** — no review, comment, or approval ever reaches GitHub from this service (there is no
posting node and no GitHub write credential). The developer's in-session agent pulls the draft,
the developer edits it, and publication happens only on the developer's ask under the
developer's own authorization. Contract: `docs/contracts/review-panel.md`.

- Prompts load at runtime from the canonical `agents/*.md` manifests (frontmatter stripped).
- Durability: LangGraph Postgres checkpointer + `review_draft` table in the dedicated
  `review_panel` schema (`REVIEW_PANEL_DATABASE_URL`); thread id = draft key
  `<repo>#<pr>@<head_sha>`. A killed run resumes without re-paying the reviewer LLM calls; a
  same-SHA re-run returns the stored draft without recomputing. Without a database, an in-memory
  fallback runs the single bounded job (logged plainly; no cross-process durability).
- Untrusted-content discipline: PR/diff/KB text is fenced, model output is schema-validated
  (`review_findings_v1`), and the GitHub adapter is read-only by construction.
- Per-step tracing (ADR-0032): one root span per draft-run attempt plus one span per graph node,
  written to a `trace_span` table in the `review_panel` schema behind a `TraceSink` port
  (`TRACE_SINK=postgres|none`); fail-soft always, never checkpointed state. No hosted tracing
  SaaS — LangChain's native `LANGSMITH_*` env instrumentation remains inert and unconfigured; the
  whole test suite is hermetic and needs no LLM/LangSmith/GitHub credentials.

## Run

```bash
# Compute or fetch the draft for a PR (JSON on stdout, logs on stderr):
uv run review-panel draft owner/repo 123
# Or from the repo root (sources .env, points at agents/):
scripts/run_review_panel_local.sh owner/repo 123
```

There is no auto-triggering GitHub Actions workflow (ADR-0031) — the trigger is on-demand.

## Develop

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest                       # hermetic; Postgres tests skip without TEST_DATABASE_URL
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/agentic_kb_test uv run pytest
```
