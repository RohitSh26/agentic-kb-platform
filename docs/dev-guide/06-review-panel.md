# 06 — Review panel: on-demand PR review drafts

> How to run the **review-panel draft engine** (`services/review-panel`) and work with the drafts
> it stores. For a developer who wants a four-lens review of a pull request *before* asking for
> human review — and for anyone wondering where those drafts live and why the panel never posts
> anything to GitHub. Contract: [`docs/contracts/review-panel.md`](../contracts/review-panel.md);
> decision: ADR-0031.

## What the review panel is

The review panel is a self-contained service that runs **one bounded job per pull request**: a
LangGraph fan-out of the four specialist reviewer lenses (**bug**, **security**, **quality**,
**test_coverage**) → deterministic reconciliation (merge duplicates, keep disagreements explicit,
rank by severity) → a `code_reviewer` synthesis pass → **one persisted draft**.

Three properties define it (ADR-0031):

1. **It drafts; it never publishes.** No review, comment, approval, or request-changes ever
   reaches GitHub from this service. It holds **no GitHub write credential** — its GitHub adapter
   can only *fetch* PR metadata and the diff (GET-only). The graph's terminal node is
   `store_draft`; there is no posting node, and tests assert that
   (`services/review-panel/tests/contract/test_dev_gate.py`).
2. **It is on-demand.** You run it from your shell (or a CI job you set up yourself, non-posting).
   There is **no auto-triggering GitHub Actions workflow** — `.github/workflows/` contains only
   `ci.yml`.
3. **The developer publishes.** You pull the draft, edit it, and post it under **your own**
   GitHub authorization. The panel's verdict is advisory only.

Its prompts are not baked in: each reviewer node loads its instruction body at runtime from the
canonical manifests in `agents/` (`bug_reviewer.md`, `security_reviewer.md`, `quality_reviewer.md`,
`test_coverage_reviewer.md`, and `code_reviewer.md` for the synthesis) — teams that edit those
manifests change the panel's voice without touching service code.

## Prerequisites and environment

The service is its own `uv` project. `make sync` (repo root) installs it along with the others.
Configuration is env-only — identifiers and references, never secret values on the command line
(a repo-root `.env` works: the wrapper script sources it):

| Env var | Needed when | Meaning |
|---|---|---|
| `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY` (/ `LLM_BASE_URL`) | computing a **new** draft | The model behind the four lenses + synthesizer. Provider-agnostic (`groq`, `openai`, `openai_compatible`, `ollama`, `anthropic`). Fetching an already-stored draft needs **no** LLM credentials. |
| `REVIEW_PANEL_DATABASE_URL` | you want durability | Postgres URL for the checkpointer + draft store. Unset ⇒ in-memory fallback (single-process only; logged plainly as `event=persistence_fallback`). |
| `GITHUB_TOKEN` | private repos / rate limits | Optional **read-only** token for the PR fetch. Never a write credential. |
| `REVIEW_PANEL_AGENTS_DIR` | non-repo checkouts | Path to the canonical `agents/` directory (the wrapper script sets it to the repo's `agents/`). |
| `REVIEW_PANEL_MCP_URL` / `REVIEW_PANEL_MCP_TOKEN` | optional KB context | When set, the panel makes one `kb_search` call during `load_pr` and shares the (fenced, untrusted) result with all four lenses. Unset = no KB access; KB failures are fail-soft. |
| `TRACE_SINK` | tuning tracing | `postgres` (default when `REVIEW_PANEL_DATABASE_URL` is set) or `none` — per-step spans, ADR-0032. See [08 — Observability](08-observability.md). |

> `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` are **inert** — LangChain's native instrumentation is
> surfaced only in the boot log and is not this platform's tracing story (ADR-0032 withdrew the
> LangSmith commitment before it ever shipped). Tracing is Postgres, via `TRACE_SINK`.

## Running it

The one-command path (sources the repo-root `.env`, points `REVIEW_PANEL_AGENTS_DIR` at `agents/`,
then executes the CLI):

```sh
./scripts/run_review_panel_local.sh RohitSh26/agentic-kb-platform 7
```

Or the underlying CLI directly:

```sh
cd services/review-panel
uv run review-panel draft RohitSh26/agentic-kb-platform 7
```

Behavior (this is the contract, not a convention):

- If a draft already exists for the PR's **current head SHA** → it prints the stored
  `review_draft_v1` JSON on stdout. **No model calls**, no LLM credentials needed.
- Otherwise → it computes the draft (LLM credentials required), stores it, and prints it.
- **stdout carries only the JSON document; all logs go to stderr** — so piping into `jq` is safe:

```sh
./scripts/run_review_panel_local.sh RohitSh26/agentic-kb-platform 7 \
  | jq -r '.summary_markdown'
```

