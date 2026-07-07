# Switch LLM providers

**Goal:** point every LLM-touching component at the provider you actually use — Groq, OpenAI,
Azure OpenAI, Azure AI Foundry (including Claude on Foundry), or a local Ollama.

Only two build passes call the chat model: **docify** (doc/wiki/ticket summaries) and the opt-in
**relationship judge**. A code-only build makes zero model calls and needs no provider at all.
The review panel needs a provider only when computing a *new* draft. The broker's default serving
path is 100% deterministic — no key.

## Pick one block

Export it in the shell you build from, or put it in the repo-root `.env` (gitignored — see
"Where keys live" below).

### Groq — recommended default

```sh
export LLM_PROVIDER=groq
export LLM_API_KEY=gsk_...                       # required (GROQ_API_KEY works too)
export LLM_MODEL=llama-3.1-8b-instant            # default for groq if unset
# LLM_BASE_URL defaults to https://api.groq.com/openai/v1
```

Fast, cheap, OpenAI-compatible — the path the rest of this guide assumes for prose builds.

### OpenAI

```sh
export LLM_PROVIDER=openai
export LLM_API_KEY=sk-...                         # required
export LLM_MODEL=gpt-4o-mini                      # default for openai if unset
# LLM_BASE_URL defaults to https://api.openai.com/v1
```

### Azure OpenAI

```sh
export LLM_PROVIDER=azure
export AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com
export AZURE_OPENAI_API_KEY=<key>
export AZURE_OPENAI_DEPLOYMENT=<your-deployment-name>     # the deployment IS the model
export AZURE_OPENAI_API_VERSION=2024-06-01                # optional; this is the default
```

A dedicated resolution path, not the generic `LLM_BASE_URL` one. All three of endpoint, key, and
deployment are required — a missing one fails the build loudly, naming exactly which is missing.

### Claude on Azure AI Foundry

```sh
export LLM_PROVIDER=anthropic_foundry
export LLM_BASE_URL=https://<your-resource>.services.ai.azure.com/anthropic
export LLM_API_KEY=<key>
export LLM_MODEL=claude-sonnet-4-6        # the Claude deployment name
```

A distinct code path through the Anthropic SDK and the Messages API — not the OpenAI-compatible
branch. Choose this when your org standardizes on Claude hosted through Azure AI Foundry.

### Ollama — local, free fallback

```sh
export LLM_PROVIDER=ollama       # also the behavior when LLM_PROVIDER is unset
export LLM_MODEL=phi4-mini       # default would be llama3.1
# LLM_BASE_URL defaults to http://localhost:11434/v1
```

No key, no cloud, no spend (`ollama serve` must be running; `ollama pull <model>` first). Expect
slower builds and fewer verbatim-quotable extractions from small local models — fine for testing
the pipeline, not the quality bar.

### Any other OpenAI-compatible endpoint

```sh
export LLM_PROVIDER=custom                # any non-reserved name works
export LLM_BASE_URL=https://<endpoint>/v1
export LLM_API_KEY=<key-or-"x" if the server ignores it>
export LLM_MODEL=<model-id>
```

OpenRouter, vLLM, LM Studio, llama.cpp, Anthropic's own OpenAI-compatibility endpoint — anything
that speaks `chat/completions`. Only `azure` and `anthropic_foundry` are special-cased provider
names; every other name is a label for this generic branch.

## Two opt-in gates that unlock extra build passes

| Env var | What it unlocks |
|---|---|
| `RELATIONSHIP_JUDGE` (any non-empty value) | The relationship judge: the chat model rules on bounded candidate pairs and promotes verdicts to inferred edges. Uses the **same** `LLM_*` provider as docify — no separate credentials. Unset means candidates are generated and audited but never judged. |
| `EMBEDDINGS_PROVIDER` (**validated**: `ollama` or `openai` only) | The semantic-linker pass: real embedding similarity between prose concepts and code. Any other value fails the build immediately with a clear error — never a silent no-op. Unset skips the pass. |

Search embeddings for artifacts are **always** a free local hash, never an API call —
`EMBEDDINGS_PROVIDER` only affects the optional semantic-similarity pass. Variable-by-variable
detail: [environment variables](../reference/environment-variables.md).

## Run docs on a different model

Set `DOC_LLM_PROVIDER` / `DOC_LLM_MODEL` / `DOC_LLM_API_KEY` / `DOC_LLM_BASE_URL` to route docify
to a separate model from the judge. Docify only switches when `DOC_LLM_PROVIDER` is set; otherwise
it shares the global `LLM_*` config.

## Which components accept which provider names

| Component | Accepted `LLM_PROVIDER` values |
|---|---|
| kb-builder (docify + judge) | Any string. `azure` and `anthropic_foundry` get dedicated paths; everything else is generic OpenAI-compatible. |
| review-panel | `groq`, `openai`, `openai_compatible`, `ollama`, `anthropic`, `anthropic_foundry`. **Not `azure`** — it raises a clear error. |

Each accepted set is pinned by a drift test in that component's own suite, so a divergence fails
loudly. `LLM_API_KEY` falls back to `GROQ_API_KEY` in every consumer.

## Where keys live

The repo-root `.env` is the conventional home; it is gitignored (only `.env.example` is tracked).
What loads it automatically differs per component:

| Component | Loads repo-root `.env` itself? |
|---|---|
| The build CLI (`agentic_kb_builder.build`) | **No.** Export vars, or `set -a; source .env; set +a` first. |
| `scripts/bootstrap.sh --with-docs` | Yes, for the optional docs pass only. |
| `scripts/kb_agent.py` / `scripts/agent_runner.py` | Yes (shell env still wins). |
| The MCP server | **No.** Export in the shell or container env. |
| The review-panel package | **No** — but `scripts/run_review_panel_local.sh` sources it for you. |
| evals (T3 tier) | **No.** It only checks whether creds are already in the environment. |

No component ever prints a key: model clients log `provider`/`model` and never the key value, and
a missing required key fails loudly before any call.

## Verify

With `LLM_PROVIDER` + `LLM_API_KEY` (+ `LLM_MODEL`) in `.env`:

```sh
./scripts/bootstrap.sh --with-docs
```

You should see the docs pass run and end in `build status : active`. If the pass fails (bad key,
rate limit, model typo), the zero-LLM knowledge base from the default build **stays active and
fully queryable** — a failed optional build never regresses what is being served. Provider errors:
[troubleshooting](troubleshoot.md).
