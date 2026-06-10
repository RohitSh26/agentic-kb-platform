# Getting started with Claude Code on this repo

## Prerequisites
- Claude Code installed (`npm install -g @anthropic-ai/claude-code`).
- A Python 3.12 toolchain via `uv`.
- (Optional) a disposable dev Postgres for migration/model tests, and a scoped GitHub token.

## First session
1. `cd agentic-kb-platform && claude`.
2. Verify the model: `/model` should show **claude-fable-5** (set in `.claude/settings.json`). For a
   one-off cheaper exploration session you can switch, but the default build model is Fable.
3. Skim `CLAUDE.md` and `docs/architecture/00-overview.md`. Everything else loads on demand.
4. `/next-pr` → Claude selects PR-01 and runs the `implement-pr` workflow.

## The loop per PR
- Claude branches, fixes contracts first, asks `architecture-guardian` to sanity-check the approach,
  implements only the brief's scope, adds tests (via `test-author`), writes migrations (via
  `write-migration`) with rollbacks, then runs `/verify`.
- For PR-09/10/13 it also runs `mcp-contract-reviewer` and `security-auditor`.
- It ends with a PR description: acceptance criteria as a checklist, rollback notes, open questions.
- You review and merge. Then `/next-pr` again.

## Useful commands
- `/verify` — lint + format-check + types + tests; the done-gate.
- `/pr-brief <desc>` — turn a new need into a bounded brief.
- `/adr <decision>` — record a decision; required before adding any V1-excluded resource.

## When Claude wants to add Redis / Blob / a graph DB / Functions
That's blocked by design. Have it run `/adr` to justify the addition with a concrete trigger
(a metric, a failing eval, a cost signal). Default answer is "not yet."

## Tips
- Keep `CLAUDE.md` short; put procedures in skills and path rules. If you find yourself repeating an
  instruction every session, it belongs in `CLAUDE.md`; if it's an occasional procedure, make it a skill.
- Let the **Explore** built-in subagent or `architecture-guardian` do heavy reading so the main
  context stays lean — this directly serves the platform's own token-discipline ethos.
- Subagent-heavy runs cost more tokens; reach for them when isolation or parallelism pays off, not for
  trivial one-shots.
