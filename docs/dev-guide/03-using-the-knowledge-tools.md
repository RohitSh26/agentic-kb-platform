# 03 — Using the knowledge tools

What the broker's tools actually give you day-to-day: `kb_search` for point questions,
`get_task_context` for change work, the budget that governs both, and — as a labeled aside — the
governed path for answers that must carry machine-checkable provenance. Read this after your host
is connected ([02 — Connect your editor](02-connect-your-editor.md)); the tool shapes below are
the contracted ones (`docs/contracts/mcp-tools-contract.md`).

## The model in one line

The agent has a **smart librarian** for the codebase. It **asks the librarian first** for exactly
the right pieces — precise, **connected** snippets with named sources, within a token budget — and
only opens specific files itself when the librarian comes up short. It keeps its own hands
(`read`/`grep`/`glob`); the KB is a fast, budgeted helper, **not a gate** (ADR-0025). And when it
does read code, the code arrives **skeleton-first** — signatures kept, bodies elided — so reading
is cheap, with the exact body one `read_full` away (ADR-0026).

## The two everyday tools

Both are **budgeted** (enforced in code, not prompts), **permission-checked** (only what this
agent may see), and **logged** (every call in the retrieval ledger):

| Capability | In plain terms |
|---|---|
| **`kb_search`** | "Here's my question — give me the few most relevant pieces, within my budget." One `query` string in; 3–5 ranked, ACL-filtered hits out, each with a source and a confidence tier. |
| **`get_task_context`** | "Here's my task — resolve what it touches, its blast radius, our conventions, and similar prior changes." One call, one budgeted response, zero LLM at query time. |

### `kb_search`, day to day

The request is one field — that is the **entire** request; identity, ACL filtering, and the dual
call+token budget all bind to the authenticated session, never to request fields:

```json
{"query": "how does the build decide to skip the LLM for unchanged documents?"}
```

The response is a handful of ranked hits plus your remaining budget:

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

Snippets are honest about their nature: retrieved text is untrusted content, and each hit carries
a `confidence_tier` (`ground_truth | deterministic | interpreted`). Calling it from your own code
takes a few lines:

```python
import asyncio
from fastmcp import Client

async def main() -> None:
    async with Client("http://127.0.0.1:8765/mcp/", auth="local-dev-token") as client:
        hits = await client.call_tool("kb_search", {"request": {
            "query": "where is the per-agent token budget enforced?",
        }})
        for hit in hits.data["results"]:
            print(hit["title"], "→", hit["source_uri"], f"[{hit['confidence_tier']}]")
        print("budget left:", hits.data["budget_remaining"])   # {"calls": N, "tokens": M}

asyncio.run(main())
```

(Any non-empty bearer works against a local-dev broker; a remote broker needs a real token — see
[07 — Providers and API keys](07-providers-and-api-keys.md).)

### `get_task_context`, for change work

> **Prompt:** *"Add a per-team daily token cap to the context broker's budget enforcement."*

One call up front — `{"task_description": "…", "hints": {...}}`, hints optional — returns
everything an implementer needs to *start*, in one budgeted payload:

- **`resolved_scope.entities`** — the files/symbols the task is about (`budgets.py`,
  `EvidencePackState`, `parse_agent_allowances`…), each stamped with how it was resolved
  (`alias_index | hint | search`) and a `confidence_tier`.
- **`blast_radius.callers / callees / tests`** — walked from the real `calls`/`imports`/`tests`
  graph. Honesty is structural: a `calls` edge reads `deterministic` **only** when the import
  graph corroborates it; otherwise it's `interpreted` with a `caveat` naming the missing
  corroboration.
- **`conventions`** — the rules and decision records that apply to those directories, with
  evidence ids.
- **`similar_prior_changes`** — commits that changed this area before, with evidence ids.
- **`open_questions` / `ambiguous_candidates`** — if the scope is genuinely ambiguous, the tool
  says so and names the candidates. **An ambiguous answer is an answer, never a silent guess.**

The response is capped server-side at the Evidence-Pack band (~8k tokens), trimmed
deterministically from the lowest-value tail if needed, and ledgered with a per-node latency
breakdown. The backend is a parallel fan-out of four pure-retrieval nodes — **zero LLM calls at
query time**, so it's fast and deterministic. It carries its own budget, separate from
`kb_search`'s, so the two never compete.

**Then the loop.** As the agent works, point questions ("what logs the budget denial?") go to
`kb_search`; exact current code for the edit comes from reading the few files `get_task_context`
already pinned.

