# Environment variables

Every variable the platform reads, grouped by component. Configuration is environment-only: no
config files, no secrets in code. Variables that name credentials are always resolved by **name**
— a token value never lands in config, storage, or logs.

## MCP server (`services/mcp-server`)

Three variables are required; the server refuses to start without them (fail-fast, no silent
defaults).

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `DATABASE_URL` | **yes** | — | asyncpg URL of the Postgres holding the active KB. Must start `postgresql+asyncpg://`. May point at a remote Postgres (e.g. over an SSH tunnel); the server only ever serves what that registry holds. |
| `MCP_ENTRA_TENANT_ID` | **yes** | — | Entra tenant id (an identifier, not a secret) the bearer's issuer must match. `local-dev` in local-dev mode. |
| `MCP_ENTRA_AUDIENCE` | **yes** | — | The `aud` claim your access token must carry, e.g. `api://agentic-kb`. |
| `MCP_AGENT_ALLOWANCES` | no | unset ⇒ defaults | Subject → per-agent budget JSON, e.g. `{"local-dev": {"max_requests": 50, "max_tokens": 50000}}`. Both keys are required per entry; parsed fail-fast. A subject not listed gets the default allowance of **1 request / 2,500 tokens**. |
| `MCP_CLIENT_REGISTRY` | no | unset | client_id → scopes + verification policy JSON. Identifiers + policy only; any client secret is referenced by env/Key Vault name, never a value. |
| `MCP_HTTP_HOST` | no | `0.0.0.0` | Transport bind host. |
| `MCP_HTTP_PORT` | no | `8000` | Transport port. |
| `MCP_HTTP_PATH` | no | `/mcp/` | Streamable-HTTP mount path. |
| `TRACE_SINK` | no | unset ⇒ `postgres` | `postgres` writes spans to `trace_span` when a database is configured; `none` disables tracing; any other value fails at startup. Tracing is fail-soft: a sink error never fails the call it observes. |
| `VERIFY_SIGNING_KEY` | no | — | Name of the env var is the default; the value is the HMAC-SHA256 key that signs verification receipts. The name may appear in logs; the value never does. |

`MCP_HTTP_*` affect the transport only — never auth, budgets, ACLs, or the ledger.

### Local-dev identity (loopback only)

| Variable | Default | Meaning |
|---|---|---|
| `MCP_LOCAL_DEV_AUTH` | unset ⇒ **off** | Truthy (`1/true/yes/on`) enables the loopback-only dev verifier: any non-empty bearer authorizes as the dev subject. Every request still flows through the normal ACL / scope / budget checks. Logged loudly on every start (`event=local_dev_auth_enabled`). |
| `MCP_LOCAL_DEV_SUBJECT` | `local-dev` | The dev identity's subject (what the ledger records). |
| `MCP_LOCAL_DEV_TEAMS` | `local-dev-team` | CSV of teams granted to the dev identity (your ACLs). |
| `MCP_LOCAL_DEV_CLIENT_ID` | = subject | Optional dev `client_id` for the platform-trust tool. |

**Guardrails** (ADR-0016): a real tenant id or a non-loopback bind refuses to boot. Never set
`MCP_LOCAL_DEV_AUTH` in a deployed image. Production auth is fail-closed Entra ID with no
override.

### L3 entailment (opt-in claim verifier)

Unset (the default) keeps the server 100% LLM-free; `context.verify_answer` runs its
deterministic L0–L2 checks only.

| Variable | Default | Meaning |
|---|---|---|
| `MCP_ENABLE_ENTAILMENT` | unset ⇒ off | Truthy attaches an entailment client for L3, cache-gated per claim. |
| `ENTAIL_LLM_PROVIDER` | `ollama` | `ollama` \| `groq` \| `openai` \| `azure`. The default needs no key. |
| `ENTAIL_LLM_BASE_URL` / `ENTAIL_LLM_API_KEY` / `ENTAIL_LLM_MODEL` | per provider | OpenAI-compatible endpoint config; the key is required for remote providers. |
| `ENTAIL_LLM_TEMPERATURE` / `ENTAIL_LLM_MAX_TOKENS` | `0` / `512` | Sampling controls. |
| `ENTAIL_AZURE_OPENAI_ENDPOINT` / `_API_KEY` / `_DEPLOYMENT` / `_API_VERSION` | — / — / — / `2024-06-01` | The Azure path; the first three are required when `ENTAIL_LLM_PROVIDER=azure`. |

