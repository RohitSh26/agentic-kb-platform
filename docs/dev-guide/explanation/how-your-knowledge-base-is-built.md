# How your knowledge base is built

Every night — or whenever you run a build — the platform turns your sources into a knowledge
graph in Postgres. This page explains what happens between "the build started" and "your agents
are answering from it", and why a bad build can never reach them. It is a concept page; the
commands live in [rebuild-after-changes](../how-to/rebuild-after-changes.md) and
[index-your-own-sources](../how-to/index-your-own-sources.md).

## Sources come in deterministically

Connectors fetch your sources: code and docs from GitHub (or a local checkout), Azure DevOps wiki
pages, work items, and your repository's own git commits. Five source types exist: `github_code`,
`github_doc`, `azure_wiki`, `ado_card`, and `git_metadata`.

Connectors are deterministic. The same source state always normalizes to the same content and
therefore the same `content_hash`, on any machine. Every fetched source is recorded with three
things: where it came from (`source_uri`), which version was read (`source_version` — a commit
SHA, a wiki revision, a work-item revision), and the hash of what was read (`content_hash`).
Everything downstream is anchored to those three facts.

## The hash gate: unchanged means untouched

Before any expensive work, the build compares each source's fresh `content_hash` with the one it
stored last time. A match means the source is skipped entirely — no chunking, no model call, no
re-embedding, no re-indexing. This is why an incremental build over a mostly unchanged codebase
finishes in seconds and makes near-zero model calls.

Two caches back this up for the sources that did change:

- The **generation cache** keys every model call on all of its inputs — content hash, prompt
  version, model name, parameters, output schema version. A hit returns the previously produced
  artifacts; the model is not called.
- The **embedding cache** does the same for embeddings, keyed on the artifact, its text hash, and
  the embedding model.

The rule is structural, not advisory: a cache hit means no model call. Model outputs are also
written to a crash-durable cache on a separate connection, so an interrupted build does not pay
for the same generation twice — and a cache problem degrades to one paid model call, never a
failed build.

The build commits **per source**. Knowledge lands as it is produced, so an interrupted build keeps
everything already committed, and one source's failure rolls back only that source. The failure is
counted, logged, and the rest of the build proceeds.

## Extraction: code deterministically, prose with a model

**Code** is extracted by Graphify in a single whole-tree pass — zero model calls. It yields files,
symbols, endpoints, and tests with exact line spans, plus structural edges: `defined_in`, `calls`,
`imports`, `inherits`, `uses`, `references`. Each symbol also carries a deterministic,
skeleton-style `search_text` — its signature and the head of its docstring — which is why
retrieval snippets read like code you can recognize at a glance. Skeletons are for finding and
thinking; citations always point at exact spans at a source version.

**Prose** (docs, wiki pages, work items) is summarized by a chat model into `summary` and
`concept` artifacts. The platform re-derives trust deterministically afterwards: a statement whose
supporting sentence is a verbatim substring of the source is promoted to a citable
`source_backed_fact`; a paraphrase stays an `interpreted` concept. Interpreted knowledge always
ranks below source-backed evidence at retrieval time — a generated summary is never treated as
truth.

## Linking: precision first

The linker connects the prose layer to the code layer. Over-linking is the failure mode it is
built against, so it works in order of certainty:

1. **Deterministic linking** — exact, word-boundary textual evidence connecting concepts to code.
2. **Cross-domain explicit references** — a commit that names a work item (`AB#123`), a commit's
   changed files, a doc that names a work item. A bare incidental number never produces a link.
3. **A bounded candidate-and-judge pass** — a cheap, deterministic generator proposes a bounded
   set of likely pairs; a chat model rules on only that set. Judge verdicts can only ever become
   `INFERRED_*` or `AMBIGUOUS` edges — routing hints, never citable support. Every judgment is
   cached so an unchanged pair is never re-judged.

Linker edges are reconciled to one row per logical link, and an edge whose textual evidence has
disappeared is deleted. The graph never serves a link whose justification no longer exists.

## The alias index: the names people actually use

