# Rule: Connectors + incremental build (services/kb-builder/src/agentic_kb_builder/connectors)

- Connectors are deterministic: same source state ⇒ same normalized content ⇒ same content_hash.
- Sources: github_code (commit SHA), github_doc (commit SHA), azure_wiki (page id+revision),
  ado_card (revision), git_metadata (local-repo commit; source_version = full commit SHA, one
  source per commit). Always capture source_uri + source_version + content_hash.
- git_metadata is zero-LLM: a commit becomes ONE deterministic `commit` artifact (no wikify, no
  graphify). Its acl_teams = the intersection of its changed files' source ACLs (never widened).
- Build is incremental and idempotent: compute content_hash, compare to source_item; if unchanged,
  skip chunk/wikify/graphify/embed/index entirely.
- Graphify runs only for changed code files; Wikify runs only on generation_cache miss.
- A kb_version is marked active only after retrieval/index consistency validation succeeds.
- Build jobs must be safely re-runnable (no duplicate artifacts/edges/cache rows on retry).
