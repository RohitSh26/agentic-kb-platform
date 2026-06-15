"""Local-dev auth verifier + selection guardrails (ADR-0016).

Hermetic: no DB, no network. Covers selection (flag unset ⇒ Entra, flag set ⇒
dev verifier), the fail-fast guardrails (real tenant / non-loopback bind), the
loud startup log, the minted identity shape, and that no token value is logged.
"""

import logging

import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier

from agentic_mcp_server.auth.local_dev import (
    LocalDevIdentity,
    LocalDevTokenVerifier,
    is_loopback_host,
    is_real_tenant,
)
from agentic_mcp_server.auth.local_dev_selection import (
    LocalDevAuthRefused,
    build_local_dev_identity,
    select_verifier,
)
from agentic_mcp_server.auth.rbac import teams_from_claims
from agentic_mcp_server.config import ServerConfig


def _config(**overrides: object) -> ServerConfig:
    base: dict[str, object] = {
        "database_url": "postgresql+asyncpg://unused@localhost/unused",
        "entra_tenant_id": "local-dev",
        "entra_audience": "api://unused",
        "local_dev_auth": True,
        "local_dev_subject": "local-dev",
        "local_dev_teams": "team-a,team-b",
        "local_dev_client_id": None,
        "http_host": "127.0.0.1",
    }
    base.update(overrides)
    return ServerConfig(**base)  # type: ignore[arg-type]


# --- selection -------------------------------------------------------------


def test_flag_unset_selects_entra_path() -> None:
    # Default production posture: no dev verifier, plain Entra JWKS verifier.
    config = _config(local_dev_auth=False, entra_tenant_id="real-tenant-id")
    verifier = select_verifier(config)
    assert isinstance(verifier, JWTVerifier)
    assert not isinstance(verifier, LocalDevTokenVerifier)


def test_flag_set_with_guardrails_ok_selects_dev_verifier() -> None:
    verifier = select_verifier(_config())
    assert isinstance(verifier, LocalDevTokenVerifier)


def test_flag_set_with_real_tenant_refuses() -> None:
    with pytest.raises(LocalDevAuthRefused, match="real Entra tenant"):
        select_verifier(_config(entra_tenant_id="00000000-1111-2222-3333-444444444444"))


def test_flag_set_with_non_loopback_bind_refuses() -> None:
    with pytest.raises(LocalDevAuthRefused, match="non-loopback host"):
        select_verifier(_config(http_host="0.0.0.0"))


def test_flag_set_with_public_hostname_refuses() -> None:
    with pytest.raises(LocalDevAuthRefused, match="non-loopback host"):
        select_verifier(_config(http_host="mcp.internal.example.com"))


# --- loud log + no secret logged -------------------------------------------


def test_enabling_dev_auth_emits_loud_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        select_verifier(_config())
    records = [r for r in caplog.records if "event=local_dev_auth_enabled" in r.getMessage()]
    assert records, "expected a loud event=local_dev_auth_enabled warning"
    assert records[0].levelno == logging.WARNING
    msg = records[0].getMessage()
    assert "local-dev" in msg  # subject surfaced
    assert "team-a" in msg and "team-b" in msg  # teams surfaced


@pytest.mark.asyncio
async def test_no_token_value_is_logged_or_echoed(caplog: pytest.LogCaptureFixture) -> None:
    verifier = LocalDevTokenVerifier(build_local_dev_identity(_config()))
    presented = "super-secret-bearer-value-should-not-leak"
    with caplog.at_level(logging.DEBUG):
        token = await verifier.verify_token(presented)
    assert token is not None
    # The minted token never echoes the presented bearer, and nothing logs it.
    assert token.token != presented
    assert all(presented not in r.getMessage() for r in caplog.records)


# --- minted identity shape -------------------------------------------------


@pytest.mark.asyncio
async def test_minted_identity_resolves_subject_and_teams() -> None:
    identity = build_local_dev_identity(_config())
    verifier = LocalDevTokenVerifier(identity)
    token = await verifier.verify_token("anything")
    assert token is not None
    assert token.subject == "local-dev"
    assert token.client_id == "local-dev"  # defaults to subject
    # teams_from_claims (the production path) reads the dev teams from `groups`.
    assert teams_from_claims(token.claims or {}) == frozenset({"team-a", "team-b"})


@pytest.mark.asyncio
async def test_explicit_client_id_is_honoured() -> None:
    identity = build_local_dev_identity(_config(local_dev_client_id="dev-app"))
    token = await LocalDevTokenVerifier(identity).verify_token("x")
    assert token is not None
    assert token.client_id == "dev-app"
    assert token.subject == "local-dev"


def test_build_identity_defaults() -> None:
    identity = build_local_dev_identity(
        _config(local_dev_subject="local-dev", local_dev_teams="local-dev-team")
    )
    assert identity == LocalDevIdentity(
        subject="local-dev", teams=("local-dev-team",), client_id="local-dev"
    )


# --- helper predicates -----------------------------------------------------


@pytest.mark.parametrize("tenant", ["", "local-dev", "common", "placeholder", "unused"])
def test_placeholder_tenants_are_not_real(tenant: str) -> None:
    assert is_real_tenant(tenant) is False


@pytest.mark.parametrize("tenant", ["real-tenant", "00000000-1111-2222-3333-444444444444"])
def test_real_tenants_are_real(tenant: str) -> None:
    assert is_real_tenant(tenant) is True


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "[::1]", ""])
def test_loopback_hosts(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5", "example.com"])
def test_non_loopback_hosts(host: str) -> None:
    assert is_loopback_host(host) is False
