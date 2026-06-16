# 00 — Getting Started: from `git clone` to asking questions in VS Code

**This is the one document to follow on a brand-new Mac.** It takes you from nothing installed to
GitHub Copilot in VS Code answering questions about this codebase through our governed knowledge
base — every answer cited and audited.

- **Time:** ~20–30 minutes (most of it is the one-time tool install).
- **Cost:** none. The default path uses **no cloud accounts, no API keys, no tokens** — it builds
  a knowledge base from this project's own source code, entirely on your laptop.
- **Assumed knowledge:** none about this project. If you can open a terminal and copy-paste, you can
  do this. Each step says what to run, what you should see, and what to do if it goes wrong.

> Follow the parts in order. **Parts 1–8 are the complete happy path.** Parts 9–11 are optional
> extras (your own real sources, the terminal multi-agent runner). The Appendix explains how the
> system works if you're curious — you don't need it to finish.

---

## The 60-second mental model (what you're about to run)

Three pieces, run in order:

```
   ┌─ 1. KB BUILDER ─┐      ┌─ 2. MCP SERVER ─┐      ┌─ 3. VS CODE + COPILOT ─┐
   reads code/docs         serves the KB to            you ask questions;
   builds a knowledge  ──▶  agents through one    ──▶  Copilot calls the server,
   graph in Postgres       governed door               answers cited from the KB
```

1. **The KB Builder** reads source code (and optionally your docs/tickets), and writes a
   *knowledge graph* — functions, files, and how they connect — into a **Postgres** database.
2. **The MCP Server** (we call it the *Context Broker*) serves that knowledge to AI agents through
   a small set of tools, enforcing budgets, permissions, and an audit log. Agents never touch the
   database directly — they go through this one door.
3. **VS Code + GitHub Copilot** is the agent. You ask it a question; it calls the server's tools to
   fetch exactly the relevant, connected code (not whole files), and answers **citing its sources**.

That's it. Now let's build it.

---

## Part 1 — Install the tools (one-time)

