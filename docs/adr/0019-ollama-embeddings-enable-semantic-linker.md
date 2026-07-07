# ADR-0019 — Ollama embeddings provider + enable the semantic linker

- Status: Accepted
- Date: 2026-06-15
- Deciders: Rohit Sharma (with an external architecture-review verdict)
- Related: ADR-0010/0011 (relationship candidates + judge), ADR-0017 (local search parity),
  ADR-0018 (code is graphify-only), `linker/semantic.py`, `linker/candidates.py`

## Context

A real production build (both services' `src` + ADO wiki/work-items) produced a graph that is
**two weakly-joined islands**: a code call-graph and a prose/concept graph, bridged by only
~95 edges. Measured on the active KB: **only 39 code artifacts (2.4%) are reachable from any
prose node**; 66% of nodes are isolated.

Root cause, confirmed in code — not corpus size:

1. The build wires `LocalHashEmbedder` (ADR-0017): an 8-dim **hash** vector. It satisfies "every
   indexable artifact has *a* vector so the index is rebuildable," but cosine similarity over hash
   vectors is meaningless, so it cannot drive semantic linking.
2. `BuildEngine` already threads `similarity: SimilarityProvider | None` to both `run_linker`
   (`linker/run.py`) and `run_candidate_generator` (`linker/run_candidates.py`), but it is
   constructed with **`None`**. So the semantic candidate signal and the `implements`
   (code_symbol → concept) fallback are hard-skipped — logged `reason=no_provider`. The Protocol
   has had no real implementation since it was written ("until the Azure Search projection lands").

An external review was commissioned. Its blunt verdict: *"You have a cross-domain graph product
whose cross-domain linker is turned off. Fix that first."* Structural edges (symbol→file,
file→file) are correct but **cleanup, not the fix**; the disabled semantic linker is the defect
that matters for agent context. Embeddings-first is the cheapest high-impact move.

## Decision

1. **Add a real embeddings provider** implementing the existing `Embedder` Protocol
   (`async embed(text) -> EmbeddingResult`), backed by **Ollama `nomic-embed-text`** (768-dim) via
   the OpenAI-compatible/`/api/embeddings` endpoint, reusing the `LLM_PROVIDER`/`LLM_BASE_URL`
   configuration pattern already established in `chat_model_client.py`. Local, zero marginal cost,
   matches the cost-conscious invariant. Provider/model/base-url are env-driven; a hosted
   OpenAI-compatible endpoint (Azure/OpenAI) is a drop-in alternative with no code change.

2. **Implement the `SimilarityProvider`** (`linker/semantic.py` Protocol,
   `similar_code_symbols(artifact_id, top_k)`) backed by that embedder: embed each linkable
   artifact's text once, **cache the vector in `embedding_cache`** keyed by
   `(artifact_id, text_hash, embedding_model)`, and answer nearest-neighbour by in-memory cosine
   over the `code_symbol` corpus. Cache reuse keeps it incremental — unchanged artifacts are never
   re-embedded (invariant 4, cost discipline).

3. **Inject the provider into the build** only when embeddings are configured; `similarity=None`
   stays the default so unit/contract tests remain hermetic (no Ollama dependency in CI).

4. **Measure the right thing.** Success = the judge's metric: prose→code reachability and
   golden-query graph-expansion quality, not isolated-node count.

## Consequences

- The semantic candidate signal (ADR-0010 3A) and the `implements` semantic fallback turn on, so
  prose↔code edges are produced by meaning, not only exact name matches. This is expected to move
  prose→code reachability well above the 2.4% baseline (re-measured after the rebuild).
- A new optional runtime dependency (an embeddings endpoint). Hermetic tests and CI do not require
  it; only a real semantic build does. Hash embeddings remain valid for index parity tests.
- Embedding vectors are derived, cached, and rebuildable — they are not truth and never gate
  activation on their own (the publish gates already cover index/citation/recall).
- We do **not** relax ADR-0018: code is still never summarised by the LLM. Embedding a code span
  is not "sending code to a chat model for generation"; it is a deterministic vector lookup with
  no generated tokens, and it is cached. Confidence on a semantic edge is the raw similarity,
  never inflated to look deterministic (trust contract).

## Alternatives considered

- **Structural edges first** (symbol→file, file→file): rejected as the *first* move per the review
  — real but cleanup; it repairs metrics, not prose→code reachability. Sequenced after (task #128).
- **Ingest whole-repo Graphify edges/communities as truth**: rejected. Graphify whole-repo output
  may serve as a *diagnostic/projection* only; ingesting its inferred edges would bypass our
  trust contract and ACL/evidence model.
- **Hosted embeddings (Azure/OpenAI) now**: viable and a zero-code config swap later, but Ollama is
  already running locally and free, so it is the default for development.
- **Relax zero-LLM-for-code with code summaries**: rejected. Summaries are not relationships and
  would mask the disabled linker under recurring cost; only a post-eval fallback (ADR-0018 Phase 3).

## Amendment (2026-07-07): `EMBEDDINGS_PROVIDER` is validated; `openai` implemented for real (task #39)

This ADR's "a hosted OpenAI-compatible endpoint (Azure/OpenAI) is a drop-in alternative with no
code change" was aspirational, not true: `EMBEDDINGS_PROVIDER` was a pure on/off gate (the value
was never inspected), and the one embedder, `OllamaEmbedder`, always spoke Ollama's native
`/api/embeddings` shape (`{"model","prompt"} -> {"embedding"}`) — pointing `EMBEDDINGS_BASE_URL`
at a real OpenAI/Azure OpenAI endpoint silently sent the wrong request shape and failed deep
inside the call, not at build start.

`EMBEDDINGS_PROVIDER` is now validated by `embeddings/factory.py::semantic_embedder_from_env`:
`ollama` (unchanged `OllamaEmbedder`) or `openai` (new `OpenAIEmbedder`, the real `/v1/embeddings`
shape — `{"model","input"} -> {"data":[{"embedding":[...]}]}`, `EMBEDDINGS_API_KEY` required).
Any other value raises a `RuntimeError` at build start. Both embedders share a small `HttpEmbedder`
base for client lifecycle (`embeddings/http_embedder.py`); `EmbeddingSimilarityProvider.aclose()`
closes either via `isinstance(embedder, HttpEmbedder)`. See docs/dev-guide/reference/environment-variables.md
for the full var/shape reference.
