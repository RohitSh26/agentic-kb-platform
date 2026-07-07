"""The four parallel pure-retrieval nodes behind ``get_task_context`` (PR-39).

Each node is zero-LLM: SearchClient relevance hints, Postgres hydration, team-ACL
filtering, and knowledge_edge traversal — the same building blocks as every other
broker tool, composed per ADR-0030 Decision §2. The nodes are genuinely parallel:
each derives its own anchor from the request via the shared, deterministic
``resolve_entities`` (hints → alias index → search fallback), so no node waits on
another. Confidence tiering — including the `calls`-edge corroboration rule from
the 2026-07-02 Graphify audit — lives here, in ``calls_edge_tier``.
"""

import json
import logging
import operator
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Required, TypedDict, cast, get_args

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.context_broker.retrieval import readable_path
from agentic_mcp_server.context_broker.trust import admits
from agentic_mcp_server.infrastructure.postgres.artifacts import ArtifactRow, fetch_artifacts
from agentic_mcp_server.infrastructure.postgres.edges import EdgeRow, fetch_edges_touching
from agentic_mcp_server.mcp.tool_schemas.search import ConfidenceTier
from agentic_mcp_server.mcp.tool_schemas.task_context import (
    AmbiguousCandidate,
    Convention,
    GetTaskContextRequest,
    GetTaskContextResponse,
    PriorChange,
    ResolutionSource,
    ScopeEntity,
)

logger = logging.getLogger(__name__)

_SYMBOL_TYPES = ("code_symbol", "endpoint")
_CODE_TYPES = ("code_symbol", "endpoint", "code_file", "test")
_ALIAS_TYPE = "alias_reference"
_COMMIT_TYPE = "commit"

_SEARCH_TOP = 8
_BROADENED_TOP = 16
_MAX_SCOPE_ENTITIES = 5  # max evidence cards per retrieval band (token-budgets rule)
_MAX_CONVENTIONS = 3
_MAX_PRIOR_CHANGES = 3
# A second alias phrase scoring >= this fraction of the top one, with different
# targets, means resolution is genuinely ambiguous — surfaced, never guessed.
_ALIAS_AMBIGUITY_RATIO = 0.8

# The broker's read-time trust admission for blast-radius traversal: the
# EXTRACTED backbone only, same default as graph.get_neighbors.
_TRUST_FLOOR = "EXTRACTED"

_TIER_RANK: dict[str, int] = {"interpreted": 0, "deterministic": 1, "ground_truth": 2}
_KNOWN_TIERS = frozenset(get_args(ConfidenceTier))

# Path segments too generic to identify "the resolved scope's directories".
_GENERIC_SEGMENTS = frozenset({"src", "services", "tests", "test", "lib", "docs"})


def admits_floor(tier: ConfidenceTier, floor: ConfidenceTier) -> bool:
    """True iff content of ``tier`` survives a ``confidence_floor`` of ``floor``."""
    return _TIER_RANK[tier] >= _TIER_RANK[floor]


def calls_edge_tier(
    *,
    caller_file: uuid.UUID | None,
    target_file: uuid.UUID | None,
    import_pairs: frozenset[tuple[uuid.UUID, uuid.UUID]],
    target_path: str | None,
) -> tuple[ConfidenceTier, str | None]:
    """Tier a `calls` edge per the 2026-07-02 Graphify-audit corroboration rule.

    `deterministic` ONLY when an independent structural signal names the
    target's module: caller and target defined in the SAME file, or the
    caller's file carries an `imports` edge to the target's file. Anything
    short of that — including a missing `defined_in` on either side — is
    `interpreted` with a caveat, because a syntactic name match can resolve to
    a single, confidently-labelled, WRONG target (the audit's failure shape).
    """
    if caller_file is not None and caller_file == target_file:
        return "deterministic", None
    if (
        caller_file is not None
        and target_file is not None
        and (caller_file, target_file) in import_pairs
    ):
        return "deterministic", None
    if target_path:
        caveat = (
            f"`calls` edge is not corroborated by an import of {target_path}; a same-named "
            "symbol elsewhere may be the real target — verify before relying on it"
        )
    else:
        caveat = (
            "`calls` edge target's defining module could not be resolved (no `defined_in` "
            "edge); the call target is unverified — a same-named symbol may be the real target"
        )
    return "interpreted", caveat


