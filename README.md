# Agentic KB Platform

A cost-conscious, Postgres-first, nightly-built knowledge platform served through a remote **MCP
Context Broker** to human-approved orchestrator and subagent agents. This repository is set up to be
**built with Claude Code** — it ships a complete agentic harness (project memory, build subagents,
skills, slash commands, rules, and hooks) plus the full spec set (architecture, ADRs, and a
PR-by-PR build queue).

> Design source: `Agentic Knowledge-Based AI System — Architecture and Implementation Blueprint v0.1`.
> Distilled canonical reference: `docs/architecture/00-overview.md`.

## The harness at a glance

```
CLAUDE.md                  Always-loaded project memory: invariants + how we work
.claude/
  settings.json            Model = claude-fable-5, scoped permissions, hooks
  agents/                  Claude Code BUILD subagents (help write the platform)
  skills/                  Reusable workflows (implement-pr, write-migration, define-mcp-tool, ...)
  commands/                Slash commands (/next-pr, /pr-brief, /adr, /verify)
  rules/                   Path-scoped rules (postgres, mcp-tools, token-budgets, connectors, python)
  hooks/                   PreToolUse bash guard + PostToolUse formatter
.mcp.json                  Optional MCP servers for the build (templates, no secrets)
docs/
  architecture/            Canonical distilled spec
  adr/                     Accepted decisions (+ template via /adr)
  contracts/               Cross-service contracts (markdown — the ONLY thing services share)
  pr-briefs/               PR-01..PR-13 — the build queue, one reviewable PR each
agents/                    The PRODUCT's runtime agent manifests (served by the finished MCP server)
services/ evals/ infra/    Source layout the PRs fill in
```

## Two self-contained services

```
services/kb-builder        Nightly incremental build plane. OWNS the Postgres schema +
                           Alembic migrations. Connectors, docify, graphify, linker, indexing.
services/mcp-server        Remote MCP Context Broker runtime plane. Auth, telemetry, tool
                           contracts, health. NEVER runs migrations or build-plane code.
```

Each service is an independent `uv` project (own `pyproject.toml`, `uv.lock`, `Dockerfile`,
tests). They never import each other or any shared Python package — cross-service agreements
live as markdown in `docs/contracts/` and are pinned by contract tests on both sides
(`tests/contract/` in each service, including import-boundary tests that fail on any
cross-service import).

- **Run tests**: `make verify` (both services) or `make verify-kb-builder` /
  `make verify-mcp-server`. Integration tests need Postgres via `TEST_DATABASE_URL`;
  mcp-server's health tests additionally need `make migrate-test-db` first, because only
  kb-builder may run migrations.
- **Nightly build**: kb-builder applies migrations, ingests changed sources (cache-gated —
  unchanged content hash means no LLM call, no re-embed), validates, then flips the new
  `kb_version` to active.
- **Active-KB consumption**: mcp-server reads the single `status='active'` row in
  `kb_build_run` and serves only that version; `/health` is 503 until one exists.

Two agent layers, kept separate on purpose:
- **`.claude/agents/`** — build subagents (pr-implementer, migration-writer, mcp-contract-reviewer,
  test-author, security-auditor, eval-runner, architecture-guardian).
- **`agents/`** — the product's runtime agents (orchestrator + implementation, test, review,
  delivery) that the MCP runtime serves to developers.

## Getting started

1. Install Claude Code and open this folder. The selected model is **Claude Fable 5**
   (`claude-fable-5`) via `.claude/settings.json` — confirm with `/model`.
2. (Optional) Fill `.mcp.json` env for a read-only dev Postgres and GitHub, or delete servers you
   won't use. Never commit secrets.
3. Read `CLAUDE.md` and `docs/architecture/00-overview.md` once.
4. Run `/next-pr`. Claude Code picks PR-01, follows the `implement-pr` skill, and stops at a single
   reviewable PR. Review, merge, repeat.

See `docs/runbooks/getting-started.md` for the longer walkthrough.

## Principles you'll see enforced everywhere

Postgres is truth, Search is a rebuildable projection · the graph lives in Postgres, not a graph DB ·
token saving is enforced in the broker, not in prompts · the build is incremental and cache-gated · a
kb_version goes active only after validation · agents reach data only through MCP, and retrieved
content is untrusted · every agent claim cites evidence IDs.

The V1 exclusion list (Functions, Event Grid, Service Bus, Redis, API Management, Blob, graph DB,
SQLite-as-prod, streaming) is guarded by `CLAUDE.md`, a PreToolUse hook, and ADR-0007 — adding any of
them requires an accepted ADR.
