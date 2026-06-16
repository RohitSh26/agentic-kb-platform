"""context.expand: trust-tiered BFS expansion from seed artifact ids.

Walks the knowledge graph in two phases:
  1. EXTRACTED tier — deterministic backbone (always).
  2. INFERRED tier — routing hints (only when include_inferred=True), starting
     from everything gathered in phase 1.

AMBIGUOUS and REJECTED are NEVER included (admits() enforces this). ACL
filtering happens per hop before the frontier expands, exactly as in
graph.get_neighbors. Membership is enforced by fetch_edges_touching /
fetch_artifacts (valid_from_seq / invalidated_at_seq vs build_seq).

Budget: artifacts are collected in BFS order (seeds first, then 1-hop, 2-hop…),
turned into EvidenceCards via build_card(), and accumulated until budget_tokens
is reached. If context_pack_id is given, the expansion is charged against the
pack's run budget and new cards are registered into the pack.

A retrieval_event row is written BEFORE the pack state is updated (store-after-
ledger ordering: no orphan state without a ledger row).
"""

import logging
import time
import uuid

from fastmcp.exceptions import ToolError

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.context_broker.error_ledger import write_error_event
from agentic_mcp_server.context_broker.retrieval import (
    authorization_decision,
    build_card,
    card_tokens,
)
from agentic_mcp_server.context_broker.state import UnknownPackError
from agentic_mcp_server.context_broker.trust import admits
from agentic_mcp_server.infrastructure.postgres.active_kb_version import fetch_active_version
from agentic_mcp_server.infrastructure.postgres.artifacts import ArtifactRow, fetch_artifacts
from agentic_mcp_server.infrastructure.postgres.edges import fetch_edges_touching
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    RetrievalEventInsert,
    insert_event,
)
from agentic_mcp_server.mcp.tool_schemas.context import ExpandRequest, ExpandResponse
from agentic_mcp_server.mcp.tool_schemas.evidence import EvidenceCard
from agentic_mcp_server.telemetry.audit import audit_context_access

logger = logging.getLogger(__name__)

_TOOL_NAME = "context.expand"
_NO_RUN_SENTINEL = "-"
# Cap the number of connected cards returned, not just the token budget. BFS order is
# closest-first, so the top _MAX_EXPAND_CARDS are the nearest neighbours — an agent needs
# the immediate neighbourhood, not the whole 2-hop frontier (which bloated read_pack and
# every consumer's context). Tunes precision/cost without losing the relevant cards.
_MAX_EXPAND_CARDS = 30


async def _bfs_tier(
    deps: BrokerDeps,
    seed_ids: list[uuid.UUID],
    build_seq: int,
    *,
    trust_floor: str,
    include_inferred: bool,
    requester: Requester,
    visited: set[uuid.UUID],
) -> list[ArtifactRow]:
    """Single-tier BFS: expand seeds outward, ACL-filtering each hop.

    Adds discovered artifact_ids to ``visited`` in-place so the caller can
    pass the result as seeds for a subsequent tier without revisiting.
    Returns artifacts in BFS order (1-hop before 2-hop, etc.).
    """
    ordered: list[ArtifactRow] = []

    frontier = list(seed_ids)
    # BFS level by level until the frontier is exhausted
    while frontier:
        async with deps.session_factory() as session:
            edges = await fetch_edges_touching(session, frontier, build_seq, None)

        frontier_set = set(frontier)
        neighbor_ids: list[uuid.UUID] = []

        for edge in edges:
            if not admits(
                edge.trust_class,
                trust_floor=trust_floor,
                include_inferred=include_inferred,
            ):
                continue
            if edge.from_artifact_id in frontier_set and edge.to_artifact_id not in visited:
                neighbor_ids.append(edge.to_artifact_id)
                visited.add(edge.to_artifact_id)
            elif edge.to_artifact_id in frontier_set and edge.from_artifact_id not in visited:
                neighbor_ids.append(edge.from_artifact_id)
                visited.add(edge.from_artifact_id)

        if not neighbor_ids:
            break

        # ACL per hop: unauthorized nodes are neither returned nor traversed
        async with deps.session_factory() as session:
            artifacts = await fetch_artifacts(session, neighbor_ids, build_seq)
        allowed = deps.authorization.filter_artifacts(requester, artifacts)
        allowed_by_id = {a.artifact_id: a for a in allowed}

        next_frontier: list[uuid.UUID] = []
        for aid in neighbor_ids:
            row = allowed_by_id.get(aid)
            if row is None:
                # ACL-suppressed: kept in visited to stop re-traversal
                continue
            ordered.append(row)
            next_frontier.append(aid)

        frontier = next_frontier

    return ordered


