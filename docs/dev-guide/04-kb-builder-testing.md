# 04 — KB-builder local testing (from a bare machine)

> A complete, copy-paste runbook for building and validating a knowledge base on a **brand-new
> machine with nothing installed**. Covers: install everything → configure any LLM provider
> (Ollama / Groq / OpenAI / Azure / Claude / …) → run a build → export to Obsidian → query the
> database to confirm everything worked. No cloud is required for a local build.

The build plane is **Postgres-first** and **deterministic**: code is extracted by Graphify
whole-tree (no LLM), prose is summarised by Graphify's LLM doc pipeline behind the `docify` adapter
(ADR-0023), embeddings are computed locally, and a `kb_version` only goes **active** after the
publish gates pass. Only two steps call the chat LLM — **docify** (doc/card summaries + concepts) and
the **relationship judge** (phase-3B inferred links); everything else is deterministic.

---

## 1. Install the toolchain (fresh machine)

You need: **Postgres 16**, **uv** (Python 3.12 manager), **git**, and — for a free local build — an
**LLM runtime** (Ollama). Pick your OS.

### macOS (Homebrew)
```sh
# Homebrew itself, if missing
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

brew install git postgresql@16 uv ollama
brew services start postgresql@16          # starts Postgres on localhost:5432
brew services start ollama                 # starts the local model server on :11434
```

### Linux (Debian/Ubuntu)
```sh
sudo apt-get update && sudo apt-get install -y git curl postgresql-16
curl -LsSf https://astral.sh/uv/install.sh | sh        # uv
curl -fsSL https://ollama.com/install.sh | sh          # ollama
sudo service postgresql start
ollama serve &                                         # background model server
```

### Verify the tools
```sh
git --version && uv --version && psql --version && ollama --version
```

> Docker alternative: `docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:16`
> gives you Postgres without a local install (then use the `postgres:postgres` role in the URLs below).

---

## 2. Get the code and install dependencies

```sh
git clone https://github.com/RohitSh26/agentic-kb-platform.git
cd agentic-kb-platform
make sync          # uv sync for kb-builder, mcp-server, and evals (each is its own uv project)
```

---

## 3. Create the database and apply migrations

```sh
createdb agentic_kb                                                    # the build database
createdb agentic_kb_test                                               # for `make verify` / evals

# macOS/Homebrew Postgres uses a role named after your OS user; Docker uses postgres:postgres.
export DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb"

cd services/kb-builder
uv run alembic upgrade head        # applies every migration (through 0015)
uv run alembic current             # should print the latest revision id
cd ../..
```

The driver **must** be `postgresql+asyncpg://` (everything is async SQLAlchemy).

---

## 4. Choose and configure an LLM provider

Only **docify** and the **relationship judge** call the chat model; **embeddings are always computed
locally** (a deterministic hash embedder), so you never need an embedding API for a local build.

The model client speaks the **OpenAI chat-completions protocol** for everything except Azure OpenAI,
which has its own path. Configure entirely by environment variables — no code change.

| Variable | Meaning | Default |
|---|---|---|
| `LLM_PROVIDER` | `ollama` \| `groq` \| `openai` \| `azure` \| *(any name → uses your `LLM_BASE_URL`)* | `ollama` |
| `LLM_MODEL` | model / deployment name | per-provider default |
| `LLM_BASE_URL` | OpenAI-compatible endpoint (non-Azure) | per-provider default |
| `LLM_API_KEY` | API key (non-Azure) | — (required for remote providers) |
| `LLM_TEMPERATURE` | sampling temperature | `0` (deterministic) |
| `LLM_MAX_TOKENS` | max output tokens | `4000` |

Pick **one** block below and `export` it in the shell you run the build from.

### A) Ollama — local, free, no key (recommended to start)
```sh
ollama pull phi4-mini            # or: gemma3:4b   (small, strong at JSON/instruction-following)
export LLM_PROVIDER=ollama
export LLM_MODEL=phi4-mini       # default would be llama3.1
# LLM_BASE_URL defaults to http://localhost:11434/v1
```

