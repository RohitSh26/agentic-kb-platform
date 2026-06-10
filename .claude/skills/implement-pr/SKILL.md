---
name: implement-pr
description: >
  Workflow for implementing one PR brief from docs/pr-briefs/ end to end. Use when starting any
  numbered PR (PR-01 .. PR-13) so scope, contracts, tests, migrations, and verification happen in
  the right order. Triggers: "implement PR", "start the next PR", /next-pr.
---

# Implement one PR

Follow in order. Do not skip ahead; do not expand scope.

1. **Load scope.** Open `docs/pr-briefs/PR-XX-*.md`. Read the architecture section it references in
   `docs/architecture/00-overview.md` and any ADRs it lists.
2. **Branch.** `git switch -c pr-XX-<slug>`.
3. **Contracts first.** Add or confirm the schemas in `packages/contracts/` the brief depends on.
   Commit these before implementation so the interface is fixed.
4. **Ask the guardian.** Have the `architecture-guardian` subagent review your intended approach
   against the invariants before writing code.
5. **Implement** only the files the brief lists. Use the interfaces (`SearchClient`, `ModelClient`)
   — never call Azure or a model endpoint directly from a tool.
6. **Migrations** (if schema changes): use the `write-migration` skill; include a real downgrade.
7. **Tests** in the same change via the `test-author` subagent. Cover budgets, dedupe, cache hits,
   evidence expansion, idempotency, and incremental skips where relevant.
8. **Verify**: run `/verify` (ruff + pyright + pytest). Fix until green.
9. **Self-review**: re-run `architecture-guardian`; for PR-09/10/13 also run `mcp-contract-reviewer`
   and `security-auditor`.
10. **PR description**: restate the brief's acceptance criteria as a checklist with results, list
    migrations + rollback notes, and record any new open questions. Then push (asks for confirmation).

If at any step the brief is ambiguous or under-specified, write the open question down and ask the
human rather than guessing.
