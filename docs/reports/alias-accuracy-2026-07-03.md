# Alias/Reference resolution accuracy report (2026-07-03)

> PR-38 (ADR-0030), `docs/contracts/alias-reference.md`. Full 25-case golden-set run
> (`evals/retrieval_cases/alias_golden_v1.yaml`) against a **locally built KB**, resolved via
> `scripts/eval_alias_resolution.py`. Target: >= 80% top-1.

## Headline

| | |
|---|---|
| Cases | 25 |
| Top-1 hits | 25 |
| **Top-1 accuracy** | **100.0%** |
| Misses | none |

```
25/25 top-1 hits = 100.0%
PASS (target >= 80%)
```

## Build used

A **real, local, zero-LLM build** of this repository's own git history — the `git_metadata`
connector only (`GitMetadataConnector`, `docs/adr/0026...` n/a here; see `.claude/rules/connectors.md`),
scanning the default `max_commits=200` most-recent commits on `main`. No `github_code` /
`github_doc` connector was configured (a `github_doc` source with a deliberately non-matching
`include` glob satisfies the schema's `sources: list[..., min_length=1]` without fetching
anything), so **zero doc/code artifacts** exist in this registry and **zero LLM calls** were made
— every alias phrase in this report comes from commit subjects + changed-file lists, exactly the
"deterministic v1" scope the brief describes.

Reproduce:

```bash
createdb agentic_kb_alias_eval
cd services/kb-builder
DATABASE_URL=postgresql+asyncpg://$USER@localhost:5432/agentic_kb_alias_eval uv run alembic upgrade head

cat > /tmp/sources.alias-eval.yaml <<'EOF'
version: 1
defaults:
  acl_teams: []
sources:
  - name: none
    type: github_doc
    repo: local/none
    branch: main
    include:
      - "__no_such_path__/**/*.md"
EOF

DATABASE_URL=postgresql+asyncpg://$USER@localhost:5432/agentic_kb_alias_eval \
  uv run python -m agentic_kb_builder.build --workspace ../.. \
  --sources /tmp/sources.alias-eval.yaml --kb-version alias-eval.1 --no-activate

DATABASE_URL=postgresql+asyncpg://$USER@localhost:5432/agentic_kb_alias_eval \
  uv run python ../../scripts/eval_alias_resolution.py
```

Build outcome: `sources_seen=200 sources_changed=200 llm_calls=0 embedding_calls=200` (commit
artifacts are embedded for keyword/vector search like any other artifact — `embedding_calls` here
is the local hash-embedder, still zero LLM); `alias_miner_completed ... phrases=3289
artifacts_inserted=3289 edges_inserted=0`.

## Honest caveats — read before trusting the 100%

- **`edges_inserted=0` in this build, by construction, not a bug.** The alias resolver
  (`alias/resolve.py`) reads target **paths** out of each alias artifact's `body_text.targets`
  (populated regardless of whether the path resolved to a live artifact —
  `docs/contracts/alias-reference.md` "targets is ranked ... `artifact_id` uuid-or-null"), so
  path-based top-1 resolution works even with zero doc/code artifacts in the registry. **The
  `aliases` graph edges themselves are NOT exercised by this run** — they require a target path to
  also resolve to a live `source_item` + artifact (i.e. a real `github_code`/`github_doc` build),
  which this git-metadata-only build deliberately skips for speed (no LLM, no `--backend
  production` PAT). The edge-writing path (`_reconcile_edges`) IS covered separately by the
  DB-backed unit/integration tests
  (`services/kb-builder/tests/integration/test_alias_miner.py`), just not by this report's numbers.
- **This golden set was hand-authored from the same commit history the build mines.** The 25
  cases were written by inspecting `git log` / `git show --name-only` for this repo (see each
  case's `provenance` field in the YAML) — so this is a **fit-to-source** check that the mining +
  resolution *code* is correct, not an independent-sample generalization estimate. A production KB
  (real `github_code`/`github_doc` ingestion, a longer commit history, multiple contributors'
  phrasing habits) would very likely score lower; 100% here should be read as "the deterministic
  pipeline resolves the phrases it CAN see with zero errors," not "alias resolution is perfect."
- Every one of the 25 target commits sits within `GitMetadataConnector`'s default 200-commit
  scan window on `main` — this was verified before the build (`git rev-list --count HEAD` = 206
  at authoring time, target SHAs all within the top ~100). A shallower window or an older commit
  would silently drop that case's mining source (`sources_seen` would simply be lower); this is a
  known scope boundary of the connector, not something PR-38 changes.
- The registry used here is scratch (`agentic_kb_alias_eval`, never activated
  `--no-activate`) — it is not the demo/dev database and was not committed anywhere.

## Bottom line

The deterministic alias miner + resolver resolve all 25 hand-verified golden phrases from this
repo's own commit history to the correct top-1 target, with zero LLM calls and zero embeddings —
comfortably above the brief's 80% bar. The main open follow-on (not required by PR-38, noted for
PR-39 / a future PR) is a production-shaped run with real `github_code`/`github_doc` ingestion so
the `aliases` graph edges (not just path-based resolution) are exercised end to end.