You need four things: **Postgres** (the database), **uv** (runs the Python services), **Git**, and
**VS Code + Copilot** (for Part 6). On macOS with [Homebrew](https://brew.sh):

```sh
# Postgres 16 — the database that stores the knowledge base
brew install postgresql@16
brew services start postgresql@16

# uv — manages Python 3.12 and the project dependencies for you
curl -LsSf https://astral.sh/uv/install.sh | sh

# Git usually ships with macOS; if not:  brew install git
```

**Verify each is working** (you should get a version number, not "command not found"):

```sh
psql --version      # -> psql (PostgreSQL) 16.x
uv --version        # -> uv 0.x.x
git --version       # -> git version 2.x
```

> If `uv` says "command not found" right after installing, open a **new** terminal window (the
> installer adds it to your shell profile, which only new terminals pick up).

VS Code + Copilot you can install now or when you reach Part 6:

- **VS Code** 1.99 or newer: download from <https://code.visualstudio.com>.
- The **GitHub Copilot** and **GitHub Copilot Chat** extensions (install from the Extensions panel),
  signed in to a GitHub account that has Copilot enabled. *(Copilot supplies the chat model; our
  server supplies the context. You need a Copilot subscription for this part — the rest of the guide
  needs nothing.)*

---

## Part 2 — Get the code

```sh
git clone https://github.com/RohitSh26/agentic-kb-platform.git
cd agentic-kb-platform
make sync            # installs Python deps for both services (uv handles Python 3.12 itself)
```

**You should see** `uv` resolve and install packages for `kb-builder`, `mcp-server`, and `evals`,
ending without errors. Everything in this guide ships on the **`main`** branch — you don't need any
other branch.

> **Keep this terminal open in the `agentic-kb-platform` folder.** Every command below assumes you
> are in this directory. We'll call it the *repo root*.

---

## Part 3 — Create the database

The KB Builder owns the database schema. Create an empty database and apply the schema once:

```sh
# Set the database connection string ONCE — every later command reuses it.
export DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb"

createdb agentic_kb
( cd services/kb-builder && DATABASE_URL="$DATABASE_URL" uv run alembic upgrade head )
```

**You should see** Alembic print a series of `Running upgrade ... -> ...` lines and finish without
error. That created the tables (artifacts, edges, caches, the audit ledger).

> **Troubleshooting**
> | Symptom | Fix |
> |---|---|
> | `createdb: command not found` | Postgres isn't on your PATH. `brew services start postgresql@16`, then open a new terminal. |
> | `database "agentic_kb" already exists` | Fine — it's already there. Skip `createdb` and run the migrate line. To start clean: `dropdb agentic_kb && createdb agentic_kb`. |
> | `role "<you>" does not exist` / auth failed | Your Postgres uses a different user. Use that user in the URL, e.g. `...://postgres@localhost:5432/agentic_kb`, and `createdb -U postgres agentic_kb`. |
> | `must use the asyncpg driver` later on | The URL must start with `postgresql+asyncpg://` exactly. |

---

## Part 4 — Build the knowledge base (no tokens, no LLM)

This builds a knowledge base from **this project's own Python source**, straight from your local
checkout. It needs **no GitHub token and no AI model** — extracting code structure is deterministic.
You get functions, files, and the `defined_in` / `calls` / `imports` graph that connects them.

```sh
# from the repo root
cd services/kb-builder
GITHUB_TOKEN="local-unused" DATABASE_URL="$DATABASE_URL" \
  uv run python -m agentic_kb_builder.build \
    --backend local --workspace ../.. \
    --sources ../../scripts/local-code-sources.yaml --no-git-metadata
cd ..
```

**You should see** a progress timeline ending in lines like `publish_gates_passed`,
`kb_version_activated`, and `build status : active`. That last line means the knowledge base is
live and ready to serve.

> `GITHUB_TOKEN="local-unused"` is a dummy value — the `--backend local` mode reads files from your
> disk and ignores it, but the config loader still wants the variable *present*. No real token is
> used or needed.

**Confirm the graph was built** (run from the repo root):

```sh
psql agentic_kb -c "select artifact_type, count(*) from knowledge_artifact where invalidated_at_seq is null group by 1 order by 2 desc;"
psql agentic_kb -c "select count(*) as edges from knowledge_edge where invalidated_at_seq is null;"
```

**You should see** a few hundred artifacts (code symbols + files) and a few hundred edges. Non-zero
counts mean you have a real, connected knowledge base to ask questions about.

> **Troubleshooting**
> | Symptom | Fix |
> |---|---|
> | `build status` is not `active` / a gate failed | Re-read the timeline for the failing gate. Most often the DB wasn't migrated — re-run Part 3, then rebuild. |
> | `no module named agentic_kb_builder` | You're in the wrong folder or skipped `make sync`. Run from `services/kb-builder`; re-run `make sync` from the repo root. |
> | `connection refused` to Postgres | Postgres isn't running: `brew services start postgresql@16`. |
> | Counts come back `0` | The build didn't activate, or pointed at the wrong DB. Confirm `DATABASE_URL` ends in `/agentic_kb` and the build printed `build status : active`. |

---

## Part 5 — Start the MCP server

This serves the knowledge base to agents. Start it in a terminal and **leave it running** — open a
second terminal for the later steps.

```sh
# from the repo root
cd services/mcp-server
MCP_LOCAL_DEV_AUTH=1 MCP_HTTP_HOST=127.0.0.1 MCP_HTTP_PORT=8765 \
MCP_ENTRA_TENANT_ID=local-dev MCP_ENTRA_AUDIENCE=api://local MCP_LOCAL_DEV_TEAMS=platform \
MCP_AGENT_ALLOWANCES='{"local-dev": {"max_requests": 50, "max_tokens": 50000}}' \
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" \
  uv run python -m agentic_mcp_server
```

**What these settings mean (plain language):**

- `MCP_LOCAL_DEV_AUTH=1` — use a simple **local developer identity** instead of corporate login.
  This only works on `127.0.0.1` (your machine) and refuses to run on a public address. It is **not**
  an "auth off" switch; it's a loopback-only convenience for local testing.
- `MCP_AGENT_ALLOWANCES=...` — gives your local identity a comfortable token budget so your first
  questions aren't cut off. Budgets are real and enforced by the server (see Part 7).
- `DATABASE_URL` — points the server at the knowledge base you just built.

**Verify it's up** — in your **second** terminal:

```sh
curl -s http://127.0.0.1:8765/health
# -> {"status":"ok", ... "active_kb_version":"local...."}
```

`"status":"ok"` means the server is serving your KB. If you instead get `503`, the build didn't
activate — go back to Part 4.

**(Optional) prove the whole tool chain works without VS Code:**

```sh
# from the repo root, in the second terminal
MCP_URL=http://127.0.0.1:8765/mcp/ \
  uv run --project services/mcp-server python scripts/smoke_client.py
```

This drives the same tools a real agent uses (retrieve → open → expand → verify → audit) and prints
what each step proves, ending in `smoke passed`.

> **Troubleshooting**
> | Symptom | Fix |
> |---|---|
> | `curl` returns nothing / connection refused | The server didn't start. Look at the server terminal for the error; the most common is a bad `DATABASE_URL`. |
> | `/health` → `503` `no_active_kb_version` | The KB isn't active — rebuild in Part 4 until you see `build status : active`. |
> | server exits with `missing required environment variables` | Copy the whole command block — all the `MCP_*` variables on one line before `uv run`. |
> | `address already in use` on `:8765` | An old server is still running. `pkill -f agentic_mcp_server`, then start again. |

---

## Part 6 — Connect VS Code + GitHub Copilot

Now point VS Code's Copilot at your running server. **The connection config already ships in the
repo** (`.vscode/mcp.json`), so opening the folder is most of the work.

1. **Open the project in VS Code.** From the repo root: `code .` (or File → Open Folder → the
   `agentic-kb-platform` folder).
2. **Start the MCP connection.** Open the file `.vscode/mcp.json`. VS Code shows a small **Start**
   link (a "code lens") just above the `"context-broker"` block — **click it**. The status should
   turn to **Running**.
   - *Alternatively:* open the Command Palette (`Cmd-Shift-P`) → **MCP: List Servers** → start
     `context-broker`.
3. **Open Copilot Chat** (the chat icon in the sidebar) and switch the mode dropdown at the bottom of
   the chat box from *Ask* to **Agent**.
4. **Confirm the tools are available.** Click the **tools** icon (🛠) in the chat box; you should see
   the `context-broker` tools listed (`context.create_pack`, `context.expand`,
   `context.open_evidence`, `context.verify_answer`, and two more). Make sure they're enabled.

> The connection file points at `http://127.0.0.1:8765/mcp/` with a placeholder token. Because the
> server is in local-dev mode on your own machine, the token value is **ignored** — any non-empty
> string works. (For a *remote* server you'd use a real token; that's a different setup.)

> **Troubleshooting**
> | Symptom | Fix |
> |---|---|
> | `context-broker` won't start / shows red in VS Code | The server (Part 5) must be running first. Confirm `curl http://127.0.0.1:8765/health` returns `ok`, then click **Start** again. |
> | No tools show up in the chat | Make sure the mode dropdown is **Agent** (not *Ask* or *Edit*), then open the 🛠 picker and enable `context-broker`. Reload the window if needed (`Cmd-Shift-P` → *Reload Window*). |
> | "I don't see a Start link in mcp.json" | Use the Command Palette route: `Cmd-Shift-P` → **MCP: List Servers** → `context-broker` → Start. |

---

## Part 7 — Ask the agent a question

In Copilot Chat (Agent mode), ask something that needs the codebase. Naming the tools in your first
prompt nudges Copilot to use them:

> *"Using the context-broker tools, how does the Context Broker enforce a per-agent token budget?
> Cite the evidence IDs you used."*

Other good first questions:

> *"Using the context-broker tools, what does `context.expand` do and what stops it returning too
> much? Cite your evidence."*
>
> *"Using the context-broker tools, how does the build decide it can skip re-running on unchanged
> files?"*

**What you should see:**

- Copilot calls **`context.create_pack`** (gets a handful of relevant "cards"), then
  **`context.expand`** (pulls the connected neighborhood — the file a function lives in, what it
  calls, what it imports), then **`context.open_evidence`** to read the one snippet it quotes. VS
  Code asks you to **allow** each tool run — click Allow (you can choose "always allow").
- The answer names **real** functions (e.g. `parse_agent_allowances`, `BudgetPolicy`) and **cites
  evidence IDs** — it's reading your actual code, not guessing.
- `context.expand` returns the **closest ~30 pieces** (capped at 30 cards / ~4,000 tokens) — enough
  to answer without dumping whole files into the chat.

> **This is the win:** instead of reading entire files, the agent asked for exactly the connected
> pieces it needed, within a budget, and cited them.

> **Troubleshooting**
> | Symptom | Fix |
> |---|---|
> | Copilot answers from general knowledge without calling tools | Start the prompt with "Using the context-broker tools, …" and confirm the tools are enabled in the 🛠 picker. |
> | A tool call returns `allowance exceeded` | Expected if it asked for a lot — the budget is doing its job. Start a fresh chat to reset, or raise `max_tokens` in the Part-5 command and restart the server. |
> | A tool call returns `401` | The server isn't in local-dev mode or isn't on `127.0.0.1` — restart it exactly as in Part 5. |

---

## Part 8 — Prove it was governed (the audit trail)

Every tool call — including any that were denied for budget — is recorded. See exactly what the
agent did, in order. First find the run id (Copilot sets one; it's visible in the tool-call details
in chat), then:

```sh
# from the repo root, in your second terminal
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" \
  uv run --project services/mcp-server python -m agentic_mcp_server.replay <run_id>
```

**You should see** a timeline: `create_pack` (which cards, their scores, the budget), `expand`
(seeds → connected cards + tokens), any denied rows, and the `verify_answer` receipt. That timeline
is the proof the agent only ever reached the KB through the governed, audited door.

You can also query it directly:

```sh
psql agentic_kb -c "select tool_name, status, tokens_returned, created_at from retrieval_event order by created_at desc limit 12;"
```

---

## You're done 🎉

You built a knowledge base, served it, connected VS Code, and watched Copilot answer from it with
citations — all on your laptop, no cloud. **Parts 9–11 below are optional.**

---

## Part 9 — (Optional) Build from YOUR real GitHub + Azure DevOps sources

Part 4 indexed this repo's own code. To index **your** repositories and Azure DevOps Wiki / Work
Items, use the **production** fetch backend. This needs access tokens and — because it summarizes
prose (docs, wiki, tickets) — an AI model.

**1. Get tokens and an AI key**, and load them into your shell. Put them in a repo-root **`.env`**
file (never commit it):

```sh
# .env  (repo root) — fill in your real values
GITHUB_TOKEN=ghp_...           # GitHub PAT: classic with `repo` scope, OR fine-grained granted to the repo (Contents: Read)
ADO_PAT=...                    # Azure DevOps PAT: Wiki (Read) + Work Items (Read) — only if you index ADO
LLM_PROVIDER=groq              # any OpenAI-compatible provider (Groq/OpenAI/Azure/Ollama…)
LLM_API_KEY=gsk_...            # the model key (used to summarize docs/wiki/tickets)
LLM_MODEL=llama-3.1-8b-instant
```

> **Code is zero-LLM.** Only *prose* sources (docs, wiki, tickets) use the model. A code-only build
> needs no `LLM_*` at all.

**2. Describe your sources.** Copy the template and edit the identifiers (you set `owner/repo`,
`organization`, `project` — not URLs; the connectors build the URLs):

```sh
cp services/kb-builder/sources.example.yaml scripts/my-sources.yaml
# edit scripts/my-sources.yaml: your GitHub owner/repo, your ADO org/project/wiki; delete source types you don't want
```

**3. Build into a fresh database with the production backend:**

```sh
export DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb"
dropdb --if-exists --force agentic_kb && createdb agentic_kb
( cd services/kb-builder && DATABASE_URL="$DATABASE_URL" uv run alembic upgrade head )

cd services/kb-builder
set -a; source ../../.env; set +a          # load GITHUB_TOKEN, ADO_PAT, LLM_*
DATABASE_URL="$DATABASE_URL" \
  uv run python -m agentic_kb_builder.build \
    --sources ../../scripts/my-sources.yaml --workspace ../.. \
    --backend production --no-git-metadata --log-format timeline
cd ..
```

Then **serve and use it exactly as in Parts 5–8** (same server command, same VS Code connection).

> **Troubleshooting real-source fetches**
> | Symptom | Cause & fix |
> |---|---|
> | One GitHub source `404`s while another (same repo) succeeds | That source is **missing its `auth:` block** — it ran unauthenticated, and a private repo returns 404. Every `github_*` / `azure_wiki` / `ado_card` source pointing at a **private** resource needs its own `auth: token_env: …`. The error now says "no Authorization header was sent" when this happens. |
> | GitHub/ADO **404** (auth present) | Repo/wiki/project is private and the token can't see it (GitHub returns 404, not 403). A classic PAT needs `repo` scope; a fine-grained PAT must be *granted to that repo*; an ADO PAT needs Wiki/Work-Item **Read**. |
> | **401** bad/expired credentials | The token is wrong or expired. Re-create it and re-`source .env` in *this* shell. |
> | **403** scope/SSO/rate-limit | Missing scope, org SSO not authorized for the PAT, or you hit a rate limit. |
> | build wants an LLM but none is set | You included a prose source (doc/wiki/card). Either set `LLM_*` in `.env`, or index code only. |

> The GitHub backend is exercised against the live API; the ADO backends are unit-tested against
> mocked transports, so a real ADO instance may surface format/auth specifics to iron out.

---

## Part 10 — (Optional) The terminal multi-agent runner

VS Code (Part 6) gives you **Copilot using our tools**. This project also ships a **5-agent gated
orchestrator** — a script where a planner agent delegates to specialist agents and **pauses for your
approval at every hand-off**. It's a different experience (terminal, not the IDE) on the same KB.

It uses an AI model for the agents' "brains", so set `LLM_*` in `.env` first (a Groq key is cheap/
fast). With the server from Part 5 running:

