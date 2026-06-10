# Rule: Postgres Knowledge Registry (services/kb-builder)

- Postgres is the source of truth. Azure AI Search is a derived projection that must be rebuildable
  from Postgres + source pointers. Never write truth only to Search.
- Store source pointers (uri, version, hash), chunks, summaries, concepts, and evidence-ready text.
  Do NOT store full raw documents by default. Snapshot only mutable/unversioned sources (e.g. ADO
  card fields) — see docs/architecture §"Raw document storage policy".
- Canonical tables: source_item, knowledge_artifact, knowledge_edge, generation_cache,
  embedding_cache, kb_build_run, retrieval_event. UUID PKs, timestamptz, explicit FKs.
- Graph edges live in knowledge_edge with edge_type, confidence, source, kb_version. Expose graph
  behavior only through MCP graph tools so the backend can change later. No graph DB in V1.
- Every model/embedding call is gated by a cache key. Cache hit ⇒ no LLM, no embedding.
