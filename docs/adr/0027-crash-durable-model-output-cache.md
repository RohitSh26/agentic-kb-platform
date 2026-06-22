# ADR-0027 — Crash-durable model-output cache: never re-pay for tokens a crashed build already spent

## Status

Proposed (2026-06-22). Strengthens architecture invariant 4 (every model call is cache-gated) by
making the gate **crash-durable**. Implemented by PR-35. Does **not** change the atomic-activation
model (ADR-0013): a `kb_version` still goes active all-or-nothing.

## Context

The nightly build runs as **one transaction that commits only at the very end** (`build_runner.py`:
`run()` → a single `self._session.commit()` after sources + graph finalize + index + activation; any
exception does `rollback()`). Only the `kb_build_run` audit row is committed up front.

The paid work — docify LLM extraction (per changed prose source), embeddings (per artifact), and the
optional Phase-3B relationship judge — is memoised so a *subsequent completed* build skips it:

- `generation_cache` / `generation_cache_artifact` — keyed by `content_hash + prompt + model + params
  + schema`; on a hit the build **replays the mapped artifact rows** and makes no LLM call.
- `embedding_cache` — stores the vector keyed by `(artifact_id, text_hash, model)`; a hit skips
  re-embedding.
- `relationship_judgment_cache` (PR-29) — keyed by content + schema + prompt + model; a hit skips the
  judge call.

But every one of those memo rows is written **into the same single transaction** and is committed only
at the end. The `generation_cache` row is recorded *after* its output artifacts are flushed and is FK'd
to them (`generation_cache.output_artifact_id`, `generation_cache_artifact.artifact_id`) — deliberately,
so a replay returns identical artifacts. The consequence:

> **If the build crashes after docifying 900 of 1000 documents, the `rollback()` discards all 900
> `generation_cache` rows. The re-run sees a cache *miss* on every one and re-pays for all 900 LLM
> calls** (plus the embeddings). The cache only protects builds whose predecessor *fully completed*; it
> does nothing for a crashed-and-restarted build. With real per-token cost, a single late-build crash
> can double the night's bill.

The naïve fixes are unsafe because the build's value-add is **global, not per-source**: after the
source loop the runner runs the deterministic linker, the candidate generator + judge, and the
identity-invalidation pass — all keyed on this build's `build_seq` (ADR-0013) and all requiring the
*complete* set of this build's artifacts. So committing artifacts per-source would strand unlinked,
un-invalidated nodes at a `build_seq` that never activated — risking a half-built served graph. The
atomicity of activation is load-bearing and must stay.

## Decision

Separate the two concerns that the current design conflates — **"this expensive model output exists"**
(idempotent, content-keyed, safe to persist eagerly) versus **"this artifact/edge is part of build N's
served graph"** (build-scoped, must commit atomically at activation).

1. **Add a durable, content-hash-keyed model-output cache**, decoupled from build-scoped artifacts and
   edges. It stores the *raw model output*, not a pointer into a build's artifact rows:
   - **doc extraction outputs** — the `DocExtractionResult` (as JSON) keyed by the existing
     `doc_extract_cache_key` inputs (`content_hash + prompt_version + model + params + schema`).
   - **embedding vectors** — keyed by `(text_hash, embedding_model)` only (drop the `artifact_id` from
     the durability key; the vector is a pure function of text + model). The artifact-scoped
     `embedding_cache` row stays in the main transaction for in-build replay — two keys by design.
   - **judge outputs** — the `relationship_judgment_cache` verdict (relation_type, trust_bucket,
     supporting_quote, reason) is **already a durable, build-scope-free row**; the only change is to
     **side-commit its writes**. No new judgment table.

   **Identity-input invariant:** a durable hit is honored only when *every* model-identity input matches
   — `model_name`, `prompt_version`, `model_params_hash`, `output_schema_version` — using the **identical
   key composition** as the existing `doc_extract_cache_key` (`cache_gates.py`). A prompt/model/schema
   bump is therefore a guaranteed miss and can never replay stale output under new semantics. (A hit
   re-maps cached output into artifacts without re-validating it, so this key discipline *is* the trust
   boundary.)

