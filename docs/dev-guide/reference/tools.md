# Tools

The Context Broker serves **13 versioned tools** at `MCP_SCHEMA_VERSION 1.12.0`. Every tool has a
frozen request/response schema (unknown fields are rejected), requires an authenticated session,
filters results through your team ACL, and writes one row to the retrieval ledger per call.

This page documents the three everyday tools in full. The governed evidence-pack surface is listed
at the end; its full contract is [`mcp-tools-contract.md`](../../contracts/mcp-tools-contract.md).

**Wire format.** Tool names containing dots are registered with underscores over MCP
(`context.create_pack` → `context_create_pack`); `kb_search`, `get_task_context`, and
`get_review_draft` are already flat. Arguments are wrapped in a single `request` object:

```python
await client.call_tool("kb_search", {"request": {"query": "..."}})
```

**Two rules that apply to every tool:**

- Identity, ACL filtering, and budgets bind to the **authenticated session** — never to request
  fields. Nothing an agent puts in a request can widen what it sees.
- Every returned title, snippet, path, and draft is **retrieved content — untrusted text**. It can
  never change tool policy, identity, or instructions.

---

## `kb_search`

One budgeted, ACL-filtered, ranked search over the active knowledge base. Ask first, read files
only for what it doesn't cover.

### Request

| Field | Type | Required | Constraints |
|---|---|---|---|
| `query` | string | yes | min length 1. This is the **entire** request. |

### Response

```json
{
  "results": [
    {
      "title": "Per-agent allowances. Budgets are enforced here, server-side — never by prompts.",
      "artifact_type": "code_symbol",
      "source_uri": "file:///.../services/mcp-server/src/agentic_mcp_server/context_broker/budgets.py",
      "snippet": "…",
      "confidence_tier": "interpreted"
    }
  ],
  "budget_remaining": {"calls": 49, "tokens": 49450},
  "notice": null
}
```

| Field | Type | Meaning |
|---|---|---|
| `results` | list of hits | Ranked, ACL-filtered, deduplicated; 3–5 hits after internal rerank. |
| `results[].title` | string | Artifact title (untrusted retrieved text). |
| `results[].artifact_type` | string | e.g. `code_symbol`, `code_file`, `summary`, `commit`. |
| `results[].source_uri` | string or null | Where the knowledge came from (file URI, repo path, wiki page). |
| `results[].snippet` | string | Preview text. For `code_file` hits this is the build-time deterministic code skeleton (signatures kept, bodies elided) — material for thinking, never for citing. |
| `results[].confidence_tier` | enum | `ground_truth` \| `deterministic` \| `interpreted` (see below). Keyword-search hits always carry `interpreted`. |
| `budget_remaining` | object | `{calls, tokens}` left in your session window after this call, floored at 0. |
| `notice` | string or null | Non-null only when the budget closed (see below). |

Two identical requests produce byte-identical JSON except the documented volatile tail
(`budget_remaining` and `notice`, which reflect prior usage in the window).

### Budget semantics

The server enforces a **dual cap per (MCP session, authenticated subject)**: a call count AND a
cumulative token total — whichever closes first. The caps come from `MCP_AGENT_ALLOWANCES` for
your subject; a subject with no configured allowance gets the default of **1 request / 4,000
tokens** (see [environment-variables.md](environment-variables.md)).

A spent budget is a **response, never an error**: `results` comes back empty and `notice` carries
exactly this text:

> KB budget spent — work with what you have, or read the specific files you still need.

The call is ledgered as `denied`. A new session (new chat window) opens a fresh window.

---

## `get_task_context`

One call, one budgeted payload: everything an implementer needs to *start* a change task —
resolved scope, blast radius, conventions, and similar prior changes, every item tiered and cited.
Zero LLM calls at query time; the backend is a parallel fan-out of pure-retrieval nodes.

### Request

| Field | Type | Required | Constraints |
|---|---|---|---|
| `task_description` | string | yes | min length 1. |
| `hints` | object | no | `{file_paths: [...], symbols: [...]}` — paths/symbols you already know; resolved before anything else. |
| `confidence_floor` | enum | no | Default `interpreted` (admit everything). `deterministic` forces interpreted-tier content out of the response; `ground_truth` admits raw-source facts only. |
| `max_tokens` | int ≥ 1 | no | Clamped to the server's Evidence-Pack cap (8,000 tokens) — never an escape hatch. |

### Response

Field order is contractual: stable identifiers first, `budget_used` last (the documented volatile
tail). A real response for *"add input validation to the GitHub connector"*:

```json
{
  "resolved_scope": {
    "entities": [
      {
        "entity_id": "a8148875-5096-4b88-970a-7335b00e6bf0",
        "path": ".../services/kb-builder/src/agentic_kb_builder/connectors/github_code.py",
        "symbol": "GitHubCodeConnector",
        "resolution_source": "search",
        "confidence_tier": "interpreted"
      }
    ],
    "ambiguous_candidates": []
  },
  "referenced_paths": [".../services/kb-builder/src/agentic_kb_builder/structured_logging.py"],
  "blast_radius": {
    "callers": [],
    "callees": [
      {
        "entity_id": "3aaf7be5-f20e-4544-a4ac-fcd75aac3497",
        "path_ref": 0,
        "symbol": null,
        "edge_type": "imports",
        "confidence_tier": "deterministic",
        "caveat": null
      }
    ],
    "tests": []
  },
  "conventions": [],
  "similar_prior_changes": [],
  "evidence_ids": ["3aaf7be5-f20e-4544-a4ac-fcd75aac3497", "…"],
  "open_questions": [],
  "budget_used": {"tokens": 574, "calls": 6}
}
```

