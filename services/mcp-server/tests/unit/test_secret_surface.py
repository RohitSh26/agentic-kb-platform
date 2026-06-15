"""No-secret-leakage scan: the broker holds identifiers, never credentials.

Token verification is JWKS-based and downstream access is managed identity,
so config must stay free of key/secret/password fields, and source must not
carry credential-looking literals (PR-13 acceptance).
"""

import re
from dataclasses import fields
from pathlib import Path

from agentic_mcp_server.config import ServerConfig

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "agentic_mcp_server"

_FORBIDDEN_FIELD_TOKENS = ("key", "secret", "password", "credential", "token")

_CREDENTIAL_LITERALS = re.compile(
    r"""(?:api[_-]?key|client[_-]?secret|password|sas[_-]?token)\s*[=:]\s*["'][^"']+["']""",
    re.IGNORECASE,
)


def test_server_config_has_no_secret_shaped_fields() -> None:
    names = {field.name for field in fields(ServerConfig)}
    # agent_allowances_json holds subjects + integer allowances; client_registry_json
    # holds client_ids + scopes + verification policy (secrets referenced by env NAME
    # only via 'secret_env'). The local_dev_* fields (ADR-0016) hold a flag + a fixed
    # dev subject/teams/client_id + the bind host: identifiers only, never secret values.
    assert names == {
        "database_url",
        "entra_tenant_id",
        "entra_audience",
        "agent_allowances_json",
        "client_registry_json",
        "local_dev_auth",
        "local_dev_subject",
        "local_dev_teams",
        "local_dev_client_id",
        "http_host",
    }
    for name in names:
        assert not any(token in name for token in _FORBIDDEN_FIELD_TOKENS)


def test_source_tree_contains_no_credential_literals() -> None:
    offenders: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if _CREDENTIAL_LITERALS.search(line):
                offenders.append(f"{path.relative_to(SRC_ROOT)}:{number}: {line.strip()}")
    assert not offenders, "credential-looking literals in source:\n" + "\n".join(offenders)
