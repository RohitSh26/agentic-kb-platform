# 03 — Local testing (no Azure required)

> Everything implemented so far runs on a laptop with **uv + a local Postgres**. No Azure Search,
> no Azure OpenAI, no cloud credentials. This is by design: every external system sits behind a
> `Protocol` (see doc 01, "External systems sit behind interfaces"), so tests inject in-memory
> fakes and the only real infrastructure a test ever touches is Postgres.

## Prerequisites

- **uv** (manages Python 3.12 per service): `brew install uv`
- **Postgres 16** running locally (Homebrew: `brew install postgresql@16 && brew services start
  postgresql@16`; or Docker: `docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres
  postgres:16`)

One-time setup (each service is its own `uv` project — ADR-0008):

```sh
make sync                     # uv sync in services/kb-builder, services/mcp-server, and evals
createdb agentic_kb_test      # dedicated test database (any name works)
```

## The verify gate

This is the definition of "done" for any change. CI (`.github/workflows/ci.yml`) runs one job per
service with the same steps against a Postgres 16 service container:

```sh
make verify                   # lint + types + tests for all three projects (both services + evals)
make verify-kb-builder        # or just one
make verify-mcp-server
make verify-evals
```

The Makefile's default `TEST_DATABASE_URL` assumes a `postgres:postgres` role (matching CI).
Homebrew/macOS Postgres creates a role named after your OS user instead — pass the URL on the
command line (or export it once in your shell):

```sh
make verify TEST_DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb_test"
```

Or by hand, inside a service directory (`services/kb-builder` or `services/mcp-server`):

```sh
uv run ruff check . --fix && uv run ruff format .          # lint + format
uv run pyright                                              # types (strict on domain/infrastructure/tool schemas)
uv run env TEST_DATABASE_URL=postgresql+asyncpg://$USER@localhost:5432/agentic_kb_test pytest -q
```

Adjust the URL for your auth (CI uses `postgres:postgres@localhost:5432/postgres`). The driver
**must** be `postgresql+asyncpg://` — everything is async SQLAlchemy.

One asymmetry to know: **mcp-server never runs migrations** (kb-builder owns the schema), so its
DB-backed tests expect an already-migrated database. Run `make migrate-test-db` once (it executes
kb-builder's Alembic migrations against `TEST_DATABASE_URL`); if the schema is missing, those
tests skip with a message telling you exactly that.

## Docker: the whole system with one command

If you prefer containers over a local Postgres + uv, the root `docker-compose.yml` spins up the
full system (PR-17):

```sh
docker compose up --build
```

Three containers, in order:

1. **postgres** — Postgres 16 with a named volume; host port `55432` by default (NOT 5432, so a
   Homebrew Postgres keeps working — override with `POSTGRES_HOST_PORT`).
2. **kb-builder** — one-shot: applies the Alembic migrations and exits (it owns the schema,
   ADR-0008). There is no build CLI yet, so migrations are this container's whole job.
3. **mcp-server** — starts only after the migration job completes; serves
   `http://localhost:8000/mcp/` (override with `MCP_HOST_PORT`). It never runs migrations.

Honesty notes, both by design:

- `GET http://localhost:8000/health` answers **503 `no_active_kb_version`** on a fresh volume —
  there is no built KB yet. That is readiness honesty, not a failure.
- The compose file boots with **placeholder Entra identifiers** (`MCP_ENTRA_TENANT_ID` /
  `MCP_ENTRA_AUDIENCE` — identifiers, never secrets). Auth is fail-closed (invariant 6, no
  auth-off switch), so no bearer token verifies until you export a real tenant id and audience.

The compose invariants — exactly three services, kb-builder as the only migration runner, the
dependency order, asyncpg URLs, no inline credentials — are pinned by
`services/kb-builder/tests/contract/test_compose_contract.py`.

To reach the compose Postgres from the host (psql, tests):
`postgresql+asyncpg://postgres:postgres@localhost:55432/agentic_kb`.

## How DB tests work

- Tests read `TEST_DATABASE_URL` (falling back to `DATABASE_URL`). If neither is set, DB-backed
  tests **skip gracefully** with a clear reason — pure unit tests still run, so `uv run pytest`
  with no env gives you a fast partial signal.
- kb-builder's DB-backed modules (`services/kb-builder/tests/integration/test_build_engine.py`,
  `test_linker.py`, `test_indexing.py`, `test_registry_roundtrip.py`) run **Alembic migrations to
  head** in a module fixture against your test DB and **downgrade to base on teardown** — every
  migration's downgrade is therefore exercised on every test run. mcp-server's DB-backed tests do
  *not* migrate (see above) — they only read/write `kb_build_run` via raw SQL.
