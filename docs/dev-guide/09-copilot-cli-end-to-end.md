# 09 — GitHub Copilot CLI against the broker

> Drive a **real external agent — the GitHub Copilot CLI, using its own model** — against the MCP
> Context Broker, through the repo's **committed, policy-carrying MCP configuration**. This is the
> non-IDE variant of the VS Code flow (dev-guide [00](00-getting-started.md) Parts 6–8): a
> third-party agent asks your KB questions via the budgeted `kb_search` tool (this guide's worked
> example) or the one-call `get_task_context` tool, and every call — including the ones the budget
> refused — lands in the retrieval ledger.

## The one design point to understand first

The framework ships its Copilot MCP configuration at
**`.copilot/mcp/repository-settings.json`**, and that file deliberately exposes **exactly the
tools the twelve-role canon grants** — today, two:

```json
{
  "mcpServers": {
    "context-broker": {
      "type": "http",
      "url": "https://<your-broker-host>/mcp/",
      "tools": [
        "get_task_context",
        "kb_search"
      ],
      "headers": {
        "Authorization": "Bearer $COPILOT_MCP_CONTEXT_BROKER_TOKEN"
      }
    }
  }
}
```

That allowlist is the point (ADR-0025/ADR-0030): a Copilot host gets the **budgeted,
ACL-filtered `kb_search`** and the **one-call, separately-budgeted `get_task_context`**
(ADR-0030's task-scoped resolved-scope/blast-radius/conventions tool) and keeps its own native
file tools — it does not get the broker's other ten tools (the governed `context.*` evidence-pack
flow). The budget and ACL are enforced **server-side per authenticated identity** either way
(deleting this file cannot widen anything), but the allowlist keeps the host's tool surface
matching the framework's design.

**Do not wire the broker with an ad-hoc `copilot mcp add` instead.** That creates a server entry
*without* the `tools` allowlist, so the CLI would see and offer the broker's entire tool surface —
bypassing the committed allowlist policy. Use the committed config, adapted only in URL and
token, for both deployment shapes:

| Deployment | Where the config goes |
|---|---|
| Copilot cloud coding agent / org rollout | Repository settings → Copilot → MCP servers: paste the file's contents; create the Copilot environment value `COPILOT_MCP_CONTEXT_BROKER_TOKEN` (Copilot only exposes values whose names start with `COPILOT_MCP_`) |
| Copilot **CLI** on your machine (this guide) | Merge the same `mcpServers` block into `~/.copilot/mcp-config.json` (§3 below) |

`.copilot/README.md` documents both shapes plus the agent renderings that go with them.

## 1. Prerequisites

- A **built KB and the broker running locally** — the quickstart
  ([00-quickstart.md](00-quickstart.md)) gets you there in one command; you need `/health` → `ok`
  on `http://127.0.0.1:8765`.
- A **GitHub account with a Copilot license**, logged in via `gh` (`gh auth status`).
- **Node.js + npm** for the CLI itself.

## 2. Install and authenticate the CLI

```sh
npm install -g @github/copilot
copilot --version
```

The CLI accepts a token from `COPILOT_GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_TOKEN`, and the `gh`
OAuth token is a supported type (classic `ghp_` PATs are not) — so a Copilot-licensed `gh` login
authenticates non-interactively:

```sh
export GH_TOKEN="$(gh auth token)"
copilot -p "Reply with exactly: AUTH_OK" --allow-all-tools   # prints AUTH_OK
```

(Interactive alternative: `copilot login` — browser device flow.)

## 3. Point the CLI at the broker — via the committed config

Take the committed server block, change only the URL (your local broker) — the token header stays
a **reference by name**, never a pasted value:

```sh
mkdir -p ~/.copilot
cat > ~/.copilot/mcp-config.json <<'EOF'
{
  "mcpServers": {
    "context-broker": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp/",
      "tools": [
        "get_task_context",
        "kb_search"
      ],
      "headers": {
        "Authorization": "Bearer local-dev-token"
      }
    }
  }
}
EOF
copilot mcp list        # context-broker (http)
```

> If you already have other servers in `~/.copilot/mcp-config.json`, merge the `context-broker`
> entry into your existing `mcpServers` object instead of overwriting the file.

Against the local broker in local-dev auth mode (ADR-0016, loopback-only), any non-empty bearer
authorizes as the `local-dev` subject — which is why a literal `local-dev-token` placeholder is
fine here. Against a **production** broker you keep the committed file's
`$COPILOT_MCP_CONTEXT_BROKER_TOKEN` reference and supply a real Entra token through the Copilot
environment — a token value never lands in a config file either way.

## 4. Ask a real question

```sh
copilot -p 'Using the context-broker kb_search tool, answer: how does the KB build
decide it can skip calling the LLM for an unchanged document? Search the KB before
reading any file, and cite the source_uri of what you used.' --allow-all-tools
```

**What happens, step by step:**

1. Copilot's model sees exactly two broker tools — `kb_search` and `get_task_context` — plus its
   own native tools. This example drives `kb_search`; the callout below this walkthrough shows
   `get_task_context` for a change-shaped task.
2. It calls `kb_search` with `{"query": ...}` (that is the whole request; identity and budget bind
   to the authenticated session, not to anything the model sends).
