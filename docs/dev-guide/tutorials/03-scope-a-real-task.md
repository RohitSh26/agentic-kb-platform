# Tutorial 3 — Scope a real task

`kb_search` answers point questions. Before *changing* code, the better opening move is
`get_task_context`: one call that resolves what a task touches, its blast radius, the
conventions that apply, and similar prior changes — in one budgeted response, with zero LLM
calls at query time. This tutorial walks one real task through it, field by field.

The task: **"add input validation to the GitHub connector."**

## 1. Make the call

From Copilot chat (agent mode, as set up in [tutorial 2](02-ask-your-first-questions.md)):

> Call get_task_context with the task "add input validation to the GitHub connector" and
> summarize what comes back.

Or directly, so this tutorial stands alone — from the repo root, broker running:

```sh
uv run --project services/mcp-server python - <<'EOF'
import asyncio
from fastmcp import Client

async def main() -> None:
    async with Client("http://127.0.0.1:8765/mcp/", auth="local-dev-token") as client:
        response = await client.call_tool("get_task_context", {"request": {
            "task_description": "add input validation to the GitHub connector",
        }})
        ctx = response.data
        for entity in ctx.resolved_scope.entities:
            print(entity.path, "::", entity.symbol,
                  f"({entity.resolution_source}, {entity.confidence_tier})")
        print("budget used:", ctx.budget_used.tokens, "tokens in", ctx.budget_used.calls, "calls")

asyncio.run(main())
EOF
```

The request is a task description plus optional `hints` (files or symbols you already know are
involved). That is the whole request.

## 2. Read the response, field by field

What follows is the real response for this task, section by section, in the order the fields
appear.

**`resolved_scope.entities`** — the files and symbols the task is about:

```json
"resolved_scope": {
  "entities": [
    {"entity_id": "a8148875-…", "path": "…/services/kb-builder/src/agentic_kb_builder/connectors/github_code.py",
     "symbol": "GitHubCodeConnector", "resolution_source": "search", "confidence_tier": "interpreted"},
    {"entity_id": "e81b5b20-…", "path": "…/connectors/github_doc.py",
     "symbol": "GitHubDocConnector", "resolution_source": "search", "confidence_tier": "interpreted"},
    {"entity_id": "63535eba-…", "path": "…/connectors/github_doc.py",
     "symbol": null, "resolution_source": "search", "confidence_tier": "interpreted"},
    {"entity_id": "d35d7aa7-…", "path": "…/connectors/github_rest.py",
     "symbol": null, "resolution_source": "search", "confidence_tier": "interpreted"},
    {"entity_id": "f715227e-…", "path": "…/connectors/github_code.py",
     "symbol": null, "resolution_source": "search", "confidence_tier": "interpreted"}
  ],
  "ambiguous_candidates": []
}
```

Five entities: both GitHub connector classes, their files, and the shared REST helper. Each is
stamped with **how it was resolved** — `alias_index` (an exact alias match), `hint` (you named
it), or `search` — and a `confidence_tier`. `ambiguous_candidates` is empty because this task
resolved cleanly; when scope is genuinely ambiguous, the tool names the candidates there instead
of guessing. **An ambiguous answer is an answer, never a silent guess.**

**`referenced_paths`** — a small table of path strings. Every long path in the rest of the
response appears here exactly once; other fields point into it by index (`path_ref`), so the
same path never costs tokens twice:

```json
"referenced_paths": ["…/services/kb-builder/src/agentic_kb_builder/structured_logging.py"]
```

**`blast_radius`** — what calls, is called by, and tests the scoped code, walked from the real
`calls`/`imports`/`tests` graph:

```json
"blast_radius": {
  "callers": [],
  "callees": [
    {"entity_id": "3aaf7be5-…", "path_ref": 0, "symbol": null,
     "edge_type": "imports", "confidence_tier": "deterministic", "caveat": null}
  ],
  "tests": []
}
```

The one callee — `path_ref: 0`, i.e. `structured_logging.py` — is an `imports` edge with tier
`deterministic`. The honesty rule is structural: a `calls` edge is `deterministic` **only** when
the import graph corroborates it; otherwise it comes back `interpreted` with a `caveat` naming
the missing corroboration. You always know which parts of the map you can lean on.

**`conventions`** — the rules and decision records that apply to the scoped directories, each
with evidence ids. Empty here; conventions surface when rule and decision documents covering
those paths are in your knowledge base.

**`similar_prior_changes`** — commits that changed this area before, with evidence ids. Also
empty for this task in this build.

**`evidence_ids`, `open_questions`, `budget_used`** — the closing accounting:

```json
"evidence_ids": ["3aaf7be5-…", "63535eba-…", "a8148875-…", "d35d7aa7-…", "e81b5b20-…", "f715227e-…"],
"open_questions": [],
"budget_used": {"tokens": 574, "calls": 6}
```

Every item in the response is backed by an evidence id. This whole task map cost **574 tokens**
— the response is capped server-side at the Evidence-Pack band (~8k tokens) and trimmed
deterministically from the lowest-value tail if it ever exceeds that. `get_task_context` carries
its own budget, separate from `kb_search`'s, so the two never compete.

## 3. See what it cost, step by step

The call is ledgered with a per-node latency breakdown — the backend is a parallel fan-out of
pure-retrieval nodes, no LLM:

```sh
psql agentic_kb -x -c "select details from retrieval_event
                       where tool_name='get_task_context' and status='approved'
                       order by created_at desc limit 1;"
```

**You should see:**

```
-[ RECORD 1 ]----------------------------------------------------------------------------------
details | {"tests": 0, "callees": 1, "callers": 0, "retried": false, "tracing": false,
          "entities": 5, "calls_used": 6, "conventions": 0, "open_questions": 0,
          "node_latency_ms": {"synthesize": 0, "conventions": 624, "blast_radius": 699,
          "resolve_scope": 699, "similar_prior_changes": 704}, "confidence_floor": "interpreted",
          "ambiguous_candidates": 0, "similar_prior_changes": 0}
```

The four retrieval nodes ran in parallel — the whole call finished in under a second.

## 4. What to read after

The response *pins* the files that matter; it does not replace reading them. From here:

- Open the pinned paths (`github_code.py`, `github_doc.py`, `github_rest.py`) with your editor
  or the agent's native file tools — exact current code for an edit always comes from the files.
- Send point questions that come up while working ("what logs a validation failure?") back to
  `kb_search`.
- Do not re-read what the knowledge base already supplied — that is the token discipline the
  budgets enforce (see [governance and budgets](../explanation/governance-and-budgets.md)).

## Next

[Tutorial 4 — Review a pull request](04-review-a-pull-request.md): the four-lens review draft
engine, and why it can never post to GitHub.