## Budget notices — what they mean, what to do

Each `kb_search` response shows `budget_remaining` counting down. When the per-session cap closes
(calls *or* tokens — whichever first), the tool answers with empty results and exactly this
notice, rather than erroring:

> *"KB budget spent — work with what you have, or read the specific files you still need."*

That is the budget working as designed (ADR-0025): a contractual outcome, never a crash. The agent
keeps its native file tools and finishes the job from them. If you hit it constantly in local dev,
start a fresh session (new chat window = new session) or raise `max_requests`/`max_tokens` in the
broker's `MCP_AGENT_ALLOWANCES` and restart it ([01 — Run the platform](01-run-the-platform.md)).
Every notice — like every call — lands in the ledger as a `denied` row.

## KB-first, file-fallback

These tools are a **preference, not a cage**. For any task the agent asks the KB first; if the KB
answers (or pins exactly which files matter), it uses and cites it and does **not** re-read what
the KB supplied. If the KB is missing, partial, or stale — or exact current code is needed for an
edit — the agent reads those **specific** files directly with its native tools. The one hard rule
is the budget above, enforced in the tool, not the prompt. Every file-fallback is a **KB-gap
signal** — a precise pointer to where the KB should improve (the dashboard's
`kb_search_zero_thin_rate` tracks the server-visible half of it; see
[06 — Observability](06-observability.md)).

## Why this matters (with vs without the KB)

| | Plain agent (grep only) | With the KB |
|---|---|---|
| How it gets context | greps/reads whole files, guesses | one `get_task_context` call scopes the task; `kb_search` answers point questions; files only on fallback |
| Token cost | huge (whole files into the prompt) | small + bounded (task context capped at the Evidence-Pack band; search capped per session); fallback reads arrive skeleton-first (~41% smaller) |
| Did it find the right code? | maybe | follows real `defined_in` / `calls` / `imports` / `tests` edges, with per-edge confidence tiers and caveats |
| Trust | claims may be invented | every response item carries sources; the governed path adds receipt-verified claims |
| Audit | none | every call — answered or budget-denied — in the retrieval ledger; KB gaps measurable |

The agent never *loses* its native tools — the KB just makes the common case faster and cheaper,
and compression makes the fallback read cheap too.

---

## Aside: the governed path, when the answer must be provable

> **This is not the everyday flow.** For a claim that must be **citation-grade** — an answer a
> host will only trust with verified provenance — the same broker serves a deliberate, heavier
> choreography: `context.create_pack` → `context.open_evidence` / `context.expand` /
> `graph.get_neighbors` → `context.verify_answer`, ending in a signed receipt. Reach for it when
> provenance must be machine-checkable; skip this section otherwise.

The four steps:

1. **`context.create_pack`** — the broker retrieves *once*, dedupes, reranks to ≤5 evidence cards,
   enforces the run budget, writes a `retrieval_event`, and returns **cards by handle** (L0/L1),
   not bulk text. The response names the `kb_version` it served.
2. **`context.expand` / `graph.get_neighbors`** — walk the connected neighborhood of a card over
   the Postgres-backed graph (a verified live run: 3 seed cards → 27 connected cards in one
   request, ~3,900 tokens). Defaults to `EXTRACTED`-trust edges; `include_inferred=true` surfaces
   inferred edges *labelled as routing hints* that cannot support a cited claim.
3. **`context.open_evidence`** — the exact raw source of ONE card, by handle, metered against the
   pack budget and flagged (never rewritten) by the deterministic injection scan. The field is
   literally `untrusted_content`.
4. **`context.verify_answer`** — every cited claim checked (evidence exists, is in-version,
   ACL-visible, in the requester's own retrieval ledger, not stale, supported by an extracted
   edge); a signed **receipt** issued (`docs/contracts/verification-receipt.md`). A claim with
   empty `evidence_ids` is rejected at the schema.

`ledger.list_retrievals` then shows the run's own audit trail. A worked client, end to end:

