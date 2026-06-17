# Contract: source configuration (`sources.yaml`)

> Schema version: **1** · Owner: `services/kb-builder` (`domain/source_config.py`) · Consumers:
> operators and teams authoring the nightly build's source set.
>
> Unlike the other documents in this directory this is not a cross-service contract — the runtime
> plane never reads it. It is the contract between **config authors** and the **build plane**:
> the example file `services/kb-builder/sources.example.yaml` is pinned against the schema by a
> contract test, so this document, the example, and the code cannot drift apart.

## Design principles

1. **Config as code** — `sources.yaml` lives in a repo and changes by reviewed PR, like
   `dependabot.yml` or `CODEOWNERS`. The file is the audit trail of what the KB ingests.
2. **Scope by glob, deny wins** — gitignore-style `include`/`exclude` patterns; anything excluded
   is never fetched, so cost and ACL exposure are controlled at the source.
3. **Secrets by reference, never by value** — config carries environment-variable *names*
   (`token_env: GITHUB_TOKEN`); values exist only in the build environment (12-factor). A token
   value in YAML is unrepresentable: the field validates as an env-var name.
4. **Fail fast, name the source** — an invalid config aborts the build at load time with the
   offending source's `name`; a half-understood config never half-runs.
5. **Versioned schema** — top-level `version: 1`; breaking changes bump it and update this document
   in the same PR.

## Top level

```yaml
version: 1            # required; the schema version of this document

defaults:             # optional; applied where a source omits the field
  acl_teams: []       # [] = org-public (any authenticated subject)

sources: []           # required; one block per ingested source, see below
```

| Field | Type | Rules |
|---|---|---|
| `version` | int | must be `1` |
| `defaults.acl_teams` | list[str] | default `[]` |
| `sources` | list | at least one; `name` must be unique across the file |

## Common fields (every source block)

| Field | Type | Rules |
|---|---|---|
| `name` | str | required; `^[a-z0-9][a-z0-9._-]{0,63}$`; unique; appears in structured logs |
| `type` | str | required discriminator: `github_code` \| `github_doc` \| `azure_wiki` \| `ado_card` |
| `enabled` | bool | default `true`; a disabled source is skipped (and logged) without being deleted from config history |
| `acl_teams` | list[str] | default = `defaults.acl_teams`; written to `source_item.acl_teams` |
| `auth.token_env` | str | optional; `^[A-Z][A-Z0-9_]*$` — the **name** of the environment variable holding the PAT/token. If present, the variable must be set at load time or the build aborts. If absent, the connector runs unauthenticated (public sources only). |
| `public` | bool | default `false`; explicit opt-in that an **auth-less** remote source is intentionally public. Without it, a source missing `auth` is a **pre-flight ERROR** in `--backend production` (an unauthenticated request 404s on a private repo). Set `public: true` only for a genuinely public source. |

Unknown fields anywhere are rejected (`extra="forbid"`)— a typo never silently changes meaning.

## Pre-flight validation (`--validate-only`)

Before any fetch, the build validates the config against the chosen backend and reports
**all** problems at once (`connectors/config_validator.py`). `--validate-only` runs just this
check and exits (no DB, no network). Any ERROR aborts the build.

- **`--backend production`** — ERROR if a remote source has no `auth` and is not `public: true`;
  ERROR if a referenced `auth.token_env` is not set in the environment; WARN if a source is both
  `public` and authed.
- **`--backend local`** — ERROR if `--workspace` does not exist; WARN if a source type is not
  file-readable locally (`azure_wiki` / `ado_card`); WARN if a `github_*` source's include globs
  match no workspace file. Tokens are not required (the local backend reads files).

## Path selection (`github_code`, `github_doc`, `azure_wiki`)

| Field | Type | Rules |
|---|---|---|
| `include` | list[str] | default `["**"]` (everything); a path is a candidate if it matches **any** include |
| `exclude` | list[str] | default `[]`; a candidate is dropped if it matches **any** exclude — **exclude wins** |

Glob semantics (deterministic, same on every machine):

| Token | Matches |
|---|---|
| `**` | any number of path segments, including zero |
| `*` | any characters **within** one segment (never crosses `/`) |
| `?` | exactly one character within a segment |
| anything else | literal — there are **no character classes**; `[ab]` matches the literal text `[ab]` |

