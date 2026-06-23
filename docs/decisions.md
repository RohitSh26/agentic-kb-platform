# Design decisions

The rationale behind the platform's key choices, for operators and integrators.

## Postgres is the source of truth

Artifacts, edges, caches, build runs, and the retrieval ledger live in Postgres. The search index is a
derived projection that can be rebuilt at any time from Postgres plus source pointers. This keeps a
single, auditable system of record and means a lost or corrupted index is never a data-loss event.

## The graph lives in Postgres, not a graph database

Nodes and edges are stored in ordinary Postgres tables, and graph behaviour (neighbour lookup,
expansion, centrality) is exposed only through broker tools. This avoids operating a separate graph
database while keeping the option to change the backend later without touching callers.

## Knowledge-first, file-fallback

The knowledge base is a fast, budgeted helper, not a gate. Agents keep their native file tools and
consult the knowledge base first; they read specific files directly when the knowledge base is missing,
partial, or stale. This keeps agents fast and capable while still benefiting from cross-source,
cross-repository grounding that file search alone cannot provide.

## Cost is controlled in code, not by prompts

Two independent levers, both enforced outside the prompt:

- **Budget** — knowledge search carries a per-task call and token cap enforced in the tool. When the
  cap is reached, the tool stops and tells the agent to work with what it has or read specific files.
- **Compression** — code an agent reads is returned as a reversible skeleton (signatures and types
  kept, bodies elided), with the exact body available on demand. This shrinks the dominant token cost
  (reading whole files) without ever restricting what the agent can read.

## Incremental, cache-gated builds

Every model call is gated by a content-addressed cache: an unchanged source makes no model call and no
re-embedding. Model outputs are persisted durably as they are produced, so a build interrupted partway
through resumes without paying for the same extraction or embedding twice, while the published version
still flips atomically.

## Versions activate atomically after validation

A new knowledge-base version becomes active only after index/retrieval consistency checks pass. Until
then, the last good version keeps serving. Membership is tracked by version interval, so an incremental
build that rewrites one source does not disturb the rest of the served graph.

## Ranking uses graph centrality

Retrieval ranks candidates not only by keyword relevance and provenance but also by how central a node
is in the dependency graph. Structural importance — what the rest of the codebase depends on — is a
signal that plain text search cannot compute, and it is the platform's distinctive advantage for code.

## Governance lives at the boundary

Authentication, access-control filtering, untrusted-content handling, and a full retrieval audit ledger
are enforced at the broker boundary. Retrieved documents are always treated as untrusted content that
cannot change tool policy, identity, or instructions. Every served claim cites evidence; an answer is
trusted only when it carries a valid provenance receipt.