Exit code `0` on success, `1` on failure. Pushing a new commit to the PR changes the head SHA,
which is a new draft key — the next run computes a fresh draft; the old one stays queryable.

## Where drafts live

The service owns exactly one Postgres schema, **`review_panel`**, created idempotently at startup
(`CREATE SCHEMA IF NOT EXISTS` — this is the repo's one documented Alembic exemption; the schema
holds only derived, recomputable state, and its rollback is `DROP SCHEMA review_panel CASCADE`).
It contains the LangGraph checkpointer's tables, `trace_span` (ADR-0032), and the draft store:

```sql
-- one row per (repo, PR, head SHA); draft_key = "<repo>#<pr_number>@<head_sha>"
SELECT draft_key, created_at
FROM review_panel.review_draft
ORDER BY created_at DESC;

-- pull one draft's editable summary out of the jsonb document
SELECT draft->>'summary_markdown'
FROM review_panel.review_draft
WHERE draft_key LIKE 'RohitSh26/agentic-kb-platform#7@%';
```

The `draft` column is a `review_draft_v1` document: `advisory_verdict`
(`approve | request_changes`), per-lens verdicts, reconciled `findings[]` (each with severity,
evidence ids, the lenses that raised it, any explicit disagreement, and a `suggested_comment` you
can edit and post), deduplicated `open_questions[]`, the synthesizer's own output, an editable
`summary_markdown` (always draft-labelled), and `provenance` (engine version, model, lenses). Full
shape: [`docs/contracts/review-panel.md`](../contracts/review-panel.md).

The panel **never touches the Knowledge Registry** (the public schema kb-builder owns) — it
imports neither SQLAlchemy models nor kb-builder code, and contract tests pin that boundary.

## Crash-resume

`draft_key` doubles as the LangGraph checkpoint `thread_id`, and drafts insert with
`ON CONFLICT DO NOTHING` (first writer wins). Two consequences you can rely on:

- A run killed after some reviewer nodes completed **resumes its checkpointed thread** on the next
  invocation — the finished lens LLM calls are *not* re-executed, and exactly one draft row lands.
  You never pay twice for the same head SHA.
- A racing second run on the same key simply reuses the stored row.

Crash-resume requires `REVIEW_PANEL_DATABASE_URL`; with the in-memory fallback a crash loses the
partial run (that is what the `event=persistence_fallback` log line is warning you about).

## From draft to a published review

The intended flow (ADR-0031): the panel computes, **you** publish.

1. **Get the draft** — run the CLI (above) and read the JSON, or have your in-session agent do it:
   in Copilot/Claude chat, ask the `code_reviewer` agent to fetch and summarize the draft for a PR
   (in v1 the CLI is the fetch path; a broker-served MCP fetch tool is the recorded PR-41
   candidate).
2. **Edit it** — `summary_markdown` and each finding's `suggested_comment` are written to be
   edited. The verdict is advisory; drop or reword anything you disagree with.
3. **Publish under your own auth** — from your own session, e.g.:

   ```sh
   gh pr review 7 --comment --body-file review.md     # or --approve / --request-changes
   ```

   The credential used is yours (`gh auth`), never the service's — the panel has nothing it
   *could* post with.

## Per-step visibility

Every draft run emits trace spans to `review_panel.trace_span` (when Postgres is configured): one
root span per attempt (`review_panel.draft_run`) plus one span per node that actually executed
(`load_pr`, `review_bug`, `review_security`, `review_quality`, `review_test_coverage`,
`reconcile`, `store_draft`). `trace_id` is the draft key, so a crash + resume correlates under one
trace. Spans carry aggregate metadata only — never PR text or diffs. Queries and the full span
shape: [08 — Observability](08-observability.md) and
[`docs/contracts/tracing.md`](../contracts/tracing.md).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `event=persistence_fallback` in the logs | `REVIEW_PANEL_DATABASE_URL` is unset — the run works but nothing survives the process. Set it for durable drafts and crash-resume. |
| Run fails asking for LLM credentials | No stored draft exists for this head SHA, so the panel must compute one — set `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY` (repo-root `.env` is the easy place). |
| GitHub fetch 404s on a private repo | Set `GITHUB_TOKEN` (read scope). Public repos need no token. |
| A lens fails with a schema-validation error twice | The model's output failed `review_findings_v1` validation, was retried **once** with the verbatim validator error fed back, and failed again — the run fails rather than storing unvalidated output. Try a stronger `LLM_MODEL` and re-run (the completed lenses resume from the checkpoint). |
| Re-run returns instantly with an "old" draft | Same head SHA ⇒ stored draft, by design. Push a commit (new head SHA) or query `review_panel.review_draft` if you want to inspect what is stored. |
| You expected it to comment on the PR | It never does — that is the dev gate (ADR-0031). Publish the edited draft yourself (§"From draft to a published review"). |
