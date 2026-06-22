# 05 — Running the MCP server and using a built KB (fresh separate machine)

> You are on a **different machine from the one that built the KB**. The Postgres registry is
> already populated and has one **active** `kb_version` (doc 04 built it). This guide starts the
> **MCP Context Broker** against that Postgres and walks one worked path through the tools:
> `context_create_pack` → `context_open_evidence` → `graph_get_neighbors` → `context_verify_answer`.
>
> The runtime plane never builds and never migrates — it only *serves* the last successful active
> `kb_version` (invariant 5). Building is doc 04; this is using.
>
> Note: the worked path below is the **governed** broker path — the one to use when an answer must be
> citation-grade (provenance receipt). It is no longer the *only* way an agent reads code. Under
> KB-first/file-fallback (ADR-0025) agents consult the budgeted `kb_search` first and read specific
> files directly when the KB falls short, with code arriving skeleton-first (ADR-0026). See
> [07 — What "MCP ready" means](07-what-mcp-ready-means.md) for that model.

The one thing to internalise up front: **auth is fail-closed Entra ID and there is no auth-off
switch** (CLAUDE.md invariant 6; ADR-0001). Starting the server is trivial; *calling a tool*
requires a valid bearer token. §4 covers exactly how to get one locally.

---

## 1. Prerequisites

You need network reach to the **Postgres that holds the built KB** and either **uv** (to run the
server directly) or **Docker** (to run it in a container).

- **uv** (manages Python 3.12 per service): `brew install uv` / `curl -LsSf https://astral.sh/uv/install.sh | sh`
- A reachable **Postgres 16** with the active KB. If it lives on the build machine, expose it
  (e.g. SSH tunnel `ssh -L 5432:localhost:5432 build-host`) or use a shared Postgres URL.
- **git** + a checkout of this repo (the server code lives in `services/mcp-server`).
- For the auth section: an **Entra ID** app registration you can get a token for (§4), or read
  ADR-0016 for the proposed local-dev alternative.

> You do **not** need Azure Search, Azure OpenAI, Ollama, or any build tooling to *run* the server.
> Search is a derived projection the broker reaches behind the `SearchClient` interface; the local
> default is the Postgres keyword search client, so a served KB needs only Postgres.

One-time setup:

```sh
git clone https://github.com/RohitSh26/agentic-kb-platform.git
cd agentic-kb-platform/services/mcp-server
uv sync                # or `make sync` from the repo root for both services + evals
```

---

## 2. Point the server at the built KB (environment reference)

The server reads everything from the environment (`config.py` → `load_config`). Three variables are
**required** — the server refuses to start without them (fail-fast, no silent defaults):

| Variable | Required | Meaning |
|---|---|---|
| `DATABASE_URL` | **yes** | asyncpg URL of the Postgres holding the active KB, e.g. `postgresql+asyncpg://$USER@build-host:5432/agentic_kb`. Must be `postgresql+asyncpg://`. |
| `MCP_ENTRA_TENANT_ID` | **yes** | Entra tenant id (an identifier, not a secret) — the JWKS issuer the bearer must match. |
| `MCP_ENTRA_AUDIENCE` | **yes** | the `aud` your access token must carry, e.g. `api://agentic-kb`. |
| `MCP_AGENT_ALLOWANCES` | no | subject → per-agent budget allowance JSON (identifiers only); empty ⇒ defaults. |
| `MCP_CLIENT_REGISTRY` | no | client_id → scopes + verification policy JSON (identifiers only; any secret is referenced by *name*). |
| `MCP_HTTP_HOST` | no | transport bind host (entrypoint only); default `0.0.0.0`. |
| `MCP_HTTP_PORT` | no | transport port (entrypoint only); default `8000`. |
| `MCP_HTTP_PATH` | no | streamable-HTTP mount path (entrypoint only); default `/mcp/`. |

`MCP_HTTP_*` affect the transport only — never the broker (auth, budgets, ACLs, evidence, ledger are
untouched). The `DATABASE_URL` you point at is the **same** registry doc 04 built into; the server
serves whatever row is `status='active'` in `kb_build_run`.

