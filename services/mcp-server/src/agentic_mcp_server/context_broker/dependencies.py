"""Broker settings, dependency container, and session identity.

Identity always comes from the authenticated MCP session (Entra ID subject +
team claims), never from request fields like agent_name or role — those are
correlation and view selectors only.
"""

from dataclasses import dataclass, field

from fastmcp.server.dependencies import get_access_token
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentic_mcp_server.auth.rbac import Requester, TeamAclAuthorization, teams_from_claims
from agentic_mcp_server.context_broker.authorization import AuthorizationPolicy
from agentic_mcp_server.context_broker.budgets import BudgetPolicy
from agentic_mcp_server.context_broker.state import PackStore
from agentic_mcp_server.infrastructure.search.search_client import SearchClient


@dataclass(frozen=True)
class BrokerSettings:
    # semantic duplicate threshold: start 0.88-0.92, tune from ledger logs
    semantic_reuse_threshold: float = 0.90
    # 3-5 cards max per retrieval after rerank (token-budgets rule)
    max_cards_per_retrieval: int = 5
    # safety cap on graph traversal fan-out at depth 3
    max_graph_neighbors: int = 100


@dataclass(frozen=True)
class BrokerDeps:
    session_factory: async_sessionmaker[AsyncSession]
    search_client: SearchClient
    settings: BrokerSettings = field(default_factory=BrokerSettings)
    budget_policy: BudgetPolicy = field(default_factory=BudgetPolicy)
    authorization: AuthorizationPolicy = field(default_factory=TeamAclAuthorization)
    packs: PackStore = field(default_factory=PackStore)


def current_requester() -> Requester:
    token = get_access_token()
    if token is None:
        return Requester(subject="unauthenticated", teams=frozenset())
    subject = token.subject or token.client_id or "unknown"
    return Requester(subject=subject, teams=teams_from_claims(token.claims or {}))
