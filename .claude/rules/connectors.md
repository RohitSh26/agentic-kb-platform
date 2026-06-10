# Rule: Connectors + incremental build (services/kb-builder/src/agentic_kb_builder/connectors)

- Connectors are deterministic: same source state ⇒ same normalized content ⇒ same content_hash.
- Sources: github_code (commit SHA), github_doc (commit SHA), azure_wiki (page id+revision),
  ado_card (revision). Always capture source_uri + source_version + content_hash.
- Build is incremental and idempotent: compute content_hash, compare to source_item; if unchanged,
  skip chunk/wikify/graphify/embed/index entirely.
- Graphify runs only for changed code files; Wikify runs only on generation_cache miss.
- A kb_version is marked active only after retrieval/index consistency validation succeeds.
- Build jobs must be safely re-runnable (no duplicate artifacts/edges/cache rows on retry).
