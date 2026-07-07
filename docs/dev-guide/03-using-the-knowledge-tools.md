# 07 — What "MCP ready" means (the agent's context toolkit, with an example)

> A plain-language explainer of what the MCP Context Broker gives a coding agent, why it
> matters, and how a real request flows through it. For anyone deciding whether the platform is
> ready to sit behind their agents. The tool shapes below are the contracted ones
> (`docs/contracts/mcp-tools-contract.md`); the numbers cite the platform's own eval reports.

## In one line

The agent now has a **smart librarian** for the codebase. It **asks the librarian first** for
exactly the right pieces — precise, **connected** snippets with named sources, within a token
budget — and only opens specific files itself when the librarian comes up short. It keeps its own
hands (`read`/`grep`/`glob`); the KB is a fast, budgeted helper, **not a gate** (ADR-0025). And
when it does read code, the code arrives **skeleton-first** — signatures kept, bodies elided — so
reading is cheap, with the exact body one `read_full` away (ADR-0026).

## The capabilities (the agent's toolkit)

Two tools carry the everyday load, each **budgeted** (enforced in code, not prompts),
**permission-checked** (only what this agent may see), and **logged** (every call in the
retrieval ledger):

| Capability | In plain terms |
|---|---|
| **`kb_search`** | "Here's my question — give me the few most relevant pieces, within my budget." One `query` string in; 3–5 ranked, ACL-filtered hits out, each with a source and a confidence tier. |
| **`get_task_context`** | "Here's my task — resolve what it touches, its blast radius, our conventions, and similar prior changes." One call, one budgeted response, zero LLM at query time. |

Behind them sits the **governed surface** for citation-grade work: `context.create_pack` /
`context.open_evidence` / `context.expand` / `context.verify_answer` (plus `graph.get_neighbors`
and `ledger.list_retrievals`) — evidence by handle, verification receipts, the full audit
choreography. Preferred flow first; governed flow when provenance must be machine-checkable.

### KB-first, file-fallback (ADR-0025)

These tools are a **preference, not a cage**. For any task the agent asks the KB first; if the KB
answers (or pins exactly which files matter), it uses and cites it and does **not** re-read what the
KB supplied. If the KB is missing, partial, or stale — or exact current code is needed for an edit —
the agent reads those **specific** files directly with its native tools. The one hard rule is a
budget: `kb_search` carries a per-session call+token cap enforced **in the tool, not the prompt**;
spend it and the tool says *"KB budget spent — work with what you have, or read the specific files
you still need."* Every file-fallback is a **KB-gap signal** — a precise pointer to where the KB
should improve (the dashboard's `kb_search_zero_thin_rate` tracks the server-visible half of it).

## Example: an agent gets a real task

> **Prompt:** *"Add a per-team daily token cap to the context broker's budget enforcement."*

**One call up front — `get_task_context`:**

```json
{"task_description": "Add a per-team daily token cap to the context broker's budget enforcement"}
```

The response is everything an implementer needs to *start*, in one budgeted payload:

- **`resolved_scope.entities`** — the files/symbols the task is about
  (`budgets.py`, `EvidencePackState`, `parse_agent_allowances`…), each stamped with how it was
  resolved (`alias_index | hint | search`) and a `confidence_tier`
  (`ground_truth | deterministic | interpreted`).
- **`blast_radius.callers / callees / tests`** — walked from the real `calls`/`imports`/`tests`
  graph (EXTRACTED trust class only). Honesty is structural: a `calls` edge reads `deterministic`
  **only** when the import graph corroborates it; otherwise it's `interpreted` with a `caveat`
  naming the missing corroboration.
- **`conventions`** — the rules/ADRs that apply to those directories (e.g. the token-budget rule
  file, ADR-0025), with evidence ids.
- **`similar_prior_changes`** — commits that changed budget enforcement before, with evidence ids.
- **`open_questions` / `ambiguous_candidates`** — if the scope is genuinely ambiguous, the tool
  says so and names the candidates. **An ambiguous answer is an answer, never a silent guess.**

The response is capped server-side at the Evidence-Pack band (~8k tokens), trimmed
deterministically from the lowest-value tail if needed, and ledgered with a per-node latency
breakdown. The backend is a LangGraph fan-out of four parallel pure-retrieval nodes — **zero LLM
calls at query time**, so it's fast and deterministic.

**Then the loop — `kb_search` + native tools.** As the agent works, point questions ("what logs
the budget denial?") go to `kb_search`; exact current code for the edit comes from reading the
few files `get_task_context` already pinned. When the search budget closes, the tool degrades
gracefully to the file-fallback message rather than erroring.

### The governed path, when the answer must be provable

For a claim that must be **citation-grade** — an answer a host will only trust with verified
provenance — the same broker serves the deliberate flow (walked live in
[05 — Running the MCP server](05-running-the-mcp-server.md) §5):

1. `context.create_pack` — a budgeted Evidence Pack: ~5 cards by handle, not raw text.
2. `context.expand` — the connected neighborhood (a verified live run: 3 seed cards → 27
   connected cards in one request, ~3,900 tokens, capped at 30 cards).
3. `context.open_evidence` — the exact source span of the one piece being quoted.
4. `context.verify_answer` — every cited claim checked (exists, in-version, ACL-visible, in the
   requester's ledger, not stale, EXTRACTED-supported); a signed **receipt** issued. In the live
   run, all six L0 provenance checks passed.

## Why this matters (with vs without MCP)

| | Plain agent (grep only) | With the KB (now) |
|---|---|---|
| How it gets context | greps/reads whole files, guesses | one `get_task_context` call scopes the task; `kb_search` answers point questions; files only on fallback |
| Token cost | huge (whole files into the prompt) | small + bounded (task context capped at the Evidence-Pack band; search capped per session); fallback reads arrive skeleton-first (~41% smaller) |
| Did it find the right code? | maybe | follows real `defined_in` / `calls` / `imports` / `tests` edges, with per-edge confidence tiers and caveats |
| Trust | claims may be invented | every response item carries sources; the governed path adds receipt-verified claims |
| Audit | none | every call — answered or budget-denied — in the retrieval ledger; KB gaps measurable |

The agent never *loses* its native tools — the KB just makes the common case faster and cheaper,
and compression makes the fallback read cheap too.

## So "MCP ready, verified" means

A coding agent can now (a) **scope a task in one call**, (b) **find the right code** with named
sources and honest confidence labels, (c) stay **within a code-enforced budget**, and (d) — when
it matters — have its answer **checked for fabrication** on the governed path. All of it
permission-filtered and ledgered.

## How it was verified

- **`get_task_context`** is scored by a two-arm A/B eval — the same model with the tool vs. file
  tools only — over ten realistic dev tasks (`scripts/eval_task_context.py`,
  `evals/agent_task_cases/task_context_ab_v1.yaml`; hermetic gate in
  `evals/tests/test_task_context_ab.py`; results: `docs/reports/task-context-ab-2026-07-03.md`).
- **The `kb_search` retrieval path** is exercised by the alias-resolution golden set against a
  really built KB (`scripts/eval_alias_resolution.py`, 25/25 top-1 on the last recorded run —
  `docs/reports/alias-accuracy-2026-07-03.md`); `make eval-all` re-runs every tier your shell can
  support ([08 — Observability](08-observability.md)).
- **The governed path** is exercised by `scripts/smoke_client.py` (`create_pack → open_evidence →
  graph.get_neighbors → context.expand → verify_answer → list_retrievals`) against a running
  broker; the live run above retrieved real budget-enforcement code, expanded 3 seeds into 27
  connected cards at 3,871 tokens, and passed all L0 provenance checks.
