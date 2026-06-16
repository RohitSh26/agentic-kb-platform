# 09 — GitHub Copilot CLI against the broker (the product runtime)

> Drive a **real external agent — the GitHub Copilot CLI, using its own model** — against our
> MCP Context Broker. This proves the product thesis end-to-end: a third-party agent gets
> context only through the governed broker, and our **budget + trust + audit** apply to it.
> Every command below was run on macOS; it actually consumed Copilot AI credits.

> Scope note: the Copilot CLI runs **one** agent (Copilot's model) with tools. The full
> **gated multi-agent orchestration** (orchestrator → subagents, human-approval gate at every
> delegation, ADR-0021) is the Groq runner in `dev-guide 08 §4`. This doc is the single-agent
> product runtime — Copilot calling our broker.

---

## 0. Prerequisites

- A **GitHub account with a Copilot license**, logged in via `gh` (`gh auth status`).
- A **built KB** + the **broker running** (dev-guide 08 §1–§3; `/health` → ok on `:8765`).
- **Node 18+** and npm.

---

## 1. Install the Copilot CLI

```sh
npm install -g @github/copilot
copilot --version            # GitHub Copilot CLI 1.0.x
```

## 2. Authenticate (headless, via your gh token)

The CLI accepts a token from `COPILOT_GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_TOKEN`. The **`gh`
OAuth token is a supported type** (classic `ghp_` PATs are not) — so a Copilot-licensed `gh`
login authenticates non-interactively:

```sh
export GH_TOKEN="$(gh auth token)"
copilot -p "Reply with exactly: AUTH_OK" --allow-all-tools   # prints AUTH_OK
```

(Interactive alternative: `copilot login` — browser device flow.)

## 3. Wire our broker as an MCP server

```sh
copilot mcp add --transport http context-broker http://127.0.0.1:8765/mcp/ \
  --header "Authorization: Bearer local-dev-token"
copilot mcp list             # context-broker (http) under "User servers"
```

This writes `~/.copilot/mcp-config.json`. The `local-dev-token` is the loopback dev-auth bearer
(ADR-0016); in production this is a real Entra token and an `https://` broker URL.

## 4. Simulate a human request (non-interactive)

With the broker running, give Copilot a task and tell it to get context **only** through the
broker. `--allow-all-tools` runs without interactive permission prompts.

```sh
export GH_TOKEN="$(gh auth token)"
copilot -p 'You have an MCP server "context-broker" serving a knowledge graph of THIS codebase.
Use ONLY its tools — do not read files from disk.
(1) call context.create_pack with run_id="copilot-demo-1", a task, retrieval_profile "default",
    budget_tokens 8000, intent "how_does_x_work".
(2) call context.expand on the top cards artifact_ids, trust_floor "EXTRACTED".
(3) Answer in 4-5 sentences and list the evidence_ids you used.
Question: How does the MCP Context Broker enforce a per-agent token budget?' \
  --allow-all-tools \
  --disable-mcp-server agentic-kb --disable-mcp-server github --disable-mcp-server postgres-dev
```

## 5. Replay — the broker's audit of what Copilot did

```sh
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" \
  uv run --project services/mcp-server python -m agentic_mcp_server.replay copilot-demo-1
```

---

## What this run actually showed (verified)

Copilot called our broker and **hit our governance** — the most convincing possible proof:

```
context.create_pack  [approved]  task=...per-agent token budgets...  cards=5
context.expand       [approved]  seeds=3 -> 316 cards, 4990 tok, truncated
context.open_evidence[denied]    agent token allowance exceeded: 4990 of 2500 used
context.read_pack    [reused]
context.open_evidence[denied]    (over budget again)
```

- Copilot retrieved via `create_pack`, **expanded the graph** (3 seeds → 316 connected cards),
  then tried to open raw evidence and was **rejected by the per-agent token budget** (the
  2,500-token agent allowance, distinct from the 8,000 run budget). It adapted via
  `context.read_pack` and answered, **citing the real evidence IDs**.
- Every call — including the **denied** ones — is in the ledger and renders in `replay`. The
  trust/budget/audit layer governed a real third-party agent, exactly as designed.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `copilot` auth fails | Your `gh` account lacks a Copilot license, or run `copilot login` (device flow). Classic `ghp_` PATs aren't accepted. |
| Copilot can't reach `context-broker` | The broker isn't running — start dev-guide 08 §3 first; confirm `curl :8765/health`. |
| Copilot reads files instead of the broker | Add `--disable-mcp-server github/postgres-dev` and instruct it to use only `context-broker`; or restrict with `--available-tools`. |
| `agent token allowance exceeded` | Working as intended (per-agent budget). Raise it via `MCP_AGENT_ALLOWANCES` on the broker if you want a bigger cap. |
