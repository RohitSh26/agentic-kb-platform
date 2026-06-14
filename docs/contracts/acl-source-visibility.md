# Contract: ACL / source-visibility model

> Cross-service contract. kb-builder stamps visibility onto artifacts/edges at build time; mcp-server
> filters every retrieval and verification by it. Formalises the per-team ACL already enforced at
> read (`team_acl_v1`) and the KB-5 follow-up.

## Principle

Visibility is **data**, stamped at build time and propagated through derivation, then enforced at
**every** read and verification path. An agent can never retrieve, traverse to, or cite an artifact
it is not authorised for — and the verifier rejects citations to artifacts the requester could not
see (`L0_acl_visible`).

## Model

- Each `source_item` declares `acl_teams` (the set of team identifiers permitted to see it). Empty /
  null means "no team" — **deny by default**, never "everyone".
- Each `knowledge_artifact` inherits `acl_teams` from its originating `source_item`. **A derived
  artifact is visible to a team if and only if that team is authorised for *every* source the
  artifact was derived from** — the intersection of its inputs' ACLs, full stop. A derivation can
  only ever narrow visibility, never widen it. (Concretely: a fact derived only from source S is
  visible exactly where S is; a fact derived from S and T is visible only where both S and T are.)
- Each `knowledge_edge` is visible only where **both** endpoints are visible: an edge's effective
  ACL is the intersection of its two artifacts' ACLs. An edge to an artifact a team cannot see is
  invisible to that team, even if the near endpoint is visible. This prevents traversal from
  leaking the existence of restricted artifacts.

## Enforcement points

1. **Retrieval** (`create_pack`/`read_pack`/`open_evidence`): filter candidates by the requester's
   team set before ranking and before returning. Already enforced; this contract makes the
   propagation rules explicit.
2. **Traversal** (`graph.get_neighbors`): a neighbour is returned only if its artifact is visible to
   the requester AND the edge's intersection ACL admits the requester. Trust filtering and ACL
   filtering compose (both must pass).
3. **Verification** (`verify_answer` L0): `L0_acl_visible` fails a claim that cites an artifact the
   requester was not authorised for.

## Cross-domain caveat

Cross-domain links (phase 2+) can connect a low-sensitivity doc to a high-sensitivity card or code.
The edge's intersection ACL handles this: the link is only visible to teams authorised for **both**
endpoints. Building a cross-domain edge never broadens either endpoint's visibility.

## Storage

`source_item.acl_teams` and `knowledge_artifact.acl_teams` (text[]/jsonb, NOT NULL, default empty).
Edge ACL is computed at read as the intersection of endpoint ACLs (no separate column needed unless
profiling shows it necessary). Phase 0 records the rule; the propagation/intersection logic lands
with the build CLI (phase 1) and the cross-domain linker (phase 2).
