# Testing and builds

The contributor dev loop: the verify gate, how the database-backed tests work, where the fakes
are, Docker compose, and the evals. Everything runs on a laptop with uv and a local Postgres —
no cloud credentials. Every external system sits behind a `Protocol`, so tests inject in-memory
fakes and the only real infrastructure a test touches is Postgres.

Providers and keys for builds: [switch-llm-providers](../how-to/switch-llm-providers.md).
Building from real GitHub/ADO sources: [index-your-own-sources](../how-to/index-your-own-sources.md).
Build CLI flags and make targets: [reference/cli](../reference/cli.md).
The SQL cookbook: [reference/database](../reference/database.md).

## The verify gate

This is the definition of "done" for any change. CI (`.github/workflows/ci.yml`) runs one job per
service with the same steps against a Postgres 16 service container:

```sh
make verify                   # lint + types + tests for all four projects (three services + evals)
make verify-kb-builder        # or just one
make verify-mcp-server
make verify-review-panel
make verify-evals
```

The Makefile's default `TEST_DATABASE_URL` assumes a `postgres:postgres` role (matching CI).
Homebrew/macOS Postgres creates a role named after your OS user instead — pass the URL on the
command line, or export it once:

```sh
createdb agentic_kb_test      # one-time: the dedicated test database
make verify TEST_DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb_test"
```

Or by hand, inside a service directory:

```sh
uv run ruff check . --fix && uv run ruff format .
uv run pyright
uv run env TEST_DATABASE_URL=postgresql+asyncpg://$USER@localhost:5432/agentic_kb_test pytest -q
```

The driver **must** be `postgresql+asyncpg://` — everything is async SQLAlchemy.

## How DB tests work

- Tests read `TEST_DATABASE_URL` (falling back to `DATABASE_URL`). If neither is set, DB-backed
  tests **skip gracefully** with a stated reason — pure unit tests still run, so `uv run pytest`
  with no env gives a fast partial signal.
- kb-builder's DB-backed modules (`tests/integration/test_build_engine.py`, `test_linker.py`,
  `test_indexing.py`, `test_registry_roundtrip.py`) migrate **to head** in a module fixture and
  **downgrade to base on teardown** — every migration's downgrade is exercised on every run.
- mcp-server **never runs migrations** (kb-builder owns the schema). Its DB-backed tests expect
  an already-migrated database and skip with a message if the schema is missing. The self-heal is
  wired into the Makefile: `make test-mcp-server` and `make test-evals` depend on
  `make migrate-test-db`, because kb-builder's suite just downgraded the shared test DB to base —
  without the dependency, a full `make verify` would fail with hundreds of missing-table errors.
  Driving suites by hand? Run the migration yourself first:

  ```sh
  cd services/kb-builder
  DATABASE_URL="$TEST_DATABASE_URL" uv run alembic upgrade head
  ```

- Between tests, a session fixture deletes table contents in FK-safe order; tests own their data.
- Caveat for log-asserting DB tests: Alembic's `fileConfig()` disables already-imported loggers,
  so re-enable the specific loggers you capture after running migrations (see `test_linker.py`'s
  `migrated_db` fixture for the pattern).

Developing a revision? Verify the rollback as you go (head is `0023`):

