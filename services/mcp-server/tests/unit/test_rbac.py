"""team_acl_v1 semantics: empty = org-public, non-empty = team intersection."""

import uuid

import pytest
from fastmcp.exceptions import ToolError

from agentic_mcp_server.auth.rbac import (
    Requester,
    TeamAclAuthorization,
    acl_admits,
    teams_from_claims,
)
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


def test_acl_admits_is_the_single_predicate_behind_filter_artifacts() -> None:
    # MCP-F5: the standalone predicate the verifier and the class both share must
    # agree with filter_artifacts row-for-row, so retrieval filtering and
    # verification visibility cannot drift apart.
    public = artifact()
    restricted = artifact(acl_teams=("team-a",))
    who = requester("team-a")
    assert acl_admits(who, public.acl_teams) is True
    assert acl_admits(requester(), restricted.acl_teams) is False
    assert acl_admits(who, restricted.acl_teams) is True
    # empty/absent ⇒ org-public for a teamless requester
    assert acl_admits(requester(), ()) is True
    assert acl_admits(requester(), None) is True
    for row in (public, restricted):
        admitted_by_class = row in POLICY.filter_artifacts(who, [row])
        assert acl_admits(who, row.acl_teams) is admitted_by_class


def test_teams_come_from_groups_and_roles_claims() -> None:
    claims = {"groups": ["team-a", "team-b"], "roles": ["reviewer"], "sub": "agent"}
    assert teams_from_claims(claims) == frozenset({"team-a", "team-b", "reviewer"})


def test_requester_identity_fails_closed_without_a_session_token() -> None:
    with pytest.raises(ToolError, match="no authenticated session"):
        current_requester()


def test_requester_identity_fails_closed_when_token_has_no_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A token with neither subject nor client_id must not collapse to a shared
    # sentinel identity (it would cross-bind ACLs between principals). invariant 6.
    from fastmcp.server.auth import AccessToken

    from agentic_mcp_server.context_broker import dependencies

    token = AccessToken(token="t", client_id="", scopes=[], subject=None, claims={})
    monkeypatch.setattr(dependencies, "get_access_token", lambda: token)
    with pytest.raises(ToolError, match="no subject"):
        dependencies.current_requester()


def test_client_identity_fails_closed_when_token_has_no_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastmcp.server.auth import AccessToken

    from agentic_mcp_server.auth.client_identity import ClientRegistry
    from agentic_mcp_server.context_broker import dependencies

    token = AccessToken(token="t", client_id="", scopes=[], subject=None, claims={})
    monkeypatch.setattr(dependencies, "get_access_token", lambda: token)
    with pytest.raises(ToolError, match="no client identity"):
        dependencies.current_client_identity(ClientRegistry())


def test_crafted_token_subject_is_sanitized_at_the_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # MCP-F6: the subject is echoed verbatim into key=value structured logs. A token
    # subject is not charset-constrained, so a crafted value with whitespace/newlines/
    # `=` could forge log fields. current_requester must replace every unsafe char with
    # "_" at the single identity chokepoint.
    from fastmcp.server.auth import AccessToken

    from agentic_mcp_server.context_broker import dependencies

    hostile = "evil subject=admin\noverall=passed\tx"
    token = AccessToken(token="t", client_id="c", scopes=[], subject=hostile, claims={})
    monkeypatch.setattr(dependencies, "get_access_token", lambda: token)

    requester_out = dependencies.current_requester()
    assert requester_out.subject == "evil_subject_admin_overall_passed_x"
    # only the safe charset survives
    assert all(c.isalnum() or c in "._@-" for c in requester_out.subject)


def test_safe_subject_passes_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastmcp.server.auth import AccessToken

    from agentic_mcp_server.context_broker import dependencies

    token = AccessToken(
        token="t", client_id="c", scopes=[], subject="impl-agent_v1.user@team", claims={}
    )
    monkeypatch.setattr(dependencies, "get_access_token", lambda: token)
    assert dependencies.current_requester().subject == "impl-agent_v1.user@team"


def test_malformed_claims_grant_no_teams() -> None:
    assert teams_from_claims({}) == frozenset()
    assert teams_from_claims({"groups": "not-a-list"}) == frozenset()
    assert teams_from_claims({"groups": [1, None, {"x": 1}]}) == frozenset()
