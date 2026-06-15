"""load_config parsing of the local-dev auth env vars (ADR-0016).

Defaults: dev-auth OFF, dev subject/teams have sane defaults, http_host defaults
to the container bind. Hermetic — only env is read.
"""

import pytest

from agentic_mcp_server.config import (
    DEFAULT_HTTP_HOST,
    DEFAULT_LOCAL_DEV_SUBJECT,
    DEFAULT_LOCAL_DEV_TEAMS,
    load_config,
)

_REQUIRED = {
    "DATABASE_URL": "postgresql+asyncpg://unused@localhost/unused",
    "MCP_ENTRA_TENANT_ID": "real-tenant",
    "MCP_ENTRA_AUDIENCE": "api://unused",
}


@pytest.fixture(autouse=True)
def _clear_dev_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _REQUIRED:
        monkeypatch.setenv(name, _REQUIRED[name])
    for name in (
        "MCP_LOCAL_DEV_AUTH",
        "MCP_LOCAL_DEV_SUBJECT",
        "MCP_LOCAL_DEV_TEAMS",
        "MCP_LOCAL_DEV_CLIENT_ID",
        "MCP_HTTP_HOST",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults_when_unset() -> None:
    config = load_config()
    assert config.local_dev_auth is False
    assert config.local_dev_subject == DEFAULT_LOCAL_DEV_SUBJECT
    assert config.local_dev_teams == DEFAULT_LOCAL_DEV_TEAMS
    assert config.local_dev_client_id is None
    assert config.http_host == DEFAULT_HTTP_HOST


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_flag_truthy_values(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_LOCAL_DEV_AUTH", value)
    assert load_config().local_dev_auth is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  "])
def test_flag_falsy_values(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_LOCAL_DEV_AUTH", value)
    assert load_config().local_dev_auth is False


def test_overrides_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_LOCAL_DEV_AUTH", "1")
    monkeypatch.setenv("MCP_LOCAL_DEV_SUBJECT", "alice")
    monkeypatch.setenv("MCP_LOCAL_DEV_TEAMS", "t1, t2")
    monkeypatch.setenv("MCP_LOCAL_DEV_CLIENT_ID", "dev-app")
    monkeypatch.setenv("MCP_HTTP_HOST", "127.0.0.1")
    config = load_config()
    assert config.local_dev_auth is True
    assert config.local_dev_subject == "alice"
    assert config.local_dev_teams == "t1, t2"
    assert config.local_dev_client_id == "dev-app"
    assert config.http_host == "127.0.0.1"
