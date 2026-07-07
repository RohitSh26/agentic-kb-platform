# Connect VS Code (GitHub Copilot agent mode)

**Goal:** ask questions in Copilot Chat and have the answers come from your knowledge base,
through the broker's budgeted, audited tools.

## Prerequisites

- VS Code 1.99 or later, with the GitHub Copilot and Copilot Chat extensions, signed in to a
  Copilot-enabled account.
- The broker running with an active knowledge base ([getting started](../getting-started.md)).
  Check it:

  ```sh
  curl -s http://127.0.0.1:8765/health
  ```

  You should see `{"status":"ok", ...}`.

## Steps

1. **Open the project in VS Code** — `code .` from the repo root. The connection config ships in
   the repo: `.vscode/mcp.json` points at `http://127.0.0.1:8765/mcp/` with a placeholder bearer
   token. Against a local broker in local-dev mode, any non-empty token works, so the file is
   ready as-is.

2. **Start the MCP connection** — open `.vscode/mcp.json` and click the **Start** code lens above
   the `"context-broker"` block (or `Cmd-Shift-P` → **MCP: List Servers** → start
   `context-broker`). The status turns to **Running**.

3. **Switch Copilot Chat to Agent mode** — open Copilot Chat and change the mode dropdown at the
   bottom of the chat box from *Ask* to **Agent**.

4. **Enable the tools** — click the tools icon in the chat box and enable `kb_search` under
   `context-broker`. Enable `get_task_context` too; it is the other tool that matters day to day.

5. **Ask.** Naming the tool in your first prompt nudges Copilot to use it:

   > *"Using the context-broker kb_search tool, how does the build decide it can skip calling the
   > LLM for an unchanged document? Name the sources you used."*

   VS Code asks you to **allow** the tool run the first time — "always allow" is fine locally.

## Verify

Copilot sends a deliberately tiny request — one field:

```json
{"query": "how does the build decide to skip the LLM for unchanged documents?"}
```

The response is a handful of ranked, permission-filtered hits plus your remaining budget:

```json
{
  "results": [
    {
      "title": "BuildRunner",
      "artifact_type": "code_symbol",
      "source_uri": "file:///.../services/kb-builder/src/agentic_kb_builder/application/build_runner.py",
      "snippet": "class BuildRunner: def __init__( self, session: AsyncSession, *, kb_version: str, ...",
      "confidence_tier": "interpreted"
    }
  ],
  "budget_remaining": {"calls": 49, "tokens": 49391},
  "notice": null
}
```

The answer names real functions and files because it is reading your knowledge base, not
guessing. Every call also lands in the retrieval ledger — proof it was governed:

```sh
psql agentic_kb -c "select tool_name, status, tokens_returned, created_at
                    from retrieval_event order by created_at desc limit 5;"
```

For a change-shaped task, ask instead: *"Call get_task_context with the task 'add input
validation to the GitHub connector' and summarize what comes back."* The response fields are
explained in [the tools reference](../reference/tools.md) and walked through in
[tutorial 3](../tutorials/03-scope-a-real-task.md).

**Recommended model.** Pick a strong model in the chat's model picker — Claude (Haiku/Sonnet) or
GPT-5. A small model tends to ignore tool guidance and over-plan simple questions. The broker
governs *what* the agent can retrieve; the model decides *how well* it uses it.

**Remote broker?** Against anything that is not a loopback local-dev broker, the placeholder
bearer is rejected — you need a real Entra token. See
[Broker bearer tokens](../reference/environment-variables.md).

Something not behaving as described: [troubleshooting](troubleshoot.md).
