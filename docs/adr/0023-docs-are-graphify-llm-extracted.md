# ADR-0023 — Documents are Graphify-LLM extracted; retire the hand-rolled wikify pipeline

## Status

Accepted (2026-06-17, architecture-guardian APPROVE-WITH-CHANGES; all changes folded in). Amends ADR-0012 (Graphify is the extraction backend) by extending Graphify
from **code-only** to **prose/document** sources. Supersedes the `wikify` pipeline (chunker +
`WikifyGenerator` LLM prose→artifacts) and the doc half of ADR-0018's routing.

## Context

ADR-0018 routes prose sources (`github_doc`, `azure_wiki`, `ado_card`) through our own `wikify`
module: a deterministic chunker plus an LLM call (`WikifyGenerator`, behind `ModelClient`) that emits
a summary + concepts + source-backed facts. Building a held-out KB from a foreign library (httpx)
exposed that we had reimplemented, mostly worse, capabilities the Graphify library already ships:
its LLM pipeline extracts **semantic concept nodes from prose** and links them into the same graph as
code (`docs/architecture` §5; the owner-driven delegation decision of 2026-06-17). Continuing to own
`wikify` is the same wheel-reinvention ADR-0012 rejected for code.

We verified Graphify's document LLM extractor empirically (`graphify.llm.extract_files_direct`,
driven at our Groq endpoint via an in-process backend registration). For a small auth guide it
returned, per concept:

```
{"id":"login_flow","label":"login flow","file_type":"concept","source_file":"auth.md",
 "source_location":"The login flow validates a session token against the AuthMiddleware",
 "source_url":null,"author":null,"contributor":null}
```

