"""Authorization filter seam for retrieval results.

Every retrieval surface (cards, evidence expansion, graph traversal) passes
its hydrated artifacts through this seam before anything reaches an agent.
The V1 policy is team_acl_v1 (auth/rbac.py); the protocol keeps the policy
swappable without touching the retrieval path
(docs/contracts/mcp-tools-contract.md).
"""

from typing import Protocol

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.infrastructure.postgres.artifacts import ArtifactRow


class AuthorizationPolicy(Protocol):
    policy_name: str

    def filter_artifacts(
        self, requester: Requester, artifacts: list[ArtifactRow]
    ) -> list[ArtifactRow]:
        """Return only the artifacts the authenticated requester may see."""
        ...
