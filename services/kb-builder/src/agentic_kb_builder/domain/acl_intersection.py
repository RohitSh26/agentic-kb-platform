"""The never-widen ACL intersection rule (docs/contracts/acl-source-visibility.md).

Shared by every derived artifact whose visibility must be computed from its
inputs' `source_item.acl_teams` — `git_metadata` commits (`application.write_commit`)
and the alias/reference miner (`alias.run`, PR-38) both derive their artifact's
ACL this way. Pure domain logic (no DB, no I/O), so it lives here rather than in
either caller's module — avoids a cross-package import cycle (`alias.run` must
not import `agentic_kb_builder.application.*`, which itself imports `build_runner`,
which wires in the alias miner) and gives every future derived-artifact producer
one place to get this right.

Subtlety — empty acl_teams means org-public (everyone) at READ time (mcp-server
auth/rbac.py: `not artifact.acl_teams or requester.teams & artifact.acl_teams`).
So an org-public input imposes NO constraint on the intersection (it is the
universe of teams), and an EMPTY intersection result can NOT be stored as `[]` —
that would widen to everyone, the exact failure acl-source-visibility.md warns
against. We therefore store an explicit deny-all sentinel (`DENY_ALL_ACL`, a team
no requester holds) for "visible to nobody": disjoint restrictions (no common
team) and unknown provenance (zero resolvable inputs) both deny by default.
"""

from collections.abc import Sequence

# "Visible to nobody". Empty acl_teams means org-public (everyone) at read, so an
# empty intersection can't be stored as []; this sentinel — a team no real
# requester ever holds — denies all without a schema change. The broker's
# read-time edge-ACL intersection inherits it, so a denied artifact's edges are
# hidden too. (Open question: a first-class deny needs a tri-state acl model.)
DENY_ALL_ACL: tuple[str, ...] = ("__no_team__",)


def commit_acl_intersection(
    target_paths: Sequence[str],
    path_acls: dict[str, list[str]],
) -> list[str]:
    """Visibility of a derived artifact = the teams authorised for EVERY input path.

    A path with no entry in `path_acls` contributes nothing (it cannot widen
    visibility). An org-public input (empty acl) is the universe of teams
    (rbac.py), so it imposes no constraint — only non-empty ACLs narrow.

    Results:
    - no constraining input (every resolved input org-public) => [] (org-public);
    - a non-empty intersection => that team set;
    - disjoint restrictions (constraints exist but share no team) => DENY_ALL_ACL;
    - zero resolvable inputs (unknown provenance) => DENY_ALL_ACL (deny by default).
    [] is NEVER used to mean "nobody" — it means "everyone" at read.
    """
    resolved = [path_acls[path] for path in target_paths if path in path_acls]
    if not resolved:
        # Unknown provenance: we can vouch for no team => deny by default.
        return list(DENY_ALL_ACL)
    # Org-public inputs (empty acl) impose no constraint; only non-empty ACLs
    # narrow. If every resolved input is org-public, the artifact is org-public.
    constraints = [set(acl) for acl in resolved if acl]
    if not constraints:
        return []
    intersection = constraints[0].intersection(*constraints[1:])
    if not intersection:
        # Disjoint teams: no team can see every input => visible to nobody.
        return list(DENY_ALL_ACL)
    return sorted(intersection)


__all__ = ["DENY_ALL_ACL", "commit_acl_intersection"]
