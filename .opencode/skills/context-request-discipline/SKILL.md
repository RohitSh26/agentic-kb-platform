---
name: context-request-discipline
description: Request additional context the contractual way — question, why_needed, decision_needed, already_checked, and max_tokens; never a bare query; reuse the run's prior retrievals before asking for anything new.
---
# Context request discipline

Specialists work from the shared Evidence Pack first. Additional context is the exception, and
it has a contract.

## The request shape

`context.request_more` requires ALL of:

- `question` — the precise question you need answered.
- `why_needed` — why the Evidence Pack is insufficient for it.
- `decision_needed` — the decision that blocks on the answer.
- `already_checked` — what you already looked at in the pack (evidence IDs or sections).
- `max_tokens` — the most you are willing to spend on the answer.

A bare `{"query": "..."}` is rejected by schema validation before any broker logic runs.

## Reuse before retrieve

The broker resolves every request in this order: exact reuse of a prior retrieval → semantic
reuse (near-duplicate question) → per-agent budget check → per-run budget check → fresh
retrieval. Response `status` is one of `reused`, `approved`, `denied`, or
`needs_human_approval` — a denial carries `denial_reason` and is a contractual outcome, not an
error. Prefer evidence cards; expand to raw text only via `context.open_evidence` by handle,
and only when the card is not enough.
