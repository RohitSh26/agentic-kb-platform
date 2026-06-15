# Contract: Relationship judgment (LLM judge over candidates, phase 3B)

> Cross-service contract. Owned by kb-builder (producer); the LLM judge is the phase-3B stage of
> candidate-then-judge (ADR-0010 §4, ADR-0011). The judge reads `relationship_candidate`
> (relationship-candidates.md) and promotes a bounded subset to `knowledge_edge` rows under the
> relation ontology + trust buckets. mcp-server consumes the resulting edges through the existing
> graph tools (no new read surface). Versioned by `relation_schema_version` + `prompt_version` +
> `model_version` (the judgment cache key).

## Why this exists

Candidate-then-judge is the architecture, not an optimisation (ADR-0010): a global LLM sweep over
every artifact pair is O(N²) and financially dead at scale. Phase 3A proved the cheap generator has
the recall to be worth judging. Phase 3B is the **first place the LLM rules on a relationship**, and
it does so ONLY over the bounded candidate set — never a global sweep — with every call cached so an
unchanged pair is never re-judged.

## What the judge rules on

The judge sees ONE candidate pair at a time: the two artifacts' titles + their evidence spans
(`knowledge_artifact.body_text`). It returns a verdict:

```json
{
  "relation_type": "documents",
  "trust_bucket": "INFERRED_HIGH | INFERRED_LOW | AMBIGUOUS | REJECTED",
  "supporting_quote": "<verbatim substring of one of the two source spans>",
  "reason": "<1-2 sentences>"
}
```

- **`relation_type`** is a closed ontology vocabulary. The judge may emit ONLY relations a
  prose/cross-domain judge is permitted to infer — in V1 just `documents` (doc/card → code,
  the one ontology type that may be `INFERRED_*`, relation-ontology.md). The judge MUST NEVER emit
  `related_to` (banned catch-all) or an AST-only deterministic relation
  (`imports`/`calls`/`inherits`/`exposes`/`tests`/`implements`/`mentions`). A relation outside the
  judge vocabulary is forced to `AMBIGUOUS` (never invented as a real edge).
- **`trust_bucket`** is an LLM-judge bucket (trust-buckets.md). The judge may NEVER assign
  `EXTRACTED` — that bucket is reserved for deterministic producers. An `EXTRACTED`/unknown bucket
  from the model is forced to `AMBIGUOUS`.

## Quote-guard (invariant 7)

`supporting_quote` MUST be a verbatim substring of one of the cited source spans (whitespace
collapsed on both sides, otherwise exact — never fuzzy/semantic). If it is not, the judgment is
**downgraded to `AMBIGUOUS`** at the call boundary, so a fabricated quote can never become an
`INFERRED_*` edge. An empty quote is never grounded.

## How a verdict becomes an edge

| bucket          | edge written?                          | trust_class | served by default | claim support |
|-----------------|----------------------------------------|-------------|--------------------|---------------|
| `INFERRED_HIGH` | yes (`source='llm_judge'`)             | `INFERRED_HIGH` | only `include_inferred=true` | no — routing hint |
| `INFERRED_LOW`  | yes (`source='llm_judge'`)             | `INFERRED_LOW`  | only `include_inferred=true` | no — routing hint |
| `AMBIGUOUS`     | yes (`source='llm_judge'`)             | `AMBIGUOUS`     | **never** (broker excludes)  | no |
| `REJECTED`      | **no** — retained in the cache only    | —           | —                  | no |

Every `INFERRED_*` / `AMBIGUOUS` edge carries: `edge_type` = `relation_type`,
`from_artifact_id`/`to_artifact_id` (the candidate's ordered pair), `trust_class` = the bucket,
`source='llm_judge'`, `relation_schema_version`, `evidence` = the quoted-span pointer
(`{"quote": …, "judge_prompt_version": …}`), and **`valid_from_seq` = this build's `build_seq`**
(interval membership, version-membership.md) so the broker actually serves it.

## Relationship-judgment cache (gates the model — invariant 4)

`relationship_judgment_cache` is keyed by `(hash_a, hash_b, relation_schema_version,
prompt_version, model_version)`, where `hash_a`/`hash_b` are the two endpoints' content hashes,
**sorted** so the key is direction-independent. A cache **hit returns the stored verdict and makes
ZERO LLM calls**, exactly like `generation_cache` / `embedding_cache`. Bumping any key part
(schema, prompt, or model) re-judges affected pairs. Idempotent on rebuild: the composite PK +
on-conflict-do-nothing make a re-run a no-op; judge edges upsert on a partial unique index
(`from`, `to`, `edge_type` WHERE `source='llm_judge'`) so no duplicate edge is accreted.

This table is a build-plane gate/audit artifact — NOT served through MCP, so it carries no
membership columns.

## Consumption (broker, already enforced — PR-23)

- `graph.get_neighbors` with `include_inferred=true` surfaces `INFERRED_*` edges as labelled
  routing hints (`claim_supporting=false`); the default (`include_inferred=false`) hides them.
- `AMBIGUOUS` / `REJECTED` are NEVER in default traversal.
- The verifier (L0) rejects an `INFERRED_*` edge as direct claim support (verification-receipt.md
  `L0_supporting_trust_ok`).

## Out of scope (phase 3B)

The judge never re-judges a deterministic fact (those pairs are excluded from candidates). It does
not write `EXTRACTED` edges, does not invent relations outside the vocabulary, and does not run a
global sweep.