```python
import asyncio
from fastmcp import Client

BEARER = "<bearer — any non-empty value against a local-dev broker>"

async def main() -> None:
    async with Client("http://127.0.0.1:8765/mcp/", auth=BEARER) as client:
        # 1) create_pack — an Evidence Pack of cards by handle, within budget.
        pack = await client.call_tool("context_create_pack", {"request": {
            "run_id": "demo-run-1",
            "task": "How does the build decide whether to call the LLM?",
            "approved_context_plan": "incremental-build summary + cache gating",
            "retrieval_profile": "default",
            "budget_tokens": 6000,
            "intent": "how_does_x_work",
        }})
        pack_id = pack.data["context_pack_id"]
        cards = pack.data["evidence_cards"]
        print("kb_version:", pack.data["kb_version"], "cards:", len(cards))

        # 2) open_evidence — expand ONE card to its raw (untrusted) text by handle.
        first = cards[0]["evidence_id"]
        opened = await client.call_tool("context_open_evidence", {"request": {
            "context_pack_id": pack_id,
            "evidence_id": first,
            "max_tokens": 1500,
        }})
        print("opened level:", opened.data["level"],
              "injection_flagged:", opened.data["injection_flagged"])

        # 3) graph_get_neighbors — walk the graph from the card's artifact.
        artifact_id = cards[0]["artifact_id"]
        neighbors = await client.call_tool("graph_get_neighbors", {"request": {
            "artifact_id": artifact_id,
            "depth": 1,
            "trust_floor": "EXTRACTED",
        }})
        print("neighbors:", [n["edge_type"] for n in neighbors.data["neighbors"]])

        # 4) verify_answer — every claim must cite evidence ids; a receipt is issued.
        receipt = await client.call_tool("context_verify_answer", {"request": {
            "answer_id": "demo-answer-1",
            "claims": [{
                "claim_id": "c1",
                "text": "The build skips the LLM when the content hash is unchanged.",
                "evidence_ids": [first],
            }],
            "verifier_levels": ["L0"],
        }})
        print("overall:", receipt.data["overall"])

asyncio.run(main())
```

`scripts/smoke_client.py` runs this same choreography against your broker and prints what each
step proves (`MCP_URL=http://127.0.0.1:8765/mcp/ uv run --project services/mcp-server python
scripts/smoke_client.py`). The verifier's optional semantic check (L3) is off by default and needs
`MCP_ENABLE_ENTAILMENT=1` plus `ENTAIL_LLM_*` creds on the server — see
[07 — Providers and API keys](07-providers-and-api-keys.md).

### The reference implementation: the terminal multi-agent runner

The repo also ships a terminal runner that drives these governed lanes rather than `kb_search`,
routing **deterministically in code**: a *question* runs a read-only EXPLAIN workflow (one pass, a
cited answer with a `file:symbol` Sources footer); a *change* runs a gated pipeline that delegates
to specialists and **pauses for your approval at every hand-off**. It uses an AI model only for
the wording of answers/plans, never for the routing decision. With the broker running and `LLM_*`
set in `.env`:

```sh
# from the repo root, second terminal
set -a; source .env; set +a
export DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb"
export MCP_URL="http://127.0.0.1:8765/mcp/"

uv run --project services/mcp-server python scripts/agent_runner.py \
  "Add input validation to the GitHub connector"
```

You approve each delegation (`[a]pprove / [e]dit / [r]eject / [x]abort`), or pass `--auto-approve`
to run unattended. It prints a `run_id` and a replay command at the end —
`python -m agentic_mcp_server.replay <run_id>` renders that run's ledger as a timeline.

> Pick a strong model: the default `llama-3.1-8b-instant` exercises the plumbing but writes rough
> answers/code; `export LLM_MODEL=llama-3.3-70b-versatile` noticeably sharpens both. The
> write-code lane needs the target's test file in the KB, so build with the default
> `scripts/local-code-sources.yaml` (it indexes tests).

---

## How this was verified

- **`get_task_context`** is scored by a two-arm A/B eval — the same model with the tool vs. file
  tools only — over ten realistic dev tasks (`scripts/eval_task_context.py`,
  `evals/agent_task_cases/task_context_ab_v1.yaml`; hermetic gate in
  `evals/tests/test_task_context_ab.py`; results: `docs/reports/task-context-ab-2026-07-03.md`).
- **The `kb_search` retrieval path** is exercised by the alias-resolution golden set against a
  really built KB (`scripts/eval_alias_resolution.py`, 25/25 top-1 on the last recorded run —
  `docs/reports/alias-accuracy-2026-07-03.md`); `make eval-all` re-runs every tier your shell can
  support ([06 — Observability](06-observability.md)).
- **The governed path** is exercised by `scripts/smoke_client.py` (`create_pack → open_evidence →
  graph.get_neighbors → context.expand → verify_answer → list_retrievals`) against a running
  broker; the live run above retrieved real budget-enforcement code, expanded 3 seeds into 27
  connected cards at 3,871 tokens, and passed all L0 provenance checks.
