# ADR-0029 — Persist knowledge per source: incremental commit, atomic activation

## Status

Accepted (2026-06-23). **Supersedes the atomic-write stance of ADR-0027** (which held all artifact
writes in one end-of-build transaction and added a durable side-cache to avoid re-paying tokens after a
crash). Atomic *activation* (ADR-0013) is unchanged. Driven by the owner: completed knowledge must land
in the database as it is produced, not only when the whole build finishes.

## Context

The build processed every source and committed once at the very end. So an interruption part-way
through — e.g. a single document's LLM call timing out — aborted the whole build and rolled back
**all** completed work (graphify + every doc already extracted). ADR-0027's durable cache reduced the
token cost of re-running, but it did not make the completed knowledge persist; the operator still saw an
empty `knowledge_artifact` after a crash. That is the wrong behavior: work that is done should be saved.

The key realization is that two things were conflated:

- **Atomic activation** — a *served* `kb_version` must be all-or-nothing. This must stay.
- **Atomic writes** — writing every artifact in one end-commit. This does **not** have to stay:
  artifacts carry `valid_from_seq` and are only *served* once a version activates, so they can be
  committed per source without ever being served early.

## Decision

1. **Commit each source's knowledge the moment it is ready.** `_process_sources` commits after each
   changed source succeeds. A source that fails is rolled back to the last commit (only that source is
   lost), counted as an extractor failure, kept seen (its prior generation is retained), and left with
   its `content_hash` unadvanced so the next build retries it. Everything already committed stays.

2. **Code (whole-tree graphify) writes its `source_item` atomically with its artifacts.** The code
   `source_item` upsert is deferred into the graphify pass so its hash advance and its artifacts commit
   together — an interrupted code write strands nothing (no advanced-hash-without-artifacts).

3. **Activation stays atomic and separate.** The graph finalize (linker, candidate/judge, centrality,
   invalidation) and `kb_build_run` completion commit at the end; activation flips the active version
   only after the publish gates pass. The linker and centrality run over **all live members**, so a
   later build finalizes and activates anything an interrupted build already committed.

4. **Safety holds:** a crashed build never activates, and serving is gated by interval membership
   against the *active* build's `build_seq`. Committed-but-unactivated rows (at a higher seq than the
   active version) are therefore **never served** until a successful build activates them.

5. **The durable model-output cache (ADR-0027) remains, complementary.** Per-source commit handles the
   common case (a completed source is saved). The durable cache still avoids re-paying when a source's
   model call succeeded but its source-level commit rolled back (e.g. a later step in that source
   failed). It is fail-soft — its unavailability never breaks a build.

## Consequences

- Completed knowledge persists continuously; a crash loses at most the in-flight source. This is what
  the owner asked for.
- A single source's failure (model timeout, etc.) is non-fatal — the build completes with everything
  else; the extractor-error-rate publish gate still blocks *activating* a build where too many failed.
- Cost: more, smaller transactions (one per source) instead of one large commit — acceptable at nightly
  scale and worth it for not losing work.
- Repeated crashed builds can leave dead rows at abandoned `build_seq`s (never served, superseded only
  when that source next changes). Add a periodic physical-GC/compaction job — see follow-ups.

## Alternatives considered and rejected

- **Keep atomic writes + the durable cache only (ADR-0027 status quo).** Rejected: re-runs were
  cheaper but completed work still vanished on a crash — the actual complaint.
- **Per-source savepoints, commit only at the end.** Rejected: a savepoint isolates a failure but the
  data is still not durable until the final commit, so a later crash still loses it.

## Follow-ups

- Periodic GC of dead rows at abandoned `build_seq`s (no active version serves them; supersession only
  retires them when that source changes again).
- Make the Phase-3B judge crash-durable too (its cache is committed in the finalize transaction — task
  to side-commit it).
