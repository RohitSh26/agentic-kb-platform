# 07 — Providers and API keys

**Reference, not a runbook.** Every key and credential this platform can use — which component
needs it, for what, and exactly how to point that component at Groq, OpenAI, Azure OpenAI, Claude
on Azure AI Foundry, or a local Ollama. Written for the "which key do I need for *this* task"
question — if you're building your first KB, start at
[01 — Run the platform](01-run-the-platform.md); if you're deep in kb-builder's provider flags,
[22 §4](22-testing-and-builds.md#4-choose-and-configure-an-llm-provider) has the copy-paste
runbook this page cross-links to. Every variable below is cited to the source that resolves it,
so nothing here is asserted without a file to check it against.

---

## 1. The one-table answer

| Component | Needs a key? | Vars | When |
|---|---|---|---|
| **Zero-LLM build** (code + git-metadata, `bootstrap.sh` default) | **No** | — | Always the default path. Code is extracted by Graphify's AST pass, commits by `GitMetadataConnector` — both deterministic, zero model calls. |
| **docify** (doc/wiki/card summaries; `--with-docs`) | **Yes** | `LLM_PROVIDER` / `LLM_API_KEY` / `LLM_MODEL` / `LLM_BASE_URL` (or `DOC_LLM_*` to run docs on a *different* model than the judge/agent) | Your sources include prose (`github_doc`, `azure_wiki`, `ado_card`). §2 below; runbook: [22 §4](22-testing-and-builds.md#4-choose-and-configure-an-llm-provider). |
| **Embeddings / semantic linker** | Opt-in; key optional for `ollama`, **required** for `openai` | `EMBEDDINGS_PROVIDER` (validated: `ollama` \| `openai` — any other value fails the build loudly at startup), `EMBEDDINGS_BASE_URL`, `EMBEDDINGS_MODEL`, `EMBEDDINGS_API_KEY` | Unset ⇒ skipped entirely; artifact embeddings for search are **always** a free local hash (`LocalHashEmbedder`), never an API call. This gate only affects the prose↔code semantic-similarity pass (ADR-0019). |
| **Relationship judge** (phase-3B inferred edges) | Opt-in, reuses docify's key | `RELATIONSHIP_JUDGE` (gate — any non-empty value) | Unset ⇒ candidates are generated and audited but never judged. Uses the **same** `LLM_*`/`DOC_LLM_*` provider as docify — no separate credentials. |
| **mcp-server** (default: serving `kb_search` / `context.*` / `graph.*`) | **No** | — | The broker's default retrieval, budget, ACL, and receipt paths are 100% deterministic Postgres reads. Verified from `mcp/server.py::create_app` — no model client is constructed unless you opt in (next row). |
| **mcp-server L3 entailment** (opt-in claim verifier) | Opt-in, key optional | `MCP_ENABLE_ENTAILMENT` (gate), `ENTAIL_LLM_PROVIDER` / `ENTAIL_LLM_API_KEY` / `ENTAIL_LLM_MODEL` / `ENTAIL_LLM_BASE_URL` (or `ENTAIL_AZURE_OPENAI_*`) | Unset (default) ⇒ the server stays LLM-free and `verify_answer` never runs L3 for any claim (L0-L2 deterministic checks still run). Set it and the server attaches an entailment client, cache-gated per claim. Defaults to local Ollama — **no key needed even when enabled**, unless you repoint it at Groq/OpenAI/Azure. |
| **review-panel drafts** (four-lens PR review) | Yes, for a *new* draft only | `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY` (/ `LLM_BASE_URL`) | Fetching an **already-stored** draft (same head SHA) needs zero LLM credentials — only computing a fresh one does. |
| **evals A/B (T3 tier) + `kb_agent.py`** | Yes, for the LLM-armed comparison | `LLM_PROVIDER` / `LLM_API_KEY` (or `GROQ_API_KEY`) / `LLM_MODEL` / `LLM_BASE_URL` | `kb_agent.py` always needs one to run at all (it *is* the agent). The eval suite's T3 tier skips itself with a stated reason when creds are absent — T1/T2/T4 don't need one. |
| **Hosts** (VS Code Copilot, OpenCode, Copilot CLI, `agent_runner.py`) | Their own model, plus a broker token | Each host's own model auth (out of this platform's scope) + `CONTEXT_BROKER_TOKEN` / `COPILOT_MCP_CONTEXT_BROKER_TOKEN` (a **real Entra token**, required for any non-loopback broker) | Against a **loopback, local-dev-auth** broker (ADR-0016), any non-empty bearer works. Against anything else, you need a real Entra access token for `MCP_ENTRA_AUDIENCE` — see §5 below. |
| **Production source connectors** (`--backend production`) | Yes, per source | Whatever env var name each source's `auth.token_env` names in `sources.yaml` — conventionally `GITHUB_TOKEN` / `ADO_PAT` | `--backend local` (the default; what `bootstrap.sh` uses) never authenticates and needs **none** of these — it only reads workspace files. |

---

## 2. Per-provider setup blocks (kb-builder: docify + relationship judge)

These four env vars resolve through one shared function,
`resolve_endpoint_from_env` in
`services/kb-builder/src/agentic_kb_builder/infrastructure/azure_openai/llm_endpoint.py`
(used by both `ChatModelClient` — the judge — and the `docify` adapter). Set `DOC_LLM_*` instead
of `LLM_*` to run documents on a separate model from the judge/agent (`docify/extract_fn.py`
`resolve_endpoint()` — only switches when `DOC_LLM_PROVIDER` is set; otherwise docify shares the
global `LLM_*` config).

### Groq — recommended default
```sh
export LLM_PROVIDER=groq
export LLM_API_KEY=gsk_...                       # required (or GROQ_API_KEY, same fallback
                                                  # scripts/kb_agent.py and review-panel have)
export LLM_MODEL=llama-3.1-8b-instant            # default for groq if unset
# LLM_BASE_URL defaults to https://api.groq.com/openai/v1
```
Fast, cheap, OpenAI-compatible — the path the rest of the dev guide assumes for prose builds. Put
the key in a repo-root `.env`: `bootstrap.sh --with-docs` and `kb_agent.py` load it themselves
(§4); a plain `uv run python -m agentic_kb_builder.build` does **not** — export it in your shell.
Defaults: `PROVIDER_DEFAULTS["groq"]`, `llm_endpoint.py` lines 39-43.

### OpenAI
```sh
export LLM_PROVIDER=openai
export LLM_API_KEY=sk-...                         # required
export LLM_MODEL=gpt-4o-mini                      # default for openai if unset
# LLM_BASE_URL defaults to https://api.openai.com/v1
```
Choose this if you already have an OpenAI account/quota and don't want a second provider.
Defaults: `PROVIDER_DEFAULTS["openai"]`, `llm_endpoint.py` lines 39-43.

### Azure OpenAI (`azure`)
```sh
export LLM_PROVIDER=azure
export AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com
export AZURE_OPENAI_API_KEY=<key>
export AZURE_OPENAI_DEPLOYMENT=<your-deployment-name>     # the deployment IS the model
export AZURE_OPENAI_API_VERSION=2024-06-01                # optional; this is the default
```
A dedicated resolution branch — not the generic `LLM_BASE_URL` path. All three of
`AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_DEPLOYMENT` are required or the
build fails loudly with a `RuntimeError` naming exactly which is missing (never a silent skip).
Choose this when your org's model spend/quota already runs through Azure OpenAI.
Source: `llm_endpoint.py` lines 98-123 (`AZURE_PROVIDER = "azure"`); Graphify itself has a
built-in `azure` backend that reads these same vars for docify (`docify/extract_fn.py` lines
84-91).

### Claude on Azure AI Foundry (`anthropic_foundry`)
```sh
export LLM_PROVIDER=anthropic_foundry
export LLM_BASE_URL=https://<your-resource>.services.ai.azure.com/anthropic
export LLM_API_KEY=<key>
export LLM_MODEL=claude-sonnet-4-6        # the Claude deployment name
```
**Not documented anywhere else in the dev guide today** — this is one of the gaps this page
closes. `anthropic_foundry` is a distinct code path, not a variant of the generic OpenAI-compatible
branch: it routes through the **Anthropic SDK's `AsyncAnthropicFoundry` client and the Messages
API**, not `chat.completions` (`LLM_BASE_URL` is the Foundry `.../anthropic` endpoint, `LLM_MODEL`
is the deployment name). Because Graphify only speaks OpenAI's API, `docify` bypasses it entirely
for this provider and calls Claude directly (`docify/extract_fn.py::_make_anthropic_foundry_doc_extract`,
added in the ADR-0023 amendment "`anthropic_foundry` docs bypass Graphify"); the relationship
judge dispatches the same way in `chat_model_client.py::_call_anthropic`. Choose this when your
org standardizes on Claude and hosts it through Azure AI Foundry rather than Anthropic directly.
Source: `llm_endpoint.py` lines 49-53, 125-148 (`ANTHROPIC_FOUNDRY_PROVIDER = "anthropic_foundry"`).

### Ollama — local, free fallback
```sh
export LLM_PROVIDER=ollama       # also the behavior when LLM_PROVIDER is unset
export LLM_MODEL=phi4-mini       # default would be llama3.1
# LLM_BASE_URL defaults to http://localhost:11434/v1
```
No key, no cloud, no spend — the build's fallback when `LLM_PROVIDER` is unset. Slower and less
reliable at verbatim-quotable extraction than a hosted model; fine for pipeline testing.
Defaults: `PROVIDER_DEFAULTS["ollama"]`, `llm_endpoint.py` lines 39-43.

> Any other provider name falls through to the generic OpenAI-compatible branch and just needs
> `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL` pointed at anything that speaks the
> `chat/completions` shape (OpenRouter, vLLM, LM Studio, Anthropic's own OpenAI-compatibility
> endpoint, …) — see [22 §4 blocks E-G](22-testing-and-builds.md#4-choose-and-configure-an-llm-provider)
> for worked examples. `llm_endpoint.py` only special-cases `azure` and `anthropic_foundry`;
> everything else is `PROVIDER_DEFAULTS.get(provider, PROVIDER_DEFAULTS["ollama"])` plus your
> override — a name like `foundry` or `custom` has no special meaning to the code, it's purely a
> label.

### Which components honor which providers — kept in sync by drift tests

Three different pieces of code resolve `LLM_PROVIDER`. They deliberately do **not** all accept
the identical set of values (kb-builder's Azure-native and Foundry-native branches don't fit
review-panel's plain-httpx shim's `LLM_*`-only config), but each accepted set is now pinned by a
test in that consumer's own suite, so a silent divergence fails loudly instead of drifting unnoticed:

| Consumer | Accepted `LLM_PROVIDER` values | Source | Drift test |
|---|---|---|---|
| **kb-builder** (docify + judge, `resolve_endpoint_from_env`) | Any string. `azure` and `anthropic_foundry` get dedicated branches; everything else (`ollama`/`groq`/`openai`/anything) is generic OpenAI-compatible. | `llm_endpoint.py` | `tests/unit/test_llm_endpoint.py::test_accepted_provider_set_is_pinned` |
| **`scripts/kb_agent.py`** | `groq` \| `openai` \| `openai_compatible` (OpenAI SDK path) **plus** `anthropic` (native Anthropic SDK) **plus** `anthropic_foundry` (native `AnthropicFoundry` SDK). Any other value is not rejected — it also dispatches to the native Anthropic SDK path (`_make_client`'s `else` branch), so treat this as the *intended* set, not an enforced one. | `kb_agent.py` `_is_openai()` / `_make_client()` | `scripts/test_kb_agent_provider_routing.py` |
| **review-panel** (`ModelClient` shim) | `groq` \| `openai` \| `openai_compatible` \| `ollama` (OpenAI-compatible path) **plus** `anthropic` (native, `https://api.anthropic.com`) **plus** `anthropic_foundry` (Claude on Azure AI Foundry — same Messages API shape as `anthropic`, at a caller-supplied Foundry `LLM_BASE_URL`, adding an `api-key` header alongside `x-api-key`). `azure` still raises `ModelAPIError` — deliberately: `kb_agent.py`, the pattern this module mirrors, never supported `azure` either, and Azure OpenAI's deployment-based config doesn't fit this shim's `LLM_*`-only settings shape. | `review_panel/infrastructure/model_client.py` | `tests/unit/test_model_client.py::test_accepted_provider_set_is_pinned` |

`LLM_API_KEY` falls back to `GROQ_API_KEY` in all three consumers now — kb-builder's
`resolve_endpoint_from_env` gained the same fallback review-panel and `kb_agent.py` already had
(`llm_endpoint.py`'s generic OpenAI-compatible branch; task #38).

---

## 3. Embeddings

`EMBEDDINGS_PROVIDER` used to be a pure on/off gate: any non-empty value enabled the pass and the
value itself was never inspected, so it silently always spoke Ollama's native wire shape even if
`EMBEDDINGS_BASE_URL` pointed at a real OpenAI-style endpoint. It is now **validated** by
`agentic_kb_builder.embeddings.factory.semantic_embedder_from_env` — exactly two values are
accepted, and anything else fails the build loudly at startup, never a silent no-op or a
wrong-shape call.

### `ollama` (default wire shape, offline-friendly)
```sh
export EMBEDDINGS_PROVIDER=ollama
export EMBEDDINGS_BASE_URL=http://localhost:11434   # default; no /v1 suffix
export EMBEDDINGS_MODEL=nomic-embed-text             # default
export EMBEDDINGS_API_KEY=...                        # optional Bearer header, for a hosted gateway
```
`OllamaEmbedder` POSTs to `{EMBEDDINGS_BASE_URL}/api/embeddings` with `{"model", "prompt"}` and
reads back `{"embedding": [...]}`  — Ollama's own shape, not OpenAI's. `EMBEDDINGS_API_KEY` is
optional (a local Ollama server needs no auth); it only adds a Bearer header for a hosted gateway
that mimics this same shape.

### `openai` (the `/v1/embeddings` shape — OpenAI, or Azure OpenAI behind an OpenAI-compatible route)
```sh
export EMBEDDINGS_PROVIDER=openai
export EMBEDDINGS_BASE_URL=https://api.openai.com/v1   # default
export EMBEDDINGS_MODEL=text-embedding-3-small          # default
export EMBEDDINGS_API_KEY=sk-...                         # REQUIRED — fails at construction if unset
```
`OpenAIEmbedder` POSTs to `{EMBEDDINGS_BASE_URL}/embeddings` with `{"model", "input"}` and reads
back `{"data": [{"embedding": [...]}]}` — the shape a real OpenAI account or any
`/v1/embeddings`-speaking gateway expects. Unlike `ollama`, `EMBEDDINGS_API_KEY` is **required**: a
real hosted endpoint always authenticates, so a missing key fails loudly at construction instead
of deep inside the first request.

### Any other value
```sh
export EMBEDDINGS_PROVIDER=azure   # NOT accepted — fails the build immediately
```
Raises `RuntimeError: EMBEDDINGS_PROVIDER='azure' is not supported; use one of ollama, openai`
before any docify/graphify/embed work starts.

Artifact embeddings for **search** are always the free local hash (`LocalHashEmbedder`), never an
API call, regardless of `EMBEDDINGS_PROVIDER` — this gate only affects the optional prose↔code
semantic-similarity pass (ADR-0019).

Source: `services/kb-builder/src/agentic_kb_builder/embeddings/factory.py` (the gate/validation),
`ollama_embedder.py` (the Ollama shape), `openai_embedder.py` (the OpenAI-compatible shape); both
embedders share a common `HttpEmbedder` base (`http_embedder.py`) for client lifecycle.

---

## 4. Where keys live

- **Repo-root `.env`** is the conventional home for every key in this repo. It is **gitignored**
  (`.gitignore` lines 11-17: `.env`, `.env.*`, `.env.mcp`; only `.env.example` is tracked).
- **What loads it automatically, and what doesn't** — verified per component, not assumed uniform:

  | Component | Loads repo-root `.env` itself? |
  |---|---|
  | `agentic_kb_builder.build` (the build CLI) | **No.** No `dotenv` import anywhere in `services/kb-builder/src/`. Export vars in your shell, or `set -a; source .env; set +a` first. |
  | `scripts/bootstrap.sh --with-docs` | **Yes**, but only for that optional pass — `source "$REPO_ROOT/.env"` right before checking for `LLM_API_KEY`/`LLM_PROVIDER` (`bootstrap.sh` lines 144-149). |
  | `scripts/kb_agent.py` | **Yes** — `_load_dotenv()` runs at import time, before any config constant is read (lines 55-76). Shell env still wins over `.env` (it only fills unset keys). |
  | `scripts/agent_runner.py` | **Yes** — the same `_load_dotenv()` pattern, at import time. |
  | `services/mcp-server` (`agentic_mcp_server`) | **No.** Runtime config only; export `DATABASE_URL`/`MCP_*`/`ENTAIL_LLM_*` in the shell or container env. |
  | `services/review-panel` (`review_panel.config`) | **No**, the Python package itself never reads `.env`. `scripts/run_review_panel_local.sh` (the one-command wrapper) sources it for you (`. "$ROOT/.env"`, lines 22-26); if you invoke `uv run review-panel draft` directly, export the vars yourself. |
  | `evals/` (T3 A/B tier) | **No.** It only checks whether `LLM_API_KEY`/`GROQ_API_KEY` are already present in the environment (`evals/harness/tiers.py` line 290) — it never loads `.env`. |

  In short: the **build itself never auto-loads `.env`**; the developer-facing wrapper scripts
  (`bootstrap.sh --with-docs`, `kb_agent.py`, `agent_runner.py`, `run_review_panel_local.sh`) do.

---

## 5. Broker bearer tokens (calling the MCP tools)

Every broker tool call requires an authenticated session, and the verifier rejects any token whose
signature, issuer, or `aud` doesn't match the server's `MCP_ENTRA_TENANT_ID` /
`MCP_ENTRA_AUDIENCE` — intentionally, with **no override**. Two ways to hold a valid bearer:

**Local-dev broker (loopback only).** When the server runs with `MCP_LOCAL_DEV_AUTH=1`
([01 — Run the platform](01-run-the-platform.md) §"Server configuration reference"), any non-empty
bearer string authorizes as the configured dev subject — this is why the shipped host configs use
placeholder tokens. It only exists on `127.0.0.1`; nothing to acquire.

**Real Entra token (everything else).** Acquire an access token for the audience the server was
started with. Two common ways:

```sh
# az CLI (interactive) — mints a token for your app's audience/scope
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

Then present `Authorization: Bearer <token>` on the MCP HTTP requests (hosts reference it by env
var name — `CONTEXT_BROKER_TOKEN` / `COPILOT_MCP_CONTEXT_BROKER_TOKEN` — never as a pasted value).
The token's `aud` **must** equal `MCP_ENTRA_AUDIENCE` and its issuer must be the server's tenant,
or the JWKS verifier rejects it (401).

---

## 6. Security notes

- **Never in code, config, or logs** — house rule (`.claude/rules/python.md`: "No secrets in code,
  fixtures, or logs"). Every model-client constructor in this repo logs `provider`/`model` and
  explicitly never the key (`llm_endpoint.py` module docstring; `chat_model_client.py` line 222;
  `docify/extract_fn.py` lines 88, 140 — "the API key is captured in the closure and NEVER
  logged"). Source `auth.token_env` values (`GITHUB_TOKEN`/`ADO_PAT`) are resolved by *name* and
  never appear in stored config (`connectors/config_loader.py::resolve_token`).
- **The retrieval ledger never carries free text.** `retrieval_event` rows record tokens charged,
  status, and artifact ids — not prompts or model output. Tracing is aggregate-only by the same
  posture: `docs/contracts/tracing.md` states spans **never** store `query_text` or
  `task_description` (line 143), and a broken trace sink logs only `name`/`service`/`status` on
  failure, never span attributes (line 99) — a trace can never leak what a call actually saw.
- **A missing key fails loudly, never silently.** Every provider branch in `llm_endpoint.py`,
  `review_panel/model_client.py`, and `embeddings/openai_embedder.py` raises before making a call
  if a required var is unset — the build/panel/agent never proceeds pretending it has a model when
  it doesn't.
- **TLS trust material** (`LLM_CA_CERT` / `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`, and the last-resort
  `LLM_SSL_VERIFY=false`) is the one exception worth calling out separately: it's not a secret, but
  disabling verification is logged loudly (`event=llm_ssl_verify_disabled`) precisely so it's never
  silently on. See `llm_endpoint.py::llm_http_client` if you're behind a corporate TLS-inspecting
  proxy.

---

Related: [22 — Testing and builds](22-testing-and-builds.md) §4 (the full build-provider runbook),
[04 — Review drafts](04-review-drafts.md) (its own env table),
[06 — Observability](06-observability.md) (what the ledger/traces do and don't record).
