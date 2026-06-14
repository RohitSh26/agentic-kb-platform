# PR-22 — Local-filesystem fetch backend + `build` CLI (end-to-end into Postgres)

## Why

`connectors_from_config(config, backend_factory)` needs an injected fetch backend, but only fakes
exist; there is no real backend and no product entry point (the Dockerfile CMD is just
`alembic upgrade head`). This PR makes the unified `build` (ADR-0010) runnable end to end on a local
workspace — the single command that wires connectors → AST extractor (PR-21) → wikify → linker →
embed → index → validate → activate — so the platform can be tested locally with no cloud and no
spend (Ollama for wikify).

## Scope

- **`connectors/local_fs.py`** — a `FetchBackend` that reads files from a workspace directory per
  `sources.yaml` include/exclude globs; captures `source_uri` (file URI), `source_version` (e.g. a
  build label or git SHA if available), and a deterministic `content_hash`. Deny-by-default ACL:
  each source's `acl_teams` from config (`acl-source-visibility.md`).
- **`build.py` (`python -m agentic_kb_builder.build`)** — the CLI. Flags: `--sources <yaml>`,
  `--workspace <dir>`, `--kb-version <label>` (default timestamp), `--activate/--no-activate`. Wires
  the real backend factory + `AstGraphifier` (code) + `WikifyGenerator(ChatModelClient.from_env())`
  (prose) + a local embedder + a local indexer into `BuildRunner`. Incremental + idempotent: a
  re-run on unchanged inputs writes no new rows.
- **Local embedder + indexer** for the no-cloud path: a deterministic/local embedder (or Ollama
  embeddings) behind the existing `Embedder` interface, and a Postgres-keyword / fake `SearchClient`
  indexer behind `Indexer`/`SearchClient` — so `build` runs without Azure. Real Azure impls stay the
  production path (selected by env), unchanged.
- Structured logs on every step (`event=build_step ...`), `kb_build_run` row written, validation
  invoked before activate.
- Tests: an integration test that builds a tiny fixture workspace (2–3 Python files + 1 markdown)
  into the test DB, asserts artifacts/edges/caches exist, re-run is a no-op (idempotency), and the
  `kb_version` activates only after validation. Wikify is faked in the test (no live LLM in CI).

## Do NOT

- Do not call Azure in the local path; do not require cloud creds to run `build`.
- The local embedder/indexer is a **rebuildable projection only** — exactly like Azure Search
  (invariant 1). It must write no truth and must not become a persistence/truth store (no local file
  index, no embedded vector DB as truth, no SQLite-as-prod). Postgres stays canonical; the local
  index must be fully rebuildable from Postgres + source pointers.
- Do not add GitHub/ADO backends (phase 2 / production track).
- Do not implement the publish gates here beyond the existing validation (PR-25 adds gates).
- Do not change the broker.

## Acceptance criteria

- [ ] `uv run python -m agentic_kb_builder.build --workspace <dir> --sources <yaml>` builds the
      fixture into Postgres and activates a `kb_version`.
- [ ] Re-running on unchanged inputs creates zero new artifacts/edges/cache rows (idempotency test).
- [ ] Code files produce AST artifacts/edges (PR-21); markdown produces wikify artifacts.
- [ ] Runs with no cloud creds; wikify uses `ChatModelClient` (Ollama locally, faked in CI).
- [ ] `make verify` green.
