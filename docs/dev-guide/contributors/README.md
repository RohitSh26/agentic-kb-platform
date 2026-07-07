# Contributors

This section is for people **changing** the platform. Running and using it is everything outside
`contributors/` — start at [getting started](../getting-started.md) if that is you.

## Where the depth lives

- [`docs/architecture/`](../../architecture/00-overview.md) — the deep design: the full system
  blueprint, the evaluation system, the retrieval model.
- [`docs/adr/`](../../adr/README.md) — every architecture decision, with status. New surfaces,
  new dependencies, and deviations from V1 start as an ADR, not as code.
- `docs/contracts/` — the cross-service agreements: the
  [MCP tool surface](../../contracts/mcp-tools-contract.md), the
  [Postgres registry](../../contracts/postgres-knowledge-registry.md), the
  [publish gates](../../contracts/publish-gates.md), the
  [review panel](../../contracts/review-panel.md). When prose and a contract disagree, the
  contract wins.
- `docs/pr-briefs/` — the implemented build units, one brief per PR.
- [Code tour](code-tour.md) — a dated, subsystem-by-subsystem walk through the code.
- [Testing and builds](testing-and-builds.md) — the verify gate, test databases, fakes, Docker,
  and the contributor dev loop.
- Repo-root `CLAUDE.md` and `.claude/rules/` — the enforced working rules for changes in this
  repo (style, storage ownership, budgets, code quality).

## The working invariants

1. Postgres is the source of truth; search is a rebuildable projection.
2. Contracts before code — schema first, in the owning service and in `docs/contracts/`.
3. Tests ship in the same PR, including budget, cache-hit, dedupe, and failure paths.
4. Every migration has a working downgrade.
5. A `kb_version` activates only after validation and the publish gates pass.
