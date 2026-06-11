# PR-14 — YAML source configuration

## Scope
A declarative, reviewable `sources.yaml` through which teams specify exactly what the nightly build
ingests: which GitHub repos (and which paths within them), which Azure Wiki pages, which ADO cards —
with gitignore-style include/exclude globs, per-source ACL teams, and credentials referenced by
environment-variable **name** only. Schema validation, the path filter, env-var token resolution,
and the config→connector factory. No real API backends (those remain the recorded follow-up); the
factory accepts any `FetchBackend` implementation.

## Context
docs/contracts/source-config.md (written in this PR). docs/architecture §5, §7.
.claude/rules/connectors.md. Pattern references: dependabot.yml (versioned schema, per-ecosystem
blocks, path scoping), CODEOWNERS (glob semantics), 12-factor config (secrets via environment).

## Files to create
- `services/kb-builder/src/agentic_kb_builder/domain/source_config.py` — pydantic models:
  `SourceConfig` (version, defaults, sources), discriminated union on `type`
  (`GithubCodeSourceSpec`, `GithubDocSourceSpec`, `AzureWikiSourceSpec`, `AdoCardSourceSpec`),
  `AuthRef(token_env)`, `PathFilter` (pure `**`/`*`/`?` glob matching, exclude wins).
- `services/kb-builder/src/agentic_kb_builder/connectors/config_loader.py` — `load_source_config`
  (yaml.safe_load → validated models, fail-fast with file + source-name context),
  `resolve_token` (os.environ lookup; configured-but-missing var is a hard error),
  `FilteredFetchBackend` (wraps any backend, applies the path filter to `list_sources`),
  `connectors_from_config` (spec + resolved token → connector, via an injected backend factory).
- `services/kb-builder/sources.example.yaml` — the documented example, contract-tested.
- `docs/contracts/source-config.md` — the schema contract.

## Files to change
- `domain/source_records.py` — add `acl_teams: list[str] = []` to `SourceRef`.
- `application/build_runner.py` — `_upsert_source_item` writes `acl_teams` from the ref.
- `services/kb-builder/pyproject.toml` — add `pyyaml`; dev: `types-pyyaml`.

## Contracts
docs/contracts/source-config.md is the authoritative schema. A contract test pins
`sources.example.yaml` against the pydantic models so the example can never rot.

## Acceptance criteria
- A valid YAML loads into typed specs; every invalid case (unknown type, duplicate name, bad repo
  format, lowercase token_env, unknown field) fails with a precise error naming the source.
- Include/exclude globs: `**` spans segments, `*` stays within a segment, exclude beats include,
  default include is everything. Deterministic across machines.
- `token_env` values are names only; resolution reads `os.environ` at load time; a configured but
  unset variable raises. **No secret value ever appears in config, code, fixtures, or logs.**
- `FilteredFetchBackend` drops non-matching sources before fetch (skipped paths are never fetched).
- `acl_teams` flows config → `SourceRef` → `source_item` upsert (insert and update).
- Structured logging on load: source count per type, never token values.

## Required tests
- Schema validation matrix (valid + each invalid case).
- Glob semantics table test (segment boundaries, `**`, exclude-wins, defaults).
- Env resolution: present, absent (raises), never logged.
- FilteredFetchBackend filtering against a fake backend.
- Round-trip: example YAML → connectors → build engine run with fakes; `acl_teams` lands on
  `source_item`.

## Do NOT
- Implement real GitHub/ADO API clients (separate follow-up PR).
- Propagate `acl_teams` onto `knowledge_artifact` rows (stays a recorded follow-up).
- Read or store token values anywhere except a local variable handed to the backend factory.

## Kickoff prompt
"Implement PR-14 per the brief. Contract doc first, then models, loader, filter, factory, tests.
PATs from env vars only."
