"""Server configuration, read from the environment.

Only identifiers live here (tenant id, audience, database URL). There are no
secrets: token verification is JWKS-based and downstream access uses managed
identity, so nothing in config ever needs Key Vault in V1.
"""

import os
from dataclasses import dataclass

SERVER_NAME = "agentic-kb-context-broker"

#: Default bind host (matches the container entrypoint). The local-dev auth
#: guardrail compares the *effective* bind host against this to refuse a public
#: bind; deployments that change the bind set ``MCP_HTTP_HOST`` to match.
DEFAULT_HTTP_HOST = "0.0.0.0"  # documentary default; no bind happens in config

#: Local-dev auth (ADR-0016) defaults. All OFF/empty unless explicitly opted in.
DEFAULT_LOCAL_DEV_SUBJECT = "local-dev"
DEFAULT_LOCAL_DEV_TEAMS = "local-dev-team"


@dataclass(frozen=True)
class ServerConfig:
    database_url: str
    entra_tenant_id: str
    entra_audience: str
    # raw MCP_AGENT_ALLOWANCES value (subject -> allowance JSON); optional —
    # parsed fail-fast by context_broker.budgets.parse_agent_allowances
    agent_allowances_json: str | None = None
    # raw MCP_CLIENT_REGISTRY value (client_id -> scopes + verification policy JSON);
    # optional — parsed fail-fast by auth.client_identity.parse_client_registry.
    # Identifiers + policy only; any client secret is referenced by env/Key Vault NAME
    # (a 'secret_env' field), never a value (PR-32).
    client_registry_json: str | None = None
    # Local-dev auth (ADR-0016): OFF by default. When true a developer-only verifier
    # mints a fixed identity locally instead of the Entra path — gated by guardrails
    # in auth.local_dev_selection. Production leaves MCP_LOCAL_DEV_AUTH unset.
    local_dev_auth: bool = False
    local_dev_subject: str = DEFAULT_LOCAL_DEV_SUBJECT
    local_dev_teams: str = DEFAULT_LOCAL_DEV_TEAMS
    local_dev_client_id: str | None = None
    # Effective bind host (MCP_HTTP_HOST), surfaced so the local-dev guardrail can
    # refuse a public bind. Identifiers only; never a bind happens here.
    http_host: str = DEFAULT_HTTP_HOST


#: Values that count as "true" for a boolean env flag (case-insensitive).
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def load_config() -> ServerConfig:
    missing = [
        name
        for name in ("DATABASE_URL", "MCP_ENTRA_TENANT_ID", "MCP_ENTRA_AUDIENCE")
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError(f"missing required environment variables: {', '.join(missing)}")
    local_dev_client_id = os.environ.get("MCP_LOCAL_DEV_CLIENT_ID")
    return ServerConfig(
        database_url=os.environ["DATABASE_URL"],
        entra_tenant_id=os.environ["MCP_ENTRA_TENANT_ID"],
        entra_audience=os.environ["MCP_ENTRA_AUDIENCE"],
        agent_allowances_json=os.environ.get("MCP_AGENT_ALLOWANCES"),
        client_registry_json=os.environ.get("MCP_CLIENT_REGISTRY"),
        local_dev_auth=env_flag("MCP_LOCAL_DEV_AUTH"),
        local_dev_subject=os.environ.get("MCP_LOCAL_DEV_SUBJECT", DEFAULT_LOCAL_DEV_SUBJECT)
        or DEFAULT_LOCAL_DEV_SUBJECT,
        local_dev_teams=os.environ.get("MCP_LOCAL_DEV_TEAMS", DEFAULT_LOCAL_DEV_TEAMS),
        local_dev_client_id=local_dev_client_id.strip() if local_dev_client_id else None,
        http_host=os.environ.get("MCP_HTTP_HOST", DEFAULT_HTTP_HOST).strip() or DEFAULT_HTTP_HOST,
    )
