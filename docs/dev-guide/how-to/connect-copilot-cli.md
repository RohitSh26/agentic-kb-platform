# Connect the GitHub Copilot CLI

**Goal:** drive a real external agent from your terminal, with the broker's tools available to it
under the committed allowlist policy.

## Prerequisites

- A GitHub account with a Copilot license, logged in via `gh`.
- Node.js + npm.
- The broker running with an active knowledge base ([getting started](../getting-started.md)).

## The one design point to understand first

The platform ships its Copilot MCP configuration at **`.copilot/mcp/repository-settings.json`**,
and that file deliberately exposes **exactly the tools the role canon grants** — today, three:

```json
{
  "mcpServers": {
    "context-broker": {
      "type": "http",
      "url": "https://<your-broker-host>/mcp/",
      "tools": [
        "get_review_draft",
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

That allowlist is the point: a Copilot host gets the budgeted, ACL-filtered `kb_search`, the
one-call `get_task_context`, and the read-only `get_review_draft` — and keeps its own native file
tools. It does not see the broker's other ten tools (the governed evidence-pack flow). Budgets and
ACLs are enforced **server-side per authenticated identity** either way — deleting this file
cannot widen anything — but the allowlist keeps the host's tool surface matching the design.

**Do not wire the broker with an ad-hoc `copilot mcp add`.** That creates a server entry *without*
the `tools` allowlist, so the CLI would see and offer the broker's entire tool surface. Use the
committed config, adapted only in URL and token, for both deployment shapes:

| Deployment | Where the config goes |
|---|---|
| Copilot cloud coding agent / org rollout | Repository settings → Copilot → MCP servers: paste the file's contents; create the Copilot environment value `COPILOT_MCP_CONTEXT_BROKER_TOKEN` (Copilot only exposes values whose names start with `COPILOT_MCP_`) |
| Copilot **CLI** on your machine (this page) | Merge the same `mcpServers` block into `~/.copilot/mcp-config.json` (below) |

`.copilot/README.md` documents both shapes plus the agent renderings that go with them.

## Steps

1. **Install and verify:**

   ```sh
   npm install -g @github/copilot
   copilot --version
   ```

2. **Authenticate.** The CLI accepts a token from `COPILOT_GITHUB_TOKEN` / `GH_TOKEN` /
   `GITHUB_TOKEN`, and the `gh` OAuth token is a supported type (classic `ghp_` PATs are not):

   ```sh
   export GH_TOKEN="$(gh auth token)"
   copilot -p "Reply with exactly: AUTH_OK" --allow-all-tools   # prints AUTH_OK
   ```

   (Interactive alternative: `copilot login` — browser device flow.)

3. **Point the CLI at the broker — via the committed config.** Take the committed server block and
   change only the URL. The token header stays a reference by name, never a pasted value:

   ```sh
   mkdir -p ~/.copilot
   cat > ~/.copilot/mcp-config.json <<'EOF'
   {
     "mcpServers": {
       "context-broker": {
         "type": "http",
         "url": "http://127.0.0.1:8765/mcp/",
         "tools": [
           "get_review_draft",
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

   If you already have other servers in `~/.copilot/mcp-config.json`, merge the `context-broker`
   entry into your existing `mcpServers` object instead of overwriting.

   Against the local broker in local-dev auth mode (loopback only), any non-empty bearer
   authorizes as the `local-dev` subject — which is why a literal `local-dev-token` placeholder is
   fine here. Against a production broker you keep the committed file's
   `$COPILOT_MCP_CONTEXT_BROKER_TOKEN` reference and supply a real Entra token through the Copilot
   environment — a token value never lands in a config file either way.

4. **Ask:**

   ```sh
   copilot -p 'Using the context-broker kb_search tool, answer: how does the KB build
   decide it can skip calling the LLM for an unchanged document? Search the KB before
   reading any file, and cite the source_uri of what you used.' --allow-all-tools
   ```

What happens, step by step:

1. Copilot's model sees exactly three broker tools plus its own native tools.
2. It calls `kb_search` with `{"query": ...}` — that is the whole request; identity and budget
   bind to the authenticated session, not to anything the model sends.
3. The broker runs the standard retrieval path — ACL filter, semantic dedupe, temporal +
   centrality re-weighting, 3–5 ranked hits — and answers with `results` (title, artifact type,
   `source_uri`, snippet, confidence tier) plus `budget_remaining` (`{calls, tokens}`).
4. Copilot answers from the snippets, citing `source_uri`s from your knowledge base.

If the model keeps searching, the dual budget cap (call count AND cumulative tokens per session,
enforced in the tool, not the prompt) eventually closes. The tool then returns empty results with
the notice *"KB budget spent — work with what you have, or read the specific files you still
need."* — a contractual outcome, never a crash, so the agent finishes with its file tools.

**For a change-shaped task, use `get_task_context` instead** — a separate, one-call tool with its
own server-side budget, so it never competes with the `kb_search` cap:

```sh
copilot -p 'Using the context-broker get_task_context tool, answer: what is the resolved scope,
blast radius, and applicable conventions for the task "add input validation to the GitHub
connector"?' --allow-all-tools
```

## Known behaviors when opening THIS repository

Two artifacts of this repository's own build tooling leak into a Copilot CLI session opened here —
neither exists in your own team repos (verified on Copilot CLI 1.0.63):

- **`.mcp.json` is merged into the workspace MCP config.** That file configures the tooling used
  to develop this platform, and Copilot CLI 1.0.63 reads it as workspace MCP servers — and ignores
  `"disabled": true` entries. Don't enable those servers in a product session; the broker config
  above is the product surface.
- **`.claude/agents/*` appear as invocable agents.** Those are internal build helpers for working
  on this platform's code — not the product roster, which lives in `.copilot/agents/`.

## Verify

Every call from your session — including any the budget refused — is one row in the retrieval
ledger:

```sh
psql agentic_kb -c "select tool_name, status, tokens_returned, created_at
                    from retrieval_event order by created_at desc limit 12;"
```

You should see `kb_search` rows with `status=approved` and their token charge, and — after the
budget closes — `denied` rows. That is the proof: a third-party agent reached your knowledge base
only through the budgeted, permission-filtered, fully-ledgered door. Deeper ledger queries:
[query traces and the ledger](query-traces-and-the-ledger.md).

Host misbehaving: [troubleshooting](troubleshoot.md).