### B) Groq (fast hosted, OpenAI-compatible)
```sh
export LLM_PROVIDER=groq
export LLM_API_KEY=gsk_...                       # required
export LLM_MODEL=llama-3.1-8b-instant            # default for groq
# LLM_BASE_URL defaults to https://api.groq.com/openai/v1
```

### C) OpenAI
```sh
export LLM_PROVIDER=openai
export LLM_API_KEY=sk-...                         # required
export LLM_MODEL=gpt-4o-mini                      # default for openai
# LLM_BASE_URL defaults to https://api.openai.com/v1
```

### D) Azure OpenAI (dedicated path — uses the deployment name as the model)
```sh
export LLM_PROVIDER=azure
export AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com
export AZURE_OPENAI_API_KEY=<key>
export AZURE_OPENAI_DEPLOYMENT=<your-deployment-name>     # e.g. gpt-4o-mini
export AZURE_OPENAI_API_VERSION=2024-06-01                # optional; this is the default
```

### E) Azure AI Foundry (serverless / OpenAI-compatible models)
Foundry models that expose an OpenAI-compatible endpoint use the generic path (NOT `azure`):
```sh
export LLM_PROVIDER=foundry            # any non-reserved name works
export LLM_BASE_URL=https://<your-foundry-endpoint>/v1
export LLM_API_KEY=<key>
export LLM_MODEL=<deployed-model-name>
```
(If your Foundry model is an *Azure OpenAI* deployment, use block **D** instead.)

### F) Anthropic Claude (via Anthropic's OpenAI-compatible endpoint)
```sh
export LLM_PROVIDER=anthropic
export LLM_BASE_URL=https://api.anthropic.com/v1/
export LLM_API_KEY=sk-ant-...
export LLM_MODEL=claude-3-5-haiku-latest          # or another Claude model id
```

### G) Any other OpenAI-compatible endpoint (OpenRouter, vLLM, LM Studio, llama.cpp …)
```sh
export LLM_PROVIDER=custom
export LLM_BASE_URL=https://<endpoint>/v1
export LLM_API_KEY=<key-or-"x" if the server ignores it>
export LLM_MODEL=<model-id>
```

> **Notes.** Temperature defaults to `0` for reproducible summaries — keep it there for testing.
> A remote provider with no key will fail fast (`LLM_API_KEY is required for provider ...`).
> Docify re-derives trust deterministically: a concept whose supporting sentence is a verbatim
> substring of the source becomes a citable `source_backed_fact`; otherwise it stays an `interpreted`
> concept (`event=docify_mapped … source_backed=N interpreted=M`) — paraphrases are downgraded, never
> dropped. Fewer source-backed facts with a small model is expected, not an error.

---

## 5. Run a local build (no source credentials)

`sources.example.yaml` ships with the repo; the local-FS backend reads files from `--workspace`.

```sh
cd services/kb-builder
uv run python -m agentic_kb_builder.build --workspace ../.. --sources ./sources.example.yaml
```

Expected tail:
```
build status : active
kb_version   : local.<timestamp>
active version: local.<timestamp>
```

Useful flags:

| Flag | Effect |
|---|---|
| `--backend {local,production}` | `local` (default) reads the workspace from disk; `production` fetches from GitHub/ADO (§7) |
| `--no-activate` | build but don't flip the active version |
| `--kb-version <label>` | override the version label (default `local.<UTC timestamp>`) |
| `--version <sha>` | the `source_version` stamp for local files (default `local`) |
| `--no-git-metadata` | skip turning local git commits into `commit` artifacts |
| `--allow-large-delta` | bypass only the symbol-count-delta publish gate (recorded + logged) |

