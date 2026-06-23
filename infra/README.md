# Infra

Lean V1 Azure footprint (Bicep or Terraform): App Service or Container Apps (MCP server), PostgreSQL
Flexible Server (registry), Azure AI Search (one derived index), Azure OpenAI endpoint, GitHub Actions
nightly build, Application Insights/Azure Monitor, Managed Identity + Key Vault.

Excluded in V1: Functions, Event Grid, Service Bus/Event Hub, Redis, API Management, Blob,
graph DB. Prefer managed identity over Key Vault secrets wherever supported.

## Identity and secrets

No IaC is authored yet (an ADR proposes it when needed); this matrix is the binding contract for
whoever provisions the footprint.

### Managed-identity matrix

| Workload | Identity | Resource | Role / access |
|---|---|---|---|
| mcp-server (App Service / Container Apps) | system-assigned MI | PostgreSQL Flexible Server | Entra-authenticated DB role: SELECT on registry tables + INSERT on `retrieval_event` only — **no DDL, no Search, no OpenAI** |
| kb-builder (GitHub Actions nightly) | OIDC-federated MI | PostgreSQL Flexible Server | Owner DB role (runs Alembic migrations + all build-plane writes) |
| kb-builder | OIDC-federated MI | Azure AI Search | `Search Index Data Contributor` (rebuilds the derived index) |
| kb-builder | OIDC-federated MI | Azure OpenAI | `Cognitive Services OpenAI User` (generation + embeddings, cache-gated) |
| both | MI | Application Insights | telemetry ingestion |

The mcp-server identity deliberately has **no** Search or OpenAI access: the broker makes no model
or embedding calls in V1 and reads relevance through its `SearchClient` interface (Postgres keyword
implementation locally). An agent compromise behind the MCP boundary therefore cannot reach
Search/OpenAI keys — there are none on that surface.

### Key Vault (residual secrets only)

Everything above uses managed identity, so Key Vault holds only credentials for systems that cannot
federate with Entra:

| Secret | Consumer | Notes |
|---|---|---|
| GitHub PAT (source connectors) | kb-builder | read-only repo scope; rotate quarterly |
| Azure DevOps PAT (wiki/ADO card connectors) | kb-builder | read-only scope; rotate quarterly |

Access via Key Vault references / `DefaultAzureCredential` — never env-baked into images, never in
code, fixtures, or logs. Local development needs none of these: tests run against local Postgres
and the fake/keyword search client (no Azure required).

### Token claims

Agent callers authenticate to mcp-server with Entra ID bearer tokens. The broker's `team_acl_v1`
policy reads team membership from the token's `groups`/`roles` claims, so the app registration must
enable `groupMembershipClaims` (or app roles) — until it does, tokens carry no teams and only
org-public (empty `acl_teams`) artifacts are visible to everyone.
