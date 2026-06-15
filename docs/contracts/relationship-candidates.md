# Contract: Relationship candidates (audit table, phase 3A)

> Cross-service contract. Owned by kb-builder (producer); the candidate generator is the cheap,
> deterministic, zero-LLM stage of candidate-then-judge (ADR-0010 §4, ADR-0011). This table is an
> **audit / measurement artifact only**. It is **never served through MCP** and carries **no
> membership columns** (no `valid_from_seq` / `invalidated_at_seq`): it is not part of the served KB.
> Phase 3B (the LLM judge) reads it to decide which candidates become `knowledge_edge` rows.

## Why this exists

Candidate-then-judge is the architecture, not an optimisation. A global LLM pass over every artifact
pair is O(N²) and financially dead at scale. Before spending any LLM tokens on judging, we must prove
the **cheap candidate generator** has the recall to be worth judging — and measure how many candidates
it produces (volume) and what it would cost to judge them all (cost-if-judged). Phase 3A builds the
generator and this audit table and **writes nothing to `knowledge_edge` and calls no LLM**.

## What a candidate is

A candidate is a *cross-domain artifact pair the generator thinks is worth the judge looking at*. It
is NOT an edge: it has no `edge_type`, no `trust_class`, and cannot support a claim. The domains are:

- `code` — artifacts whose `source_type` is a code source (`code_file` / `code_symbol` / `endpoint`).
- `doc` — artifacts from `azure_wiki` / `github_doc` sources.
- `card` — artifacts from `ado_card` (work-item) sources.
- `commit` — artifacts from `git_metadata` sources.

A pair is **cross-domain** when its two endpoints are in **different** domains. A pair already linked
deterministically (a live `knowledge_edge` row with `source='linker'` for that ordered pair, in
either direction) is **excluded** — the judge never re-judges a deterministic fact.

## Signals (cheap, deterministic, zero-LLM)

Each signal returns a score in `[0, 1]`. A candidate records **every** signal that fired and its
score in `signals` (jsonb). The generator never calls an LLM and never calls a model endpoint
directly — embedding similarity goes through the linker's `SimilarityProvider` Protocol (which the
build passes `None` until the vector projection lands, in which case the signal simply does not fire).

| signal                  | fires when                                                                 |
|-------------------------|----------------------------------------------------------------------------|
| `embedding_similarity`  | the `SimilarityProvider` returns the other artifact above a floor (None-safe) |
| `token_overlap`         | name/path/symbol tokens of the two artifacts overlap (Jaccard above a floor) |
| `section_proximity`     | a doc/card section names or path-references the other artifact's path/symbol  |
| `path_colocation`       | the two artifacts share a directory prefix (code-ownership co-location)       |

`candidate_score` is the max of the firing signal scores (audit summary only; the judge re-scores).

## Bounded fan-out (no O(N²))

For each `from` artifact the generator keeps only its **top-K** highest-scoring candidates
(`CANDIDATE_FAN_OUT_K`, default **10**), tie-broken deterministically by `(−score, to_artifact_id)`.
The total candidate count is therefore bounded by `K × |artifacts|`, never the full cross-product.
The generator is **deterministic** for fixed inputs: stable ordering and stable tie-breaks.

## `relationship_candidate` table

| column                    | type        | notes                                                          |
|---------------------------|-------------|----------------------------------------------------------------|
| `candidate_id`            | uuid PK     | `gen_random_uuid()`                                            |
| `from_artifact_id`        | uuid FK     | → `knowledge_artifact(artifact_id)`                            |
| `to_artifact_id`          | uuid FK     | → `knowledge_artifact(artifact_id)`                           |
| `signals`                 | jsonb       | `{signal_name: score, ...}` for every signal that fired       |
| `candidate_recall_bucket` | text        | coarse confidence bucket: `high` / `medium` / `low`            |
| `kb_version`              | text        | the build label that generated the candidate (logging only)   |
| `created_at`              | timestamptz | `now()`                                                       |

No membership columns by design (see header). A re-run is idempotent on
`(from_artifact_id, to_artifact_id, kb_version)`: the same build re-writes the same candidate in
place rather than accreting duplicates.

## Metrics (eval harness)

- **candidate_recall** — of the cross-domain golden set's expected relations, the fraction the
  generator surfaced as a candidate (in either direction). The headline phase-3A go/no-go number.
- **candidate_precision** — sampled: of surfaced candidates, the fraction a reviewer would judge real.
- **volume_per_artifact** — mean candidates per `from` artifact (must stay ≤ `K`).
- **cost_if_judged** — estimated tokens to LLM-judge the whole candidate set
  (`JUDGE_TOKENS_PER_CANDIDATE × count`), so phase 3B affordability is decidable.

## Out of scope (phase 3A)

No edge is written; no LLM is called; candidates are never ranked-for-serving, promoted, or exposed
through the broker. Promotion is phase 3B (the judge), under the relation ontology + trust buckets.
