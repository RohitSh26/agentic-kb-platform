# Contract: Alias/Reference index (`alias_reference` artifacts)

> Cross-service contract (PR-38, authorized by ADR-0030; design per
> `docs/proposals/2026-07-02-tool-design-first-kb-architecture.md` §1). Produced by kb-builder's
> deterministic build-time alias miner; consumed by mcp-server retrieval (`kb_search` today,
> `get_task_context` in PR-39). **Deterministic v1: zero LLM calls, zero embeddings at build
> time** — alias rows ride the existing keyword-search surface via `search_text`.

## Why

A developer types a terse phrase ("the durable cache fix", "the retrieval budget check") and it
must resolve to the right file(s) without a paragraph of context. Commit subjects, PR-brief
titles, and ADR titles are already short, human-written alias phrases, and the commit →
changed-files mapping already exists at build time (`git_metadata` commit artifacts persist the
sorted changed-file list in `body_text`; `parse_changed_files` recovers it deterministically).

## Artifact row (existing `knowledge_artifact` table — no new table, no migration)

| Column | Value |
|---|---|
| `artifact_type` | `alias_reference` (plain TEXT; verified unconstrained — no enum/CHECK) |
| `title` | the normalized alias phrase (lowercase, punctuation stripped, stopwords removed) |
| `search_text` | phrase + deterministic variants (hyphen/underscore/compound joins), so the existing keyword search matches it with **zero** search-backend changes |
| `body_text` | the JSON payload below (`alias_reference_v1`) |
| `content_hash` | `content_hash(body_text)` |
| `knowledge_kind` | `interpreted` (the phrase→target mapping is co-occurrence-derived) |
| `source_id` | anchor: the source_item of the lexicographically-first contributing source (stable) |
| `acl_teams` | **intersection of the targets' source ACLs** — same never-widen rule as `git_metadata` (`write_commit.commit_acl_intersection`): org-public inputs impose no constraint; disjoint restrictions ⇒ the `DENY_ALL_ACL` sentinel; zero resolvable targets ⇒ deny by default |
| `valid_from_seq` / `invalidated_at_seq` | standard interval membership (ADR-0013) |

### `body_text` JSON (`alias_reference_v1`)

```json
{
  "schema": "alias_reference_v1",
  "alias": "durable model output cache",
  "variants": ["durable-model-output-cache", "durable_model_output_cache"],
  "confidence_tier": "interpreted",
  "confirmation_count": 2,
  "targets": [
    { "path": "services/kb-builder/.../durable_output_cache.py",
      "artifact_id": "uuid-or-null", "count": 2 }
  ],
  "evidence": [
    { "source": "commit:git:<sha>", "ref": "<sha12>", "content_hash": "<mined-at hash>",
      "targets": ["path", "..."] }
  ]
}
```

- `targets` is ranked: `count` desc (how many contributing sources name the path), then
  non-test paths before test paths, then filename-token overlap with the phrase desc, then path
  asc. Fully deterministic.
- `evidence` (sorted by `source` key) records every contributing source with the
  `content_hash` it was mined at — this **is** the incremental-skip watermark: on the next
  build, a source whose live artifact still has that hash is not re-mined; its stored
  contribution is reused.
- `confirmation_count` = number of distinct contributing sources this build. It is recomputed
  from current evidence (a vanished source drops out); the feedback-loop promotion semantics
  from the proposal (§4) are out of PR-38 scope.
- `confidence_tier` is `"interpreted"` at creation. Promotion to `deterministic` via
  `confirm_alias` is PR-39+ scope.

## Mining rules (deterministic, per source artifact)

Runs in the build's finalize phase, AFTER the invalidation pass (so it reconciles against the
live set) and BEFORE centrality + index reconciliation. Inputs are Postgres rows only.

1. **Commit artifacts** (`artifact_type='commit'`, live, source not deleted). From `body_text`:
   subject = first line; changed files via `parse_changed_files`.
   - *Scope tokens*: conventional-commit scope (`feat(kb-builder): …` → `kb builder`); a
     non-conventional leading label (`docify: …`) is treated as the scope. Targets = the
     commit's changed files.
   - *Subject n-grams*: 2–4-word windows over contiguous runs of non-stopword tokens of the
     description. Targets = the commit's changed files.
   - *Doc filename slugs*: each changed `docs/**/*.md` file contributes its filename slug
     (leading `PR`/`ADR`/numeric tokens stripped, ≥ 2 tokens) as a phrase targeting **that file
     only** (brief/ADR titles are slugified filenames in this repo).
2. **Doc sources** (`source_type='github_doc'`, `.md` path, not deleted), when present: the
   filename slug targets the doc's own path. Covers production KBs where briefs/ADRs are
   ingested as documents.

Aggregation: the same normalized phrase seen in N sources ⇒ one artifact with
`confirmation_count = N` and the union of targets ranked by frequency (rank key above).

## Edges

One `aliases` edge (`docs/contracts/relation-ontology.md`) per resolved target artifact, from
the alias artifact, capped at the top `20` ranked targets:

- `source = 'alias_miner'`, `trust_class = 'EXTRACTED'` (deterministic producer,
  `trust-buckets.md`), `confidence = target.count / confirmation_count`,
  `evidence = {"alias": phrase, "target_path": path, "sources": [refs]}`.
- **Routing hint only** — never claim support (see the ontology table).
- Target resolution: `(repo, path)` → live `source_item` → preferred live artifact
  (`code_file`, then `summary`, then any, deterministic). Unresolved paths stay in the body
  JSON (no edge) — never a dangling endpoint, so the no-ghost publish gate holds.

## Incrementality + idempotency

- Mining is keyed on the source artifact's `content_hash` (stored per contribution in
  `evidence`): unchanged source ⇒ extraction skipped (logged), exactly like docify/graphify
  skip on cache hit.
- The pass is a full reconcile of the live alias set: per-phrase upsert in place (mirrors the
  linker-edge pattern: refresh keeps the original `valid_from_seq`, revives a same-build sweep
  invalidation), stale aliases/edges soft-invalidated (`invalidated_at_seq = build_seq`), never
  physically deleted. Re-running a completed build inserts/refreshes/invalidates **nothing**.
- One live row per normalized phrase; enforced by the reconcile (single nightly runner), tested.

## Serving

- `alias_reference` is added to `PROJECTABLE_ARTIFACT_TYPES`, so alias rows are projected into
  the existing search index by the existing reconcile path — **no search-backend or ranking
  changes** (they rank via `search_text`/`title` like every other artifact).
- No embedding in v1 (`embedding` stays NULL in the projection). The proposal's
  `alias_text_embedding` is a later enrichment, ADR-gated.

## Resolution (eval + PR-39 input)

`alias/resolve.py` (pure, hermetic): normalize the query with the same tokenizer; exact
normalized match wins; else best token-set Jaccard ≥ 0.3, tie-broken by `confirmation_count`
desc then phrase asc. The winner's ranked `targets` are the answer; top-1 = `targets[0].path`.
Golden eval: `evals/retrieval_cases/alias_golden_v1.yaml` + `scripts/eval_alias_resolution.py`
(target ≥ 80% top-1 against a locally built KB).
