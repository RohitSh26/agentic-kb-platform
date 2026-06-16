# 10 — VS Code (Copilot agent mode) against the local broker

> Connect **GitHub Copilot in VS Code** to your locally-running Context Broker and ask the
> agent questions about the codebase — answered through our **governed** KB tools
> (`context.*` / `graph.*`), cited and audited, instead of the agent grep'ing whole files.

This is the IDE companion to dev-guide 08. After 08 you have a built KB and a broker running
on `127.0.0.1:8765`; this guide points VS Code's Copilot at it.

> **Which "agent" is this?** Inside VS Code you talk to **Copilot's** agent, which *uses our
> MCP tools*. This is **not** the 5-agent gated orchestrator (that is the terminal
> `scripts/agent_runner.py` in dev-guide 08 §4). Same governed KB underneath; different driver.

---

## 0. Prerequisites

- **dev-guide 08 §1–3 done**: KB built and the broker running. Verify:
  ```sh
  curl -s http://127.0.0.1:8765/health      # -> {"status":"ok", "active_kb_version":"local..."}
  ```
- **VS Code** 1.99+ (MCP + agent mode is GA).
- **GitHub Copilot** + **GitHub Copilot Chat** extensions, signed in to an account with Copilot
  enabled. (Copilot supplies the chat model; our broker supplies the *context*.)

---

## 1. The MCP connection (already in the repo)

This repo ships **`.vscode/mcp.json`**, so opening the folder in VS Code is all the wiring you
need:

```json
{
  "servers": {
    "context-broker": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp/",
      "headers": { "Authorization": "Bearer local-dev-token" }
    }
  }
}
```

> The bearer value is a placeholder: under **local-dev auth** (ADR-0016) the broker is bound to
> loopback and mints a fixed dev identity, so it **ignores the token value** — any non-empty
> string works. This is *not* an auth-off switch; it only applies on `127.0.0.1`. For a remote
> broker use `.copilot/mcp/vscode-mcp.json` instead (real host + `${input:...}` token prompt).

---

## 2. Start the server in VS Code

1. Open the repo folder in VS Code (`code .` from the repo root).
2. Open `.vscode/mcp.json`. VS Code shows a **Start** code-lens above the `context-broker`
   block — click it. (Or run **MCP: List Servers** from the Command Palette and start it there.)
3. The server status should go **Running**, and `context-broker` exposes our tools:
   `context.create_pack`, `context.expand`, `context.open_evidence`, `context.verify_answer`,
   `graph.get_neighbors`, `ledger.list_retrievals`.

> "Start" here just opens the MCP *client* connection to the already-running broker from §0 —
> it does not launch our Python server. If status is red, the broker isn't up: start dev-guide
> 08 §3 first.

---

## 3. Ask the agent a question

1. Open **Copilot Chat** (the chat icon) and switch the mode dropdown to **Agent**.
2. Click the **tools** (🛠) icon in the chat box and confirm the `context-broker` tools are
   checked/available.
3. Ask something that needs the codebase, e.g.:

   > *"Using the context-broker tools, how does the Context Broker enforce a per-agent token
   > budget? Cite the evidence IDs you used."*

Naming the tools in the first prompt nudges Copilot to call them; once it sees their value it
keeps using them.

### What you should see

- Copilot calls **`context.create_pack`** (a handful of cards), then **`context.expand`** to
  pull the connected neighborhood (capped at **30 cards / ~4,000 tokens**), and
  **`context.open_evidence`** for the one span it quotes. VS Code prompts you to **allow** each
  tool run (approve once / always).
- The answer names real symbols (e.g. `parse_agent_allowances`, `BudgetPolicy`) and **cites
  evidence IDs** — not invented files.
- If Copilot is greedy it may hit the **2,500-token per-agent allowance** and get a `denied` on
  a further `open_evidence` — that is the governance working, exactly as in dev-guide 09. It
  adapts (reuses the pack) and still answers.

---

## 4. Prove it was governed (not just chatting)

Every tool call — including denials — is in the retrieval ledger. In a terminal:

```sh
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" \
  uv run --project services/mcp-server python -m agentic_mcp_server.replay <run_id>
```

`<run_id>` is whatever Copilot passed (visible in the tool-call args in chat, or take the most
recent run from `ledger.list_retrievals`). You'll see `create_pack`, `expand` (seeds → capped
connected cards + tokens), any `denied` budget rows, and the `verify_answer` receipt — the
audit trail for what the IDE agent did on your behalf.

---

## 5. What "good" looks like

- The answer is **correct and cited** (Copilot's model is strong — unlike the deliberately
  rough 8B Groq model in 08 §4, quality here is high).
- The **tokens stay bounded**: `expand` never floods the chat — it returns the closest ~30
  cards, and raw bodies come only via `open_evidence` under budget.
- You can **replay** every step. If you can do all three, the IDE-to-governed-KB loop is proven
  on your machine.

---

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `context-broker` server won't start / red in VS Code | The broker isn't running — start dev-guide 08 §3 (`/health` must return 200) and click **Start** again. |
| Tools don't appear in Agent mode | Ensure the mode dropdown is **Agent** (not Ask/Edit), then open the 🛠 picker and enable `context-broker`. Reload the window if needed. |
| Copilot answers without calling the tools | Explicitly say "using the context-broker tools…" in the prompt; confirm the tools are enabled in the 🛠 picker. |
| Tool call → `401` | Non-loopback host or local-dev auth off — the broker must be started as in 08 §3 (`MCP_LOCAL_DEV_AUTH=1`, host `127.0.0.1`). |
| `/health` → 503 `no_active_kb_version` | The KB didn't activate — re-run the build (08 §2) until `kb_version_activated`. |
| `open_evidence` keeps getting `denied` | Expected when Copilot exceeds the 2,500-token agent allowance — it's the budget firing, not a bug. Start a fresh chat to reset the run. |

---

> **Verified vs. not:** the broker's MCP HTTP endpoint is exercised end-to-end by
> `scripts/smoke_client.py` and by the Copilot **CLI** (dev-guide 09) — VS Code's Copilot is
> just another MCP client hitting the same `/mcp/` URL with the same bearer. The `.vscode/mcp.json`
> and these steps are correct against that endpoint; the final in-editor clicks are yours to do
> on the machine (they cannot be driven headlessly).
