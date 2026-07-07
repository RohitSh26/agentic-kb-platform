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

- `provenance` (PR-43, ADR-0034): optional field. **Absent ⇒ `"build_mined"`** — every row this
  document has described so far (mined at build time from commit subjects / doc filename slugs,
  §"Mining rules" below) is written WITHOUT this field, and stays that way (PR-43 does not rewrite
  existing rows). A row written by the ledger-mining pass (`alias/ledger_mining.py`) instead carries
  `"provenance": "ledger_mined"` explicitly. The two provenances share the SAME artifact/edge shape,
  reconcile independently (own lifecycle, §"Ledger-mined aliases" below), and never overwrite each
  other's row for the same phrase (a title collision is resolved in favour of whichever provenance
  already owns it — see below).

## Ledger-mined aliases (PR-43, ADR-0034)

A second, independent build step — `alias/ledger_mining.py`'s `run_ledger_alias_miner`, wired into
`build_runner.py` immediately AFTER the deterministic alias miner (`run_alias_miner`) and before
graph centrality — mines alias phrases from the KB's **own retrieval misses** instead of commit/doc
content, closing the loop ADR-0034 describes: a phrase a developer typed and missed resolves on the
next build.

**Input (read-only).** Recent `retrieval_event` rows where `tool_name = 'kb_search'`,
`status = 'approved'`, and `coalesce(cardinality(returned_artifact_ids), 0) <= 1` (the SAME
zero/thin predicate migration `0020_dashboard_views`'s `kb_search_zero_thin` column uses — the two
must never disagree), within a trailing window (`window_days`, default **14**). Ledger rows are
never modified — this pass only ever `SELECT`s them. kb-builder never writes `retrieval_event`
(that table's runtime-write ownership is mcp-server's alone, `postgres-knowledge-registry.md`).

**Untrusted-input handling.** Each row's raw `query_text` is treated as untrusted content: ASCII
control characters (`\x00`–`\x1f`, `\x7f`) are stripped, the result is length-capped at **80**
characters, and ONLY THEN normalized with `alias/mining.py`'s `normalize_phrase` — the identical
normalizer commit/doc mining and `alias/resolve.py` already use. A query is a search string, never
executed or templated. Misses are grouped by their normalized phrase; `miss_count` = row count,
`confirmation_count` = number of DISTINCT calendar days (UTC) the phrase appeared, `first_seen` /
`last_seen` = min/max `created_at` in the group.

**Candidate matching — zero new matching code.** Candidates are every LIVE `knowledge_artifact` row
(any `artifact_type` except `alias_reference` itself) with a non-null `title`, joined to its
`source_item.path` (also non-null, source not deleted): each becomes a single-target
`alias/resolve.py` `AliasEntry(alias=normalize_phrase(title), targets=(path,))`. The miss phrase is
resolved against this candidate set with the UNMODIFIED `alias/resolve.py` `resolve()` (same
exact-match-then-Jaccard-fuzzy algorithm, same `MIN_FUZZY_SCORE` floor, §"Resolution" above) — no
new scoring code. A match's `resolution.targets[0]` is the ONE target path (each candidate is
single-target by construction, so a winning entry never has more than one). No match ⇒ the phrase
stays an open gap (dashboard `kb_search_zero_thin`) — nothing is written.

**Title-collision rule.** If the normalized miss phrase is already the `title` of a LIVE
`alias_reference` row of a DIFFERENT provenance (e.g. a commit/doc already mined the exact same
phrase), the phrase counts as resolved (`mined`) but this pass writes NOTHING for it — the
one-row-per-phrase invariant (`alias-reference.md` "Incrementality + idempotency") is never
violated by a second writer racing a title. `_reconcile_artifacts`'s existing invalidation sweep
(`alias/run.py`) is taught to skip any prior row whose body carries `provenance: "ledger_mined"` —
that pass never desires ledger-mined titles, so without this skip it would invalidate every
ledger-mined row on every build; the exclusion means the ledger-mining pass has EXCLUSIVE,
independent ownership of the rows it writes (its own reconcile: upsert-if-changed, soft-invalidate
what it no longer mines — same discipline as PR-38, `docs/contracts/postgres-knowledge-registry.md`
"idempotent build jobs").

**ACL.** The never-widen intersection over the ONE resolved target path's `source_item.acl_teams`,
via the existing `domain.acl_intersection.commit_acl_intersection` helper (no bespoke ACL logic).

**Ledger-mined `body_text` shape** (schema stays `alias_reference_v1`; only the provenance/evidence
values differ from a build-mined row):

```json
{
  "schema": "alias_reference_v1",
  "alias": "the durable cache fix",
  "variants": ["the-durable-cache-fix", "the_durable_cache_fix"],
  "confidence_tier": "interpreted",
  "confirmation_count": 3,
  "provenance": "ledger_mined",
  "targets": [
    { "path": "services/kb-builder/.../durable_output_cache.py",
      "artifact_id": "uuid", "count": 1 }
  ],
  "evidence": [
    { "first_seen": "2026-06-24T10:03:00+00:00", "last_seen": "2026-07-06T08:41:00+00:00",
      "miss_count": 5 }
  ]
}
```

`evidence` is a **single-element list** containing the `{first_seen, last_seen, miss_count}` object
(list-wrapped, not a bare object) — `alias/run.py`'s `_prior_extractions` iterates `body["evidence"]`
generically for EVERY live `alias_reference` row (any provenance) to rebuild its own watermark map;
a bare object there would break that iteration (`for entry in {...}` walks dict KEYS, not entries)
the next time the deterministic alias miner runs. Each entry's `.get("source")` /
`.get("content_hash")` come back `None` for a ledger-mined entry, which `_prior_extractions` already
treats as "not a recognized watermark row" and skips — no crash, no false incremental-skip.

**No `aliases` edges (open question, v1).** Unlike build-mined rows, this pass does not write
`knowledge_edge` rows for its targets — ledger-mined aliases ride the existing keyword search
surface (`search_text`) only, exactly like a build-mined row before its edge is written. Whether
ledger-mined aliases should also be graph-traversable (`aliases` edges, so `graph.get_neighbors` can
route through them) is left open for a follow-up PR; nothing here blocks adding it later (same
`edge_type = 'aliases'`, no ontology change).

**Structured log:** `event=ledger_mining_completed kb_version=... build_seq=... window_days=...
misses_seen=... phrases_seen=... mined=... unresolved=... artifacts_inserted=...
artifacts_refreshed=... artifacts_unchanged=... artifacts_invalidated=...`.

**Privacy note (ADR-0034 Consequences).** A ledger-mined alias's `title` IS the normalized phrase a
developer typed. It becomes an org-visible, ACL-gated search hit only when it resolves to a target
whose own ACL admits the requester (never widened beyond that target's visibility) — the alias is
exactly as visible as the artifact it points to, never more.

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