@dataclass(frozen=True)
class TaskContextCtx:
    """Per-call runtime context threaded through the graph state (never serialized)."""

    deps: BrokerDeps
    requester: Requester
    request: GetTaskContextRequest
    build_seq: int
    kb_version: str


@dataclass(frozen=True)
class NodeSpan:
    """One node execution window (monotonic clock) — logged and structure-tested."""

    node: str
    started: float
    ended: float


@dataclass(frozen=True)
class ScopeResolution:
    entities: tuple[ScopeEntity, ...] = ()
    ambiguous: tuple[AmbiguousCandidate, ...] = ()
    open_questions: tuple[str, ...] = ()

    @property
    def unresolved(self) -> bool:
        """Truly empty — an ambiguous answer IS an answer and is not 'unresolved'."""
        return not self.entities and not self.ambiguous


@dataclass(frozen=True)
class BlastEntry:
    """One blast-radius neighbor with its FULL path — internal graph state only.

    The wire shape dedups paths (1.12.0): synthesize maps each entry's path to a
    ``path_ref`` into the response's ``referenced_paths`` table. Keeping the full
    path here keeps the nodes and the floor/trim logic independent of the table.
    """

    entity_id: uuid.UUID
    path: str
    symbol: str | None
    edge_type: str
    confidence_tier: ConfidenceTier
    caveat: str | None


@dataclass(frozen=True)
class BlastResolution:
    callers: tuple[BlastEntry, ...] = ()
    callees: tuple[BlastEntry, ...] = ()
    tests: tuple[BlastEntry, ...] = ()


@dataclass
class _Meter:
    """Per-node retrieval accounting, merged into the graph state by reducers."""

    calls: int = 0
    suppressed: list[uuid.UUID] = field(default_factory=list)


class TaskContextUpdate(TypedDict, total=False):
    """A node's partial state update. Parallel nodes write disjoint keys; the
    shared counters (`calls_used`, `suppressed`, `node_spans`) carry reducers so
    concurrent updates merge instead of colliding."""

    broadened: bool
    scope: ScopeResolution
    blast: BlastResolution
    conventions: tuple[Convention, ...]
    prior_changes: tuple[PriorChange, ...]
    calls_used: Annotated[int, operator.add]
    suppressed: Annotated[list[uuid.UUID], operator.add]
    node_spans: Annotated[list[NodeSpan], operator.add]
    response: GetTaskContextResponse


class TaskContextState(TaskContextUpdate, total=False):
    """The full LangGraph state: every update key plus the per-call runtime ctx
    (always present — seeded by ``run_task_context_graph``, never written by a node)."""

    ctx: Required[TaskContextCtx]


# --------------------------------------------------------------------- shared retrieval


async def _search_rows(
    ctx: TaskContextCtx, query: str, top: int, meter: _Meter
) -> tuple[list[ArtifactRow], dict[uuid.UUID, float]]:
    """SearchClient hints -> Postgres hydration -> ACL, in deterministic rank order."""
    meter.calls += 1
    hits = await ctx.deps.search_client.search(query, build_seq=ctx.build_seq, top=top)
    if not hits:
        return [], {}
    scores = {hit.artifact_id: hit.score for hit in hits}
    async with ctx.deps.session_factory() as session:
        rows = await fetch_artifacts(session, list(scores), ctx.build_seq)
    allowed = ctx.deps.authorization.filter_artifacts(ctx.requester, rows)
    allowed_ids = {row.artifact_id for row in allowed}
    meter.suppressed.extend(row.artifact_id for row in rows if row.artifact_id not in allowed_ids)
    allowed.sort(key=lambda a: (-scores.get(a.artifact_id, 0.0), str(a.artifact_id)))
    return allowed, scores


async def _hydrate_allowed(
    ctx: TaskContextCtx, artifact_ids: list[uuid.UUID], meter: _Meter
) -> dict[uuid.UUID, ArtifactRow]:
    if not artifact_ids:
        return {}
    async with ctx.deps.session_factory() as session:
        rows = await fetch_artifacts(session, artifact_ids, ctx.build_seq)
    allowed = {
        row.artifact_id: row for row in ctx.deps.authorization.filter_artifacts(ctx.requester, rows)
    }
    meter.suppressed.extend(row.artifact_id for row in rows if row.artifact_id not in allowed)
    return allowed


