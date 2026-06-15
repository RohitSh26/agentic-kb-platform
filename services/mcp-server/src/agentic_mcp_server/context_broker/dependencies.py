"""Broker settings, dependency container, and session identity.

Identity always comes from the authenticated MCP session (Entra ID subject +
team claims), never from request fields like agent_name or role — those are
correlation and view selectors only.
"""

from dataclasses import dataclass, field

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentic_mcp_server.auth.client_identity import ClientIdentity, ClientRegistry
from agentic_mcp_server.auth.rbac import Requester, TeamAclAuthorization, teams_from_claims
from agentic_mcp_server.context_broker.authorization import AuthorizationPolicy
from agentic_mcp_server.context_broker.budgets import BudgetPolicy
from agentic_mcp_server.context_broker.state import PackStore
from agentic_mcp_server.infrastructure.entailment.client import EntailmentClient
from agentic_mcp_server.infrastructure.search.search_client import SearchClient


@dataclass(frozen=True)
class BrokerSettings:
    # semantic duplicate threshold for CROSS-query reuse (request_more): start
    # 0.88-0.92, tune from ledger logs
    semantic_reuse_threshold: float = 0.90
    # semantic duplicate threshold for WITHIN-retrieval dedupe: two candidate
    # cards whose normalized title+summary similarity is >= this collapse to one
    # before the card cap is applied (token-budgets rule: semantic dedupe
    # 0.88-0.92 BEFORE the 3-5 card cap). Same band as the reuse threshold.
    semantic_dupe_threshold: float = 0.90
    # 3-5 cards max per retrieval after rerank (token-budgets rule)
    max_cards_per_retrieval: int = 5
    # safety cap on graph traversal fan-out at depth 3
    max_graph_neighbors: int = 100
    # full run context budget upper bound (token-budgets rule: 12k-18k);
    # the request value is a floor request, never an escape hatch
    max_run_budget_tokens: int = 18_000
    # L1 span cap: max chars a single claim quote may carry. A quote longer than
    # this fails L1 — it is lifting more raw text than a citation should, the same
    # "evidence by handle, not bulk text" principle the broker enforces elsewhere.
    max_quote_chars: int = 600
    # Env var NAME holding the receipt signing key value. The NAME is config; the
    # VALUE is read from env at sign time and never literalised (PR-31). When the
    # var is unset the verifier still issues an (unsigned) receipt.
    signing_key_env: str = "VERIFY_SIGNING_KEY"


@dataclass(frozen=True)
class BrokerDeps:
    session_factory: async_sessionmaker[AsyncSession]
    search_client: SearchClient
    settings: BrokerSettings = field(default_factory=BrokerSettings)
    budget_policy: BudgetPolicy = field(default_factory=BudgetPolicy)
    authorization: AuthorizationPolicy = field(default_factory=TeamAclAuthorization)
    packs: PackStore = field(default_factory=PackStore)
    # L3 verifier (PR-31): the entailment backend. None ⇒ L3 cannot run even if
    # requested (the verifier drops it from verifier_levels_run); a configured
    # client + an "L3" request runs the cached entailment check.
    entailment_client: EntailmentClient | None = None
    # Client/app identity registry (PR-32): authenticated client_id -> scopes +
    # verification_required policy. Default empty ⇒ every client resolves to the
    # unregistered, non-scope-gated, non-verification-required identity (existing
    # behaviour unchanged for deployments that ship no registry).
    client_registry: ClientRegistry = field(default_factory=ClientRegistry)


def current_requester() -> Requester:
    # fail closed: org-public artifacts are "any *authenticated* subject", so a
    # missing token must never synthesize an identity that passes that branch
    token = get_access_token()
    if token is None:
        raise ToolError("no authenticated session")
    # Fail closed: never collapse a subject-less token to a shared sentinel — that
    # would let distinct principals share one identity (and one ACL). invariant 6.
    subject = token.subject or token.client_id
    if not subject:
        raise ToolError("authenticated session carries no subject")
    return Requester(subject=subject, teams=teams_from_claims(token.claims or {}))


def current_client_identity(registry: ClientRegistry) -> ClientIdentity:
    """Resolve the client/app identity for the request from the authenticated token.

    The client is identified by the verified bearer token's ``client_id`` claim — the
    same authenticated session ``current_requester`` reads, never a request-body field.
    The registry maps that client_id to its scopes + verification policy; an absent
    client resolves to the unregistered identity (no scopes, verification not required).
    Fails closed on a missing session, exactly like ``current_requester``.
    """
    token = get_access_token()
    if token is None:
        raise ToolError("no authenticated session")
    # Fail closed: a token with neither client_id nor subject must not resolve to a
    # shared sentinel identity that cross-binds receipts between distinct clients.
    client_id = token.client_id or token.subject
    if not client_id:
        raise ToolError("authenticated session carries no client identity")
    return registry.resolve(client_id)