```sh
# from the repo root, second terminal
set -a; source .env; set +a
export DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb"
export MCP_URL="http://127.0.0.1:8765/mcp/"

uv run --project services/mcp-server python scripts/agent_runner.py \
  "Add input validation to the GitHub connector"
```

You approve each delegation (`[a]pprove / [e]dit / [r]eject / [x]abort`). It prints a `run_id` and a
replay command at the end. The generated code is rough on purpose (small 8B model) — this exercises
the *plumbing* (right-code retrieval, approval gates, trust, audit), which a stronger model improves
with zero changes.

---

## Part 11 — Stop and clean up

```sh
# stop the server: press Ctrl-C in its terminal, or:
pkill -f agentic_mcp_server

# (optional) delete the knowledge base entirely:
dropdb agentic_kb
```

In VS Code, stop the `context-broker` connection from `.vscode/mcp.json` (the **Stop** code lens) or
just close the folder.

---

## Master troubleshooting table

| Where | Symptom | Fix |
|---|---|---|
| Install | `command not found` after install | Open a new terminal; the installer updated your shell profile. |
| DB | `must use the asyncpg driver` | `DATABASE_URL` must start with `postgresql+asyncpg://`. |
| DB | auth / role errors | Use your real Postgres user in the URL; or `createdb -U postgres ...`. |
| Build | not `active` / gate failed | Migrate the DB (Part 3) first, then rebuild. Read the timeline for the named gate. |
| Build | counts are `0` | Wrong DB or build didn't activate — confirm `DATABASE_URL` and `build status : active`. |
| Server | connection refused | The server isn't running, or `DATABASE_URL` is wrong — check the server terminal. |
| Server | `/health` → `503` | KB not active — rebuild (Part 4). |
| Server | `address already in use` | `pkill -f agentic_mcp_server` and restart. |
| VS Code | server red / won't start | The server (Part 5) must be up first — `curl :8765/health` must say `ok`. |
| VS Code | no tools in chat | Switch the mode to **Agent**; enable `context-broker` in the 🛠 picker; reload the window. |
| VS Code | Copilot ignores the tools | Prompt with "Using the context-broker tools, …". |
| Asking | `allowance exceeded` | Budget working as designed — new chat to reset, or raise `max_tokens` in Part 5 and restart. |
| Asking | `401` on a tool call | Restart the server exactly as in Part 5 (local-dev, `127.0.0.1`). |