def _entity(row: ArtifactRow, source: ResolutionSource, tier: ConfidenceTier) -> ScopeEntity:
    symbol = row.title if row.artifact_type in _SYMBOL_TYPES else None
    return ScopeEntity(
        entity_id=row.artifact_id,
        path=readable_path(row.source_uri),
        symbol=symbol,
        resolution_source=source,
        confidence_tier=tier,
    )


# ----------------------------------------------------------------------- scope stages


def _alias_targets(row: ArtifactRow) -> list[uuid.UUID]:
    """Target artifact ids from an alias_reference row's body_text JSON (PR-38).

    Tolerant of both shapes the proposal/PR-38 brief describe (`target_entity_ids`
    or `targets: [{artifact_id, path}]`); anything unparseable degrades to [] —
    logged as a KB-gap signal, never a crash (the resolver falls back to search).
    """
    try:
        payload = json.loads(row.body_text or "")
    except (json.JSONDecodeError, ValueError):
        logger.warning(
            "event=task_context_alias_unparseable artifact_id=%s title=%r",
            row.artifact_id,
            (row.title or "")[:80],
        )
        return []
    if not isinstance(payload, dict):
        return []
    raw = payload.get("target_entity_ids")
    if not isinstance(raw, list):
        targets = payload.get("targets")
        raw = (
            [t.get("artifact_id") for t in targets if isinstance(t, dict)]
            if isinstance(targets, list)
            else []
        )
    out: list[uuid.UUID] = []
    for value in raw:
        try:
            parsed = uuid.UUID(str(value))
        except ValueError:
            continue
        if parsed not in out:
            out.append(parsed)
    return out


def _alias_tier(row: ArtifactRow) -> ConfidenceTier:
    """The alias row's own tier (interpreted at creation; promotable per proposal §4)."""
    try:
        payload = json.loads(row.body_text or "")
    except (json.JSONDecodeError, ValueError):
        return "interpreted"
    tier = payload.get("confidence_tier") if isinstance(payload, dict) else None
    if isinstance(tier, str) and tier in _KNOWN_TIERS:
        return cast(ConfidenceTier, tier)
    return "interpreted"


async def _resolve_from_hints(ctx: TaskContextCtx, meter: _Meter) -> ScopeResolution:
    hints = ctx.request.hints
    if hints is None or (not hints.symbols and not hints.file_paths):
        return ScopeResolution()
    entities: list[ScopeEntity] = []
    ambiguous: list[AmbiguousCandidate] = []
    questions: list[str] = []

    for symbol in hints.symbols:
        rows, _ = await _search_rows(ctx, symbol, _SEARCH_TOP, meter)
        matches = [
            row
            for row in rows
            if row.artifact_type in _SYMBOL_TYPES and (row.title or "").rstrip("()") == symbol
        ]
        if len(matches) == 1:
            entities.append(_entity(matches[0], "hint", "deterministic"))
        elif len(matches) > 1:
            ambiguous.append(
                AmbiguousCandidate(
                    alias_text=symbol,
                    candidates=[row.artifact_id for row in matches],
                    reason=(
                        f"symbol '{symbol}' matches {len(matches)} distinct definitions; "
                        "resolution stopped rather than guessing"
                    ),
                )
            )
            questions.append(
                f"Which '{symbol}' is in scope? {len(matches)} definitions match — see "
                "ambiguous_candidates."
            )
        else:
            questions.append(f"Hint symbol '{symbol}' was not found in the knowledge base.")

    for path in hints.file_paths:
        rows, _ = await _search_rows(ctx, path.rsplit("/", 1)[-1], _SEARCH_TOP, meter)
        matched_paths: dict[str, ArtifactRow] = {}
        for row in rows:
            row_path = readable_path(row.source_uri)
            if row_path == path or row_path.endswith("/" + path):
                # prefer the code_file artifact as the file's identity
                current = matched_paths.get(row_path)
                if current is None or (
                    row.artifact_type == "code_file" and current.artifact_type != "code_file"
                ):
                    matched_paths[row_path] = row
        if len(matched_paths) == 1:
            entities.append(_entity(next(iter(matched_paths.values())), "hint", "deterministic"))
        elif len(matched_paths) > 1:
            candidates = sorted(matched_paths.values(), key=lambda r: readable_path(r.source_uri))
            ambiguous.append(
                AmbiguousCandidate(
                    alias_text=path,
                    candidates=[row.artifact_id for row in candidates],
                    reason=(
                        f"file hint '{path}' matches {len(matched_paths)} distinct paths; "
                        "resolution stopped rather than guessing"
                    ),
                )
            )
            questions.append(
                f"Which file did you mean by '{path}'? {len(matched_paths)} paths match — see "
                "ambiguous_candidates."
            )
        else:
            questions.append(f"Hint file '{path}' was not found in the knowledge base.")

    return ScopeResolution(
        entities=tuple(entities), ambiguous=tuple(ambiguous), open_questions=tuple(questions)
    )