## KB builder (`services/kb-builder`)

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `DATABASE_URL` | **yes** (build/migrate) | — | asyncpg URL of the Knowledge Registry to build into. |
| `LLM_PROVIDER` | no | `ollama` | Chat model for docify + the relationship judge. Any string accepted: `azure` and `anthropic_foundry` get dedicated code paths; everything else is generic OpenAI-compatible. A code-only build makes zero model calls and needs none of the `LLM_*` vars. |
| `LLM_MODEL` | no | per provider | Model / deployment name. |
| `LLM_BASE_URL` | no | per provider | OpenAI-compatible endpoint (non-Azure providers). |
| `LLM_API_KEY` | for remote providers | — | Falls back to `GROQ_API_KEY` when unset. A missing key for a remote provider fails the build loudly before any call. |
| `LLM_TEMPERATURE` | no | `0` | Deterministic summaries by default. |
| `LLM_MAX_TOKENS` | no | `4000` | Max output tokens per call. |
| `DOC_LLM_*` | no | falls back to `LLM_*` | Same quartet with the `DOC_LLM_` prefix — runs document extraction on a separate model from the judge/agent. Only switches when `DOC_LLM_PROVIDER` is set. |
| `AZURE_OPENAI_ENDPOINT` / `_API_KEY` / `_DEPLOYMENT` | with `LLM_PROVIDER=azure` | — | All three required or the build fails naming the missing one. The deployment IS the model. |
| `AZURE_OPENAI_API_VERSION` | no | `2024-06-01` | Azure API version. |
| `EMBEDDINGS_PROVIDER` | no | unset ⇒ pass skipped | Enables the optional prose↔code semantic-linker pass (ADR-0019). **Validated**: exactly `ollama` or `openai`; any other value fails the build at startup. Artifact embeddings for search are always a free local hash, never an API call, regardless of this gate. |
| `EMBEDDINGS_BASE_URL` | no | `http://localhost:11434` (ollama) / `https://api.openai.com/v1` (openai) | Embedding endpoint. |
| `EMBEDDINGS_MODEL` | no | `nomic-embed-text` (ollama) / `text-embedding-3-small` (openai) | Embedding model. |
| `EMBEDDINGS_API_KEY` | openai: **yes**; ollama: no | — | Required for `openai` (fails at construction if unset); optional Bearer header for a hosted ollama-shaped gateway. |
| `RELATIONSHIP_JUDGE` | no | unset ⇒ pass skipped | Any non-empty value enables the phase-3B relationship judge (inferred edges). Reuses the `LLM_*` provider; every judgment is gated by `relationship_judgment_cache`. Unset ⇒ candidates are generated and audited but never judged. |
| `KB_LOCAL_INDEX_PATH` | no | `./.kb-local-search-index.json` | Where the persistent local search index lives (also `--index-path`). |
| `LOG_FORMAT` | no | `timeline` on a TTY, else `raw` | Terminal log rendering: `timeline` \| `raw` \| `json` (also `--log-format`). |

### Provider defaults (`LLM_PROVIDER` → base URL, model)

| Provider | `LLM_BASE_URL` default | `LLM_MODEL` default |
|---|---|---|
| `ollama` (and unset) | `http://localhost:11434/v1` | `llama3.1` |
| `groq` | `https://api.groq.com/openai/v1` | `llama-3.1-8b-instant` |
| `openai` | `https://api.openai.com/v1` | `gpt-4o-mini` |
| any other name | your `LLM_BASE_URL` | your `LLM_MODEL` |

Full copy-paste setup blocks per provider: [switch-llm-providers](../how-to/switch-llm-providers.md).

### Source credentials (production backend)

| Variable | Meaning |
|---|---|
| `GITHUB_TOKEN` | Conventional name for a GitHub PAT (classic with `repo` scope, or fine-grained with Contents: Read). Each source's `auth.token_env` in `sources.yaml` names the env var to read — the value never appears in stored config. |
| `ADO_PAT` | Conventional name for an Azure DevOps PAT (Wiki Read + Work Items Read). |

`--backend local` (the bootstrap default) reads workspace files and needs neither.

### TLS trust (corporate proxies)

| Variable | Meaning |
|---|---|
| `LLM_CA_CERT` / `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` | CA bundle for the LLM HTTP client, checked in that order. |
| `LLM_SSL_VERIFY` | `false` disables verification — last resort, logged loudly (`event=llm_ssl_verify_disabled`) so it is never silently on. |

## Review panel (`services/review-panel`)

| Variable | Required | Meaning |
|---|---|---|
| `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY` (/ `LLM_BASE_URL`) | to compute a **new** draft | The model behind the four lenses + synthesizer. Accepted providers: `groq` \| `openai` \| `openai_compatible` \| `ollama` \| `anthropic` \| `anthropic_foundry`. **`azure` is rejected** — this component's provider set is narrower than kb-builder's. Fetching an already-stored draft needs no LLM credentials. `LLM_API_KEY` falls back to `GROQ_API_KEY`. |
| `REVIEW_PANEL_DATABASE_URL` | for durability | Postgres URL for the checkpointer + draft store (the `review_panel` schema). Unset ⇒ in-memory fallback: the run works, nothing survives the process, logged as `event=persistence_fallback`. |
| `GITHUB_TOKEN` | private repos / rate limits | Optional **read-only** token for the PR fetch. The service holds no write credential. |
| `REVIEW_PANEL_AGENTS_DIR` | non-repo checkouts | Path to the canonical `agents/` directory (the wrapper script sets it). |
| `REVIEW_PANEL_MCP_URL` / `REVIEW_PANEL_MCP_TOKEN` | optional | When set, the panel makes one `kb_search` call during `load_pr` and shares the fenced, untrusted result with all four lenses. Failures are fail-soft. |
| `TRACE_SINK` | no | Same semantics as the MCP server; spans land in `review_panel.trace_span`. |

