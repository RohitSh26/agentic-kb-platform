# PR-26 — Deterministic cross-domain links (git metadata + work-item/PR/commit references)

## Why

The judge ruled: build **deterministic** work-item linking FIRST; semantic inference only after
("ask an LLM which card drove which code and you get plausible lies"). This PR adds the first
cross-domain edges with zero LLM — `implements`/`documents`/`mentions` from explicit references in
git metadata and source text — all `EXTRACTED` trust. Phase 2 of ADR-0010.

## Scope

- **Git-metadata source:** read commits/branches/messages for the local workspace (local `git log`
  / `git show`); capture commit SHA, branch, message, changed files. Deterministic
  (`source_version` = commit SHA). No GitHub/ADO API yet (production track).
- **Deterministic linker rules (extend the existing linker):**
  - work-item-ID / PR / commit / branch references parsed from commit messages and branch names ⇒
    `implements` edges (code/PR → work-item) with the matched reference as the evidence pointer.
  - changed-file → symbol edges (a commit touched file F ⇒ link the commit/work-item to F's symbols).
  - verbatim identifier matches (a doc names a symbol/path/work-item-ID) ⇒ `mentions` /
    deterministic `documents` (`EXTRACTED`).
  - All edges: `edge_type` ∈ ontology, `trust_class=EXTRACTED`, `source=linker_deterministic`,
    evidence pointer, `relation_schema_version`. No `related_to`. No inference.
- **ACL intersection** on every cross-domain edge (`acl-source-visibility.md`): the edge is visible
  only where both endpoints are — never widens visibility.
- Cross-domain golden queries added to the eval set (why-was-X-changed, which-card-drove-Y).
- Tests: reference parsing (positive + negative, no false links); changed-file→symbol; ACL
  intersection (restricted endpoint hides the edge); idempotent re-link on rebuild.

## Do NOT

- No LLM judgment, no `INFERRED_*` edges (phase 3). No GitHub/ADO API backends (production track).
- Do not create an edge for a fuzzy/partial match — deterministic exact references only.

## Acceptance criteria

- [ ] Work-item/PR/commit/branch references produce `implements`/`mentions`/`documents` `EXTRACTED`
      edges with evidence pointers; no `related_to`.
- [ ] changed-file → symbol edges created from commit metadata.
- [ ] Cross-domain edge ACL = intersection of endpoints (test proves no leak / no widening).
- [ ] Re-link on unchanged inputs is idempotent.
- [ ] Cross-domain golden queries pass; `make verify` + `make eval-run` green.
