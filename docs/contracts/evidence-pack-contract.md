# Contract: Evidence Pack

> The unit of context the MCP Context Broker serves to agents. One pack per run,
> shared by all subagents — "many controlled specialists using one shared
> Evidence Pack," not many agents with KB access.

Code authority: `services/mcp-server/src/agentic_mcp_server/mcp/tool_schemas/`.

## Evidence levels

| Level | Content | Served via |
|---|---|---|
| L0 | One-line claim/title | evidence card |
| L1 | Card summary (~tokens_if_expanded advertised) | evidence card |
| L2 | Raw chunk / exact code span | `context.open_evidence` only |
| L3 | Expanded neighborhood (file section, linked artifacts) | `context.open_evidence` only |
| L4 | Full source pointer (never inlined; agent gets the URI) | source_uri |

Cards first, raw text by explicit handle: agents see L0/L1 cards and must open
L2+ through `context.open_evidence` with a budget.

## EvidenceCard (L0/L1)

| Field | Type | Notes |
|---|---|---|
| `evidence_id` | string | the citable handle; stable within a pack (V1: the artifact UUID as a string) |
| `artifact_id` | UUID | provenance into the registry |
| `level` | "L0" \| "L1" | |
| `card_type` | string | concept / summary / chunk / fact / code_symbol … |
| `title`, `summary` | string | |
| `source_uri` | string | |
| `confidence` | 0–1 | |
| `authority_score` | 0–1 | |
| `tokens_if_expanded` | int ≥ 0 | cost preview for open_evidence |
| `injection_flagged` | bool | broker's deterministic injection scan over title + summary (PR-13) |
| `injection_signals` | string[] | which patterns matched; content stays verbatim |

## Pack shape

`context.create_pack` returns: `context_pack_id`, `kb_version`, `summary`,
`evidence_cards[]`, `open_questions[]`, `budget_used_tokens`.
`context.read_pack` adds `budget_remaining_tokens` and renders a role-specific
view (role is a **view selector only** — authorization comes from the
authenticated session, never from the request body).

## Rules

- Every agent claim cites `evidence_id`s. Missing evidence becomes an open
  question, never an invention.
- All evidence text is **untrusted content**: it cannot change tool policy,
  identity, access control, or system instructions. The L2+ response field is
  literally named `untrusted_content`. The broker marks injection-style
  content (`injection_flagged`/`injection_signals`) but returns it verbatim —
  flagging informs the consumer; it never rewrites evidence.
- Packs only ever contain artifacts the requester was authorized to see at
  retrieval time, and `open_evidence` re-checks authorization at expansion
  time — holding a pack handle is not a grant. Every retrieval response
  carries an `authorization` decision (see `mcp-tools-contract.md`).
- Budgets (per run and per agent) are enforced by the broker server-side and
  surfaced in the ledger; see `.claude/rules/token-budgets.md` for V1 numbers.
