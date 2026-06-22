# PR-35 — Crash-durable model-output cache: re-run a crashed build without re-paying tokens

## Why

The build commits once at the very end (`build_runner.run()`), and the cache rows that memoise paid
model work (`generation_cache`, `embedding_cache`, `relationship_judgment_cache`) are written into that
same transaction. A crash mid-build `rollback()`s them, so the re-run sees cache misses and **re-pays
for every LLM/embedding call the crashed run already made**. Implements ADR-0027: persist the
*expensive model outputs* durably and incrementally, decoupled from build-scoped artifacts, so a
re-run makes zero model calls for already-processed work — while activation stays atomic (ADR-0013).

## Scope

- **Migration (kb-builder owns the schema):** add a durable, content-keyed model-output cache,
  independent of `knowledge_artifact` (no FK into build-scoped rows):
  - `doc_extraction_output(cache_key PK, input_hash, prompt_version, model_name, model_params_hash,
    output_schema_version, output_json jsonb NOT NULL, created_at)` — the raw `DocExtractionResult`.
    Key composition is **identical** to `doc_extract_cache_key` (`cache_gates.py`): a hit requires every
    model-identity input (model_name, prompt_version, params_hash, schema_version) to match, so a
    prompt/model/schema bump is a guaranteed miss.
  - `embedding_output(text_hash, embedding_model, embedding_hash, embedding vector/jsonb NOT NULL,
    created_at, PRIMARY KEY(text_hash, embedding_model))` — **artifact-agnostic** durable vector keyed by
    text+model only. The existing `embedding_cache` row (keyed by `artifact_id, text_hash, model`) stays
    written in the main tx for in-build replay — two keys by design.
  - **Judge: no new table.** `relationship_judgment_cache` already stores the full verdict durably with
    no FK into build-scoped tables; the only change is to **side-commit its writes**
    (`linker/judgment_cache.py` `record(...)` currently writes on the main session).
  - Forward + rollback; idempotent inserts (`ON CONFLICT DO NOTHING`).
- **Side-commit cache writer** (`application/durable_cache.py`): a small writer with its **own
  `AsyncSession`** (separate engine/connection from the build session) that upserts an output row and
  commits it immediately, so a durable-cache write never depends on the main build transaction and
  never makes a half-built artifact servable. Idempotent; structured-logged. The side session/engine
  must be **context-managed and disposed in a `finally`** so a crash never leaks a pool connection
  (none of the existing single-session call sites model this — add the pattern). Reads happen on the
  main build session on the *next* attempt, so the side-committed row is visible there — do not expect
  to read it back through the build session mid-transaction.
- **Wire the gates to the durable layer (read-through, write-through):**
  - `_docify_gated`: before calling the extractor, look up `doc_extraction_output`; on a hit, rebuild
    artifacts from the cached `output_json` (no LLM). On a miss, call the model, **side-commit the
    output first**, then map to artifacts + record the existing `generation_cache` in the main tx.
  - `_embed_gated`: look up `embedding_output(text_hash, model)`; on a hit reuse the vector (no embed);
    on a miss embed, side-commit the vector, then record `embedding_cache` as today.
  - Judge step: same read-through/write-through against the durable judgment output.
- **No change** to the source loop ordering, linker/candidate/judge/invalidation finalize, index
  reconcile, or activation. The main build transaction and its single end-commit are unchanged.
- **Tests** (the point of the PR):
  - *Crash → re-run pays once:* process a build that calls a counting fake extractor/embedder for N
    sources, inject a crash **after** the side-commits but **before** the main commit; assert the main
    transaction rolled back (no active version, no served artifacts) **and** the re-run makes **zero**
    extractor/embedder calls for the already-processed sources.
    *(Note: do not run against the `:55432` demo DB — use the test DB / `TEST_DATABASE_URL`.)*
  - *Atomicity preserved:* a crashed build never activates and never serves partial artifacts.
  - *Idempotency:* re-running a completed build inserts no duplicate output rows; concurrent-safe
    upserts.
  - *Durable hit ⇒ no model call:* counters prove the model client is not invoked on a durable hit.
  - *Vector reuse across artifact identity:* same text+model hits `embedding_output` even when the
    artifact_id differs.

## Do NOT

- Do not commit artifacts/edges incrementally or per-source; activation stays atomic (ADR-0013).
- Do not give the durable output-cache any FK into `knowledge_artifact`/`knowledge_edge`, and never
  serve it as evidence — it is pure derived data that only gates model calls (no trust/provenance).
- Do not run the crash/atomicity tests against the demo DB on `:55432` (teardown wipes the active KB).
- Do not weaken the existing `generation_cache`/`embedding_cache` replay path — the durable cache sits
  *beneath* it, it does not replace it.

## Acceptance criteria

- [ ] Migration adds the durable output-cache table(s) with forward + rollback; inserts idempotent.
- [ ] Side-commit writer persists outputs on its own session, independent of the build transaction,
      and disposes the side session/engine in a `finally` (no leaked connection on crash).
- [ ] Durable keys use the identical model-identity composition as `doc_extract_cache_key`; a
      prompt/model/schema bump is a guaranteed miss (test). Judge change is side-commit only — no new table.
- [ ] docify / embed / judge are read-through + write-through against the durable cache.
- [ ] Crash-after-side-commit test: main tx rolled back (no active version), **and** re-run makes zero
      model calls for already-processed sources (counted).
- [ ] Activation atomicity unchanged: a crashed build never activates or serves partial artifacts.
- [ ] Architecture overview §7/§10 + invariant-4 note updated to record the durable, side-committed gate.
- [ ] Local run validated with Ollama; `make verify-kb-builder` green; no excluded-V1 resource added.
