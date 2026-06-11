"""team_acl_v1 semantics: empty = org-public, non-empty = team intersection."""

import uuid

import pytest
from fastmcp.exceptions import ToolError

from agentic_mcp_server.auth.rbac import Requester, TeamAclAuthorization, teams_from_claims
from agentic_mcp_server.context_broker.dependencies import current_requester
from agentic_mcp_server.infrastructure.postgres.artifacts import ArtifactRow

POLICY = TeamAclAuthorization()


def artifact(acl_teams: tuple[str, ...] = ()) -> ArtifactRow:
    return ArtifactRow(
        artifact_id=uuid.uuid4(),
        artifact_type="doc_chunk",
        title="t",
        body_text="b",
        knowledge_kind="source_backed",
        authority_score=0.8,
        source_uri="https://example.test/doc",
        acl_teams=acl_teams,
    )


def requester(*teams: str) -> Requester:
    return Requester(subject="agent", teams=frozenset(teams))


def test_empty_acl_is_org_public_even_for_teamless_requesters() -> None:
    rows = [artifact()]
    assert POLICY.filter_artifacts(requester(), rows) == rows


def test_non_empty_acl_requires_team_intersection() -> None:
    restricted = artifact(acl_teams=("team-payments",))
    assert POLICY.filter_artifacts(requester(), [restricted]) == []
    assert POLICY.filter_artifacts(requester("team-search"), [restricted]) == []
    assert POLICY.filter_artifacts(requester("team-payments"), [restricted]) == [restricted]


def test_any_overlapping_team_grants_access() -> None:
    restricted = artifact(acl_teams=("team-a", "team-b"))
    assert POLICY.filter_artifacts(requester("team-b", "team-z"), [restricted]) == [restricted]


def test_filter_preserves_order_and_drops_only_unauthorized() -> None:
    public = artifact()
    restricted = artifact(acl_teams=("team-a",))
    public_two = artifact()
    rows = [public, restricted, public_two]
    assert POLICY.filter_artifacts(requester(), rows) == [public, public_two]


def test_policy_name_is_the_contract_value() -> None:
    assert POLICY.policy_name == "team_acl_v1"


def test_teams_come_from_groups_and_roles_claims() -> None:
    claims = {"groups": ["team-a", "team-b"], "roles": ["reviewer"], "sub": "agent"}
    assert teams_from_claims(claims) == frozenset({"team-a", "team-b", "reviewer"})


def test_requester_identity_fails_closed_without_a_session_token() -> None:
    with pytest.raises(ToolError, match="no authenticated session"):
        current_requester()


def test_malformed_claims_grant_no_teams() -> None:
    assert teams_from_claims({}) == frozenset()
    assert teams_from_claims({"groups": "not-a-list"}) == frozenset()
    assert teams_from_claims({"groups": [1, None, {"x": 1}]}) == frozenset()