plus concept→concept edges (`calls`, `cites`, `conceptually_related_to`) with `EXTRACTED`/`INFERRED`
confidence. The decisive finding: **`source_location` for a concept node is the verbatim supporting
sentence from the source text** — exactly the citable anchor our trust contract (a
`source_backed_fact` carries a verbatim quote; the broker's L0 verifier does a quote-substring check)
requires. So Graphify's doc output is mappable to broker-grade, citable, ACL'd artifacts — provided
we re-derive trust ourselves rather than copy Graphify's labels (the ADR-0012 rule, again).

## Decision

1. **Graphify's LLM pipeline is the document-extraction backend.** A new adapter
   (`docify`, mirroring `graphify_backend`) calls `graphify.llm.extract_files_direct([doc], backend,
   api_key, model, root)` and re-normalizes the result into our `knowledge_artifact` model (doc
   **artifacts only** — see §3b for why no edges). We do **not** use Graphify's graph.json / MCP /
   report as truth (invariant 1, same as ADR-0012).

2. **Graphify owns the doc LLM call; we configure it from our existing model env.** Code extraction
   needs no key, but doc extraction does. We register a Graphify backend in-process from the same
   `LLM_PROVIDER` / `LLM_API_KEY` / `LLM_MODEL` the `ChatModelClient` already resolves (e.g. Groq →
   `https://api.groq.com/openai/v1`). This is a **deliberate, documented exception** to the "all model
   calls go through `ModelClient`" rule (rules/python.md): the value of Graphify is that *it* owns the
   prompt + extraction; wrapping that in our `ModelClient` would mean reimplementing the prompt, i.e.
   the reinvention we are removing. The interface boundary we keep is the *adapter* (`docify`), which
   is the single swappable seam. No corpus or key leaves the configured endpoint (Graphify's custom-
   provider base_url guard applies; we never load project-local provider files).

3. **Trust is re-derived, never copied** (ADR-0012 rule). Two SEPARATE axes — do not conflate them:

   a. **Artifact `knowledge_kind` (per concept node).** Check whether the node's `source_location` is
      a verbatim substring of the normalized source text, using the **exact same normalization as the
      broker's L0 verifier** (`verify._normalize_whitespace` in `services/mcp-server`). The two layers
      MUST share one whitespace-normalization rule so an artifact promoted to `source_backed` at build
      time cannot fail L0 grounding at read time (or vice-versa); the rule is pinned in the artifact
      contract, not duplicated divergently.
      - **substring present →** `knowledge_kind="source_backed"`, body carries the verbatim quote.
        This is what the L0 verifier and `verify_answer` can confirm.
      - **substring absent →** `knowledge_kind="interpreted"` (a `concept`): the model paraphrased; we
        keep it as interpreted knowledge ranked below source-backed evidence, never as a false
        citation (invariant 7).
      The document node itself becomes an interpreted `summary`/pointer artifact (its `source_location`
      is a heading, not a quote).

   b. **No concept→concept edges (artifacts only).** Graphify's LLM doc extraction also emits
      concept→concept relations (`calls`/`cites`/`conceptually_related_to`). These are generic
      concept-relatedness, which the relation ontology **explicitly bans** as an edge
      (`docs/contracts/relation-ontology.md`: "Banned: `related_to` and any other generic catch-all
      … it becomes a candidate (phase 3 audit table) or an open question, never an edge"). They do not
      fit any allowed edge type with valid evidence (`mentions` requires a verbatim-identifier match
      and is `EXTRACTED`; `documents` is doc→code, not concept→concept; and a single-document
      extraction has no code artifacts to resolve a `documents` edge against). Therefore **docify
      writes ARTIFACTS ONLY and creates no edges** — exactly the parity posture of the wikify it
      replaces, which also wrote no edges. A `source_backed` concept artifact stays independently
      citable (L0 admits standalone source-backed evidence). Promoting these concept relations into the
      phase-3 candidate table for the LLM judge to rule on (where they may legitimately become
      `INFERRED_*` `documents`/judged edges) is a tracked **follow-up**, not part of this change.

   The artifact `acl_teams` is derived from the **source_item ACL** (the connector's source ACL) and
   is **never** widened and **never** taken from Graphify output — a concept inherits its document's
   ACL exactly (the connectors-rule "never widened" posture).

4. **The LLM is gated by the generation cache (invariant 4).** The doc LLM call is keyed by
   `(content_hash, graphify_doc_prompt_version, model, params)`; an unchanged document is a cache hit
   and makes **no** LLM call. The cache stores the **mapped artifact rows** (our normalized
   `knowledge_artifact`/`knowledge_edge` drafts), NOT the raw Graphify JSON — so a replay never re-runs
   a (possibly newer) mapper over stale output and silently shifts a trust classification. This is the
   same `generation_cache` + `generation_cache_artifact` replay `wikify` used. Non-determinism of the
   LLM is bounded by this content-hash gate (one extraction per unique document content) plus
   `temperature=0`. The `docify` write path is idempotent: a retry produces no duplicate
   artifacts/edges/cache rows (same guarantee as the whole-tree code path).

5. **`wikify` is removed.** `WikifyGenerator`, the chunker, and the wikify write path are deleted;
   `github_doc` / `azure_wiki` / `ado_card` route through `docify`. The *artifact contract* (the
   `knowledge_artifact` shape: types, `knowledge_kind`, authority/freshness scores, `acl_teams`,
   citable body) is **frozen and unchanged**, so the broker, verifier, and Search projection are
   unaffected and **no Alembic migration is required**. `WikifyArtifactDraft` is retained or renamed
   to a doc-artifact draft with identical fields. `docify` takes the Graphify extraction function as
   an **injectable dependency** so unit tests run hermetically against a captured-fixture extraction
   (mirroring how `map_extraction` is tested against a captured `graph.json`) — the live LLM is never
   required by the test suite.

6. **Validated before it lands.** The change is proven by rebuilding the dogfood `agentic_kb` KB
   end-to-end and running the retrieval evals; doc-source retrieval quality must not regress versus
   the wikify baseline. Removal of `wikify` and the rebuild ship together.

## Consequences

- One LLM pass per document now yields **citable concept/fact artifacts with verbatim anchors**,
  replacing `wikify` (summaries/concepts/facts). Fewer moving parts, real delegation. Graphify's
  concept→concept relations are deliberately NOT written as edges (§3b) — parity with wikify, which
  wrote none.
- Doc knowledge that the model paraphrases (no verbatim anchor) is correctly demoted to `interpreted`
  rather than fabricating a citation — a *stricter* provenance posture than wikify's free-text facts.
- New constraint: the build now needs an LLM key for **documents** (code stays key-free). Offline/
  no-doc builds are unaffected; a missing key fails the doc path loudly, never silently drops docs.
- Graphify owns a model call outside `ModelClient`. Mitigated by §2 (single adapter seam, same env)
  and accepted as the cost of delegation; revisit if we need provider features Graphify lacks.

## Alternatives rejected

- **Keep `wikify`.** The reinvention we are removing; an external/foreign-KB eval showed it is weaker
  than Graphify's pipeline.
- **Use Graphify's offline `extract` for docs.** It emits only heading structure (`contains`), not
  interpreted concepts or verbatim concept anchors — it does not replace `wikify`.
- **Wrap Graphify's doc LLM call inside `ModelClient`.** Would mean re-owning the prompt/extraction —
  exactly the reinvention this ADR removes; the swap seam is the `docify` adapter instead.
- **Trust Graphify's `source_location` as citable without checking.** Would let a paraphrase pose as a
  verbatim quote and break the L0 verifier; we substring-verify every quote.

## Status note

Reviewed by `architecture-guardian` (2026-06-17): **APPROVE-WITH-CHANGES**; all six requested
tightenings folded into §3–§6 above (shared whitespace-normalization with the verifier; cache stores
mapped rows; injectable extractor for hermetic tests; ACL from source_item only; doc edges
`INFERRED_*`/`AMBIGUOUS` never `EXTRACTED`; idempotency + schema-frozen/no-migration).

## Follow-ups

- Promote Graphify's concept→concept doc relations into the phase-3 candidate table so the LLM judge
  can rule on them (→ `INFERRED_*` `documents`/judged edges where warranted), instead of dropping them.
- Pin `graphifyy`; contract-test the doc-extraction output schema against the adapter.
- Contract-test that the Graphify backend registration never logs `LLM_API_KEY` and that the
  custom-provider base_url guard rejects a non-http(s)/exfiltration endpoint.
- Tune the concept→concept edge trust posture against eval feedback (these overlap the linker/judge
  cross-domain edges; the linker/judge retirement is a later phase of the delegation decision).
- Consider `extract_corpus_parallel` (chunked, token-budgeted) for large docs once single-doc is
  proven.
