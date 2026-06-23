"""Verifier selection + guardrails for local-dev auth.

``select_verifier`` is the single seam that decides whether a deployment runs the
production Entra verifier (default) or the opt-in ``LocalDevTokenVerifier``. It is
the ONLY place the local-dev flag is honoured, so production wiring stays a
straight call to ``build_entra_verifier`` whenever ``MCP_LOCAL_DEV_AUTH`` is unset.

When the flag IS set the dev verifier may only be selected if every guardrail
holds; any violation raises ``LocalDevAuthRefused`` to fail the boot fast rather
than silently weaken auth. Selecting the dev verifier logs a LOUD warning so it
can never be silently active.
"""

import logging

from fastmcp.server.auth import AuthProvider

from agentic_mcp_server.auth.entra import build_entra_verifier
from agentic_mcp_server.auth.local_dev import (
    LocalDevIdentity,
    LocalDevTokenVerifier,
    is_loopback_host,
    is_real_tenant,
)
from agentic_mcp_server.config import ServerConfig

logger = logging.getLogger(__name__)


class LocalDevAuthRefused(RuntimeError):
    """Raised when MCP_LOCAL_DEV_AUTH is set but a guardrail forbids dev-auth."""


def _parse_teams(raw: str) -> tuple[str, ...]:
    return tuple(team.strip() for team in raw.split(",") if team.strip())


def build_local_dev_identity(config: ServerConfig) -> LocalDevIdentity:
    """Build the fixed dev identity from config (subject, teams csv, client_id)."""
    subject = config.local_dev_subject.strip() or "local-dev"
    teams = _parse_teams(config.local_dev_teams)
    client_id = (config.local_dev_client_id or subject).strip() or subject
    return LocalDevIdentity(subject=subject, teams=teams, client_id=client_id)


def _assert_guardrails(config: ServerConfig) -> None:
    """Fail fast if dev-auth is requested next to a real tenant or a public bind."""
    if is_real_tenant(config.entra_tenant_id):
        raise LocalDevAuthRefused(
            "MCP_LOCAL_DEV_AUTH is set but MCP_ENTRA_TENANT_ID names a real Entra "
            "tenant; refusing to run local-dev auth next to a real tenant. Unset "
            "MCP_LOCAL_DEV_AUTH for the production Entra path, or point "
            "MCP_ENTRA_TENANT_ID at a placeholder for local development."
        )
    if not is_loopback_host(config.http_host):
        raise LocalDevAuthRefused(
            f"MCP_LOCAL_DEV_AUTH is set but the server binds to non-loopback host "
            f"{config.http_host!r}; refusing to expose local-dev auth on a public "
            f"bind. Set MCP_HTTP_HOST to a loopback host (e.g. 127.0.0.1) for local "
            f"development."
        )


def select_verifier(config: ServerConfig) -> AuthProvider:
    """Return the AuthProvider to wire: Entra by default, dev-auth only when opted in.

    Production (``MCP_LOCAL_DEV_AUTH`` unset) is byte-for-byte unchanged: a plain
    ``build_entra_verifier(config)``. When the flag is set the guardrails are
    enforced first (raising ``LocalDevAuthRefused`` on any violation), then the
    dev verifier is returned with a LOUD warning identifying the dev identity. No
    token value is ever logged.
    """
    if not config.local_dev_auth:
        return build_entra_verifier(config)

    _assert_guardrails(config)
    identity = build_local_dev_identity(config)
    logger.warning(
        "event=local_dev_auth_enabled msg=%r subject=%s teams=%s client_id=%s host=%s",
        "LOCAL DEV AUTH ACTIVE — bearer verification is bypassed; never use in production",
        identity.subject,
        ",".join(identity.teams),
        identity.client_id,
        config.http_host,
    )
    return LocalDevTokenVerifier(identity)
