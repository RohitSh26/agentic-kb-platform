# Pilot checklist — first developers on the platform (local, per-machine)

> Audience: whoever runs the pilot (2–3 developers, a few days) and the developers themselves.
> The system is **pilot-ready**: bootstrap proven on a clean clone, docs verified command-by-command,
> all gates green, observability self-hosted. What a pilot proves is the one thing automation
> cannot: real people, on machines we've never seen, doing real work.

## For each pilot developer (~15 minutes to productive)

1. **Bootstrap** — follow `docs/dev-guide/00-quickstart.md`: prerequisites (git, uv, Python 3.12,
   Postgres), then `./scripts/bootstrap.sh`. Expected: `build status : active` and a 25/25 alias
   smoke check. No credentials needed for the default build.
2. **Optional full build** (doc summaries): put a Groq key in repo-root `.env`
   (`LLM_PROVIDER=groq`, `LLM_API_KEY=...`), rerun with `--with-docs`.
3. **Serve + connect** — `docs/dev-guide/05` to run the MCP server, then your host:
   OpenCode (`.opencode/` ships ready), VS Code Copilot agent mode (`.vscode/mcp.json`),
   or Copilot CLI (`docs/dev-guide/09`). The tool surface is `kb_search` + `get_task_context`.
4. **Work normally.** Ask real questions about the codebase; start real tasks. When something
   feels wrong or slow, keep going — the ledger records it; don't self-censor.

## What the pilot runner watches (daily, 5 minutes)

- `make dashboard` — retrieval health (error rate, the zero-result KB-gap proxy), token spend
  per agent, build health, budget breaches.
- Slowest spans: `SELECT name, count(*), round(avg(extract(milliseconds from ended_at-started_at)))
  FROM trace_span GROUP BY 1 ORDER BY 3 DESC;` (baseline: every node ≤ ~800ms; root p50 ≈ 0.9s).
- Friction log: every "I had to fall back to reading files" moment is a KB gap — collect the
  actual phrasings; they become new alias/eval cases.

## Exit criteria — "ready for the team"

- [ ] All pilot machines bootstrapped without hand-holding (doc gaps found → fixed same week).
- [ ] Each developer completed ≥1 real task where `get_task_context` demonstrably shortened the
      path (ledger + their own account agree).
- [ ] Zero budget/ACL surprises in the dashboard; error rate ~0 outside induced failures.
- [ ] The friction log is empty OR every item is a filed task.

## Known limits to set expectations on (all documented, none blockers)

- Rebuilds are manual locally (`bootstrap.sh` rerun is incremental and fast); nightly scheduling
  arrives with the cloud move (ADR-0004).
- The review draft engine runs via CLI (`docs/dev-guide/06`); the in-chat draft-fetch MCP tool is
  PR-41 (queued).
- One builder per registry (advisory lock aborts a second, loudly).
- Cloud/multi-user (production connectors, team auth, scheduling) is the layer-2 track — nothing
  in the local pilot is throwaway; the same schema, tools, and configs move as-is.
