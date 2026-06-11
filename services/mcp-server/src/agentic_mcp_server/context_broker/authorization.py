"""Authorization filter seam for retrieval results.

V1 ships allow-all: ACL metadata on sources/artifacts and real RBAC arrive
with PR-13, which swaps the policy behind this seam without touching the
retrieval path (docs/contracts/mcp-tools-contract.md).
"""

from typing import Protocol

from agentic_mcp_server.infrastructure.postgres.artifacts import ArtifactRow


class AuthorizationPolicy(Protocol):
    def filter_artifacts(self, subject: str, artifacts: list[ArtifactRow]) -> list[ArtifactRow]:
        """Return only the artifacts the authenticated subject may see."""
        ...


class AllowAllAuthorization:
    def filter_artifacts(self, subject: str, artifacts: list[ArtifactRow]) -> list[ArtifactRow]:
        return artifacts
