# ADR-0034 — Ledger-mined corrections: the KB learns from its own misses

- Status: Accepted
- Date: 2026-07-07
- Deciders: platform owner (same directive as ADR-0033), Claude

## Context

Every retrieval is already ledgered — including the misses: `retrieval_event` rows where
`kb_search` returned zero/thin results carry the developer's exact `query_text`. Today that signal
is only *observed* (the dashboard's KB-gap proxy); nothing acts on it. Headroom's `learn` command
(mines failed sessions, writes corrections) validates closing this loop automatically — but our
version should mine **our ledger**, which is structured, ACL-attributed, and already the system of
record, rather than session transcripts.

## Decision

A deterministic, zero-LLM **correction-mining step in the nightly build**: read recent
`retrieval_event` misses (zero/thin `kb_search` results), normalize their `query_text`, and where
the phrase's terms match existing artifacts (title/path matching, reusing the PR-38 alias
machinery), emit `alias_reference` rows with provenance `ledger_mined` — so the exact phrase a
developer tried and missed **resolves on the next build**. Repeat misses raise
`confirmation_count`. Guardrails: ACLs are the intersection of the targets' ACLs (never widened —
the standing rule); mined phrases are length-capped and normalized (a query is a search string,
not a document — but treat it as untrusted content regardless); idempotent and content-hash-gated
like every build step; misses that match nothing remain what they are today — an honest gap
signal on the dashboard, now with a "mined vs unresolved" split.

Success metric: the KB-gap proxy rate falls build-over-build on real usage, and the alias golden
set stays ≥ its floor (mining must never degrade hand-verified accuracy — the Goodhart line
applies: mining creates *candidates from real usage*, it never tunes against the golden set).

## Consequences

- The feedback loop the architecture always promised ("file-fallback is a KB-gap signal") becomes
  mechanical instead of aspirational.
- Ledger `query_text` surfaces into searchable alias titles: bounded by the ACL-intersection rule
  and normalization, and worth a privacy note in the contract (developers' query phrasings become
  org-visible aliases only when they match org-visible artifacts).
- One new build step; no new stores; no schema change expected (alias_reference rows).

## Alternatives considered

- **Mine host session transcripts (Headroom's approach)** — rejected: we don't own host sessions;
  the ledger is richer, structured, and already governed.
- **LLM-suggested corrections** — rejected for v1: the deterministic term-match lane must prove
  itself first (same sequencing as PR-38's zero-LLM mining).
