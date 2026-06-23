"""Read-only typed claim/evidence ledger over the existing registry.

This is NOT a new truth store: it derives nothing and writes nothing. It projects
``knowledge_artifact`` / ``knowledge_edge`` + spans into typed, ID-stable, citeable
**fact units** the deterministic L1/L2 verifier can adjudicate without an LLM:

  - AST facts   — "symbol X defined in file F (span)", "file F imports module M".
  - edge facts  — "an edge of type T exists between artifacts A and B".
  - prose facts — a doc/concept statement and its source span (read via the
    artifact rows; L2 only adjudicates the AST/edge kinds deterministically).

Every query filters by interval membership against the active build_seq
(version-membership.md) exactly like artifacts.py/edges.py, and returns each
row's ACL set so the caller applies the SAME requester-team filter as retrieval
(acl-source-visibility.md) — an edge unit carries the INTERSECTION of its two
endpoints' ACLs. No ORM model crosses the service boundary; raw SQL with pinned
names, kept honest by the contract tests.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

KNOWLEDGE_ARTIFACT_TABLE = "knowledge_artifact"
SOURCE_ITEM_TABLE = "source_item"
KNOWLEDGE_EDGE_TABLE = "knowledge_edge"

# Artifact types that name a code symbol whose definition lives at a file+span.
_SYMBOL_ARTIFACT_TYPES = ("code_symbol", "test")

# "symbol X defined in file F": a code_symbol/test artifact titled X whose source
# path is F, a member of the served build_seq. Returns its span + ACL so the
# caller can filter by requester teams and surface the citeable unit.
_SYMBOL_IN_FILE_QUERY = text(
    f"""
    SELECT a.artifact_id, a.title, s.path, a.span_start, a.span_end, a.acl_teams
    FROM {KNOWLEDGE_ARTIFACT_TABLE} a
    JOIN {SOURCE_ITEM_TABLE} s ON s.source_id = a.source_id
    WHERE a.artifact_type = ANY(CAST(:symbol_types AS text[]))
      AND a.title = :symbol
      AND s.path = :file
      AND a.valid_from_seq <= :build_seq
      AND (a.invalidated_at_seq IS NULL OR a.invalidated_at_seq > :build_seq)
    """
)

# "file F imports module M": an EXTRACTED `imports` edge (member) from the
# code_file whose source path is F to an artifact titled M. Both endpoints are
# membership-filtered via the joins; the unit's ACL is the intersection of the
# two endpoints' ACLs (acl-source-visibility.md), computed in the caller.
_FILE_IMPORTS_MODULE_QUERY = text(
    f"""
    SELECT e.edge_id, e.trust_class, e.from_artifact_id,
           ffile.acl_teams AS from_acl, tmod.acl_teams AS to_acl
    FROM {KNOWLEDGE_EDGE_TABLE} e
    JOIN {KNOWLEDGE_ARTIFACT_TABLE} ffile ON ffile.artifact_id = e.from_artifact_id
    JOIN {SOURCE_ITEM_TABLE} fsrc ON fsrc.source_id = ffile.source_id
    JOIN {KNOWLEDGE_ARTIFACT_TABLE} tmod ON tmod.artifact_id = e.to_artifact_id
    WHERE e.edge_type = 'imports'
      AND ffile.artifact_type = 'code_file'
      AND fsrc.path = :file
      AND tmod.title = :module
      AND e.valid_from_seq <= :build_seq
      AND (e.invalidated_at_seq IS NULL OR e.invalidated_at_seq > :build_seq)
      AND ffile.valid_from_seq <= :build_seq
      AND (ffile.invalidated_at_seq IS NULL OR ffile.invalidated_at_seq > :build_seq)
      AND tmod.valid_from_seq <= :build_seq
      AND (tmod.invalidated_at_seq IS NULL OR tmod.invalidated_at_seq > :build_seq)
    """
)

# "an edge of type T exists between A and B": a member edge of the given type
# whose endpoints are exactly the two artifact ids (in either direction — the
# verifier checks for existence of the relation, not its orientation). Both
# endpoints are membership-filtered; unit ACL = intersection of endpoint ACLs.
_EDGE_BETWEEN_QUERY = text(
    f"""
    SELECT e.edge_id, e.trust_class, e.from_artifact_id, e.to_artifact_id,
           af.acl_teams AS from_acl, at2.acl_teams AS to_acl
    FROM {KNOWLEDGE_EDGE_TABLE} e
    JOIN {KNOWLEDGE_ARTIFACT_TABLE} af ON af.artifact_id = e.from_artifact_id
    JOIN {KNOWLEDGE_ARTIFACT_TABLE} at2 ON at2.artifact_id = e.to_artifact_id
    WHERE e.edge_type = :edge_type
      AND (
            (e.from_artifact_id = CAST(:a AS uuid) AND e.to_artifact_id = CAST(:b AS uuid))
         OR (e.from_artifact_id = CAST(:b AS uuid) AND e.to_artifact_id = CAST(:a AS uuid))
      )
      AND e.valid_from_seq <= :build_seq
      AND (e.invalidated_at_seq IS NULL OR e.invalidated_at_seq > :build_seq)
      AND af.valid_from_seq <= :build_seq
      AND (af.invalidated_at_seq IS NULL OR af.invalidated_at_seq > :build_seq)
      AND at2.valid_from_seq <= :build_seq
      AND (at2.invalidated_at_seq IS NULL OR at2.invalidated_at_seq > :build_seq)
    """
)


@dataclass(frozen=True)
class SymbolFactRow:
    """An AST fact: ``title`` is defined in ``path`` at [span_start, span_end]."""

    artifact_id: uuid.UUID
    title: str
    path: str | None
    span_start: int | None
    span_end: int | None
    # empty = org-public; non-empty = requester team set must intersect.
    acl_teams: tuple[str, ...]


@dataclass(frozen=True)
class EdgeFactRow:
    """An edge fact: an edge of some type with its trust class and effective ACL."""

    edge_id: uuid.UUID
    trust_class: str
    # The edge's ``from`` artifact — the cited unit for a file_imports_module fact
    # (the code_file). Lets L2 require the resolving unit to be cited evidence.
    from_artifact_id: uuid.UUID
    # Intersection of the two endpoints' ACLs (acl-source-visibility.md): empty
    # = org-public; non-empty = requester team set must intersect.
    acl_teams: tuple[str, ...]


def _acl_intersection(from_acl: list[str] | None, to_acl: list[str] | None) -> tuple[str, ...]:
    """Edge ACL = intersection of endpoint ACLs; empty endpoint set = org-public.

    An empty (org-public) endpoint imposes no team restriction, so the edge's
    restriction is whatever the OTHER endpoint imposes. When both are restricted
    the edge is visible only to teams authorised for both — the set intersection.
    """
    left = set(from_acl or ())
    right = set(to_acl or ())
    if not left:
        return tuple(sorted(right))
    if not right:
        return tuple(sorted(left))
    return tuple(sorted(left & right))


async def fetch_symbol_in_file_units(
    session: AsyncSession, *, symbol: str, file: str, build_seq: int
) -> list[SymbolFactRow]:
    result = await session.execute(
        _SYMBOL_IN_FILE_QUERY,
        {
            "symbol_types": list(_SYMBOL_ARTIFACT_TYPES),
            "symbol": symbol,
            "file": file,
            "build_seq": build_seq,
        },
    )
    return [
        SymbolFactRow(
            artifact_id=row.artifact_id,
            title=row.title,
            path=row.path,
            span_start=row.span_start,
            span_end=row.span_end,
            acl_teams=tuple(row.acl_teams or ()),
        )
        for row in result
    ]


async def fetch_file_imports_module_units(
    session: AsyncSession, *, file: str, module: str, build_seq: int
) -> list[EdgeFactRow]:
    result = await session.execute(
        _FILE_IMPORTS_MODULE_QUERY,
        {"file": file, "module": module, "build_seq": build_seq},
    )
    return [
        EdgeFactRow(
            edge_id=row.edge_id,
            trust_class=row.trust_class,
            from_artifact_id=row.from_artifact_id,
            acl_teams=_acl_intersection(row.from_acl, row.to_acl),
        )
        for row in result
    ]


async def fetch_edge_between_units(
    session: AsyncSession,
    *,
    edge_type: str,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    build_seq: int,
) -> list[EdgeFactRow]:
    result = await session.execute(
        _EDGE_BETWEEN_QUERY,
        {
            "edge_type": edge_type,
            "a": str(from_id),
            "b": str(to_id),
            "build_seq": build_seq,
        },
    )
    return [
        EdgeFactRow(
            edge_id=row.edge_id,
            trust_class=row.trust_class,
            from_artifact_id=row.from_artifact_id,
            acl_teams=_acl_intersection(row.from_acl, row.to_acl),
        )
        for row in result
    ]