```sh
cd services/kb-builder
export DATABASE_URL=postgresql+asyncpg://$USER@localhost:5432/agentic_kb_test
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

## Where the fakes are — and what they replace

| Real system (V1 target) | Interface (Protocol) | Defined in | Fake used in tests |
|---|---|---|---|
| GitHub / Azure Wiki / ADO APIs | `FetchBackend` | `connectors/source_connector.py` | in-test backends returning canned text (`test_connectors.py`, `test_build_engine.py`) |
| Chat model (docify doc extraction, ADR-0023) | `DocExtractor` | `application/build_runner.py`, `docify/extractor.py` | spy doc extractors returning canned `DocExtractionResult` |
| Code extraction | whole-tree `graphify_tree` | `graphify/graphify_backend.py` | spies returning fixture `GraphifyResult`s |
| Azure OpenAI embeddings | `Embedder` | `application/build_runner.py` | fake returning a deterministic `embedding_hash` |
| Azure AI Search (upsert) | `SearchIndexer` | `application/build_runner.py` | `SpyIndexer` counting received artifact ids |
| Azure AI Search (index) | `SearchClient` | `infrastructure/azure_search/search_client.py` | `FakeSearchClient` — a real in-memory index; tests inject drift/orphans by mutating `.docs` |
| Azure AI Search (similarity) | `SimilarityProvider` | `linker/semantic.py` | `FakeSimilarityProvider` with canned scores |

(Paths relative to `services/kb-builder/src/agentic_kb_builder/`.)

The pattern is always the same: implement the Protocol in the test module, record calls and
arguments, return deterministic canned data. Adding a new external dependency means adding a
Protocol next to its caller and a fake next to its tests — never importing an SDK in library
code. `FakeSearchClient` stands in for the *whole* index: projection, changed-only upsert, orphan
deletion, and the drift consistency check all run offline; the one module allowed to import the
Azure SDK is `infrastructure/azure_search/azure_search_client.py`. Real Azure enters the picture
only at deployment (`infra/`) — never in the development loop.

## What each test file covers

All under `services/kb-builder/tests/`:

| File | Covers |
|---|---|
| `integration/test_build_engine.py` | cache-key determinism; first build vs unchanged-skip; **cache hit ⇒ no model call** (invariant 4); idempotent re-runs; failed-docify rollback; cross-file graphify edges; validation-gated activation (invariant 5) |
| `integration/test_linker.py` | deterministic matching + precision guards; semantic threshold and dedupe; reconcile-in-place; protected-edge survival without a provider; low-confidence flagging |
| `integration/test_indexing.py` | projection field mapping; changed-docs-only upsert; orphan-doc deletion; consistency check failing on each injected drift class |
| `unit/test_docify_mapping.py` | docify trust derivation (verbatim quote ⇒ `source_backed_fact`, else `interpreted`); artifacts-only mapping |
| `integration/test_graphify.py` | whole-tree extraction; key round-trips; exact spans; span-past-EOF rejection; edge confidences |
| `integration/test_registry_roundtrip.py` | migrations up/down + model round-trips |
| `unit/test_connectors.py` | normalize+hash determinism per source type |
| `unit/test_alias_mining.py` / `test_alias_resolve.py` | the deterministic alias miner and pure resolver |
| `unit/test_alias_golden_subset.py` | hermetic golden subset through the real mine→aggregate→resolve pipeline (no DB) |
| `integration/test_alias_miner.py` | `run_alias_miner`: artifacts+edges written; incremental skip; never-widen ACL; `BuildRunner` wiring |
| `contract/test_import_boundaries.py` | no cross-service or legacy root-package imports (ADR-0008) |

mcp-server's suite mirrors the same `{unit,integration,contract}` split — see the
[code tour](code-tour.md) for what each layer exercises.

For a fully hermetic view of the pipeline, `test_build_engine.py` drives a real `BuildRunner`
against local Postgres with fakes, end to end: fetch → hash-skip → docify (cache-gated) →
graphify (cache-gated) → embed (cache-gated) → index → edges → linker → run accounting →
activation gating. The production backends are tested hermetically too:
`production_backend_factory(client_transport=httpx.MockTransport(...))` drives the whole
config→connector→HTTP path with canned responses, no network.

To *watch* a build narrate itself, run one integration test verbosely:

```sh
cd services/kb-builder
uv run env TEST_DATABASE_URL=postgresql+asyncpg://$USER@localhost:5432/agentic_kb_test \
  pytest tests/integration/test_build_engine.py -k "first_build" -v --log-cli-level=INFO
```

To experiment interactively, copy the fixture pattern from `test_build_engine.py` into a scratch
script: build a `BuildRunner(session, kb_version=..., doc_extractor=fake, embedder=fake,
indexer=fake)`, feed it a connector whose `FetchBackend` returns whatever you want ingested, then
inspect `knowledge_artifact` / `knowledge_edge` / `kb_build_run` directly
([reference/database](../reference/database.md)).

Write new tests in the same spirit: budgets, dedupe, cache hits, idempotency, and failure paths —
not just happy-path success (CLAUDE.md, "Tests ship in the same PR").

## Docker: the whole system with one command

```sh
docker compose up --build
```

Three containers, in order:

1. **postgres** — Postgres 16 with a named volume; host port `55432` by default (not 5432, so a
   Homebrew Postgres keeps working — override with `POSTGRES_HOST_PORT`).
2. **kb-builder** — one-shot: applies the Alembic migrations and exits. It is the only migration
   runner. To come up **already serving a built KB**, add the optional profile:
   `docker compose --profile build up` (a full no-cloud build that activates a `kb_version`).
3. **mcp-server** — starts only after the migration job completes; serves
   `http://localhost:8000/mcp/` (override with `MCP_HOST_PORT`). It never runs migrations.

Two honesty notes, both by design:

- `GET http://localhost:8000/health` answers **503 `no_active_kb_version`** on a fresh volume —
  readiness honesty, not a failure. Run the build profile.
- The compose file boots with **placeholder Entra identifiers**. Auth is fail-closed with no
  auth-off switch, so no bearer token verifies until you export a real tenant id and audience.

The compose invariants — exactly three services on a default `up`, kb-builder as the only
migration runner, the dependency order, asyncpg URLs, no inline credentials — are pinned by
`services/kb-builder/tests/contract/test_compose_contract.py`. Reaching the compose Postgres from
the host: `postgresql+asyncpg://postgres:postgres@localhost:55432/agentic_kb`.

## Evals

```sh
make eval-run    # golden-query retrieval evals against the migrated test registry
make eval-all    # the consolidated T0–T4 report
```

`make eval-all` runs every tier that *can* run in your shell — deterministic golden sets (T1),
zero-LLM checks against a really built KB (T2), the LLM-armed A/B smoke (T3), the
adversarial-fixture inventory (T4) — and **skips anything unconfigured with a stated reason**
rather than failing or inventing a number. Add T0 (the full `make verify` gate) with
`cd evals && uv run python run_all.py --with-gates`. The report lands at `evals/report_all.md`.
What each tier proves and how to add cases:
[evaluation-system](../../architecture/evaluation-system.md).