`LANGSMITH_TRACING` / `LANGSMITH_API_KEY` are **inert** — surfaced in the boot log only. Tracing
is Postgres, via `TRACE_SINK` (ADR-0032).

## Evals and scripts

| Variable | Default | Meaning |
|---|---|---|
| `TEST_DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/agentic_kb_test` (Makefile) | The shared test database for `make verify` / `make test-*` / `make eval-run`. Homebrew Postgres users pass their own role in the URL. |
| `DATABASE_URL` | — | A real, built registry for `make dashboard`, `make eval-all`'s T2/T3 tiers, and the eval scripts. |
| `MCP_URL` | — | Broker URL for `scripts/smoke_client.py` and `scripts/agent_runner.py`, e.g. `http://127.0.0.1:8765/mcp/`. |
| `GROQ_API_KEY` | — | Accepted as the `LLM_API_KEY` fallback by kb-builder, the review panel, and the eval/agent scripts. |
| `OPENCODE_MODEL` | — | Groq-hosted model id for `scripts/integration/run_opencode.sh`. |

## Host-side (editors and agents)

| Variable | Meaning |
|---|---|
| `CONTEXT_BROKER_TOKEN` | The bearer OpenCode's config references as `{env:CONTEXT_BROKER_TOKEN}`. Any non-empty value against a local-dev broker; a real Entra token otherwise. |
| `COPILOT_MCP_CONTEXT_BROKER_TOKEN` | Same for GitHub Copilot; Copilot only exposes environment values whose names start with `COPILOT_MCP_`. |

## Docker compose

| Variable | Default | Meaning |
|---|---|---|
| `POSTGRES_HOST_PORT` | `55432` | Host port for the compose Postgres (not 5432, so a Homebrew Postgres keeps working). |
| `MCP_HOST_PORT` | `8000` | Host port for the compose MCP server. |

## `.env` loading

The repo-root `.env` is the conventional, gitignored home for keys. Loading is **not** uniform —
verified per component:

| Component | Loads repo-root `.env` itself? |
|---|---|
| `agentic_kb_builder.build` (the build CLI) | **No.** Export vars in your shell, or `set -a; source .env; set +a` first. |
| `scripts/bootstrap.sh --with-docs` | **Yes**, for that optional pass only. |
| `scripts/kb_agent.py` / `scripts/agent_runner.py` | **Yes** — at import time; shell env still wins. |
| `agentic_mcp_server` | **No.** Export in the shell or container env. |
| `review-panel` (the Python package) | **No** — but `scripts/run_review_panel_local.sh` sources it for you. |
| `evals/` (T3 tier) | **No.** It only checks whether the vars are already present. |

## Broker bearer tokens

Every broker tool call requires an authenticated session. The verifier rejects any token whose
signature, issuer, or `aud` doesn't match `MCP_ENTRA_TENANT_ID` / `MCP_ENTRA_AUDIENCE` — with no
override.

**Local-dev broker (loopback only).** With `MCP_LOCAL_DEV_AUTH=1`, any non-empty bearer string
authorizes as the configured dev subject. Nothing to acquire — the placeholder tokens in the
shipped host configs are fine as-is.

**Real Entra token (everything else).** Acquire an access token for the audience the server was
started with:

```sh
# az CLI (interactive)
az login
az account get-access-token --resource "api://agentic-kb" --query accessToken -o tsv
```

```python
# MSAL client-credentials (a service identity calling the broker)
import msal
app = msal.ConfidentialClientApplication(
    client_id="<app-client-id>",
    authority="https://login.microsoftonline.com/<tenant-guid>",
    client_credential="<client-secret-or-cert>",  # from your secret store, never hard-coded
)
token = app.acquire_token_for_client(scopes=["api://agentic-kb/.default"])["access_token"]
```

Present it as `Authorization: Bearer <token>`. Host configs reference it by env var name
(`CONTEXT_BROKER_TOKEN` / `COPILOT_MCP_CONTEXT_BROKER_TOKEN`) — a token value never lands in a
config file. The token's `aud` must equal `MCP_ENTRA_AUDIENCE` and its issuer must be the server's
tenant, or the JWKS verifier rejects it (401).

## Security notes

- **Keys are never logged.** Every model-client constructor logs `provider`/`model` and never the
  key. Source tokens are resolved by env var name and never appear in stored config.
- **A missing key fails loudly, never silently.** Every provider branch raises before making a
  call if a required variable is unset.
- **The ledger and traces never carry free text.** `retrieval_event` records tokens, status, and
  artifact ids — not prompts or model output; trace spans reject content-shaped attribute keys.
