# ADR-0015 — Production source connectors (GitHub now; ADO Wiki + Work Items next)

## Status

Accepted (2026-06-15). Extends ADR-0004 (nightly incremental build) and the connector boundary
established with the local-FS backend (ADR-0010). Owner decisions are binding: PAT auth via
`token_env`, GitHub REST contents API pinned to a commit SHA, all three sources (GitHub, ADO Wiki,
ADO Work Items) planned in parallel — this PR ships the foundation + GitHub and stubs ADO. Managed
identity is deferred to the backlog.

## Context

Until now the only real `FetchBackend` was the local-filesystem backend (ADR-0010): it reads a
workspace directory so the whole build plane runs locally with no network and no credentials. To
build a knowledge base from real sources we need backends that fetch from GitHub and Azure DevOps
over HTTP, while preserving every connector invariant:

- Connectors are deterministic — same source state ⇒ same normalized content ⇒ same `content_hash`
  (rules/connectors.md).
- All network I/O sits **behind `FetchBackend`** so connectors keep depending only on the Protocol
  and tests stay hermetic (rules/python.md — same pattern as `SearchClient` / `ModelClient`).
- Secrets are referenced by environment-variable NAME only (`AuthRef.token_env`); a token value is
  unrepresentable in config and must never reach a `source_uri`, `source_version`, `content_hash`,
  or a log line.
- V1 is a **nightly batch pull** — no webhooks, no Event Grid / Service Bus, no streaming ingestion
  (CLAUDE.md V1 exclusions).

## Decision

1. **HTTP lives behind `FetchBackend` via `httpx`.** A new `httpx` (async) dependency is added to
   kb-builder only. It is used *inside* the backend boundary exactly like the Azure SDKs sit behind
   `SearchClient` / `ModelClient`: a small `connectors/http_client.py` wraps `httpx.AsyncClient`,
   injects the auth header, exposes `get_json` / `get_text`, and is the only place a real request is
   made. Connectors (`BaseConnector` subclasses) still depend solely on the `FetchBackend` Protocol.

2. **Auth = PAT via `token_env`** (managed identity is BACKLOG, not built here). Tokens are resolved
   from the environment by the existing `resolve_token` and handed to the backend factory as a local
   value. Header schemes:
   - **GitHub:** `Authorization: Bearer <PAT>`.
   - **ADO (both backends):** HTTP Basic with username empty and password `<PAT>`, i.e.
     `Authorization: Basic base64(":" + PAT)`.
   The HTTP client never logs the header or the token; structured logs carry only `event=http_fetch_*`
   with method, host, path, and status — never query strings that could embed a credential.

3. **Per-source determinism — `source_version` pins the exact source state.**
   - **GitHub (`github_code` / `github_doc`):** resolve the configured branch to a commit SHA once
     (`GET /repos/{owner}/{repo}/branches/{branch}` → `commit.sha`), then do everything at that SHA.
     `list_sources` enumerates blob paths via the git trees API
     (`GET /repos/{owner}/{repo}/git/trees/{sha}?recursive=1`); `fetch_text` reads each file via the
     contents API (`GET /repos/{owner}/{repo}/contents/{path}?ref={sha}`, base64-decode, UTF-8
     strict). `source_version` is the commit SHA. Same repo state ⇒ same SHA ⇒ same `content_hash`.
   - **ADO Wiki (next PR):** list pages with `recursionLevel=full`, fetch page content;
     `source_version` = page version / ETag.
   - **ADO Work Items (next PR):** WIQL query → ids; `GET workitems/{id}?$expand=fields`; normalize
     the fields into a deterministic snapshot text; `source_version` = the work-item `rev`.

4. **One GitHub backend serves both `github_code` and `github_doc`.** `spec.type` decides the emitted
   `source_type`; include/exclude globs are applied upstream by `FilteredFetchBackend`, so the backend
   only enumerates and reads.

5. **Pagination + bounded retry.** The git trees API returns the whole tree in one recursive call, so
   GitHub needs no pagination here; ADO list endpoints (next PR) will page via continuation tokens.
   The HTTP client retries on **429** (honoring `Retry-After`) and **5xx** with bounded exponential
   backoff and a capped attempt count, then surfaces the error — no infinite loops, no silent drops.

6. **`production_backend_factory`** mirrors `local_fs_backend_factory`: it dispatches on `spec.type`
   (`github_code` / `github_doc` → `GitHubRestBackend`; `azure_wiki` → `AdoWikiBackend`; `ado_card` →
   `AdoWorkItemBackend`) and raises `SourceConfigError` for an unsupported type. The build CLI selects
   it with `--backend production` (default stays `local` to preserve current behavior).

7. **V1 stays a nightly batch pull.** These backends are invoked by the nightly build only. No
   webhooks, no event bus, no streaming — the V1 exclusions are respected.

## Consequences

- New runtime dependency `httpx` in **kb-builder only**; mcp-server is untouched.
- GitHub builds are reproducible: pinning to a SHA before listing/fetching guarantees that a build
  restarted or re-run against an unchanged repo produces identical hashes (idempotency).
- ADO support lands cleanly because the stubs already satisfy the factory and Protocol; the follow-up
  PRs replace `NotImplementedError` bodies with the documented REST approach without touching the
  foundation.

## Known limitations / follow-ups

- **Git tree truncation.** The recursive git trees API caps very large trees and sets
  `"truncated": true`. When that happens `GitHubRestBackend` logs a clear warning
  (`event=github_tree_truncated`) and proceeds with the partial listing. A complete listing for such
  repos (recursive per-subtree walk, or the Contents API per directory) is a tracked follow-up; very
  large monorepos are out of scope for this PR.
- **Managed identity** auth is backlog (owner decision); only PAT/`token_env` is built.
- ADO Wiki and ADO Work Item backends are stubs here and are filled in by two follow-up PRs.

## Alternatives rejected

- **Calling `httpx` directly from connectors:** breaks the `FetchBackend` boundary and the
  swappable-projection discipline; would make tests non-hermetic.
- **GitHub archive/tarball download:** coarser, harder to filter by path, and does not give a stable
  per-file pin as cleanly as `?ref={sha}` on the contents API.
- **Webhooks / streaming ingestion:** excluded from V1 without an ADR; nightly batch pull is the
  contract.