2. **Commit the output cache incrementally, on a side transaction**, the moment each model call returns
   — independent of the main build transaction. Because these rows have **no FK into build-scoped
   tables**, they can be committed eagerly without making any half-built artifact servable.

3. **The main build transaction is unchanged and still atomic.** It just consults the durable
   output-cache *before* any model call. On a miss it calls the model, writes the output to the durable
   cache (side-committed), then maps it into artifacts/edges in the main transaction exactly as today.
   The existing artifact-mapped `generation_cache` stays as the *within-completed-build* replay layer;
   the new output-cache is the *crash-durable* layer beneath it.

4. **Crash semantics become: lose the build, keep the receipts.** A crashed build still rolls back all
   artifacts/edges and never activates (atomicity preserved); only the cheap, idempotent model outputs
   survive. The re-run re-maps those cached outputs into a fresh `build_seq` and **makes zero model
   calls for everything the crashed run already processed.** Paid work is bought at most once.

5. **No change to activation, serving, or the graph-finalize ordering.** Linker, judge, and
   invalidation still run once over the complete build at the end under the same `build_seq`.

## Consequences

- A build is effectively **resumable for cost**: a crash never re-bills tokens already spent. The wall
  of LLM/embedding calls is paid once even across multiple crashed attempts.
- Invariant 4 is **strengthened**: the cache gate is now durable, not transaction-scoped. The
  architecture overview §7 (cache keys) and §10 gain a note that the model-output cache is side-committed.
- Adds a migration (the durable output-cache table(s)) and a small **side-commit cache writer** on its
  **own `AsyncSession`/engine** (Postgres has no autonomous transactions — a separate session/connection
  is the mechanism), used only for these idempotent rows. All writes stay idempotent (`ON CONFLICT DO
  NOTHING` / hash-keyed) so retries never duplicate. The side session must be **context-managed/disposed
  in a `finally`** so a crash never leaks a pool connection. A crash *between* the model call and the
  side-commit loses that one output (re-paid once) — acceptable.
- The durable cache can outlive any single `kb_version` and is safe to share across builds; it is pure
  derived data (re-creatable by paying the model again) so it carries no provenance/trust weight and is
  never served — it only gates model calls.
- Slightly more storage (raw extraction JSON + vectors held independently of artifacts). Acceptable: far
  cheaper than re-paying the model, and prunable by `(model, prompt_version)` once superseded.

## Alternatives considered and rejected

- **Per-source artifact commits (commit each source's artifacts as it finishes).** Rejected: the
  linker, judge, and invalidation pass are global and keyed on `build_seq`; committing artifacts before
  they run strands unlinked, un-invalidated nodes and risks serving a half-built graph. Breaks the
  atomic-activation invariant (ADR-0013).
- **Periodic checkpoint commits of the whole build transaction (every N sources).** Rejected: same
  atomicity break — a crash would leave committed-but-unactivated artifacts that the next build must
  reconcile, and the deletion/supersession sweeps assume a single coherent generation.
- **Resumable build via a per-source progress marker only.** Rejected as insufficient on its own: it
  records *what* was done but not the *outputs*, so a resume still can't reconstruct artifacts without
  re-calling the model. (A progress marker is a fine *additional* optimisation on top of the durable
  cache, deferred.)
- **Status quo (atomic single commit).** Rejected: re-pays for all model work on every crash — the
  problem this ADR exists to fix.

## Follow-ups

- PR-35: migration + side-commit cache writer; wire docify, embed, and judge to read/write the durable
  output-cache; tests for the crash→re-run "zero model calls" guarantee and for activation atomicity.
- Update `docs/architecture/00-overview.md` §7/§10 and the invariant-4 enforcement note once landed.
- Consider a `(model, prompt_version)` pruning job for superseded output-cache rows.
- Optional later: a per-source progress marker to also skip re-*fetching* unchanged sources on resume.
