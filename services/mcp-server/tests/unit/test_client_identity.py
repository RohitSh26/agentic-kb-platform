"""Client/app identity registry parsing + resolution (PR-32, ADR-0011 §6).

Pure, config-only coverage: the registry maps an authenticated client_id to its
scopes + verification policy, fails the boot on malformed config, and NEVER carries
a secret value (a secret is referenced by env/Key Vault NAME via ``secret_env`` only).
"""

import json

import pytest

from agentic_mcp_server.auth.client_identity import (
    ClientRegistry,
    parse_client_registry,
)


def test_unset_registry_is_empty_and_resolves_unregistered() -> None:
    for raw in (None, "", "   "):
        registry = parse_client_registry(raw)
        assert registry.policies == {}
        identity = registry.resolve("any-client")
        assert identity.client_id == "any-client"
        assert identity.scopes == frozenset()
        assert identity.verification_required is False
        assert identity.registered is False


def test_registered_client_resolves_scopes_and_policy() -> None:
    raw = json.dumps(
        {
            "official-copilot": {
                "scopes": ["context.read", "context.verify"],
                "verification_required": True,
            }
        }
    )
    registry = parse_client_registry(raw)
    identity = registry.resolve("official-copilot")
    assert identity.registered is True
    assert identity.verification_required is True
    assert identity.has_scope("context.read")
    assert identity.has_scope("context.verify")
    assert not identity.has_scope("graph.read")


def test_absent_client_is_never_verification_required() -> None:
    raw = json.dumps({"official-copilot": {"verification_required": True}})
    registry = parse_client_registry(raw)
    # A client NOT in the registry is unaffected — verification is never mandatory.
    other = registry.resolve("some-other-client")
    assert other.verification_required is False
    assert other.registered is False


def test_secret_env_names_a_reference_not_a_value() -> None:
    raw = json.dumps(
        {"official-copilot": {"scopes": ["context.verify"], "secret_env": "COPILOT_SECRET"}}
    )
    # secret_env is a NAME only; parsing must accept it and never read its value.
    registry = parse_client_registry(raw)
    assert registry.resolve("official-copilot").has_scope("context.verify")


@pytest.mark.parametrize(
    "forbidden_key", ["secret", "client_secret", "key", "password", "credential"]
)
def test_secret_valued_keys_fail_the_boot(forbidden_key: str) -> None:
    raw = json.dumps({"c1": {forbidden_key: "super-secret-value"}})
    with pytest.raises(RuntimeError, match="must not carry secret values"):
        parse_client_registry(raw)


def test_malformed_config_fails_fast() -> None:
    with pytest.raises(RuntimeError, match="not valid JSON"):
        parse_client_registry("{not json")
    with pytest.raises(RuntimeError, match="JSON object"):
        parse_client_registry("[]")
    with pytest.raises(RuntimeError, match="unknown keys"):
        parse_client_registry(json.dumps({"c1": {"bogus": 1}}))
    with pytest.raises(RuntimeError, match="scopes must be"):
        parse_client_registry(json.dumps({"c1": {"scopes": "context.read"}}))
    with pytest.raises(RuntimeError, match="verification_required must be a boolean"):
        parse_client_registry(json.dumps({"c1": {"verification_required": "yes"}}))
    with pytest.raises(RuntimeError, match="empty or padded"):
        parse_client_registry(json.dumps({" c1 ": {}}))
    with pytest.raises(RuntimeError, match="duplicate key"):
        parse_client_registry('{"c1": {}, "c1": {}}')


def test_default_registry_is_empty() -> None:
    assert ClientRegistry().resolve("x").registered is False
