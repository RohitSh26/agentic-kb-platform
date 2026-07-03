"""Client-scope gating of the tool surface (PR-32, ADR-0011 §6).

Scopes are an ADDITIONAL layer of authorization carried by the registered client —
never a replacement for the user team ACL. The gate is opt-in: unregistered clients
are never scope-gated; registered clients are held to their grants.
"""

from agentic_mcp_server.auth.client_identity import ClientIdentity
from agentic_mcp_server.auth.scopes import (
    SCOPE_CONTEXT_READ,
    SCOPE_VERIFY,
    TOOL_REQUIRED_SCOPES,
    client_may_call,
)


def test_unregistered_client_is_never_scope_gated() -> None:
    client = ClientIdentity(client_id="anon")
    for tool in TOOL_REQUIRED_SCOPES:
        assert client_may_call(client, tool) is True


def test_registered_client_needs_the_tool_scope() -> None:
    reader = ClientIdentity(
        client_id="reader", scopes=frozenset({SCOPE_CONTEXT_READ}), registered=True
    )
    assert client_may_call(reader, "context.create_pack") is True
    assert client_may_call(reader, "context.open_evidence") is True
    # kb_search is the ADR-0025 read path: the context.read grant covers it.
    assert client_may_call(reader, "kb_search") is True
    # Lacks the verify + graph + ledger scopes.
    assert client_may_call(reader, "context.verify_answer") is False
    assert client_may_call(reader, "graph.get_neighbors") is False
    assert client_may_call(reader, "ledger.list_retrievals") is False


def test_verify_scope_gates_both_verify_and_platform_trust() -> None:
    verifier = ClientIdentity(
        client_id="verifier", scopes=frozenset({SCOPE_VERIFY}), registered=True
    )
    assert client_may_call(verifier, "context.verify_answer") is True
    assert client_may_call(verifier, "context.platform_trust") is True
    assert client_may_call(verifier, "context.create_pack") is False


def test_registered_client_with_no_scopes_is_denied_guarded_tools() -> None:
    client = ClientIdentity(client_id="empty", scopes=frozenset(), registered=True)
    assert client_may_call(client, "context.create_pack") is False
    assert client_may_call(client, "kb_search") is False
