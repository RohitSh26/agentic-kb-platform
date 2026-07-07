# PR-43 — Ledger-mined alias corrections: the learn loop (ADR-0034)

## Why

ADR-0034: `retrieval_event` already records every miss with the developer's exact `query_text`;
nothing acts on it. This step makes the phrase a developer tried and missed resolve on the next
build — deterministically, from our own governed data.

## Scope (kb-builder only)

- New build step after alias mining: select recent misses from the SAME registry —
  `tool_name='kb_search'`, empty/thin `returned_artifact_ids`, window configurable (default 14
  days). Normalize `query_text` with the existing mining normalizer; length-cap (~80 chars);
  treat as untrusted text (strip control chars; never executed or templated).
- Candidate match: normalized phrase terms vs live artifacts' titles/paths — REUSE the PR-38
  resolve/mining machinery, no new matching code. Match ⇒ emit `alias_reference` rows with
  provenance `ledger_mined`, evidence `{first_seen, last_seen, miss_count}`, `confirmation_count`
  from distinct-day misses. No match ⇒ leave it — it remains an honest gap on the dashboard.
- ACLs: intersection of the targets' ACLs via the existing domain helper — never widened.
- Idempotent + incremental: re-running over the same window creates no duplicates (same
  content-hash/upsert discipline as PR-38); ledger rows are never modified (read-only input).
- Dashboard: `v_retrieval_health` gains the mined-vs-unresolved split (view-only migration, next
  revision, reversible; update `docs/contracts/observability-dashboard.md` first).
- Structured logs: `event=ledger_mining_completed misses_seen=… mined=… unresolved=…`.

## Do NOT

- No LLM calls. No modification of ledger rows. No golden-set involvement anywhere in mining
  (the Goodhart line: usage-mined candidates, never golden-tuned).

## Acceptance

- [ ] Seeded-ledger tests: mining produces the expected aliases; unresolved stays unresolved;
      ACL intersection; length caps + untrusted normalization; idempotent re-run.
- [ ] Alias golden set unchanged and ≥ floor after mining runs (proved in-suite).
- [ ] View migration up→down→up; dashboard renders the split; contracts updated first.
- [ ] kb-builder + evals suites green; ruff/format/pyright clean.