async def _resolve_from_alias(
    ctx: TaskContextCtx,
    rows: list[ArtifactRow],
    scores: dict[uuid.UUID, float],
    meter: _Meter,
) -> ScopeResolution:
    """Resolve via PR-38 alias_reference rows among the task-description search hits.

    Degrades gracefully when the KB predates PR-38: no alias rows ⇒ empty result
    and the caller falls through to plain search.
    """
    alias_rows = [row for row in rows if row.artifact_type == _ALIAS_TYPE]
    if not alias_rows:
        return ScopeResolution()
    top = alias_rows[0]
    top_targets = _alias_targets(top)
    if not top_targets:
        return ScopeResolution()  # unparseable/empty alias — fall through to search
    top_score = scores.get(top.artifact_id, 0.0)
    contenders = [
        row
        for row in alias_rows[1:]
        if scores.get(row.artifact_id, 0.0) >= _ALIAS_AMBIGUITY_RATIO * top_score
    ]
    differing = [
        row
        for row in contenders
        if (targets := _alias_targets(row)) and set(targets) != set(top_targets)
    ]
    if differing:
        candidate_ids: list[uuid.UUID] = list(top_targets)
        for row in differing:
            for target in _alias_targets(row):
                if target not in candidate_ids:
                    candidate_ids.append(target)
        alias_text = top.title or ctx.request.task_description
        return ScopeResolution(
            ambiguous=(
                AmbiguousCandidate(
                    alias_text=alias_text,
                    candidates=candidate_ids,
                    reason=(
                        f"{1 + len(differing)} alias phrases match with comparable relevance "
                        "and point at different targets; resolution stopped rather than guessing"
                    ),
                ),
            ),
            open_questions=(
                f"The phrase resolves to {len(candidate_ids)} different targets via the alias "
                "index — which one is in scope? See ambiguous_candidates.",
            ),
        )
    allowed = await _hydrate_allowed(ctx, top_targets, meter)
    tier = _alias_tier(top)
    # targets are ranked in the alias body (PR-38 contract), so truncation to the
    # scope-entity cap keeps the best-ranked ones (token-budgets: 3-5 per retrieval)
    entities = tuple(
        _entity(allowed[target], "alias_index", tier) for target in top_targets if target in allowed
    )[:_MAX_SCOPE_ENTITIES]
    return ScopeResolution(entities=entities)


def _resolve_from_search(rows: list[ArtifactRow], *, broadened: bool) -> ScopeResolution:
    if broadened:
        candidates = [row for row in rows if row.artifact_type not in (_ALIAS_TYPE, _COMMIT_TYPE)]
    else:
        candidates = [row for row in rows if row.artifact_type in _CODE_TYPES]
    entities = tuple(
        _entity(row, "search", "interpreted") for row in candidates[:_MAX_SCOPE_ENTITIES]
    )
    return ScopeResolution(entities=entities)


