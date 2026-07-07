# 02 — Connect your editor

This page connects an agent host — **VS Code + GitHub Copilot**, the **GitHub Copilot CLI**, or
**OpenCode** — to your running broker, and walks one real question through each. Every host gets
the same two tools (`kb_search` and `get_task_context`), with budgets and permissions enforced
server-side; what those tools give you is [03 — Using the knowledge tools](03-using-the-knowledge-tools.md).

## Before you start

- The broker must be running with an active KB — [01 — Run the platform](01-run-the-platform.md).
  Verify: `curl -s http://127.0.0.1:8765/health` → `{"status":"ok", ...}`.
- Against a **local** broker in local-dev mode, any non-empty bearer token works — the placeholder
  values in the shipped configs are fine as-is. Against a **remote** broker you need a real Entra
  token: [07 — Providers and API keys](07-providers-and-api-keys.md) §"Broker bearer tokens".

---

## VS Code + GitHub Copilot (agent mode)

The connection config already ships in the repo (`.vscode/mcp.json`, pointing at
`http://127.0.0.1:8765/mcp/`), so opening the folder is most of the work. You need VS Code 1.99+
with the GitHub Copilot + Copilot Chat extensions, signed in to a Copilot-enabled account.

1. **Open the project in VS Code** — `code .` from the repo root.
2. **Start the MCP connection** — open `.vscode/mcp.json`; click the **Start** code lens above the
   `"context-broker"` block (or `Cmd-Shift-P` → **MCP: List Servers** → start `context-broker`).
   The status should turn to **Running**.
3. **Open Copilot Chat** and switch the mode dropdown at the bottom of the chat box from *Ask* to
   **Agent**.
4. **Confirm the tools** — click the tools icon in the chat box; enable at least `kb_search` under
   `context-broker` (`get_task_context` is the other one that matters day-to-day).
5. **Ask.** Naming the tool in your first prompt nudges Copilot to use it.

**One real question, walked through:**

> *"Using the context-broker kb_search tool, how does the build decide it can skip calling the
> LLM for an unchanged document? Name the sources you used."*

Copilot calls `kb_search` (VS Code asks you to **allow** the tool run — "always allow" is fine
locally). The request is deliberately tiny:

```json
{"query": "how does the build decide to skip the LLM for unchanged documents?"}
```

The response is a handful of ranked, permission-filtered hits plus your remaining budget:

```json
{
  "results": [
    {
      "title": "GenerationCacheGate.lookup_artifact_ids",
      "artifact_type": "code_symbol",
      "source_uri": "services/kb-builder/src/agentic_kb_builder/application/cache_gates.py",
      "snippet": "…cache hit ⇒ return prior artifact ids, no model call…",
      "confidence_tier": "interpreted"
    }
  ],
  "budget_remaining": {"calls": 49, "tokens": 49574},
  "notice": null
}
```

The answer names **real** functions and files because it's reading your actual KB, not guessing.
For a change-shaped task, try instead: *"Call get_task_context with the task 'add input validation
to the GitHub connector' and summarize what comes back."* — what each field means is in
[03 — Using the knowledge tools](03-using-the-knowledge-tools.md).

> **Recommended model.** Pick a strong model in the chat's model picker — Claude (Haiku/Sonnet) or
> GPT‑5. A small model tends to ignore tool guidance and over-plan simple questions. The broker
> governs *what* the agent can retrieve; the model decides *how well* it uses it.

---

## GitHub Copilot CLI

The non-IDE variant: a real external agent, using its own model, driven from your terminal.

### The one design point to understand first

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

That allowlist is the point: a Copilot host gets the budgeted, ACL-filtered `kb_search` and the
one-call, separately-budgeted `get_task_context`, and keeps its own native file tools — it does
not get the broker's other ten tools (the governed evidence-pack flow). The budget and ACL are
enforced **server-side per authenticated identity** either way (deleting this file cannot widen
anything), but the allowlist keeps the host's tool surface matching the framework's design.

