# Host integration harness

Executes `docs/runbooks/host-integration-test-plan.md` (the pre-pilot gate) against the two real
host binaries — GitHub Copilot CLI and OpenCode — over the committed host configurations, and
grades the evidence in the reporting discipline of `docs/architecture/evaluation-system.md`
(PASS/FAIL/SKIP-with-reason, verbatim failures, flakes counted separately).

## Layout

| File | Role |
|---|---|
| `run_all.sh` | whole matrix: T1 → Copilot → OpenCode → T5 → grader |
| `preflight.sh` | T1: pins versions, checks the active KB, snapshots ledger/trace baselines |
| `run_copilot.sh` | T2–T4 on Copilot CLI (subject `copilot-cli`) |
| `run_opencode.sh` | T2–T4 on OpenCode (subject `opencode-cli`, Groq provider) |
| `t5_governance.sh` | T5: session-ledger dump, secret scan (counts only), dashboard render |
| `grade.py` | shared grader → `$EVIDENCE_DIR/report.md` with a `GATE VERDICT:` line |
| `common.sh` | shared helpers: server lifecycle (PID file `/tmp/mcp-it.pid`), SQL deltas, retry |

## Committed-config policy (the point of T2)

The test never hand-rolls host policy. Where a CLI cannot consume the committed file in place,
its native config is **generated from** the committed one, substituting only the broker URL
(and, for local-dev auth per ADR-0016, a placeholder bearer):

- **Copilot CLI**: `~/.copilot/mcp-config.json` is generated from
  `.copilot/mcp/repository-settings.json` exactly as docs/dev-guide/how-to/connect-copilot-cli.md documents
  (the committed tool allowlist is preserved verbatim). The pre-existing user file is backed up to the
  evidence dir and restored on exit. The committed agent renderings `.copilot/agents/*.agent.md`
  are installed to `~/.copilot/agents/` (the user-level discovery location named in
  `.copilot/README.md`) with one generation transform — the frontmatter `description:` value is
  YAML-quoted, because Copilot CLI 1.0.63 drops any agent whose plain-scalar description contains
  `": "` (verified live: exactly `orchestrator.agent.md`; finding in the run report) — and removed
  on exit.
- **OpenCode**: the committed `.opencode/opencode.json` + `.opencode/agents/*` are auto-discovered
  at the repo root. Only the broker URL placeholder needs overriding, and a root `opencode.json`
  does NOT win that merge — the supported merge-last mechanism is the `OPENCODE_CONFIG_CONTENT`
  environment variable, generated at runtime from the committed file with only the URL substituted.

Session-scoped exclusions on Copilot (`--disable-builtin-mcps`, `--disable-mcp-server` for the
three `.mcp.json` build-tooling servers) keep the visible surface equal to the committed policy
without editing any tracked file.

## Secrets

`.env` is sourced in-process (`load_secrets`); values are exported into child processes only and
never echoed. The T5 scan greps the *values* across every captured transcript and server log and
reports **counts only** — zero tolerance.

## Running

```sh
EVIDENCE_DIR=/abs/path/to/evidence scripts/integration/run_all.sh

# fix-loop: re-run only named cases on one host (appends a phase interval)
EVIDENCE_DIR=... scripts/integration/run_copilot.sh copilot-t4-explain-1
```

One MCP server instance at a time (the harness refuses to start over a live PID file or a busy
port 8765). The server always runs against `DB_NAME` (default `agentic_kb_full`) with local-dev
auth on loopback. Ledger/trace rows written by the run are evidence and are left in place.

**Phase bookkeeping:** every runner invocation records its `[start, end]` interval + subject in
`$EVIDENCE_DIR/phases/`. T5's graded ledger window is the union of those intervals, each bound to
its subject — so a full host re-run (which archives the host's previous case dirs and intervals)
or a targeted case re-run (which appends an interval) keeps the "per-case deltas sum to the
window" zero-gap check exact, and probe or third-party broker traffic never pollutes the window.

## Flake policy

A case whose host errored **and** whose transcript matches a provider-error signature
(rate limit / 429 / tool_use_failed / Groq's `Failed to call a function` 400 / 5xx) is retried
once; the first attempt is preserved as `transcript.attempt1.*` and the retry is recorded in
`meta.json` (`attempts: 2`). The grader reports flakes separately from failures — a graded
expectation is never retried against.

The OpenCode model is `groq/llama-3.3-70b-versatile` (the platform's documented agent default,
`scripts/kb_agent.py`) — deliberately NOT `.env`'s `LLM_MODEL`, which is the build-plane docify
model. Override with `OPENCODE_MODEL`.