async def _resolve_entities_and_rows(
    ctx: TaskContextCtx, *, broadened: bool, meter: _Meter
) -> tuple[ScopeResolution, list[ArtifactRow] | None]:
    """As ``resolve_entities``, but also returns the task_description search rows
    when the chain actually issued that search (the hint-less, or hints-exhausted,
    path) — `None` when resolution was satisfied by hints alone (no such search
    ran). Lets a caller that ALSO wants a task_description search (conventions_node)
    reuse this result instead of re-issuing an identical, redundant search+hydrate
    round trip — the measured cause of conventions_node running ~2x its sibling
    nodes (a duplicate `SearchClient.search` + Postgres hydration + ACL filter).
    """
    from_hints = await _resolve_from_hints(ctx, meter)
    if from_hints.entities or from_hints.ambiguous:
        return from_hints, None

    top = _BROADENED_TOP if broadened else _SEARCH_TOP
    rows, scores = await _search_rows(ctx, ctx.request.task_description, top, meter)
    from_alias = await _resolve_from_alias(ctx, rows, scores, meter)
    if from_alias.entities or from_alias.ambiguous:
        return (
            ScopeResolution(
                entities=from_alias.entities,
                ambiguous=from_alias.ambiguous,
                open_questions=from_hints.open_questions + from_alias.open_questions,
            ),
            rows,
        )

    from_search = _resolve_from_search(rows, broadened=broadened)
    return (
        ScopeResolution(
            entities=from_search.entities,
            ambiguous=(),
            open_questions=from_hints.open_questions,
        ),
        rows,
    )


async def resolve_entities(
    ctx: TaskContextCtx, *, broadened: bool, meter: _Meter
) -> ScopeResolution:
    """The shared, deterministic resolution chain: hints → alias index → search.

    Every parallel node anchors on this, so no node waits on another (ADR-0030
    §2 fan-out) and all nodes agree on the same scope for the same input.
    Ambiguity at any stage STOPS the chain — the candidates are the answer.
    """
    scope, _rows = await _resolve_entities_and_rows(ctx, broadened=broadened, meter=meter)
    return scope


# ------------------------------------------------------------------------ graph nodes


def _span(node: str, started: float) -> list[NodeSpan]:
    return [NodeSpan(node=node, started=started, ended=time.monotonic())]


async def resolve_scope_node(state: TaskContextState) -> TaskContextUpdate:
    started = time.monotonic()
    ctx = state["ctx"]
    meter = _Meter()
    scope = await resolve_entities(ctx, broadened=state.get("broadened", False), meter=meter)
    logger.info(
        "event=task_context_node node=resolve_scope subject=%s entities=%d ambiguous=%d "
        "broadened=%s calls=%d",
        ctx.requester.subject,
        len(scope.entities),
        len(scope.ambiguous),
        state.get("broadened", False),
        meter.calls,
    )
    return {
        "scope": scope,
        "calls_used": meter.calls,
        "suppressed": meter.suppressed,
        "node_spans": _span("resolve_scope", started),
    }


def _admitted(edges: list[EdgeRow]) -> list[EdgeRow]:
    return [
        edge
        for edge in edges
        if admits(edge.trust_class, trust_floor=_TRUST_FLOOR, include_inferred=False)
    ]


