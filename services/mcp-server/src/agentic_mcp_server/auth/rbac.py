"""Team-ACL retrieval filtering (team_acl_v1).

The requester is the authenticated session subject plus the team set carried
in the bearer token's groups/roles claims — never anything from a request
body. An artifact with an empty acl_teams is org-public (any authenticated
subject); a non-empty acl_teams requires a non-empty intersection with the
requester's teams (docs/contracts/mcp-tools-contract.md).
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, cast

from agentic_mcp_server.infrastructure.postgres.artifacts import ArtifactRow

# Entra ID puts team memberships in `groups` (object ids or names, depending on
# groupMembershipClaims) and app-level roles in `roles`; both grant access
TEAM_CLAIMS = ("groups", "roles")


@dataclass(frozen=True)
class Requester:
    """Authenticated identity: session subject plus team memberships."""

    subject: str
    teams: frozenset[str]


def teams_from_claims(claims: Mapping[str, Any]) -> frozenset[str]:
    teams: set[str] = set()
    for claim in TEAM_CLAIMS:
        value = claims.get(claim)
        if isinstance(value, list):
            teams.update(item for item in cast("list[object]", value) if isinstance(item, str))
    return frozenset(teams)


def acl_admits(requester: Requester, acl_teams: Collection[str] | None) -> bool:
    """team_acl_v1 visibility for one row's ACL: an empty/absent ``acl_teams`` is
    org-public (any authenticated subject); a non-empty one requires a non-empty
    intersection with the requester's teams. The single source of the ACL predicate
    — `TeamAclAuthorization.filter_artifacts` and the verifier both call it, so
    retrieval filtering and verification cannot drift apart."""
    return not acl_teams or bool(requester.teams.intersection(acl_teams))


class TeamAclAuthorization:
    policy_name: str = "team_acl_v1"

    def filter_artifacts(
        self, requester: Requester, artifacts: list[ArtifactRow]
    ) -> list[ArtifactRow]:
        return [artifact for artifact in artifacts if acl_admits(requester, artifact.acl_teams)]