async def expand(deps: BrokerDeps, request: ExpandRequest, requester: Requester) -> ExpandResponse:
    started = time.monotonic()

    # Resolve active version once; all BFS uses this build_seq.
    async with deps.session_factory() as session:
        active = await fetch_active_version(session)
    if active is None:
        await write_error_event(
            deps,
            tool_name=_TOOL_NAME,
            subject=requester.subject,
            query_text=str(request.seed_artifact_ids),
        )
        raise ToolError("no active kb_version; the knowledge base has not been built yet")
    kb_version = active.kb_version
    build_seq = active.build_seq

    # Resolve optional pack; determine effective budget.
    pack = None
    if request.context_pack_id is not None:
        try:
            pack = deps.packs.get(request.context_pack_id)
        except UnknownPackError:
            await write_error_event(
                deps,
                tool_name=_TOOL_NAME,
                subject=requester.subject,
                query_text=request.context_pack_id,
            )
            raise ToolError(f"unknown context_pack_id: {request.context_pack_id}") from None

    # Effective budget: request value clamped to server max, and further clamped
    # to the pack's remaining tokens if a pack was supplied.
    effective_budget = min(request.budget_tokens, deps.settings.max_run_budget_tokens)
    if pack is not None:
        effective_budget = min(effective_budget, pack.run_remaining_tokens)

    # Artifact ids already in the pack (skip these in the returned cards).
    pack_artifact_ids: frozenset[uuid.UUID] = (
        frozenset(card.artifact_id for card in pack.cards.values())
        if pack is not None
        else frozenset()
    )

    # Fetch seed artifacts (membership + ACL); seeds not in pack will become cards.
    async with deps.session_factory() as session:
        seed_rows_all = await fetch_artifacts(session, list(request.seed_artifact_ids), build_seq)
    seed_rows_allowed = deps.authorization.filter_artifacts(requester, seed_rows_all)
    seed_by_id: dict[uuid.UUID, ArtifactRow] = {r.artifact_id: r for r in seed_rows_allowed}

    # BFS visited set starts with all requested seeds (allowed or not) so we
    # never re-traverse through them.
    visited: set[uuid.UUID] = set(request.seed_artifact_ids)

    # Phase 1: EXTRACTED backbone BFS from allowed seeds.
    extracted_rows = await _bfs_tier(
        deps,
        list(seed_by_id),
        build_seq,
        trust_floor=request.trust_floor,
        include_inferred=False,
        requester=requester,
        visited=visited,
    )

    # Phase 2: INFERRED tier BFS from everything gathered so far (seeds + phase 1).
    inferred_rows: list[ArtifactRow] = []
    if request.include_inferred:
        phase2_seeds = list(seed_by_id) + [r.artifact_id for r in extracted_rows]
        inferred_rows = await _bfs_tier(
            deps,
            phase2_seeds,
            build_seq,
            trust_floor=request.trust_floor,
            include_inferred=True,
            requester=requester,
            visited=visited,
        )

    # Collect in BFS order: seeds, then EXTRACTED neighbors, then INFERRED neighbors.
    # Skip seeds already in the pack.
    ordered_rows: list[ArtifactRow] = []
    for row in seed_rows_allowed:
        if row.artifact_id not in pack_artifact_ids:
            ordered_rows.append(row)
    for row in extracted_rows:
        if row.artifact_id not in pack_artifact_ids:
            ordered_rows.append(row)
    for row in inferred_rows:
        if row.artifact_id not in pack_artifact_ids:
            ordered_rows.append(row)

    # Build cards in BFS order (closest first), capped by BOTH the token budget AND a
    # card-count limit so the response stays the immediate neighbourhood, not the whole frontier.
    cards: list[EvidenceCard] = []
    tokens_used = 0
    truncated = False
    for row in ordered_rows:
        if len(cards) >= _MAX_EXPAND_CARDS:
            truncated = True
            break
        card = build_card(row)
        cost = card_tokens(card)
        if tokens_used + cost > effective_budget:
            truncated = True
            break
        cards.append(card)
        tokens_used += cost

    all_artifact_ids = [c.artifact_id for c in cards]
    suppressed_seed_ids = [r.artifact_id for r in seed_rows_all if r.artifact_id not in seed_by_id]

    # Audit
    audit_context_access(
        tool=_TOOL_NAME,
        requester=requester,
        kb_version=kb_version,
        artifact_ids=all_artifact_ids,
        suppressed_artifact_ids=suppressed_seed_ids,
        injection_flagged_ids=[c.artifact_id for c in cards if c.injection_flagged],
    )

    # Determine run_id for ledger row.
    run_id = pack.run_id if pack is not None else _NO_RUN_SENTINEL
    pack_id_for_ledger = uuid.UUID(pack.context_pack_id) if pack is not None else None

    # Build observability details (best-effort).
    tiers = ["EXTRACTED"]
    if request.include_inferred and inferred_rows:
        tiers.append("INFERRED")
    # Count edge types touched across both BFS phases. We record card-level
    # card_type counts as a proxy (we don't have per-hop edge counts without
    # plumbing through _bfs_tier); that is sufficient for the timeline view.
    _expand_details: dict[str, object] = {
        "seed_artifact_ids": [str(s) for s in request.seed_artifact_ids],
        "tiers": tiers,
        "cards_added": len(cards),
        "truncated": truncated,
        "tokens": tokens_used,
    }

    # Write ledger row BEFORE updating pack state (store-after-ledger ordering).
    async with deps.session_factory() as session:
        await insert_event(
            session,
            RetrievalEventInsert(
                run_id=run_id,
                agent_name=requester.subject,
                tool_name=_TOOL_NAME,
                status="approved",
                kb_version=kb_version,
                context_pack_id=pack_id_for_ledger,
                query_text=",".join(str(s) for s in request.seed_artifact_ids),
                returned_artifact_ids=all_artifact_ids,
                new_evidence_ids=[uuid.UUID(c.evidence_id) for c in cards],
                tokens_returned=tokens_used,
                latency_ms=int((time.monotonic() - started) * 1000),
                details=_expand_details,
            ),
        )

    # Update pack state: charge budget and register new cards.
    if pack is not None:
        async with pack.lock:
            pack.charge(requester.subject, tokens_used)
            for card in cards:
                pack.cards[card.evidence_id] = card

    logger.info(
        "broker.expand seeds=%d subject=%s trust_floor=%s include_inferred=%s "
        "cards=%d tokens=%d truncated=%s kb_version=%s",
        len(request.seed_artifact_ids),
        requester.subject,
        request.trust_floor,
        request.include_inferred,
        len(cards),
        tokens_used,
        truncated,
        kb_version,
    )

    return ExpandResponse(
        cards=cards,
        tokens_used=tokens_used,
        truncated=truncated,
        authorization=authorization_decision(deps),
    )
