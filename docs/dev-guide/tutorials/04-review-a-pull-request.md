# Tutorial 4 — Review a pull request

The platform ships a review draft engine: four specialist reviewer lenses — **bug**,
**security**, **quality**, and **test coverage** — fan out over a pull request in parallel,
their findings are reconciled deterministically (duplicates merged, disagreements kept
explicit, ranked by severity), and one draft is stored. The engine **never posts to GitHub**.
You pull the draft, edit it, and publish it under your own name — that design is explained in
[the review flow](../explanation/the-review-flow.md).

## 1. Set up credentials

Computing a new draft calls a chat model, so put LLM credentials in the repo-root `.env`
(gitignored — never commit it), plus a Postgres URL so drafts survive the process:

```sh
LLM_PROVIDER=groq
LLM_API_KEY=gsk_...
LLM_MODEL=llama-3.1-8b-instant
REVIEW_PANEL_DATABASE_URL=postgresql+asyncpg://$USER@localhost:5432/agentic_kb
```

Other providers: [switch LLM providers](../how-to/switch-llm-providers.md). Private repo? Add a
read-only `GITHUB_TOKEN` — the engine can only ever *fetch* PR data with it.

## 2. Run the engine

```sh
./scripts/run_review_panel_local.sh <owner>/<repo> <pr-number>
# e.g. ./scripts/run_review_panel_local.sh RohitSh26/agentic-kb-platform 7
```

(The script sources `.env` and runs the CLI; the direct form is
`cd services/review-panel && uv run review-panel draft <owner>/<repo> <pr-number>`.)

The behavior is a contract, not a convention:

- A draft already stored for the PR's **current head SHA** → printed instantly, **zero model
  calls**, no LLM credentials needed.
- No stored draft → the four lenses run, the draft is computed, stored, and printed.
- **stdout carries only the JSON document; all logs go to stderr** — piping into `jq` is safe:

```sh
./scripts/run_review_panel_local.sh <owner>/<repo> <pr-number> | jq -r '.summary_markdown'
```

## 3. Inspect the draft

**You should see** a `review_draft_v1` document (a real stored draft, trimmed):

```json
{
  "schema_version": "1.0.0",
  "advisory_verdict": "request_changes",
  "lens_verdicts": {"bug": "request_changes", "security": "request_changes",
                    "quality": "request_changes", "test_coverage": "request_changes"},
  "findings": [
    {
      "lenses": ["security"],
      "finding": "SQL injection in search query building",
      "severity": "blocker",
      "disagreement": null,
      "evidence_ids": ["src/search.py:10"],
      "suggested_comment": "**[blocker]** SQL injection in search query building\nEvidence: `src/search.py:10` (lenses: security)"
    },
    {
      "lenses": ["bug", "security"],
      "finding": "Race condition in cache write path allows a double write",
      "severity": "major",
      "disagreement": "severity disputed (bug=major, security=minor); highest kept",
      "evidence_ids": ["src/cache.py:40", "src/cache.py:42"],
      "suggested_comment": "**[major]** Race condition in cache write path allows a double write\n..."
    }
  ],
  "open_questions": ["Is the cache writer covered by an integration test elsewhere?"],
  "summary_markdown": "## Review-panel draft — acme/platform#7 @ `e90fc31461a1`\n\n**Advisory verdict (draft, not published): `request_changes`**\n...",
  "provenance": {"engine": "review-panel", "engine_version": "0.1.0",
                 "lenses": ["bug", "security", "quality", "test_coverage"], "...": "..."}
}
```

Read it like a reviewer would: `findings[]` carries each issue with its severity, the lenses
that raised it, evidence pointers, and a `suggested_comment` written to be edited. Disagreements
between lenses are kept **explicit** — here, bug and security disputed a severity and the draft
says so. `advisory_verdict` is advice, not a decision.

## 4. Or fetch it from chat

On hosts without a shell (VS Code Copilot chat), the agent fetches the same draft through the
`get_review_draft` tool — read-only, compute-never, no budget charge:

```json
{"repo": "acme/platform", "pr_number": 7}
```

**You should see** (real responses; `head_sha` is optional and defaults to the newest draft):

```json
{"schema_version": "1.12.0", "found": true,
 "draft": {"draft_key": "acme/platform#7@e90fc3146…", "repo": "acme/platform", "pr_number": 7,
           "head_sha": "e90fc3146…", "draft": { "…the review_draft_v1 document…": "…" }}}
```

And when nothing is stored yet — a clean miss, never an error:

```json
{"schema_version": "1.12.0", "found": false, "draft": null}
```

## 5. Revise it

Edit `summary_markdown` and any finding's `suggested_comment`; drop or reword anything you
disagree with. The verdict is advisory — you own what gets published.

## 6. Publish under your own auth

```sh
gh pr review 7 --comment --body-file review.md     # or --approve / --request-changes
```

The credential used is **yours** (`gh auth`) — the engine holds no GitHub write credential, so
it has nothing it *could* post with.

## Where drafts live, and what survives a crash

Drafts land in the `review_panel.review_draft` table, keyed
`<repo>#<pr_number>@<head_sha>` — at most one row per key. Pushing a new commit changes the head
SHA, which is a new key: the next run computes a fresh draft and the old one stays queryable.
A run killed midway resumes its checkpointed thread on the next invocation — completed lens
calls are never re-executed, and the draft insert is `ON CONFLICT DO NOTHING`, so you never pay
twice for the same head SHA. Queries for the draft store and its trace spans:
[query traces and the ledger](../how-to/query-traces-and-the-ledger.md).

## You have completed the journey

You have built and explored a knowledge base, put it in an agent's hands, scoped a change, and
drafted a review. From here:

- How-to guides — one recipe per task, when you know what you want: start with
  [connect VS Code](../how-to/connect-vscode.md) or [troubleshoot](../how-to/troubleshoot.md).
- [Reference](../reference/tools.md) — the complete, dry facts: tools, variables, CLI, database.
- [Explanation](../explanation/how-your-knowledge-base-is-built.md) — how and why it all works.