**Do not wire the broker with an ad-hoc `copilot mcp add` instead.** That creates a server entry
*without* the `tools` allowlist, so the CLI would see and offer the broker's entire tool surface —
bypassing the committed allowlist policy. Use the committed config, adapted only in URL and token,
for both deployment shapes:

| Deployment | Where the config goes |
|---|---|
| Copilot cloud coding agent / org rollout | Repository settings → Copilot → MCP servers: paste the file's contents; create the Copilot environment value `COPILOT_MCP_CONTEXT_BROKER_TOKEN` (Copilot only exposes values whose names start with `COPILOT_MCP_`) |
| Copilot **CLI** on your machine (this section) | Merge the same `mcpServers` block into `~/.copilot/mcp-config.json` (below) |

`.copilot/README.md` documents both shapes plus the agent renderings that go with them.

### Connect in four steps

You need a GitHub account with a Copilot license (logged in via `gh`) and Node.js + npm.

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

3. **Point the CLI at the broker — via the committed config.** Take the committed server block,
   change only the URL; the token header stays a **reference by name**, never a pasted value:

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

   > If you already have other servers in `~/.copilot/mcp-config.json`, merge the
   > `context-broker` entry into your existing `mcpServers` object instead of overwriting.

   Against the local broker in local-dev auth mode (loopback-only), any non-empty bearer
   authorizes as the `local-dev` subject — which is why a literal `local-dev-token` placeholder is
   fine here. Against a **production** broker you keep the committed file's
   `$COPILOT_MCP_CONTEXT_BROKER_TOKEN` reference and supply a real Entra token through the Copilot
   environment — a token value never lands in a config file either way.

4. **Ask.**

### One real question, walked through

```sh
copilot -p 'Using the context-broker kb_search tool, answer: how does the KB build
decide it can skip calling the LLM for an unchanged document? Search the KB before
reading any file, and cite the source_uri of what you used.' --allow-all-tools
```

What happens, step by step:

1. Copilot's model sees exactly two broker tools — `kb_search` and `get_task_context` — plus its
   own native tools.
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
you still need."* — a contractual outcome, never a crash, so the agent keeps working with its file
tools.

**For a change-shaped task, use `get_task_context` instead** — a separate, one-call tool with its
own server-side budget, so it never competes with the `kb_search` cap:

```sh
copilot -p 'Using the context-broker get_task_context tool, answer: what is the resolved scope,
blast radius, and applicable conventions for the task "add input validation to the GitHub
connector"?' --allow-all-tools
```

One request returns the resolved scope, blast radius (callers/callees/tests), conventions, and
similar prior changes in a single budgeted response, every item citing an evidence id — the full
response shape is in [03 — Using the knowledge tools](03-using-the-knowledge-tools.md).

### Known behaviors when opening THIS repo (verified on Copilot CLI 1.0.63, 2026-07-06)

Two artifacts of this repository being *built with* Claude Code leak into a Copilot CLI session
opened here — neither exists in your own team repos, so pilot developers won't see them:

- **`.mcp.json` is merged into the workspace MCP config.** That file configures Claude Code's
  *build-plane* tooling for developing this platform, and Copilot CLI 1.0.63 reads it as workspace
  MCP servers — and ignores `"disabled": true` entries. Don't enable those servers in a product
  session; the broker config above is the product surface.
- **`.claude/agents/*` appear as invocable agents.** Those are Claude Code *build* subagents
  (reviewers, migration writers) for working on this platform's code — not the product roster,
  which lives in `.copilot/agents/`.

---

## OpenCode

The `.opencode/` directory ships a complete, ready-to-use OpenCode rendering of the agent
framework — the orchestrator plus the specialist roles, each granted exactly its canonical tools.
`.opencode/README.md` is the full reference; connecting takes five steps:

