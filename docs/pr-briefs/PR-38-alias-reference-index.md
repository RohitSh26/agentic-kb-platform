# PR-38 — Alias/Reference index: deterministic build-time mining (ADR-0030)

## Why

The goal is a developer typing a terse phrase ("the durable cache fix", "the retrieval budget
check") and having it resolve to the right code entities without writing a paragraph of context.
This index is the first pillar of `get_task_context` (PR-39) and immediately improves `kb_search`
hits too (alias phrases become searchable artifacts). Design per
`docs/proposals/2026-07-02-tool-design-first-kb-architecture.md` §1, authorized by ADR-0030.

**Deterministic v1 — zero LLM calls at build time.** Commit subjects, PR-brief titles, and ADR
titles are already short, human-written alias phrases; the commit→changed-files mapping already
exists at build time (the acl-intersection rule in `.claude/rules/connectors.md` proves it). LLM
mining is a possible later enrichment, not v1.

## Scope

- **Discover the actual `git_metadata` commit-artifact shape first** (what's in `body_text` /
  metadata / edges). If changed-file targets aren't currently persisted on the artifact, persist
  what alias mining needs at mining time — do not re-derive from git at query time.
- **New artifact rows, `artifact_type='alias_reference'`** in the existing `knowledge_artifact`
  table. No new table expected — verify; if any constraint/enum forces a migration, it ships
  forward + rollback per house rules.
  - `title` = the normalized alias phrase; `search_text` = phrase + variants (so the existing
    keyword search client matches it with zero changes); `body_text` = JSON: target entity refs
    (artifact ids + source paths), evidence (commit SHAs / brief paths), `confirmation_count`,
    `confidence_tier: "interpreted"`.
  - `acl_teams` = intersection of the targets' ACLs (same never-widen rule as git_metadata).
- **Edges**: `alias_reference → target artifact` rows in `knowledge_edge`. Add the relation to
  `docs/contracts/relation-ontology.md` FIRST (contracts before code), then write edges
  (kb_version'd, idempotent).
- **Mining (deterministic):** per commit/brief/ADR artifact — conventional-commit scope tokens,
  stopword-filtered subject n-grams (2–4 words), doc titles. Normalize (lowercase, strip
  punctuation). Aggregate across sources: the same normalized phrase seen in N sources gets
  `confirmation_count=N` and the union of targets ranked by frequency.
- **Incremental + idempotent:** mining is keyed on the source artifact's `content_hash` — unchanged
  source ⇒ skipped, exactly like docify/graphify. Re-running a completed build creates no duplicate
  alias rows/edges (tested).
- **Golden eval (the point):** `evals/retrieval_cases/alias_golden_v1.yaml` — 25 real terse
  phrases from THIS repo's history, each with hand-verified expected target path(s) written into
  the file with a provenance comment (which commit/brief it came from) BEFORE running the resolver
  against them. Plus `scripts/eval_alias_resolution.py`: resolves each phrase via the built index,
  prints per-case hit/miss and top-1 accuracy. Hermetic pytest covers a seeded 5-case subset;
  the full 25-case run executes against a locally built KB and its accuracy is recorded in
  `docs/reports/alias-accuracy-<date>.md`. Target: ≥80% top-1 (misses must be explainable as
  mining gaps, not resolver bugs).

## Do NOT

- No LLM calls anywhere in this PR.
- No new search backend or ranking changes — alias rows ride the existing search surface.
- No mcp-server changes (PR-39's job).
- Do not widen ACLs; do not store raw documents; Postgres remains truth.

## Acceptance criteria

- [ ] Relation ontology contract updated before edge code.
- [ ] `alias_reference` artifacts + edges written by the build; migration only if actually forced,
      with rollback.
- [ ] Incremental skip on unchanged sources (tested); idempotent re-run, no duplicates (tested).
- [ ] Golden set (25 hand-verified cases) + runner ship; hermetic subset in pytest; full-run
      accuracy documented in docs/reports (or the run documented as blocked if no local KB exists,
      with the hermetic subset green).
- [ ] `ruff` + `pyright` clean, tests green, no excluded-V1 resource.
