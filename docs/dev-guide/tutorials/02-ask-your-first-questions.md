# Tutorial 2 — Ask your first questions

In [tutorial 1](01-explore-what-got-built.md) you looked at the knowledge base directly. Now you
give an AI agent access to it — through the broker's budgeted, audited tools — and watch the
ledger record every call. The host here is **VS Code + GitHub Copilot in agent mode**, the
default choice; the [Copilot CLI](../how-to/connect-copilot-cli.md) and
[OpenCode](../how-to/connect-opencode.md) recipes connect the same way.

Prerequisites: the broker running with an active knowledge base
([getting started](../getting-started.md) steps 3–4), VS Code 1.99+ with the GitHub Copilot and
Copilot Chat extensions, signed in to a Copilot-enabled account.

## 1. Open the project

```sh
code .
```

The connection config ships in the repo — `.vscode/mcp.json` already points at your local
broker:

```json
{
  "servers": {
    "context-broker": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp/",
      "headers": {
        "Authorization": "Bearer local-dev-token"
      }
    }
  }
}
```

The placeholder bearer is fine as-is: against a local broker in local-dev mode, any non-empty
token authorizes as the `local-dev` identity, and budgets and permissions are enforced
server-side regardless.

## 2. Start the connection

Open `.vscode/mcp.json` and click the **Start** code lens above the `"context-broker"` block
(or `Cmd-Shift-P` → **MCP: List Servers** → start `context-broker`).

**You should see** the server status turn to **Running**.

## 3. Switch to agent mode and enable the tools

Open Copilot Chat and switch the mode dropdown at the bottom of the chat box from *Ask* to
**Agent**. Click the tools icon in the chat box and enable `kb_search` and `get_task_context`
under `context-broker`.

**You should see** both tools listed and checked under the `context-broker` server.

## 4. Ask a real question

> Using the context-broker kb_search tool, how does the build decide it can skip calling the
> LLM for an unchanged document? Name the sources you used.

Copilot calls `kb_search` (VS Code asks you to **allow** the tool run — "always allow" is fine
locally). The entire request is one field; identity and budget bind to your authenticated
session, never to anything the model sends:

```json
{"query": "how does the build decide to skip the LLM for unchanged documents?"}
```

**You should see** the tool return ranked, permission-filtered hits plus your remaining budget
(trimmed to three of the five results):

```json
{
  "schema_version": "1.12.0",
  "results": [
    {
      "title": "default_collaborators()",
      "artifact_type": "code_symbol",
      "source_uri": "file:///Users/edhaa/Development/agentic-kb-platform/services/kb-builder/src/agentic_kb_builder/build.py",
      "snippet": "def default_collaborators(session: AsyncSession, *, index_path: Path) -> Collaborators: ...",
      "confidence_tier": "interpreted"
    },
    {
      "title": "BuildRunner",
      "artifact_type": "code_symbol",
      "source_uri": "file:///Users/edhaa/Development/agentic-kb-platform/services/kb-builder/src/agentic_kb_builder/application/build_runner.py",
      "snippet": "class BuildRunner: def __init__( self, session: AsyncSession, *, kb_version: str, ...",
      "confidence_tier": "interpreted"
    },
    {
      "title": "services/kb-builder/src/agentic_kb_builder/application/build_runner.py",
      "artifact_type": "code_file",
      "source_uri": "file:///Users/edhaa/Development/agentic-kb-platform/services/kb-builder/src/agentic_kb_builder/application/build_runner.py",
      "snippet": "Incremental build runner — the 8-step algorithm from docs/architecture §7. Unchanged content_hash => docify/graphify/embed/index are all skipped. ...",
      "confidence_tier": "interpreted"
    }
  ],
  "budget_remaining": {"calls": 49, "tokens": 49391},
  "notice": null
}
```

`source_uri` values are `file://` URIs into your own checkout. Each hit carries a
`confidence_tier` — retrieved text is treated as content to check, not truth. Copilot's answer
names real functions and files because it is reading your knowledge base, not guessing.

> **Pick a strong model** in the chat's model picker. A small model tends to ignore tool
> guidance and answer from its priors. The broker governs *what* the agent can retrieve; the
> model decides *how well* it uses it.

## 5. Watch the ledger record it

Every tool call — including ones the budget refuses — writes one row to the retrieval ledger.
In a terminal:

```sh
psql agentic_kb -c "select tool_name, status, tokens_returned, created_at
                    from retrieval_event order by created_at desc limit 5;"
```

**You should see** one row per call from your session:

```
    tool_name     |  status  | tokens_returned |          created_at
------------------+----------+-----------------+-------------------------------
 kb_search        | approved |             550 | 2026-07-07 16:35:08.104717-05
 kb_search        | approved |             609 | 2026-07-07 16:30:27.80531-05
(2 rows)
```

Each `kb_search` row's `details` column records the budget window after the call:

```sh
psql agentic_kb -x -c "select tool_name, status, tokens_returned, details
                       from retrieval_event where tool_name='kb_search'
                       order by created_at desc limit 1;"
```

```
-[ RECORD 1 ]---+------------------------------------------------------------------------------------------------------------------------------
tool_name       | kb_search
status          | approved
tokens_returned | 550
details         | {"session": "5af289b9e74d43ee99ab507d81758862", "calls_used": 1, "max_tokens": 50000, "tokens_used": 550, "max_requests": 50}
```

`approved` means evidence was returned and charged. `denied` means a budget said no — a
contractual outcome, not an error. `kb_search` rows are session-scoped, not run-scoped: they use
the `run_id = "-"` sentinel and record the session in `details`, so SQL is the way to see them.
This is the proof the call was governed: an external agent reached your knowledge base only
through the budgeted, permission-filtered, fully-ledgered door.

## What happens when the budget runs out

Each response's `budget_remaining` counts down. When the per-session cap closes — calls or
tokens, whichever first — the tool returns empty results with exactly this notice, and never an
error:

> KB budget spent — work with what you have, or read the specific files you still need.

The agent keeps its native file tools and finishes from them. A fresh chat window is a fresh
session; to raise the cap itself, see [tune budgets](../how-to/tune-budgets.md).

## Next

Point questions answered. For change-shaped work there is a better opening move:
[Tutorial 3 — Scope a real task](03-scope-a-real-task.md).
