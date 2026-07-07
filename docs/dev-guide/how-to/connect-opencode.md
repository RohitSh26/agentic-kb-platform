# Connect OpenCode

**Goal:** run the platform's full agent roster — the orchestrator plus the specialist roles — in
OpenCode, each role granted exactly its canonical tools.

## Prerequisites

- The broker running with an active knowledge base ([getting started](../getting-started.md)).
- A paid or self-hosted model provider with a **strong tool-calling model** — this is a measured
  requirement, not a preference (see below).

## What ships in the repo

The `.opencode/` directory is a complete, ready-to-use OpenCode rendering of the agent framework.
Tool grants mirror the role canon: `kb_search` goes to every role; `get_task_context` only to the
task-scoped roles (`orchestrator`, `implementation`, `infra_code`, `test_layer`);
`get_review_draft` to `code_reviewer`. Native tools (`read`, `edit`, `grep`, `list`) are granted
per agent and carry no broker budget. Budgets and ACLs are enforced server-side — editing these
files cannot widen anything. `.opencode/README.md` is the full reference.

## Steps

1. **Install OpenCode** — `brew install sst/tap/opencode`, or see
   [opencode.ai](https://opencode.ai).

2. **Put the config where OpenCode looks.** Working inside this repo, `.opencode/` is already at
   the root and auto-discovered. For your own project, copy the whole `.opencode/` directory to
   its root (or to `~/.config/opencode/` globally).

3. **Set the broker URL** — in `.opencode/opencode.json`, replace the
   `https://<your-broker-host>/mcp/` placeholder with `http://127.0.0.1:8765/mcp/`. Edit the file
   **inside `.opencode/`** — a root-level `opencode.json` does not win the merge against it.

4. **Export the one credential:**

   ```sh
   export CONTEXT_BROKER_TOKEN=anything
   ```

   Any non-empty value works in local-dev mode; a remote broker needs a real Entra token
   ([Broker bearer tokens](../reference/environment-variables.md)). The config references it as
   `{env:CONTEXT_BROKER_TOKEN}` — never write a token value into `opencode.json`.

5. **Configure a strong tool-calling model** (requirement below), then run `opencode` and ask:

   > *"Using the context-broker kb_search tool: how does the build decide it can skip calling the
   > LLM for an unchanged document? Search the KB before reading any file, and cite your sources."*

## The host-model requirement (measured)

The host-integration test drove this exact OpenCode rendering against the real broker on four
Groq free-tier models (gpt-oss-120b, llama-4-scout, llama-3.3-70b-versatile, qwen3-32b): **none
passed agent discipline**. Two failed mechanically at the provider boundary (mangled tool names
and arguments rejected by Groq); two were mechanically clean but answered from priors without
consulting the knowledge base. The identical orchestrator body passes 9/9 discipline cases on
Copilot with claude-haiku-4.5 — same tools, same broker, same prompts — so this is host-model
capability, not a platform or config defect. Configure a real provider with a strong tool-calling
model for OpenCode sessions. Measured evidence:
[the host-integration report](../../reports/host-integration-2026-07-06.md).

Spot-check discipline with the harness (it drives OpenCode through Groq — pass a Groq-hosted
model id, or adapt `scripts/integration/run_opencode.sh` for your provider):

```sh
OPENCODE_MODEL=<groq-model-id> scripts/integration/run_opencode.sh opencode-t4-explain-1
```

## Verify

In the session transcript, a `kb_search` call precedes any file read, and the answer cites real
`source_uri`s — the same request/response shapes as the
[Copilot CLI walkthrough](connect-copilot-cli.md). The ledger check works for any host:

```sh
psql agentic_kb -c "select tool_name, status, tokens_returned, created_at
                    from retrieval_event order by created_at desc limit 12;"
```

Tools listed but never called, or answers without citations: that is the host-model requirement
above. More symptoms: [troubleshooting](troubleshoot.md).
