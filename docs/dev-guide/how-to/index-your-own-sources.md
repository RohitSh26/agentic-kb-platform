# Index your own repositories

**Goal:** build the knowledge base from **your** GitHub repositories and Azure DevOps wiki /
work items, instead of this checkout.

The default build indexes the local checkout with no credentials. Real sources use the
production fetch backend, which authenticates with Personal Access Tokens supplied by
environment-variable **name** — the token value never appears in config, storage, or logs.

## Steps

1. **Put credentials in the repo-root `.env`** (gitignored — never commit it):

   ```sh
   GITHUB_TOKEN=ghp_...           # GitHub PAT: classic with `repo` scope, OR fine-grained
                                  # granted to the repo (Contents: Read)
   ADO_PAT=...                    # Azure DevOps PAT: Wiki (Read) + Work Items (Read) — only if
                                  # you index ADO
   LLM_PROVIDER=groq              # only needed if your sources include prose
   LLM_API_KEY=gsk_...
   LLM_MODEL=llama-3.1-8b-instant
   ```

   Code is zero-LLM — a code-only build needs no `LLM_*` at all
   ([switch LLM providers](switch-llm-providers.md) for other providers).

2. **Describe your sources** — copy the template and edit the identifiers. You set `owner/repo`,
   `organization`, `project` — not URLs:

   ```sh
   cp services/kb-builder/sources.example.yaml scripts/my-sources.yaml
   # edit: your GitHub owner/repo, your ADO org/project/wiki; delete source types you don't want
   ```

   Each source authenticates via `auth.token_env: <ENV_VAR_NAME>`. A private repo **must** carry
   an `auth:` block — GitHub returns 404 (not 403) for an unauthenticated private fetch. The full
   schema: [the source-config contract](../../contracts/source-config.md).

3. **(Optional) pre-flight the config without building:**

   ```sh
   cd services/kb-builder
   uv run python -m agentic_kb_builder.build \
     --sources ../../scripts/my-sources.yaml --backend production --validate-only
   ```

   Prints `config ok` (exit 0) or the exact errors (exit 1) — no database or network access.

4. **Build into a fresh database with the production backend:**

   ```sh
   export DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb"
   dropdb --if-exists --force agentic_kb && createdb agentic_kb
   ( cd services/kb-builder && DATABASE_URL="$DATABASE_URL" uv run alembic upgrade head )

   cd services/kb-builder
   set -a; source ../../.env; set +a          # load GITHUB_TOKEN, ADO_PAT, LLM_*
   DATABASE_URL="$DATABASE_URL" \
     uv run python -m agentic_kb_builder.build \
       --sources ../../scripts/my-sources.yaml --workspace ../.. \
       --backend production --no-git-metadata --log-format timeline
   cd ../..
   ```

   `--no-git-metadata` is deliberate: you are indexing a *remote* repo, so this checkout's local
   commits don't belong in that knowledge base.

5. **Serve and connect exactly as usual** — [getting started](../getting-started.md) for the
   server command, the connect pages for hosts.

## Verify

During the build you should see real-fetch log lines — `event=github_branch_resolved`,
`event=github_listed`, `event=ado_wiki_*`, `event=ado_work_item_*` — with deterministic source
versions (GitHub commit SHA pinned per fetch, ADO wiki git head, work-item revision). The tail is
the standard:

```
build status : active
kb_version   : local.<timestamp>
active version: local.<timestamp>
search index : .kb-local-search-index.json
```

## Notes

- The local backend (`--backend local`, the default) can only fetch `github_code`/`github_doc`
  from your `--workspace` checkout. Any `azure_wiki`/`ado_card` source is **skipped** with
  `event=source_skipped_not_locally_fetchable` — a skip, not an error; those types need
  `--backend production`.
- The GitHub backend is exercised against the live API; the ADO backends are tested against
  mocked transports, so a real ADO instance may surface format/auth specifics to iron out.
- Fetch errors (404/401/403): [troubleshooting](troubleshoot.md), "Real-source fetches".
