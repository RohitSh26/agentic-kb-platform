"""Server configuration, read from the environment.

Only identifiers live here (tenant id, audience, database URL). There are no
secrets: token verification is JWKS-based and downstream access uses managed
identity, so nothing in config ever needs Key Vault in V1.
"""

import os
from dataclasses import dataclass

SERVER_NAME = "agentic-kb-context-broker"


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


def load_config() -> ServerConfig:
    missing = [
        name
        for name in ("DATABASE_URL", "MCP_ENTRA_TENANT_ID", "MCP_ENTRA_AUDIENCE")
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError(f"missing required environment variables: {', '.join(missing)}")
    return ServerConfig(
        database_url=os.environ["DATABASE_URL"],
        entra_tenant_id=os.environ["MCP_ENTRA_TENANT_ID"],
        entra_audience=os.environ["MCP_ENTRA_AUDIENCE"],
        agent_allowances_json=os.environ.get("MCP_AGENT_ALLOWANCES"),
        client_registry_json=os.environ.get("MCP_CLIENT_REGISTRY"),
    )
