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
    )