async def blast_radius_node(state: TaskContextState) -> TaskContextUpdate:
    started = time.monotonic()
    ctx = state["ctx"]
    meter = _Meter()
    scope = await resolve_entities(ctx, broadened=state.get("broadened", False), meter=meter)
    seed_ids = [entity.entity_id for entity in scope.entities]
    if not seed_ids:
        return {
            "blast": BlastResolution(),
            "calls_used": meter.calls,
            "suppressed": meter.suppressed,
            "node_spans": _span("blast_radius", started),
        }

    meter.calls += 1
    async with ctx.deps.session_factory() as session:
        edges = _admitted(
            await fetch_edges_touching(
                session, seed_ids, ctx.build_seq, ["calls", "imports", "tests", "defined_in"]
            )
        )
    seed_set = set(seed_ids)
    cap = ctx.deps.settings.max_graph_neighbors

    caller_edges = [
        edge
        for edge in edges
        if edge.edge_type in ("calls", "imports")
        and edge.to_artifact_id in seed_set
        and edge.from_artifact_id not in seed_set
    ][:cap]
    callee_edges = [
        edge
        for edge in edges
        if edge.edge_type in ("calls", "imports")
        and edge.from_artifact_id in seed_set
        and edge.to_artifact_id not in seed_set
    ][:cap]
    test_edges = [
        edge
        for edge in edges
        if edge.edge_type == "tests"
        and edge.to_artifact_id in seed_set
        and edge.from_artifact_id not in seed_set
    ][:cap]

    defined_in = {
        edge.from_artifact_id: edge.to_artifact_id
        for edge in edges
        if edge.edge_type == "defined_in"
    }
    import_pairs = {
        (edge.from_artifact_id, edge.to_artifact_id)
        for edge in edges
        if edge.edge_type == "imports"
    }

    neighbor_ids = {edge.from_artifact_id for edge in caller_edges + test_edges} | {
        edge.to_artifact_id for edge in callee_edges
    }

    # Second hop for corroboration inputs only: the neighbors' defining files and
    # the import graph between the files involved (still the existing query layer).
    corroboration_seeds = list(neighbor_ids | set(defined_in.values()))
    if corroboration_seeds:
        meter.calls += 1
        async with ctx.deps.session_factory() as session:
            corroboration_edges = _admitted(
                await fetch_edges_touching(
                    session, corroboration_seeds, ctx.build_seq, ["defined_in", "imports"]
                )
            )
        for edge in corroboration_edges:
            if edge.edge_type == "defined_in":
                defined_in.setdefault(edge.from_artifact_id, edge.to_artifact_id)
            else:
                import_pairs.add((edge.from_artifact_id, edge.to_artifact_id))
    frozen_imports = frozenset(import_pairs)

    # Hydrate + ACL-filter every artifact we might surface. An unauthorized
    # neighbor is dropped here, BEFORE it can reveal its connectivity (the same
    # rule as graph.get_neighbors' per-hop filter).
    allowed = await _hydrate_allowed(ctx, list(neighbor_ids | set(defined_in.values())), meter)

    def _file_path(file_id: uuid.UUID | None) -> str | None:
        if file_id is None:
            return None
        row = allowed.get(file_id)
        return readable_path(row.source_uri) if row is not None else None

    def _blast_entity(
        neighbor_id: uuid.UUID, edge: EdgeRow, *, caller_id: uuid.UUID, target_id: uuid.UUID
    ) -> BlastEntry | None:
        row = allowed.get(neighbor_id)
        if row is None:
            return None  # ACL-suppressed: neither returned nor acknowledged
        if edge.edge_type == "calls":
            tier, caveat = calls_edge_tier(
                caller_file=defined_in.get(caller_id),
                target_file=defined_in.get(target_id),
                import_pairs=frozen_imports,
                target_path=_file_path(defined_in.get(target_id)),
            )
        else:
            # imports/tests are direct AST facts (the statement names the module).
            tier, caveat = "deterministic", None
        return BlastEntry(
            entity_id=row.artifact_id,
            path=readable_path(row.source_uri),
            symbol=row.title if row.artifact_type in _SYMBOL_TYPES else None,
            edge_type=edge.edge_type,
            confidence_tier=tier,
            caveat=caveat,
        )

    def _ordered(entities: list[BlastEntry | None]) -> tuple[BlastEntry, ...]:
        # Documented sort (mcp-tools-contract.md 1.12.0): (path, symbol, entity_id).
        present = [entity for entity in entities if entity is not None]
        present.sort(key=lambda e: (e.path, e.symbol or "", str(e.entity_id)))
        return tuple(present)

    callers = _ordered(
        [
            _blast_entity(
                edge.from_artifact_id,
                edge,
                caller_id=edge.from_artifact_id,
                target_id=edge.to_artifact_id,
            )
            for edge in caller_edges
        ]
    )
    callees = _ordered(
        [
            _blast_entity(
                edge.to_artifact_id,
                edge,
                caller_id=edge.from_artifact_id,
                target_id=edge.to_artifact_id,
            )
            for edge in callee_edges
        ]
    )
    tests = _ordered(
        [
            _blast_entity(
                edge.from_artifact_id,
                edge,
                caller_id=edge.from_artifact_id,
                target_id=edge.to_artifact_id,
            )
            for edge in test_edges
        ]
    )
    demoted = sum(1 for e in (*callers, *callees) if e.edge_type == "calls" and e.caveat)
    logger.info(
        "event=task_context_node node=blast_radius subject=%s seeds=%d callers=%d callees=%d "
        "tests=%d calls_edges_demoted=%d calls=%d",
        ctx.requester.subject,
        len(seed_ids),
        len(callers),
        len(callees),
        len(tests),
        demoted,
        meter.calls,
    )
    return {
        "blast": BlastResolution(callers=callers, callees=callees, tests=tests),
        "calls_used": meter.calls,
        "suppressed": meter.suppressed,
        "node_spans": _span("blast_radius", started),
    }