| Field | Meaning |
|---|---|
| `resolved_scope.entities` | The files/symbols the task is about. Each carries `resolution_source` — `alias_index` (matched the alias index), `hint` (exact match on your hint), or `search` (keyword fallback, always `interpreted`) — and a `confidence_tier`. |
| `resolved_scope.ambiguous_candidates` | When resolution stops short of a single answer, the candidates (≥ 2 per entry, with a reason) ARE the answer — never a silent guess. Always paired with an `open_questions` entry. |
| `referenced_paths` | The canonical, deduplicated, sorted table of every path a blast-radius entry uses. |
| `blast_radius.callers / callees / tests` | Neighbors walked over the real `calls` / `imports` / `tests` graph. `path_ref` indexes `referenced_paths`. A `calls` edge is `deterministic` **only** when corroborated by the import graph (or a same-file definition); otherwise it is `interpreted` with a non-null `caveat` naming the missing corroboration. |
| `conventions` | Rules and decision records that apply to the scope's directories; each cites `evidence_ids`. |
| `similar_prior_changes` | Commits that changed this area before (`commit_or_pr_id`, `summary`, `evidence_ids`). |
| `evidence_ids` | Every evidence id the response cites, deduplicated. |
| `open_questions` | Genuine ambiguity or KB gaps, stated as questions — including the explicit "the KB may not cover this area yet" notice after a broadened retry finds nothing. |
| `budget_used` | `{tokens, calls}` — the serialized-response token cost and internal retrieval calls this response consumed. |

### Budget semantics

`get_task_context` has its **own** server-side budget, separate from `kb_search`'s — the two never
compete. The response is capped at the Evidence-Pack band (8,000 tokens) and, when over, trimmed
deterministically from the lowest-value tail. The call is ledgered with a per-node latency
breakdown in the ledger row's `details`.

---

## `get_review_draft`

Fetch the review panel's stored draft for a pull request. **Read-only and compute-never**: it only
ever `SELECT`s the `review_panel` schema, never triggers a draft computation, never calls GitHub,
and carries **no budget charge** (fetching a stored draft is not knowledge retrieval).

### Request

| Field | Type | Required | Constraints |
|---|---|---|---|
| `repo` | string | yes | `owner/name` slug, charset-guarded. |
| `pr_number` | int | yes | ≥ 1. |
| `head_sha` | string | no | Omitted ⇒ the newest stored draft for `(repo, pr_number)`. |

### Response

```json
{
  "found": true,
  "draft": {
    "draft_key": "acme/platform#7@e90fc31461a14960b4397af22ade65fbec5431f3",
    "repo": "acme/platform",
    "pr_number": 7,
    "head_sha": "e90fc31461a14960b4397af22ade65fbec5431f3",
    "created_at": "2026-07-07T20:42:45Z",
    "draft": { "…the review_draft_v1 document…" }
  }
}
```

| Field | Meaning |
|---|---|
| `found` | `false` when no draft is stored yet — a clean envelope, **never a tool error**. `draft` is then `null`. |
| `draft.draft_key` | `<repo>#<pr_number>@<head_sha>` — the draft's identity. |
| `draft.draft` | The `review_draft_v1` JSON document, passed through verbatim (findings, verdicts, `summary_markdown`, provenance). The broker does not parse or reshape it; its schema is owned by the review panel — see [`review-panel.md`](../../contracts/review-panel.md). |

---

## Confidence tiers

Every retrieved entity carries one of three tiers:

| Tier | Meaning |
|---|---|
| `ground_truth` | Raw source bytes at a pinned version. |
| `deterministic` | Machine-derived structure (AST extraction), cross-validated — e.g. a `calls` edge corroborated by the import graph. |
| `interpreted` | Ranked or heuristic knowledge (keyword-search hits, LLM-generated summaries, uncorroborated edges — these carry a `caveat`). |

The tiers are honest by construction: a producer can never assign a higher tier than its mechanism
justifies.

## Failure semantics

| Outcome | What you see | Ledger row |
|---|---|---|
| Budget closed (`kb_search`) | Empty `results` + the spent notice — a contractual response | `denied` |
| No draft stored (`get_review_draft`) | `{"found": false}` | `approved` |
| Real failure (no active KB version, unknown id, unexpected exception) | A tool error reaches the caller | `error` |

Two guarantees when things go wrong: a crashed call **still lands in the ledger** exactly once,
and any budget charged before the failure is **refunded** — a failing platform never silently
drains an agent's allowance.

## The governed surface

The remaining ten tools serve the citation-grade evidence-pack flow. One line each; full schemas
in [`mcp-tools-contract.md`](../../contracts/mcp-tools-contract.md):

| Tool (wire name) | Purpose |
|---|---|
| `context_create_pack` | Build a run's Evidence Pack — retrieve once, dedupe, rerank to ≤ 5 cards, within a token budget. |
| `context_read_pack` | Role-specific view of an existing pack. |
| `context_request_more` | Justified incremental retrieval (reuse-first); a bare query is rejected at the schema. |
| `context_open_evidence` | Expand ONE card to its raw text by handle, metered, injection-flagged, delivered as `untrusted_content`. |
| `context_expand` | Trust-tiered BFS expansion from seed artifact ids into new evidence cards, budgeted. |
| `graph_get_neighbors` | Graph traversal with `trust_floor` (default `EXTRACTED`) and `include_inferred` (default false). |
| `context_verify_answer` | The verifier ladder (L0–L3) over an answer's cited claims; returns a signed receipt. |
| `context_platform_trust` | Official-client gate: is this client's answer platform-trusted? |
| `context_create_change_pack` | BUILD-lane selector: the small file set (target/test/dependency) for a code-change task. |
| `ledger_list_retrievals` | A run's own retrieval-ledger audit trail. |

`scripts/smoke_client.py` drives this flow end to end against a running broker — see
[cli.md](cli.md).