If something here doesn't match what you see, the deeper references are: **dev-guide 04**
(KB-builder internals, switching AI providers, SQL queries), **dev-guide 05** (server config
reference), and **dev-guide 09** (the GitHub Copilot **CLI**, the non-IDE version of Part 6).

---

## Appendix — how it actually works (for the curious)

You don't need this to finish, but it explains *why* the pieces exist.

**Two ideas hold the whole system together:**

1. **Postgres is the single source of truth.** The artifacts, the graph edges, the caches, and the
   audit ledger all live in Postgres. The search index is a *rebuildable projection* — locally the
   server just keyword-searches Postgres directly, so you need no separate search service.
2. **The agent never touches a data store.** Everything goes through the **Context Broker** (the MCP
   server), which enforces identity, permissions (ACLs), token budgets, de-duplication, and
   "evidence by handle" (you get a compact *card* first, and only fetch raw text on demand). The
   agent can't exceed its budget, can't read evidence it didn't retrieve, and any answer it wants
   *trusted* must carry a verification **receipt**.

**The tools the agent uses** (what you watched Copilot call in Part 7):

| Tool | In plain terms |
|---|---|
| `context.create_pack` | "Here's my task — give me the few most relevant pieces of the codebase." |
| `context.expand` | "Now pull everything *connected* to those — the file they live in, what they call/import." |
| `context.open_evidence` | "Show me the exact source of *this one* piece." |
| `context.verify_answer` | "Here's my answer and which pieces I used — check I didn't invent anything." |
| `graph.get_neighbors` | "Walk one step out in the graph from this piece." |
| `ledger.list_retrievals` | "Show me everything I did this run" (the audit trail). |

**What runs locally vs. what's swapped in production:**

| Concern | Local (this guide) | Production |
|---|---|---|
| AI model (summaries) | none for code; Groq/OpenAI/Ollama for prose | Azure OpenAI |
| Embeddings | local hash embedder | Azure OpenAI |
| Search | keyword-search over Postgres | Azure AI Search |
| Identity | local-dev (loopback only) | Entra ID, fail-closed |
| Sources | your local checkout / GitHub + ADO via PAT | GitHub + ADO via managed identity |

Everything that differs sits behind an interface, so the build, broker, budgets, permissions,
verifier, and audit logic you ran locally is the *same code* that runs in production — only the
swapped seam changes.
