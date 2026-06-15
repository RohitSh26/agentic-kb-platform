"""Typed claim/evidence ledger adjudication for the L2 verifier (ADR-0011, PR-30).

The deterministic L2 level asks the ledger one question per claim: "does a real,
in-version, requester-visible ledger unit support this claim's typed assertion?"
This module answers it with NO LLM — it resolves the matching fact unit through
``infrastructure.postgres.ledger_facts`` (membership-filtered SQL) and applies
the SAME requester-team ACL filter as retrieval (acl-source-visibility.md), then
returns a boolean verdict.

The headline case it catches that L0 alone misses: the cited evidence is real and
retrieved, but the claim MISREADS it — e.g. "symbol foo is in bar.py" while the
ledger only shows ``foo`` defined in ``baz.py``. The quote can be genuine; the
ASSERTION is false ⇒ L2 fails the claim.
"""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.trust import CLAIM_SUPPORTING
from agentic_mcp_server.infrastructure.postgres.ledger_facts import (
    EdgeFactRow,
    SymbolFactRow,
    fetch_edge_between_units,
    fetch_file_imports_module_units,
    fetch_symbol_in_file_units,
)
from agentic_mcp_server.mcp.tool_schemas.verification import (
    ClaimAssertion,
    FileImportsModuleAssertion,
    SymbolInFileAssertion,
)

logger = logging.getLogger(__name__)


def _acl_visible(acl_teams: tuple[str, ...], requester: Requester) -> bool:
    """Same admission rule as retrieval/L0: empty ACL = org-public; else intersect."""
    return not acl_teams or bool(requester.teams.intersection(acl_teams))


def _visible_symbol_units(rows: list[SymbolFactRow], requester: Requester) -> list[SymbolFactRow]:
    return [row for row in rows if _acl_visible(row.acl_teams, requester)]


def _claim_supporting_edge_units(
    rows: list[EdgeFactRow], requester: Requester
) -> list[EdgeFactRow]:
    """Visible edge units whose trust class can support a cited claim (EXTRACTED).

    An INFERRED_* edge is a routing hint only (trust-buckets.md) and must not let
    L2 adjudicate a typed fact as true — consistent with L0_supporting_trust_ok.
    """
    return [
        row
        for row in rows
        if _acl_visible(row.acl_teams, requester) and row.trust_class == CLAIM_SUPPORTING
    ]


async def adjudicate_typed_fact(
    session: AsyncSession,
    assertion: ClaimAssertion,
    *,
    build_seq: int,
    requester: Requester,
) -> bool:
    """True iff a deterministic, visible ledger unit supports the typed assertion.

    Each assertion kind resolves the matching fact unit family; membership is
    enforced in SQL and ACL visibility is applied here against the requester's
    teams. No LLM, no inference — a missing or misread unit ⇒ False.
    """
    if isinstance(assertion, SymbolInFileAssertion):
        rows = await fetch_symbol_in_file_units(
            session, symbol=assertion.symbol, file=assertion.file, build_seq=build_seq
        )
        return bool(_visible_symbol_units(rows, requester))

    if isinstance(assertion, FileImportsModuleAssertion):
        rows = await fetch_file_imports_module_units(
            session, file=assertion.file, module=assertion.module, build_seq=build_seq
        )
        return bool(_claim_supporting_edge_units(rows, requester))

    # EdgeBetweenAssertion: a malformed endpoint id can match no edge.
    try:
        from_id = uuid.UUID(assertion.from_id)
        to_id = uuid.UUID(assertion.to_id)
    except ValueError:
        return False
    edge_rows = await fetch_edge_between_units(
        session,
        edge_type=assertion.edge_type,
        from_id=from_id,
        to_id=to_id,
        build_seq=build_seq,
    )
    return bool(_claim_supporting_edge_units(edge_rows, requester))
