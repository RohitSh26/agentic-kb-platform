# ADR-0017 — Persistent local search projection (local ↔ Azure parity)

## Status

Accepted (2026-06-15). Implements the local half of the `SearchClient` interface (ADR-0006) so the
development loop has the SAME index-persistence semantics as Azure AI Search. Reinforces invariant 1
(Search is a derived, rebuildable projection of Postgres — never truth) and invariant 4 (incremental
build skips unchanged work). No new production resource: this is the local-dev implementation of an
existing interface; the in-memory `FakeSearchClient` remains the test double.

## Context

The build plane talks to the search index only through the `SearchClient` interface. Two
implementations existed: the real Azure client, and `FakeSearchClient`, an **in-memory** dict used
by both tests and the local `python -m agentic_kb_builder.build` dev loop.

The post-build **index-consistency** publish gate compares Postgres membership (the truth) against
the index state and blocks activation on any drift (missing / orphaned / drifted). Because an
incremental build correctly upserts nothing for unchanged sources (invariant 4, connectors rule),
the index must already contain the carried-forward members from earlier builds.

Azure AI Search persists, so this holds in production. The in-memory fake does **not**: each
`build` invocation is a fresh process that starts with an empty index. The documented
"run the same command again" incremental rebuild therefore failed locally —
`event=index_drift class=missing count=92` for every carried-forward member —
and the new `kb_version` stayed inactive. The bug was purely an environment-parity gap in the
projection backend, not in versioning, incrementality, or the gate.

## Decision

Add a **persistent, file-backed** `LocalFileSearchClient` and use it for the local dev loop, behind
the unchanged `SearchClient` interface:

- The projection is a JSON file (`--index-path`, or `$KB_LOCAL_INDEX_PATH`, default
  `./.kb-local-search-index.json`). Write-through on every upsert/delete with an atomic
  `os.replace`, so an interrupted build never leaves a half-written index.
- The build, validation, incremental-skip, and orphan-reconciliation logic are **unchanged** and
  identical across environments — only the `SearchClient` implementation differs (Azure SDK vs local
  file vs in-memory fake). That is the parity guarantee: no divergence in build behaviour anywhere.
- The file is a **rebuildable projection of Postgres**, never truth. Deleting it — or recreating the
  database — forces a clean reprojection on the next build, and the existing per-build orphan sweep
  (`delete_orphaned`) keeps it honest after a from-scratch rebuild.

`FakeSearchClient` stays the deliberate in-memory double for unit/integration tests, which run two
builds in one process and so never needed persistence.

## Consequences

- The documented incremental-rebuild flow now passes locally exactly as it does against Azure.
- One new local-dev file artifact to be aware of (and to delete when resetting the database). The
  CLI prints `search index : <path>` so its location is never a mystery.
- Follow-up (backlog): a `reindex` command that rebuilds the projection from Postgres on demand —
  the explicit realisation of invariant 1 — would let an operator repair index drift in either
  environment without a database reset.

## Alternatives considered

- **Reproject the full membership into the index on every build.** Self-healing, but it contradicts
  the written "skip index for unchanged" incremental rule and would re-upsert the whole corpus every
  night in Azure. Rejected: persistence parity is the smaller, prod-faithful change.
- **Document the limitation only.** Leaves the documented two-run flow broken locally and the
  local/Azure behaviours divergent. Rejected.
