# 07 — What "MCP ready" means (the agent's context toolkit, with an example)

> A plain-language explainer of what the MCP Context Broker gives a coding agent, why it
> matters, and how a real request flows through it. Everything below was verified live
> against a built KB (`docs/dev-guide/06`), not aspirational.

## In one line

The agent now has a **smart librarian** for the codebase. It **asks the librarian first** for
exactly the right pieces — precise, **connected**, **cited** snippets within a token budget —
and only opens specific files itself when the librarian comes up short. It keeps its own hands
(`read`/`grep`/`glob`); the KB is a fast, budgeted helper, **not a gate** (ADR-0025). And when it
does read code, the code arrives **skeleton-first** — signatures kept, bodies elided — so reading is
cheap, with the exact body one `read_full` away (ADR-0026).

## The capabilities (the agent's toolkit)

Four things the agent can ask the librarian (the MCP tools), each **budgeted** (won't blow
the token limit), **permission-checked** (only what this agent may see), and **logged**
(full audit trail in the retrieval ledger):

| Capability | In plain terms |
|---|---|
| **`context.create_pack`** | "Here's my task — give me the handful of most relevant pieces of the codebase." |
| **`context.expand`** | "Now pull everything *connected* to those pieces — the file they live in, what they call, what they import." |
| **`context.open_evidence`** | "Show me the exact source of *this one* piece." |
| **`context.verify_answer`** | "Here's my answer and which pieces I used — check I didn't make anything up." |

(`graph.get_neighbors` and `ledger.list_retrievals` round out the surface: walk one edge of
the graph, and inspect the audit trail.)

### KB-first, file-fallback (ADR-0025)

These tools are a **preference, not a cage**. For any task the agent asks the KB first; if the KB
answers (or pins exactly which files matter), it uses and cites it and does **not** re-read what the
KB supplied. If the KB is missing, partial, or stale — or exact current code is needed for an edit —
the agent reads those **specific** files directly with its native tools. The one hard rule is a
budget: `kb_search` carries a per-task call+token cap enforced **in the tool, not the prompt**; spend
it and the tool says "work with what you have, or read the specific files you still need." Every
file-fallback is logged as a **KB-gap signal** — a precise pointer to where the KB should improve. The
governed `create_pack → open_evidence → verify_answer` path (below) stays available for when a claim
must be citation-grade.

## Example: an agent gets a real task

> **Prompt:** *"Add a per-team daily token cap to the context broker's budget enforcement."*

The flow — exactly what was verified live:

1. **Agent asks for relevant context → `create_pack`.** The librarian searches the KB and
   returns ~5 cards (titles + handles, not walls of text): `retrieve_cards`, `Requester`,
   `BrokerDeps`, `budgets.py`… — the actual budget-enforcement code. *No file read yet.*
2. **Agent asks "give me everything connected" → `context.expand`** *(the keystone)*. From
   those few cards the librarian walks the graph and returns the **closest connected
   neighborhood** — the defining file, the functions it calls, what it imports. Live result:
   **3 cards in → 27 connected pieces back, one request, ~3,900 tokens** (the response is
   capped — BFS closest-first — at **30 cards / ~4,000 tokens**, so it stays the immediate
   neighborhood, not the whole frontier). The agent now sees *how budgets work today* without
   grep'ing or reading whole files.
3. **Agent opens the exact source it needs → `open_evidence`.** "Show me the real code of
   `retrieve_cards`." → the exact source span (scanned for prompt-injection, never rewritten).
4. **Agent writes the code** using that precise context.
5. **Agent submits its work for checking → `verify_answer`.** "My change does X; here are
   the pieces I used." → the librarian confirms every claim points to **real, current,
   allowed** evidence and issues a **receipt**. Live: all 6 L0 provenance checks passed.
   *No fabricated files, no invented APIs.*

## Why this matters (with vs without MCP)

| | Plain agent (grep only) | With the KB (now) |
|---|---|---|
| How it gets context | greps/reads whole files, guesses | asks the KB first for the exact connected pieces; reads files only on fallback |
| Token cost | huge (whole files into the prompt) | small + bounded (~4k for the connected neighborhood, capped at 30 cards); fallback reads arrive skeleton-first (~41% smaller) |
| Did it find the right code? | maybe | follows real `defined_in` / `calls` / `imports` links — including cross-repo a grep can't reach |
| Trust | claims may be invented | every claim cited + receipt-verified (on the governed path) |
| Audit | none | every step written to the retrieval ledger; KB gaps surfaced by fallback logging |

The agent never *loses* its native tools — the KB just makes the common case faster and cheaper, and
compression makes the fallback read cheap too.

## So "MCP ready, verified" means

A coding agent can now (a) **find the right code**, (b) get the **full connected context
cheaply in one call**, (c) **cite exact sources**, and (d) be **checked for fabrication** —
all governed (budgets, ACLs) and logged. That is the foundation the multi-agent end-to-end
run builds on next.

## How it was verified

`docs/dev-guide/05` starts the broker against a built KB; `scripts/smoke_client.py` runs the
worked path `create_pack → open_evidence → graph.get_neighbors → context.expand →
verify_answer → list_retrievals`. The live run retrieved real code for a budget question,
expanded 3 seed cards into 27 connected cards at 3,871 tokens (capped at 30 cards), passed
all L0 provenance checks, and ledgered every step.