What the log narrates (these are the same `event=` lines you'd grep in production): source upserts →
`docify`/`graphify` writes (cache-gated) → `linker_*` (deterministic + cross-domain links) →
`candidate_*` (phase-3A) → `judge_*` (phase-3B inferred links, only if the model infers one) →
`publish_gate_*` → `build_activation`.

### Incremental rebuild (validates the versioning fix, PR-27)
Run the **same** command again with no file changes:
```sh
uv run python -m agentic_kb_builder.build --workspace ../.. --sources ./sources.example.yaml
```
Expect `event=build_skip_unchanged` for most sources and **near-zero `llm_calls`** (generation-cache
hits). Crucially, the newly-activated version must still serve the **complete** set (see the
membership query in §8) — not just the day's delta.

The local search index is **persistent** (a JSON file, default `./.kb-local-search-index.json`,
overridable with `--index-path` or `$KB_LOCAL_INDEX_PATH`). It is a *derived, rebuildable projection
of Postgres* — never truth — and mirrors how Azure AI Search persists between builds, so the
unchanged rebuild (which upserts nothing) still passes the index-consistency publish gate against
the carried-forward membership. The CLI prints its location as `search index : <path>`. See
ADR-0017.

> **If you recreate the database** (a from-scratch Phase 1 rerun), do the **first** rebuild with the
> *same* index file in place — the build's orphan sweep removes the stale docs and re-projects the
> fresh ones, so it self-heals. To force a fully clean projection instead, delete the index file
> (`rm .kb-local-search-index.json`) before rebuilding.
>
> An `event=index_drift class=missing … count=N` followed by `publish_gate_failed
> gate=index_consistency` means the index is missing members the registry has — almost always a
> *stale or deleted* index file paired with a freshly built database. Rebuild from scratch (DB +
> index) and the gate clears.

---

## 6. Export the graph to an Obsidian vault

Browse the knowledge graph as linked Markdown notes (one note per artifact, `[[wikilinks]]` for
edges, foldered by type):

```sh
cd services/kb-builder
# DATABASE_URL must point at the built DB; defaults to the ACTIVE version
uv run python -m agentic_kb_builder.export_obsidian --out ./vault
#   --kb-version <label>   # optional: export a specific version instead of the active one
```

Then open the `./vault` folder in Obsidian (**Open folder as vault**). Each note has YAML frontmatter
(`type`, `kb_version`, `source_uri`, `acl_teams`) and a `## Links` section whose `[[wikilinks]]`
resolve to the other notes. `index.md` is a map-of-content with per-type counts. Re-running is
deterministic (stable slugs) and overwrites the folder cleanly.

---

## 7. Run a production build (real sources, with PATs)

Point at GitHub / Azure DevOps and authenticate with **Personal Access Tokens supplied by
environment-variable name** (the token value never appears in config, storage, or logs).

1. Write a `sources.yaml` with real sources (see `sources.example.yaml` for the schema), e.g. each
   source's `auth.token_env: GITHUB_TOKEN` (the *name* of the env var holding the PAT).
2. Export the PAT(s) under the names your YAML references:
   ```sh
   export GITHUB_TOKEN=ghp_...
   export ADO_PAT=...
   ```
3. Build with the production backend:
   ```sh
   uv run python -m agentic_kb_builder.build --backend production --sources ./sources.yaml --workspace ../..
   ```

Expect real-fetch logs (`event=github_branch_resolved`, `github_listed`, `ado_wiki_*`,
`ado_work_item_*`) and deterministic `source_version`s (GitHub commit SHA, ADO wiki git head,
work-item revision). This is also where the **commit → work-item** cross-domain links become real
against your ADO cards.

---

## 8. Query the database — checks, analysis, "did it work?"

Connect (note: `agentic_kb` is a **database**, not a table):
```sh
psql agentic_kb            # or: psql "postgresql://$USER@localhost:5432/agentic_kb"
```
Inside psql, `\dt` lists tables and `\c agentic_kb` (re-)connects.

### 8.1 Build health — start here
```sql
-- the most recent builds and their cost/outcome
SELECT kb_version, build_seq, status,
       sources_seen, sources_changed, artifacts_created,
       llm_calls, embedding_calls, search_docs_upserted,
       extractor_failures, failed_gate, gate_measured_value
FROM kb_build_run
ORDER BY build_seq DESC
LIMIT 10;

-- the single version MCP would serve (there is exactly one active row)
SELECT kb_version, build_seq FROM kb_build_run WHERE status = 'active';

-- a failed publish is a first-class, queryable outcome (failed_gate names the gate)
SELECT kb_version, status, failed_gate, gate_measured_value, error_summary
FROM kb_build_run WHERE status IN ('failed', 'validation_failed');
```
Healthy build: exactly one `active` row, `failed_gate` is NULL, `artifacts_created > 0`.

### 8.2 What was extracted (artifacts)
```sql
-- node counts by type (expect code_file/code_symbol/concept/summary/chunk/commit/endpoint)
SELECT artifact_type, count(*) FROM knowledge_artifact GROUP BY 1 ORDER BY 2 DESC;

-- artifacts per source type
SELECT s.source_type, a.artifact_type, count(*)
FROM knowledge_artifact a JOIN source_item s ON s.source_id = a.source_id
GROUP BY 1, 2 ORDER BY 1, 3 DESC;

-- sample concepts the LLM produced, and code symbols the AST extractor found
SELECT title FROM knowledge_artifact WHERE artifact_type = 'concept' LIMIT 20;
SELECT title FROM knowledge_artifact WHERE artifact_type = 'code_symbol' LIMIT 20;
```

### 8.3 The graph (edges)
```sql
-- edges by type / producer / trust bucket
SELECT edge_type, source, trust_class, count(*)
FROM knowledge_edge GROUP BY 1, 2, 3 ORDER BY 1;

-- every edge should carry an evidence pointer + a relation schema version
SELECT count(*) AS edges_missing_evidence
FROM knowledge_edge WHERE source = 'linker' AND evidence IS NULL;

-- inspect cross-references (doc/concept/commit ↔ code/work-item) with titles
SELECT e.edge_type, e.trust_class, a.title AS from_title, b.title AS to_title, e.evidence
FROM knowledge_edge e
JOIN knowledge_artifact a ON a.artifact_id = e.from_artifact_id
JOIN knowledge_artifact b ON b.artifact_id = e.to_artifact_id
ORDER BY e.edge_type
LIMIT 40;
```

### 8.4 Cross-domain links (PR-26) and inferred links (phase 3)
```sql
-- commit → work-item (implements) and commit → code_file (mentions), from git metadata
SELECT e.edge_type, c.title AS commit_title, t.title AS target, e.evidence
FROM knowledge_edge e
JOIN knowledge_artifact c ON c.artifact_id = e.from_artifact_id AND c.artifact_type = 'commit'
JOIN knowledge_artifact t ON t.artifact_id = e.to_artifact_id
LIMIT 30;

-- phase-3A CANDIDATES (measurement only — these are NOT edges)
SELECT count(*) FROM relationship_candidate;
SELECT candidate_recall_bucket, count(*) FROM relationship_candidate GROUP BY 1;

-- phase-3B INFERRED edges from the LLM judge (lower-trust routing hints)
SELECT trust_class, count(*) FROM knowledge_edge
WHERE source = 'llm_judge' GROUP BY 1;        -- INFERRED_HIGH / INFERRED_LOW / AMBIGUOUS

-- the judge cache: a hit means zero LLM calls on re-judge
SELECT count(*) FROM relationship_judgment_cache;
```

### 8.5 Version membership — the served set (PR-27)
This is the set MCP actually serves; it must be **complete** after an incremental build, not just the
last delta.
```sql
WITH active AS (SELECT build_seq FROM kb_build_run WHERE status = 'active')
SELECT
  count(*) FILTER (
    WHERE a.valid_from_seq <= active.build_seq
      AND (a.invalidated_at_seq IS NULL OR a.invalidated_at_seq > active.build_seq)
  ) AS served_by_active_version,
  count(*) AS total_artifacts_all_versions
FROM knowledge_artifact a, active;

-- what (if anything) the active build invalidated (deletes/renames/supersession)
WITH active AS (SELECT build_seq FROM kb_build_run WHERE status = 'active')
SELECT artifact_type, count(*) FROM knowledge_artifact, active
WHERE invalidated_at_seq = active.build_seq GROUP BY 1;
```

### 8.6 No ghost edges (a key build-quality gate)
An edge whose endpoint isn't a live member of the active version is a "ghost" — a good build has none.
```sql
WITH active AS (SELECT build_seq FROM kb_build_run WHERE status = 'active'),
members AS (
  SELECT artifact_id FROM knowledge_artifact, active
  WHERE valid_from_seq <= build_seq
    AND (invalidated_at_seq IS NULL OR invalidated_at_seq > build_seq)
)
SELECT count(*) AS ghost_edges
FROM knowledge_edge e, active
WHERE e.valid_from_seq <= active.build_seq
  AND (e.invalidated_at_seq IS NULL OR e.invalidated_at_seq > active.build_seq)
  AND ( e.from_artifact_id NOT IN (SELECT artifact_id FROM members)
     OR e.to_artifact_id   NOT IN (SELECT artifact_id FROM members) );
```
Expect `0`. (If non-zero, the publish gate should have blocked activation — check §8.1.)

### 8.7 Caching / cost analysis (incremental builds should be cheap)
```sql
-- model + embedding spend per build; an incremental rebuild should be ~0 llm_calls
SELECT build_seq, kb_version, llm_calls, embedding_calls, sources_changed
FROM kb_build_run ORDER BY build_seq DESC LIMIT 10;

-- cache sizes: a generation-cache hit means docify/graphify did NOT call the model
SELECT count(*) AS generation_cache_rows FROM generation_cache;
SELECT count(*) AS embedding_cache_rows  FROM embedding_cache;
```

### 8.8 Sources, freshness, and ACLs
```sql
-- sources by type + how many are tombstoned (deleted/renamed away)
SELECT source_type, count(*) FILTER (WHERE NOT is_deleted) AS live,
       count(*) FILTER (WHERE is_deleted) AS deleted
FROM source_item GROUP BY 1;

-- restricted (non-org-public) artifacts and their teams
SELECT artifact_type, acl_teams, count(*)
FROM knowledge_artifact WHERE acl_teams <> '{}' GROUP BY 1, 2;

-- determinism check: same source state ⇒ same content_hash (re-fetch shouldn't change it)
SELECT source_type, source_uri, source_version, content_hash, last_seen_at
FROM source_item ORDER BY last_seen_at DESC LIMIT 10;
```

### 8.9 One-shot "is everything OK?" rollup
```sql
SELECT
  (SELECT count(*) FROM kb_build_run WHERE status = 'active')                 AS active_versions, -- want 1
  (SELECT count(*) FROM kb_build_run WHERE failed_gate IS NOT NULL)           AS builds_with_failed_gate,
  (SELECT count(*) FROM knowledge_artifact)                                   AS artifacts,
  (SELECT count(*) FROM knowledge_edge)                                       AS edges,
  (SELECT count(*) FROM relationship_candidate)                              AS candidates;
```

---

## 9. The verify gate + evals (optional but recommended)

```sh
# from the repo root; adjust the role for your Postgres auth
make verify-kb-builder TEST_DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb_test"
make eval-run          TEST_DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb_test"
```
`make verify-kb-builder` runs ruff + pyright + pytest; `make eval-run` runs the golden-query
retrieval evals (the DB-backed tests migrate up and downgrade to base on teardown, so re-run
`make migrate-test-db` before a manual psql session against the test DB).

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `No module named agentic_kb_builder.build` | Out-of-date checkout — `git pull` (the build CLI exists since PR-22). |
| `relation "knowledge_artifact" does not exist` | You're connected to the wrong DB — `\c agentic_kb`; and run `alembic upgrade head`. |
| `role "postgres" does not exist` | Homebrew/macOS Postgres uses your `$USER` — set `DATABASE_URL`/`TEST_DATABASE_URL` accordingly. |
| `LLM_API_KEY is required for provider ...` | A remote provider needs a key — export `LLM_API_KEY` (or use `LLM_PROVIDER=ollama`). |
| `Connection refused` to `:11434` | Ollama isn't running — `ollama serve` (and `ollama pull <model>`). |
| `event=docify_mapped … source_backed=N interpreted=M` | Normal: docify classified each concept — verbatim quotes became `source_backed_fact`, paraphrases stayed `interpreted`. Not an error. |
| Build runs but `status` is `failed`/`validation_failed` | A publish gate blocked activation — see `failed_gate`/`gate_measured_value` in §8.1. |
| Migrations behind | `cd services/kb-builder && uv run alembic upgrade head` (head is the highest `00NN` revision). |
