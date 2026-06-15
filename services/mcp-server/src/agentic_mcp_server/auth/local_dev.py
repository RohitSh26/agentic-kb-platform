"""Opt-in, OFF-by-default local-dev auth verifier (ADR-0016).

The production path is fail-closed Microsoft Entra (``auth/entra.py``): every
request carries a JWKS-verified bearer and there is NO auth-off switch
(invariant 6). This module does NOT add one. It adds a strictly opt-in verifier
that — only when a developer explicitly sets ``MCP_LOCAL_DEV_AUTH`` — accepts the
local request and produces a *fixed, configurable* dev identity (subject + teams,
optional client_id). The identity has the SAME shape the Entra verifier yields,
so ``current_requester`` / ``current_client_identity`` resolve it and the request
still flows through the normal ACL / scope / trust authorization path. Nothing
here weakens production: when the flag is unset the Entra path is byte-for-byte
unchanged.

Guardrails (enforced at selection time, fail-fast — see ``select_verifier``):

* refuse if a real Entra tenant is configured (never run dev-auth next to a real
  tenant);
* refuse if the server is bound to a non-loopback host (no public bind);
* log a LOUD ``event=local_dev_auth_enabled`` warning every time it is active.

The verifier accepts ANY presented bearer string when enabled — it is a
developer convenience, not a credential check — so it MUST never reach a
production deployment. The guardrails above make that structurally hard.
"""

import logging
from dataclasses import dataclass

from fastmcp.server.auth import AccessToken, TokenVerifier

logger = logging.getLogger(__name__)

#: Loopback hosts the dev verifier is allowed to bind to. An empty host is the
#: dual-stack loopback some servers use for "localhost only"; anything else
#: (``0.0.0.0``, ``::``, a routable IP/name) is treated as a public bind.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "", "[::1]"})

#: Tenant id placeholders that DO NOT count as "a real Entra tenant" for the
#: guardrail. A dev/test fixture ships one of these so the config still loads;
#: any other value is treated as a real tenant and refuses dev-auth.
_PLACEHOLDER_TENANT_IDS = frozenset(
    {
        "",
        "local-dev",
        "local-dev-tenant",
        "placeholder",
        "placeholder-tenant",
        "unused",
        "00000000-0000-0000-0000-000000000000",
        "common",
        "organizations",
    }
)


@dataclass(frozen=True)
class LocalDevIdentity:
    """The fixed identity the local-dev verifier mints (from config)."""

    subject: str
    teams: tuple[str, ...]
    client_id: str


class LocalDevTokenVerifier(TokenVerifier):
    """Accepts the local request and mints a fixed, configurable dev identity.

    Mirrors the shape ``build_entra_verifier`` yields: the resulting
    ``AccessToken`` carries ``subject`` + a ``client_id`` and puts the dev teams
    under the ``groups`` claim, exactly where ``teams_from_claims`` reads them.
    No real token is verified — any presented bearer string is accepted — so this
    is gated behind ``MCP_LOCAL_DEV_AUTH`` and the startup guardrails. The token
    string itself is NEVER logged.
    """

    def __init__(self, identity: LocalDevIdentity) -> None:
        super().__init__()
        self._identity = identity

    async def verify_token(self, token: str) -> AccessToken | None:
        del token  # any presented bearer is accepted when dev-auth is active
        identity = self._identity
        return AccessToken(
            token="local-dev",  # opaque placeholder; never the presented value
            client_id=identity.client_id,
            scopes=[],
            subject=identity.subject,
            claims={"sub": identity.subject, "groups": list(identity.teams)},
        )


def is_real_tenant(tenant_id: str) -> bool:
    """True when ``tenant_id`` looks like a real Entra tenant (not a placeholder)."""
    return tenant_id.strip().lower() not in _PLACEHOLDER_TENANT_IDS


def is_loopback_host(host: str) -> bool:
    """True when ``host`` is a loopback bind the dev verifier may run on."""
    return host.strip().lower() in _LOOPBACK_HOSTS