A deterministic miner extracts the names your team actually uses — symbol names, doc slugs,
recurring phrases — and aggregates them across sources with confirmation counts. This index is
what lets a plain-language question like "the alias reference index" resolve straight to the right
file. Alias entries inherit the **intersection** of their confirming sources' ACLs; an alias can
narrow access, never widen it.

## Ledger mining: your misses become next build's hits

The build also reads the retrieval ledger — the record of every `kb_search` call your agents made
— and looks at the misses: answered calls that returned nothing, or nearly nothing. The exact
phrases that missed are mined into new alias entries, so the same question resolves after the next
build. This pass is zero-LLM and strictly read-only over the ledger; it never modifies a ledger
row. Each build records how many misses it saw, how many it mined, and how many remain unresolved,
and the dashboard shows the mined-vs-unresolved split day by day (ADR-0034). Your knowledge base
literally learns from what it failed to answer.

## Centrality: structure informs ranking

After the graph is assembled, each artifact gets a centrality score derived from its position in
the graph. Retrieval uses it as a ranking prior: code that everything imports outranks a leaf
utility when both match a query equally.

## Version membership: a version is an interval, not a label

Every build gets a `kb_version` label and a monotonically increasing `build_seq`. But rows are not
stamped "version N" — they carry a validity interval: `valid_from_seq` (the build that introduced
them) and `invalidated_at_seq` (empty while live). A row belongs to a version if that version's
sequence falls inside the interval.

This is why an incremental build's new version serves the **complete** knowledge set, not the
day's delta: unchanged artifacts carry forward automatically. Before activation, an invalidation
pass reconciles identity over time — renamed sources keep their history and their edges, deleted
sources are tombstoned and their rows invalidated, superseded generations are closed out, and ACL
changes are propagated. ACL revocation is the one deliberate exception to immutability: a revoked
permission is applied to live rows in place, so it takes effect on every still-served version, not
just future ones.

## Publish gates: why a bad build cannot reach your agents

A version is not served because it finished. It is served because it passed the publish gates:

| Gate | What it refuses to serve |
|---|---|
| `index_consistency` | a search projection that disagrees with Postgres |
| `extractor_error_rate` | a build where more than 1% of sources failed extraction |
| `symbol_count_delta` | a suspicious collapse or explosion in extracted symbols |
| `no_dangling_citations` | facts whose cited source rows are missing |
| `edge_evidence_integrity` | ghost edges — edges whose endpoints are not members of the version, or with an out-of-vocabulary type |

The first failing gate records its name and measured value on the build run, and the version
simply never activates. Nothing rolls back, because nothing was ever promoted: the previous active
version keeps serving, untouched. Exactly one version is active at a time (a database constraint
guarantees it), and the server only ever serves the active one. One gate is overridable —
`symbol_count_delta`, for legitimate large refactors — and the override is recorded on the build
run and logged.

One more fence: a Postgres advisory lock keeps builders single-writer. A second build against the
same registry aborts immediately rather than queueing, and the lock is released even on a crash.

## The trust vocabulary

Two small vocabularies carry trust through the whole platform:

- **Trust buckets on edges** — `EXTRACTED` (deterministic producers only) is the one bucket that
  can support a cited claim. `INFERRED_HIGH` and `INFERRED_LOW` are routing hints, surfaced only
  on request and never citable. `AMBIGUOUS` is excluded from default traversal. `REJECTED` is
  retained for audit and never returned. Buckets are derived from the producing mechanism, never
  from a free-floating score.
- **Confidence tiers on retrieval results** — every hit and every resolved entity carries
  `ground_truth`, `deterministic`, or `interpreted`, so an agent (and you) can always see how a
  piece of knowledge was established.

The full vocabularies are contracts:
[trust-buckets](../../contracts/trust-buckets.md),
[publish-gates](../../contracts/publish-gates.md),
[version-membership](../../contracts/version-membership.md).

## Related

- [Governance and budgets](governance-and-budgets.md) — what happens when agents read this graph.
- [Observability](observability.md) — the records each build and retrieval leaves behind.
- [Rebuild after changes](../how-to/rebuild-after-changes.md) — the commands.
