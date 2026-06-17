"""Client-scope gating for the MCP tool surface (ADR-0011 §6, phase 4).

Scopes are an ADDITIONAL layer of authorization carried by the registered client/app
identity — they NEVER replace the per-user team ACLs (defence in depth). A tool may
declare a required scope; a request from a registered client lacking that scope is
denied before the tool runs. To preserve backwards compatibility (and to never make
scopes mandatory for clients that did not opt in), the gate is OPT-IN per client:

  - An UNREGISTERED client (absent from ``MCP_CLIENT_REGISTRY``) is unaffected — it
    has no scopes and the gate does not apply to it. Existing deployments that ship
    no registry keep their exact behaviour.
  - A REGISTERED client is held to its granted scopes: a scope-guarded tool requires
    the matching grant.

A tool with no entry in ``TOOL_REQUIRED_SCOPES`` requires no scope (open to any
authenticated client). This keeps the platform's default-deny posture on the USER
ACL side while letting a host additionally fence specific clients to specific tools.
"""

from agentic_mcp_server.auth.client_identity import ClientIdentity

#: Scope vocabulary for the V1 tool surface. A grouping by capability, not per-tool,
#: so a host grants e.g. ``context.read`` once for the read path.
SCOPE_CONTEXT_READ = "context.read"
SCOPE_GRAPH_READ = "graph.read"
SCOPE_LEDGER_READ = "ledger.read"
SCOPE_VERIFY = "context.verify"

#: tool name -> required scope. A tool absent here is open to any authenticated
#: client (no scope required). Present here ⇒ a REGISTERED client must hold the scope.
TOOL_REQUIRED_SCOPES: dict[str, str] = {
    "context.create_pack": SCOPE_CONTEXT_READ,
    "context.read_pack": SCOPE_CONTEXT_READ,
    "context.request_more": SCOPE_CONTEXT_READ,
    "context.open_evidence": SCOPE_CONTEXT_READ,
    "context.create_change_pack": SCOPE_CONTEXT_READ,
    "graph.get_neighbors": SCOPE_GRAPH_READ,
    "ledger.list_retrievals": SCOPE_LEDGER_READ,
    "context.verify_answer": SCOPE_VERIFY,
    "context.platform_trust": SCOPE_VERIFY,
}


def client_may_call(client: ClientIdentity, tool_name: str) -> bool:
    """True iff ``client`` is permitted (by scope) to call ``tool_name``.

    An UNREGISTERED client is never scope-gated (opt-in only). A REGISTERED client
    must hold the tool's required scope; a tool with no required scope is always
    permitted. Scope gating is ADDITIONAL to the user ACL filter the tool then
    applies — passing here never widens what the user may see.
    """
    if not client.registered:
        return True
    required = TOOL_REQUIRED_SCOPES.get(tool_name)
    if required is None:
        return True
    return client.has_scope(required)
