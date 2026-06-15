# ADR-0018 — Code is graphify-only: deterministic spans + metadata, never the LLM

## Status

**Accepted** (2026-06-15) — owner-directed, confirmed by two independent design reviews. Completes
the intent of ADR-0012 (Graphify adopted as the code extractor) and the "unified graphify vision":
**code is extracted deterministically; the LLM is reserved for prose (docs/wiki/work-items) and the
cross-domain relationship judge.** Supersedes the current behaviour where every `github_code` file is
ALSO sent through wikify (one LLM call per file, per nightly build).

## Context

The platform serves an engineering org's knowledge to **LLM agents** through the MCP Context Broker.
Agents answer cited questions ("how does X work?", "what calls Y?", "where is X defined?") using two
retrieval mechanisms: **search** over `knowledge_artifact.body_text` (lexical now, semantic later —
only artifacts WITH `body_text` are searchable) and **graph** traversal over typed edges.

Two pipelines build artifacts: **graphify** (deterministic AST → `code_file`/`code_symbol` nodes +
structural edges, NO LLM) and **wikify** (LLM → `summary`/`concept` artifacts WITH `body_text`).

Today code goes through **both**. Critically, graphify's `code_symbol` artifacts are **pointer-only**
(`body_text = NULL`), so code is keyword-searchable today **only because wikify writes an LLM summary
per file**. That is recurring per-file LLM spend, nightly, over the whole codebase — to produce a
*lossy, unciteable paraphrase* of evidence the consuming agent (itself an LLM) could read directly
from the raw code. The reviews were blunt: for an agentic code-retrieval system this is the wrong
cost shape, and an LLM summary of code adds little the agent can't get from the snippet.

## Decision

**Code is graphify-only. The LLM never sees routine code.** Per `github_code` symbol, store:

- `code_symbol.body_text` = the **exact raw source span** (start..end line, including the symbol's
  leading docstring/decorators). This is the **citable evidence** an agent reads.
- `code_symbol.search_text` (deterministic retrieval surface, NO LLM) = qualified name + split
  snake/camel identifiers + signature + decorators + docstring + comments + import names + called
  symbol names + route strings / log messages / config keys present in the span.
- `code_symbol` metadata = name, qualified_name, kind, file_path, start_line, end_line, signature.
- `code_file.body_text` = optional deterministic file-level text (imports / top-of-file comments).
- `knowledge_edge` = structural navigation (imports / calls / contains / references / documents).

**Wikify (LLM) runs ONLY for prose sources** (`github_doc`, `azure_wiki`, `ado_card`). LLM code
summaries are **not** part of routine ingestion; if recall ever demands them they are **targeted,
on-demand, and cached** (keyed by `code_hash + prompt_version + model_version`) for hot/ambiguous
symbols — never a nightly per-file tax. The cross-domain **relationship judge** (LLM) stays as-is.

### Why (the reviews, condensed)
- The consumer is an LLM that reads code natively; a pre-written summary is a lossy second source of
  truth that can drift and omit the exact identifier the answer must cite. The snippet IS the
  evidence; the summary is an unciteable gloss.
- Raw code search wins the dominant code queries — exact symbol/route/config tokens are literal in
  code and often reworded or absent in a summary. Summaries only win for business-language "intent"
  queries against badly-named code; that gap is better closed deterministically (docstrings/comments
  in the span, split identifiers) + the graph linking the symbol to the doc that explains it (docs
  stay wikified) — not by summarizing every file.
- Semantic search **strengthens** this: embed the deterministic `search_text`, not minified code,
  and the conceptual-recall gap (B's only real argument) mostly closes — while B's cost stays linear
  in file count.

## Consequences

- **Zero LLM tokens for code ingestion.** A `github_code`-only `sources.yaml` (+ the automatic
  `git_metadata`) builds with **no LLM at all** — real production-source builds of your own repos
  need no Ollama/model spend; an LLM is needed only when you add doc/wiki/work-item sources.
- Code remains keyword-searchable (on exact spans + `search_text`) and graph-navigable; evidence
  cards for code are now the **real code**, not a paraphrase.
- **Implementation reality:** the external Graphify extractor emits only start-line + kind + imports,
  so exact spans + metadata require a **deterministic AST pass** (Python's `ast`: `end_lineno`,
  `get_source_segment`, `get_docstring`, decorators, signature). Python-first; other languages fall
  back (file-level body / next-sibling span) until per-language span recovery lands. Reuse the
  existing `graphify/to_artifacts.py` `_snippet(span_start, span_end)` machinery.
- **Recall risk** (raw code under-serves concept queries that name no code token) is mitigated by:
  docstrings/comments in the indexed span, the `search_text` enrichment, graph links from
  docs/wiki/work-items to code, prioritising semantic search for code, and optional cached on-demand
  summaries for hot symbols — never nightly.

## Phasing

- **Phase 1 (unblock the cost goal):** deterministic exact-span recovery → `code_symbol.body_text`
  (incl. leading docstring/decorators); flip `github_code` routing to graphify-only (no wikify);
  guard test asserting a code-only build has `llm_calls == 0` AND produces searchable `code_symbol`
  artifacts. Ships zero-LLM, searchable code.
- **Phase 2 (recall):** the deterministic `search_text` field + symbol metadata + index/search over
  it (lexical).
- **Phase 3 (later):** semantic/embedding search over `search_text`; optional targeted, cached
  on-demand LLM summaries for hot/ambiguous symbols (its own ADR if structural).

## Alternatives rejected

- **Keep wikify on code (status quo):** recurring per-file LLM spend for a lossy, unciteable proxy
  of evidence the agent can read directly; redundant once semantic search lands. Rejected.
- **Graphify-only, graph-reachable only (no snippet):** drops code keyword search, so the broker can
  only find a symbol if another searchable artifact already points at it — no entry point. Rejected.