def _scope_terms(scope: ScopeResolution) -> set[str]:
    """Directory/module terms identifying the resolved scope's neighborhood."""
    terms: set[str] = set()
    for entity in scope.entities:
        segments = entity.path.lower().split("/")
        stem = segments[-1].rsplit(".", 1)[0]
        if stem and stem not in _GENERIC_SEGMENTS:
            terms.add(stem)
        for segment in segments[:-1]:
            if segment and segment not in _GENERIC_SEGMENTS:
                terms.add(segment)
    return terms


async def conventions_node(state: TaskContextState) -> TaskContextUpdate:
    started = time.monotonic()
    ctx = state["ctx"]
    meter = _Meter()
    broadened = state.get("broadened", False)
    scope, task_rows = await _resolve_entities_and_rows(ctx, broadened=broadened, meter=meter)
    # Reuse the task_description search resolve_entities already ran (same query,
    # same _SEARCH_TOP) instead of re-issuing it — see _resolve_entities_and_rows.
    # Only when broadened does resolve_entities' internal search use a different
    # top (_BROADENED_TOP), so conventions' own _SEARCH_TOP-sized search stays a
    # separate call in that (rare, retry-only) case.
    if broadened or task_rows is None:
        rows, _ = await _search_rows(ctx, ctx.request.task_description, _SEARCH_TOP, meter)
    else:
        rows = task_rows
    docs = [
        row
        for row in rows
        if row.artifact_type not in _CODE_TYPES
        and row.artifact_type not in (_ALIAS_TYPE, _COMMIT_TYPE)
    ]
    terms = _scope_terms(scope)
    if terms:
        relevant = [
            row
            for row in docs
            if any(term in f"{row.title or ''} {row.body_text or ''}".lower() for term in terms)
        ]
        docs = relevant or docs  # minimal v1: fall back to the top doc hits for the task
    conventions = tuple(
        Convention(
            pattern=row.title or readable_path(row.source_uri),
            evidence_ids=[row.artifact_id],
            confidence_tier="interpreted",
        )
        for row in docs[:_MAX_CONVENTIONS]
    )
    logger.info(
        "event=task_context_node node=conventions subject=%s conventions=%d scope_terms=%d "
        "calls=%d",
        ctx.requester.subject,
        len(conventions),
        len(terms),
        meter.calls,
    )
    return {
        "conventions": conventions,
        "calls_used": meter.calls,
        "suppressed": meter.suppressed,
        "node_spans": _span("conventions", started),
    }


async def similar_prior_changes_node(state: TaskContextState) -> TaskContextUpdate:
    started = time.monotonic()
    ctx = state["ctx"]
    meter = _Meter()
    rows, _ = await _search_rows(ctx, ctx.request.task_description, _BROADENED_TOP, meter)
    commits = [row for row in rows if row.artifact_type == _COMMIT_TYPE][:_MAX_PRIOR_CHANGES]
    prior = tuple(
        PriorChange(
            commit_or_pr_id=readable_path(row.source_uri) or str(row.artifact_id),
            summary=(row.title or next(iter((row.body_text or "").strip().splitlines()), ""))[:200],
            evidence_ids=[row.artifact_id],
        )
        for row in commits
    )
    logger.info(
        "event=task_context_node node=similar_prior_changes subject=%s prior_changes=%d calls=%d",
        ctx.requester.subject,
        len(prior),
        meter.calls,
    )
    return {
        "prior_changes": prior,
        "calls_used": meter.calls,
        "suppressed": meter.suppressed,
        "node_spans": _span("similar_prior_changes", started),
    }