3. The broker runs the standard retrieval path — ACL filter, semantic dedupe, temporal +
   centrality re-weighting, 3–5 ranked hits — and answers with `results` (title, artifact type,
   `source_uri`, snippet, confidence tier) plus `budget_remaining` (`{calls, tokens}`).
4. Copilot answers from the snippets, typically citing `source_uri`s like
   `services/kb-builder/src/agentic_kb_builder/application/cache_gates.py`.

If the model keeps searching, the **dual budget cap** (call count AND cumulative tokens per
session, enforced in the tool — not the prompt) eventually closes. The tool then returns empty
results with the notice *"KB budget spent — work with what you have, or read the specific files
you still need."* — a contractual outcome, never a crash, so the agent keeps working with its
file tools.

**For a change-shaped task, use `get_task_context` instead** — it is a separate, one-call tool
with its own server-side budget (the Evidence-Pack band), so it never competes with the
`kb_search` cap above:

```sh
copilot -p 'Using the context-broker get_task_context tool, answer: what is the resolved scope,
blast radius, and applicable conventions for the task "add input validation to the GitHub
connector"?' --allow-all-tools
```

One request (`{"task_description": "…", "hints": {...}}` — hints optional) returns the resolved
scope, blast radius (callers/callees/tests), conventions, and similar prior changes in a single
budgeted response, every item citing an evidence id — see
[00 — Getting Started](00-getting-started.md) Part 7 for the full response shape.

## 5. What the ledger records

Every `kb_search` call — answered or refused — writes one `retrieval_event` row. `kb_search`
carries no run handle, so its rows use the non-run sentinel `run_id = "-"` and record the session
in `details`; inspect them with SQL (the run-scoped `replay` CLI and `ledger.list_retrievals` are
for run-scoped tools):

```sh
psql agentic_kb -c "
  SELECT status, tokens_returned, details, created_at
  FROM retrieval_event
  WHERE tool_name = 'kb_search'
  ORDER BY created_at DESC LIMIT 10;"
```

You should see rows like:

```
 status   | tokens_returned | details
----------+-----------------+------------------------------------------------------------------
 approved |             412 | {"session": "…", "calls_used": 1, "tokens_used": 412,
          |                 |  "max_requests": 50, "max_tokens": 50000}
 denied   |               0 | {"session": "…", "calls_used": 50, …}
```

- `approved` rows carry the tokens charged and the returned artifact ids — the audit of exactly
  what knowledge a third-party agent received.
- `denied` rows are the budget doing its job (the dual cap closed); raise the cap for your local
  subject via `MCP_AGENT_ALLOWANCES` on the broker if you want a longer session.
- Answered-but-thin `kb_search` results also feed the dashboard's **KB-gap proxy**
  (`v_retrieval_health.kb_search_zero_thin_rate` — see
  [08 — Observability](08-observability.md)): the signal that agents asked for knowledge the KB
  doesn't hold yet.

That triplet — server-enforced budget, ACL-filtered results, a complete ledger — is the product
thesis demonstrated on an agent we don't control.

## Scope note

The Copilot CLI here runs **one** agent (Copilot's model) with the two governed retrieval tools
(`kb_search`, `get_task_context`). Two other runtimes exist for comparison: the VS Code + Copilot
flow
([00](00-getting-started.md) Parts 6–8, same broker, full tool surface) and the terminal
multi-agent runner (`scripts/agent_runner.py`, [00](00-getting-started.md) Part 10), which drives
the **governed `context.*` lanes** — evidence packs, human-approval gates, verification receipts —
for when citation-grade provenance is the goal.

## Known behaviors when opening THIS repo (verified on Copilot CLI 1.0.63, 2026-07-06)

Two artifacts of this repository being *built with* Claude Code leak into a Copilot CLI session
opened here — neither exists in your own team repos, so pilot developers won't see them:

- **`.mcp.json` is merged into the workspace MCP config.** That file configures Claude Code's
  *build-plane* tooling for developing this platform, and Copilot CLI 1.0.63 reads it as workspace
  MCP servers — and ignores `"disabled": true` entries. Don't enable those servers in a product
  session; the broker config in §3 is the product surface.
- **`.claude/agents/*` appear as invocable agents.** Those are Claude Code *build* subagents
  (reviewers, migration writers) for working on this platform's code — not the product roster,
  which lives in `.copilot/agents/`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `copilot` auth fails | Your `gh` account lacks a Copilot license, or use `copilot login` (device flow). Classic `ghp_` PATs aren't accepted for the CLI token env vars. |
| Copilot can't reach `context-broker` | The broker isn't running — start it (quickstart "Connect a host", or dev-guide 00 Part 5); confirm `curl http://127.0.0.1:8765/health` → `ok`. |
| Copilot answers without calling `kb_search` | Say so in the prompt ("search the KB before reading any file"), and confirm the server shows up in `copilot mcp list`. |
| More tools than `kb_search`/`get_task_context` show up | The server entry is missing its `tools` allowlist — you added it ad-hoc. Replace it with the committed block (§3). |
| Every call comes back `denied` | The session budget is spent. New session, or raise `MCP_AGENT_ALLOWANCES` for your subject on the broker and restart it. |
| `401` on tool calls | The broker isn't in local-dev mode (or isn't on loopback), so the placeholder bearer is rejected — restart it as in dev-guide 00 Part 5, or supply a real Entra token. |