- Between tests, a session fixture deletes table contents in FK-safe order; tests own their data.
- Caveat: Alembic's `fileConfig()` disables already-imported loggers, so tests that assert on log
  records re-enable the specific loggers they capture after running migrations (see
  `test_linker.py`'s `migrated_db` fixture if you write a new log-asserting DB test).

Running migrations by hand (useful when developing a revision):

```sh
cd services/kb-builder
export DATABASE_URL=postgresql+asyncpg://$USER@localhost:5432/agentic_kb_test
uv run alembic upgrade head
uv run alembic downgrade -1    # verify the rollback
uv run alembic upgrade head
```

## Where the fakes are — and what they replace

| Real system (V1 target) | Interface (Protocol) | Defined in | Fake used in tests |
|---|---|---|---|
| GitHub / Azure Wiki / ADO APIs | `FetchBackend` | `connectors/source_connector.py` | in-test backends returning canned text (`test_connectors.py`, `test_build_engine.py`) |
| Azure OpenAI (wikify) | `ModelClient` / `Wikifier` | `infrastructure/azure_openai/model_client.py`, `application/build_runner.py` | spy wikifiers returning canned `WikifyGeneration` (`test_build_engine.py`) |
| Code parser | `Graphifier` | `application/build_runner.py` | spies returning fixture `FileGraph`s |
| Azure OpenAI embeddings | `Embedder` | `application/build_runner.py` | fake returning a deterministic `embedding_hash` |
| Azure AI Search (upsert) | `SearchIndexer` | `application/build_runner.py` | `SpyIndexer` counting received artifact ids |
| Azure AI Search (index) | `SearchClient` | `infrastructure/azure_search/search_client.py` | `FakeSearchClient` — a real in-memory index holding full `SearchDoc`s; tests inject drift/orphans by mutating `.docs` (`test_indexing.py`) |
| Azure AI Search (similarity) | `SimilarityProvider` | `linker/semantic.py` | `FakeSimilarityProvider` with canned scores (`test_linker.py`) |

(Paths are relative to `services/kb-builder/src/agentic_kb_builder/`.)

The pattern for every fake is the same: implement the Protocol in the test module, record calls
and arguments, return deterministic canned data. If you add a new external dependency, add a
Protocol next to its caller and a fake next to its tests — never import an SDK in library code.

## Running an end-to-end build locally

There is no CLI entry point yet (it arrives with the nightly workflow). The supported way to run
the full pipeline locally **is the integration test suite** — `test_build_engine.py` constructs a
real `BuildRunner` against your local Postgres with fake connectors/model/embedder/indexer and
exercises the complete flow: fetch → hash-skip → wikify (cache-gated) → graphify (cache-gated) →
embed (cache-gated) → index → edges → linker → run accounting → activation gating.

To watch a full build happen, run one test verbosely with log output:

```sh
cd services/kb-builder
uv run env TEST_DATABASE_URL=postgresql+asyncpg://$USER@localhost:5432/agentic_kb_test \
  pytest tests/integration/test_build_engine.py -k "first_build" -v --log-cli-level=INFO
```

The structured `event=...` log lines (source upserts, cache lookups/hits, wikify/graphify writes,
linker matching, edge reconciliation) narrate every step — they are the same lines you would grep
in production logs.

To experiment interactively, copy the fixture pattern from `test_build_engine.py` into a scratch
script: build a `BuildRunner(session, kb_version=..., wikifier=fake, graphifier=fake,
embedder=fake, indexer=fake)` and feed it a connector whose `FetchBackend` returns whatever
markdown/code you want ingested. Inspect results directly in Postgres:

```sql
select artifact_type, title, knowledge_kind, kb_version from knowledge_artifact;
select edge_type, confidence, source from knowledge_edge order by edge_type;
select kb_version, status, llm_calls, embedding_calls from kb_build_run;
```

## What each test file covers

All under `services/kb-builder/tests/`:

| File | Covers |
|---|---|
| `integration/test_build_engine.py` | cache-key determinism; first build vs unchanged-skip; **cache hit ⇒ no model call** (invariant 4); idempotent re-runs; failed-wikify rollback (audit row survives, no orphan cache rows); cross-file graphify edges; validation-gated activation (invariant 5) |
| `integration/test_linker.py` | deterministic matching + precision guards; semantic threshold and dedupe; reconcile-in-place (rerun refresh, new-version refresh, stale deletion); protected-edge survival without a provider; low-confidence flagging |
| `integration/test_indexing.py` | projection field mapping + stale-embedding exclusion; changed-docs-only upsert (no duplicates on rerun); orphan-doc deletion; consistency check passing on a mirrored index and failing on each injected drift class |
| `integration/test_wikify.py` | chunker determinism and packing; draft kind/type validation |
| `integration/test_graphify.py` | parse validation; key round-trips; exact spans; span-past-EOF rejection; edge confidences |
| `integration/test_registry_roundtrip.py` | migrations up/down + model round-trips |
| `unit/test_connectors.py` | normalize+hash pipeline determinism per source type |
| `contract/test_import_boundaries.py` | no cross-service or legacy root-package imports (ADR-0008) |

mcp-server's suite mirrors the same split (`services/mcp-server/tests/{unit,integration,contract}`)
— see doc 02 §10 for what each layer exercises.

Write new tests in the same spirit: budgets, dedupe, cache hits, idempotency, and failure paths —
not just happy-path success (see CLAUDE.md "Tests ship in the same PR").

## What stays fake until later PRs

- Since **PR-08**, `FakeSearchClient` is the local stand-in for the *whole* index: projection,
  changed-only upsert, orphan deletion, and the drift consistency check all run offline. Azure is
  only ever touched by the one `agentic_kb_builder/infrastructure/azure_search/azure_search_client.py`
  module behind the interface.
- **PR-09/10** (MCP server + Context Broker) follow the same rule: fastmcp tools tested against
  the fakes + local Postgres; budgets and ledger assertions never need cloud resources.
- Real Azure resources enter the picture only for deployment (infra/) and the nightly GitHub
  Actions build — never for the development loop.
