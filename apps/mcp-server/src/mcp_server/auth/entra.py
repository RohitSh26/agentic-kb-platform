"""Entra ID bearer-token verification — the only module that knows Entra specifics.

Verification is JWKS-based: signing keys come from the tenant's public
discovery endpoint, so the server holds no client secret at all. The seam for
tests is fastmcp's TokenVerifier base class; test suites inject their own
verifier into build_server instead of this one.
"""

from fastmcp.server.auth.providers.jwt import JWTVerifier

from mcp_server.config import ServerConfig


def build_entra_verifier(config: ServerConfig) -> JWTVerifier:
    tenant = config.entra_tenant_id
    return JWTVerifier(
        jwks_uri=f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys",
        issuer=f"https://login.microsoftonline.com/{tenant}/v2.0",
        audience=config.entra_audience,
    )