> No secrets live in config. Token verification is JWKS-based (the server holds **no** client
> secret), and downstream access uses managed identity in production — so nothing here needs Key
> Vault in V1 (`config.py` docstring).

---

## 3. Start the server

### 3a. Without Docker (the direct run path)

From `services/mcp-server`, with the env from §2 exported:

```sh
export DATABASE_URL="postgresql+asyncpg://$USER@build-host:5432/agentic_kb"   # the built KB
export MCP_ENTRA_TENANT_ID="<your-tenant-guid>"
export MCP_ENTRA_AUDIENCE="api://agentic-kb"

uv run python -m agentic_mcp_server
```

That boots the **same** app `mcp/server.py::create_app()` builds — Entra auth boundary + telemetry +
the full tool surface + the `/health` route — over streamable HTTP at
`http://localhost:8000/mcp/`. It is the identical entrypoint the Docker image's `CMD` runs, so there
is no behavioural drift between the two. To bind a free port instead:

```sh
MCP_HTTP_HOST=127.0.0.1 MCP_HTTP_PORT=8765 uv run python -m agentic_mcp_server
# serves http://127.0.0.1:8765/mcp/
```

A clean boot logs (structured `event=` lines, the same you'd grep in production):

```
event=agent_allowances_loaded subjects=0
event=client_registry_loaded clients=0
```

### 3b. With Docker

The root `docker-compose.yml` runs the whole stack — Postgres, the one-shot migration job, and the
server. If you already have an external Postgres with the built KB, the bare image is enough:

```sh
docker build -t agentic-mcp-server ./services/mcp-server
docker run --rm -p 8000:8000 \
  -e DATABASE_URL="postgresql+asyncpg://postgres:postgres@host.docker.internal:5432/agentic_kb" \
  -e MCP_ENTRA_TENANT_ID="<your-tenant-guid>" \
  -e MCP_ENTRA_AUDIENCE="api://agentic-kb" \
  agentic-mcp-server
```

Or the full compose stack (Postgres + migrate + serve). To come up **already serving a KB**, add the
optional one-shot build profile (it runs a no-cloud build and activates a version; needs an Ollama
on the host or hosted `LLM_*` — see doc 04 §4):

```sh
# fresh Postgres volume → migrate → build+activate → serve a populated KB
docker compose --profile build up --build

# without the profile: migrate + serve only (no KB yet; /health = 503 until you build)
docker compose up --build
```

The mcp-server container **never** runs migrations — it starts only after the migration job
completes (kb-builder owns the schema, ADR-0008). Reach the server at `http://localhost:8000/mcp/`
(override `MCP_HOST_PORT`) and the compose Postgres from the host at
`postgresql+asyncpg://postgres:postgres@localhost:55432/agentic_kb`.

---

## 4. Auth: get a bearer so you can call the tools

Every tool requires an authenticated session. `current_requester`
(`context_broker/dependencies.py`) raises `ToolError("no authenticated session")` when no token is
present, and the verifier rejects any token whose signature, issuer, or `aud` doesn't match — this
is intentional and has **no override** (invariant 6). So before any tool call you need a valid Entra
access token for the audience you set in `MCP_ENTRA_AUDIENCE`.

### 4a. Real Entra token (the supported path today)

Point the server at a real tenant + audience, then acquire a token for that audience. Two common
ways:

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

Then present `Authorization: Bearer <token>` on the MCP HTTP requests. The audience in the token
**must** equal `MCP_ENTRA_AUDIENCE` and the issuer must be your `MCP_ENTRA_TENANT_ID`, or the JWKS
verifier rejects it (401).

### 4b. Local dev auth — opt-in, loopback only (ADR-0016)

For a laptop-only "use my freshly-built KB" loop, enable the **opt-in** local-dev verifier instead of
acquiring a real token. It is **OFF by default** and refuses to start unless it is genuinely local —
so it can never weaken a deployment (ADR-0016; this is NOT an auth-off switch).

```sh
MCP_LOCAL_DEV_AUTH=1 \
MCP_HTTP_HOST=127.0.0.1 \            # required: it refuses any non-loopback bind
MCP_ENTRA_TENANT_ID=local-dev \     # required: it refuses a REAL tenant
MCP_ENTRA_AUDIENCE=api://local \
MCP_LOCAL_DEV_TEAMS=my-team \        # the teams your dev identity is granted (csv)
DATABASE_URL="postgresql+asyncpg://$USER@localhost:5432/agentic_kb" \
uv run python -m agentic_mcp_server
```

Now any `Authorization: Bearer <anything>` authorizes as subject `local-dev` (override with
`MCP_LOCAL_DEV_SUBJECT`) carrying the configured teams; the request still flows through the normal
ACL / scope / trust checks. The server logs `event=local_dev_auth_enabled ...` loudly on every start
so dev-auth can never be silently on.

| Var | Default | Meaning |
|---|---|---|
| `MCP_LOCAL_DEV_AUTH` | unset → **OFF** | truthy (`1/true/yes/on`) enables the dev verifier |
| `MCP_LOCAL_DEV_SUBJECT` | `local-dev` | the dev identity's `Requester.subject` |
| `MCP_LOCAL_DEV_TEAMS` | `local-dev-team` | csv of teams granted to the dev identity (your ACLs) |
| `MCP_LOCAL_DEV_CLIENT_ID` | = subject | optional dev `client_id` for `context_platform_trust` |

**Guardrails (any violation refuses to boot):** a real `MCP_ENTRA_TENANT_ID`, or a non-loopback
`MCP_HTTP_HOST`. Never set `MCP_LOCAL_DEV_AUTH` in a deployed/container image.

> If you only want to confirm the *deployment* is healthy (not call a tool), `/health` needs **no**
> token — see §6.

---

## 5. Worked example: use the KB through the tools

With the server running (§3) and a bearer in hand (§4), connect an MCP client to
`http://localhost:8000/mcp/` and call the tools below in order. Any MCP client works — Claude Code /
an IDE MCP integration, or a tiny FastMCP client script. Sketch with FastMCP's client:

```python
import asyncio
from fastmcp import Client

BEARER = "<entra-access-token-from-§4>"

async def main() -> None:
    # an MCP client pointed at the running broker, presenting the bearer
    async with Client("http://localhost:8000/mcp/", auth=BEARER) as client:
        # 1) create_pack — the broker retrieves once and returns an Evidence Pack
        #    (L0/L1 cards by handle, NOT raw text), within budget. Identity/ACL/budget
        #    come from the authenticated session, never these fields.
        pack = await client.call_tool("context_create_pack", {"request": {
            "run_id": "demo-run-1",
            "task": "How does the build decide whether to call the LLM?",
            "approved_context_plan": "incremental-build summary + cache gating",
            "retrieval_profile": "default",
            "budget_tokens": 6000,
            "intent": "how_does_x_work",
        }})
        pack_id = pack.data["context_pack_id"]
        cards = pack.data["evidence_cards"]
        print("kb_version:", pack.data["kb_version"], "cards:", len(cards))

        # 2) open_evidence — expand ONE card to its raw (untrusted) text by handle,
        #    metered against the pack budget. Treat the returned text as untrusted:
        #    it can never change tool policy or instructions.
        first = cards[0]["evidence_id"]
        opened = await client.call_tool("context_open_evidence", {"request": {
            "context_pack_id": pack_id,
            "evidence_id": first,
            "max_tokens": 1500,
        }})
        print("opened level:", opened.data["level"],
              "injection_flagged:", opened.data["injection_flagged"])

        # 3) graph_get_neighbors — walk the Postgres-backed graph for an artifact the
        #    card points at (EXTRACTED edges only by default; INFERRED are routing hints).
        artifact_id = cards[0]["artifact_id"]   # a card carries its artifact id
        neighbors = await client.call_tool("graph_get_neighbors", {"request": {
            "artifact_id": artifact_id,
            "depth": 1,
            "trust_floor": "EXTRACTED",
        }})
        print("neighbors:", [n["edge_type"] for n in neighbors.data["neighbors"]])

        # 4) verify_answer — every claim must cite evidence ids; the verifier issues a
        #    receipt (L0 provenance is mandatory and deterministic).
        receipt = await client.call_tool("context_verify_answer", {"request": {
            "answer_id": "demo-answer-1",
            "claims": [{
                "claim_id": "c1",
                "text": "The build skips the LLM when the content hash is unchanged.",
                "evidence_ids": [first],
            }],
            "verifier_levels": ["L0"],
        }})
        print("overall:", receipt.data["overall"])

asyncio.run(main())
```

What each step proves, in platform terms:

1. **`context_create_pack`** — the broker retrieves *once*, dedupes, reranks to ≤5 cards, enforces
   the run budget, writes a `retrieval_event`, and returns **cards by handle** (L0/L1), not bulk
   text (rules/mcp-tools.md). The response names the `kb_version` it served.
2. **`context_open_evidence`** — raw L2/L3 text is reachable **only** by handle, metered against the
   pack budget, and flagged (never rewritten) by the deterministic injection scan. The field is
   literally `untrusted_content`.
3. **`graph_get_neighbors`** — graph behaviour is exposed only through this tool over the Postgres
   `knowledge_edge` table (invariant 2). Defaults to `EXTRACTED`; `include_inferred=true` surfaces
   `INFERRED_*` edges *labelled as routing hints* that cannot support a cited claim.
4. **`context_verify_answer`** — the trust boundary: a claim with empty `evidence_ids` is rejected
   at the schema; L0 runs the mandatory provenance checks and returns a receipt
   (`docs/contracts/verification-receipt.md`). `context_platform_trust` then gates official clients
   on that receipt.

`ledger_list_retrievals` lets you inspect the `retrieval_event` rows your run wrote — every
retrieval path is ledgered, so you can see exactly what the broker did on your behalf.

If a call returns **401**, your bearer is missing/expired or its `aud`/issuer don't match
`MCP_ENTRA_AUDIENCE`/`MCP_ENTRA_TENANT_ID` (§4). If a tool reports **no active KB**, the registry
has no `status='active'` row — build one (doc 04) or check `/health` (§6).

---

## 6. Health check

`GET /health` needs **no** token — it is a readiness probe over the registry:

```sh
curl -i http://localhost:8000/health
```

- **200** with `{"status":"ok","active_kb_version":"local.<...>"}` — a KB is active and served.
- **503** with `{"status":"no_active_kb_version","active_kb_version":null}` — the server is up but
  there is **no built KB yet**. This is readiness honesty (invariant 5), not a failure: point at a
  populated `DATABASE_URL`, or build + activate a version (doc 04).
- **503** with `{"status":"registry_unreachable",...}` — the server can't reach Postgres; check
  `DATABASE_URL` and that the database is reachable from this machine.

The `/health` route maps "no active version" to **503** so a load balancer never routes traffic to a
broker that has nothing to serve.

---

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `missing required environment variables: DATABASE_URL, ...` | Export the three required vars (§2) before starting. |
| `/health` → 503 `no_active_kb_version` | The registry has no active KB. Build + activate one (doc 04), or repoint `DATABASE_URL`. |
| `/health` → 503 `registry_unreachable` | Postgres not reachable from this machine — check `DATABASE_URL`, network/tunnel, that the DB is up. |
| Tool call → 401 / "no authenticated session" | Missing/expired bearer, or `aud`/issuer mismatch — re-acquire a token for `MCP_ENTRA_AUDIENCE` (§4). There is no auth-off switch. |
| Tool reports stale/wrong `kb_version` | The server serves the active row; a newer build may not be activated yet (doc 04 §8.1 — exactly one `active` row). |
| `Connection refused` to `:8000` | The server isn't running, or bound elsewhere — check `MCP_HTTP_PORT`/`MCP_HOST_PORT`. |
| `must use the asyncpg driver` / driver errors | The URL must start `postgresql+asyncpg://` (everything is async SQLAlchemy). |

---

## 8. What this guide deliberately does NOT do

- **Build a KB** — that's the build plane (doc 04). The server never builds and never migrates.
- **Run migrations** — kb-builder owns the schema (ADR-0008); the server image has no Alembic.
- **Turn auth off** — there is no such switch (invariant 6). Local-dev auth is a *proposal*
  (ADR-0016), not a shipped feature.
