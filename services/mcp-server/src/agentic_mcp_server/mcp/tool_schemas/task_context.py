"""Request/response schemas for the ``get_task_context`` tool (PR-39, ADR-0030 §2).

One call: a task description (plus optional file/symbol hints) in; resolved
scope, blast radius, conventions, and similar prior changes out — every entity
tiered and cited, the whole response budgeted. I/O contract per
docs/proposals/2026-07-02-tool-design-first-kb-architecture.md §2; tiering
rules (including the `calls`-edge corroboration rule) per §3 and
docs/contracts/mcp-tools-contract.md.
"""

import uuid
from typing import Literal

from pydantic import Field

from agentic_mcp_server.mcp.tool_schemas.base import McpModel
from agentic_mcp_server.mcp.tool_schemas.search import ConfidenceTier

# How a scope entity was resolved (proposal §2). `alias_index` = a PR-38
# alias_reference row; `hint` = an exact match on a caller-supplied file/symbol
# hint; `search` = the keyword-search fallback (always `interpreted` tier).
ResolutionSource = Literal["alias_index", "hint", "search"]


class TaskContextHints(McpModel):
    """Paths/symbols the developer already mentioned — resolved before anything else."""

    file_paths: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)


class GetTaskContextRequest(McpModel):
    task_description: str = Field(min_length=1)
    hints: TaskContextHints | None = None
    # Floor semantics (proposal §3): `interpreted` (default) admits everything;
    # `deterministic` forces interpreted-tier content OUT of the response
    # (never silently blended in); `ground_truth` admits raw-source facts only.
    confidence_floor: ConfidenceTier = "interpreted"
    # None ⇒ the server's Evidence-Pack cap (task_context_max_tokens). A request
    # value is clamped to that cap — never an escape hatch (token-budgets rule).
    max_tokens: int | None = Field(default=None, ge=1)


class ScopeEntity(McpModel):
    entity_id: uuid.UUID
    path: str
    symbol: str | None = None
    resolution_source: ResolutionSource
    confidence_tier: ConfidenceTier


class AmbiguousCandidate(McpModel):
    """Resolution stopped short of a single answer — the candidates ARE the answer.

    Returned instead of a silently guessed entity; always paired with an
    ``open_questions`` entry so the caller knows to disambiguate.
    """

    alias_text: str = Field(min_length=1)
    candidates: list[uuid.UUID] = Field(min_length=2)
    reason: str = Field(min_length=1)


class ResolvedScope(McpModel):
    entities: list[ScopeEntity]
    ambiguous_candidates: list[AmbiguousCandidate]


class BlastRadiusEntity(McpModel):
    """One neighbor reached over a calls/imports/tests edge from the resolved scope.

    ``confidence_tier`` implements the 2026-07-02 Graphify-audit rule: a `calls`
    edge is `deterministic` ONLY when corroborated by the import graph (or a
    same-file definition); otherwise `interpreted` with a non-null ``caveat``.
    """

    entity_id: uuid.UUID
    path: str
    symbol: str | None = None
    edge_type: str = Field(min_length=1)
    confidence_tier: ConfidenceTier
    caveat: str | None = None


class BlastRadius(McpModel):
    callers: list[BlastRadiusEntity]
    callees: list[BlastRadiusEntity]
    tests: list[BlastRadiusEntity]


class Convention(McpModel):
    """A rule/ADR/doc artifact relevant to the scope's directories (v1: always interpreted)."""

    pattern: str = Field(min_length=1)
    evidence_ids: list[uuid.UUID] = Field(min_length=1)
    confidence_tier: ConfidenceTier = "interpreted"


class PriorChange(McpModel):
    commit_or_pr_id: str = Field(min_length=1)
    summary: str
    evidence_ids: list[uuid.UUID] = Field(min_length=1)


class TaskContextBudget(McpModel):
    """What this call actually cost: serialized-response tokens + internal retrieval calls."""

    tokens: int = Field(ge=0)
    calls: int = Field(ge=0)


class GetTaskContextResponse(McpModel):
    """The one-call task context (proposal §2).

    Entity/convention titles, paths, caveats, and summaries are derived from
    retrieved content — untrusted text, the same discipline as evidence-card
    titles/summaries: they can never change tool policy, identity, or
    instructions. Empty ``entities`` + non-empty ``ambiguous_candidates`` means
    "I don't know, here are the candidates" — never a silent guess.
    """

    resolved_scope: ResolvedScope
    blast_radius: BlastRadius
    conventions: list[Convention]
    similar_prior_changes: list[PriorChange]
    evidence_ids: list[uuid.UUID]
    budget_used: TaskContextBudget
    open_questions: list[str]