1. **Install OpenCode** (`brew install sst/tap/opencode`, or see [opencode.ai](https://opencode.ai)).
2. **Put the config where OpenCode looks.** Working inside this repo, `.opencode/` is already at
   the root and auto-discovered. For your own project, copy the whole `.opencode/` directory to
   its root (or `~/.config/opencode/` globally) — everything lands in its discovery location.
3. **Set the broker URL** — in `.opencode/opencode.json`, replace the
   `https://<your-broker-host>/mcp/` placeholder with `http://127.0.0.1:8765/mcp/`. (A root-level
   `opencode.json` does **not** win the merge against `.opencode/opencode.json` — edit the file
   inside `.opencode/`.)
4. **Export the one credential** — `export CONTEXT_BROKER_TOKEN=anything` (any non-empty value in
   local-dev mode; a real Entra token against a remote broker). The config references it as
   `{env:CONTEXT_BROKER_TOKEN}` — never write a token value into `opencode.json`.
5. **Configure a strong tool-calling model** — see the requirement below — then run `opencode`
   and ask the same question as the other hosts:

   > *"Using the context-broker kb_search tool: how does the build decide it can skip calling the
   > LLM for an unchanged document? Search the KB before reading any file, and cite your sources."*

   You should see a `kb_search` call precede any file read, and an answer citing real
   `source_uri`s — same request/response shapes as the Copilot walkthrough above.

Tool grants mirror the canon: `kb_search` goes to every role; `get_task_context` only to the
task-scoped BUILD-lane roles (`orchestrator`, `implementation`, `infra_code`, `test_layer`).
Native tools (`read`, `edit`, `grep`, `list`) are granted per agent and carry no broker budget.
The budget and ACL are enforced server-side — editing these files cannot widen anything.

> **The model requirement (measured, not a preference).** The pre-pilot host-integration test
> drove the committed OpenCode rendering against the real broker on **four Groq free-tier models**
> (gpt-oss-120b, llama-4-scout, llama-3.3-70b-versatile, qwen3-32b): **none passed agent
> discipline.** Two failed mechanically at the provider boundary (mangled tool names/arguments
> rejected by Groq), two were mechanically clean but answered from priors without consulting the
> KB. The identical orchestrator body passes 9/9 discipline cases on Copilot × claude-haiku-4.5 —
> same tools, same broker, same prompts — so this is host-model capability, not a platform or
> config defect. **Configure a real provider with a strong tool-calling model for OpenCode
> sessions**, then spot-check discipline with the harness (it drives OpenCode through Groq —
> pass a Groq-hosted model id, or adapt `scripts/integration/run_opencode.sh` for your provider):
>
> ```sh
> OPENCODE_MODEL=<groq-model-id> scripts/integration/run_opencode.sh opencode-t4-explain-1
> ```
>
> Full evidence: `docs/reports/host-integration-2026-07-06.md`.

---

## Prove it was governed (any host)

Every tool call — including the ones the budget refused — writes one row to the **retrieval
ledger** (`retrieval_event` in Postgres):

```sh
psql agentic_kb -c "
  select tool_name, status, tokens_returned, details, created_at
  from retrieval_event
  order by created_at desc limit 12;"
```

You should see one row per call from your session: `kb_search` rows with `status=approved` and
their token charge, `details` showing the budget window
(`{"session": …, "calls_used": …, "tokens_used": …, "max_requests": 50, "max_tokens": 50000}`),
any `denied` rows after the budget closed, and `get_task_context` rows with a per-node latency
breakdown. That is the proof: a third-party agent reached your KB only through the budgeted,
permission-filtered, fully-ledgered door.

- `approved` rows carry the tokens charged and the returned artifact ids — the audit of exactly
  what knowledge the agent received.
- `denied` rows are the budget doing its job; raise the cap for your subject via
  `MCP_AGENT_ALLOWANCES` on the broker if you want a longer session.
- `kb_search` is session-scoped, not run-scoped — its rows use the `run_id = "-"` sentinel and
  record the session in `details`, so SQL is the way to see them (the `replay` CLI and
  `ledger.list_retrievals` are for run-scoped tools).
- Answered-but-thin results also feed the dashboard's KB-gap proxy — see
  [06 — Observability](06-observability.md) for the dashboard and deeper ledger queries.

---

Host misbehaving? [08 — Troubleshooting](08-troubleshooting.md) §"Editor doesn't see the tools"
and §"Budget notices".