`**` must stand alone as a full path segment (`a**b` is rejected); consecutive `**` segments
collapse to one.

Paths are repo-relative (GitHub) or wiki-page paths (Azure Wiki), `/`-separated, no leading `/`.
Examples: `services/**/*.py` matches `services/a/b.py` but not `docs/x.py`; `*.md` matches `README.md`
but not `docs/intro.md`; `**/tests/**` matches any `tests` directory at any depth.

## Per-type fields

### `github_code` / `github_doc`

| Field | Type | Rules |
|---|---|---|
| `repo` | str | required; `owner/name` |
| `branch` | str | default `main`; resolved to **one commit SHA per build** — every fetched file carries that SHA as `source_version` |

`github_code` feeds Graphify (normalized line-endings-only; evidence stays byte-exact).
`github_doc` feeds Docify (full prose normalization, ADR-0023).

### `azure_wiki`

| Field | Type | Rules |
|---|---|---|
| `organization` | str | required |
| `project` | str | required |
| `wiki` | str | required; the wiki identifier |

Page id lands in `SourceRef.external_id`; page revision in `source_version`; the page path (used by
`include`/`exclude`) in `SourceRef.path`.

### `ado_card`

Cards have no paths; selection is query-shaped instead of glob-shaped:

| Field | Type | Rules |
|---|---|---|
| `organization` | str | required |
| `project` | str | required |
| `area_path` | str | optional; subtree filter (e.g. `Platform\\KB`) |
| `work_item_types` | list[str] | default `[]` = all (e.g. `["User Story", "Bug"]`) |
| `states` | list[str] | default `[]` = all |
| `tags` | list[str] | default `[]` = no tag filter; a card must carry **all** listed tags |

Card id → `external_id`; card revision → `source_version`; fields are snapshot-rendered (cards
mutate — see the raw-document storage policy).

## Full example

The canonical, contract-tested example is
[`services/kb-builder/sources.example.yaml`](../../services/kb-builder/sources.example.yaml):

```yaml
version: 1

defaults:
  acl_teams: []

sources:
  - name: platform-code
    type: github_code
    repo: RohitSh26/agentic-kb-platform
    branch: main
    include:
      - "services/**/*.py"
    exclude:
      - "**/tests/**"
    auth:
      token_env: GITHUB_TOKEN

  - name: platform-docs
    type: github_doc
    repo: RohitSh26/agentic-kb-platform
    branch: main
    include:
      - "docs/**/*.md"
      - "README.md"

  - name: platform-wiki
    type: azure_wiki
    organization: contoso
    project: platform
    wiki: platform.wiki
    include:
      - "Architecture/**"
      - "Runbooks/**"
    exclude:
      - "Archive/**"
    acl_teams: ["platform-eng"]
    auth:
      token_env: ADO_PAT

  - name: roadmap-cards
    type: ado_card
    organization: contoso
    project: platform
    area_path: "Platform\\KB"
    work_item_types: ["User Story", "Bug"]
    states: ["Active", "Resolved", "Closed"]
    auth:
      token_env: ADO_PAT
```

## Runtime semantics

- The build reads the config path from `SOURCE_CONFIG_PATH` (no default in production — explicit
  beats implicit; tests pass paths directly).
- Load order: parse YAML (`yaml.safe_load`) → validate schema → resolve every **enabled** source's
  configured `token_env` against the environment → construct connectors. Any failure aborts before
  any fetch. (A disabled source's `token_env` is not resolved — disabling a source must not require
  its credential.)
- `FilteredFetchBackend` applies `include`/`exclude` to `list_sources()` output — an excluded path
  is never fetched, hashed, or stored.
- `acl_teams` flows `sources.yaml` → `SourceRef` → `source_item.acl_teams` on insert **and**
  update. (Propagation onto derived `knowledge_artifact` rows is a recorded follow-up; until it
  lands, artifact-level ACLs remain org-public.)
- Structured log on load: `event=source_config_loaded sources=N github_code=N ...` — never a token
  value, never a token env-var's *value*.

## What this contract does not cover

The real GitHub/Azure DevOps API `FetchBackend` implementations (recorded follow-up). The factory
seam `connectors_from_config(config, backend_factory)` is where they plug in; this schema is
deliberately sufficient to drive them when they land.
