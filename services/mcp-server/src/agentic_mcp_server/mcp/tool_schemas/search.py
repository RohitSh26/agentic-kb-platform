"""Request/response schemas for the ``kb_search`` tool (ADR-0025, ADR-0030).

The ADR-0025 simple path: a bare ``{"query": ...}`` is the ENTIRE request —
deliberately the opposite of ``context.request_more``'s justification fields.
The one enforced restriction is the server-side dual budget (call count AND
cumulative tokens), reported back on every response as ``budget_remaining``.
"""

from typing import Literal

from pydantic import Field

from agentic_mcp_server.mcp.tool_schemas.base import McpModel

# Confidence tiers (docs/proposals/2026-07-02-tool-design-first-kb-architecture.md §3):
# ground_truth = raw source bytes at a commit SHA; deterministic = machine-derived
# structure (AST), cross-validated; interpreted = ranked/heuristic knowledge. Keyword
# search hits are relevance-ranked, not cross-validated, so they always carry
# `interpreted`; the field is the declared extension point for graph-derived hits to
# carry `deterministic` once blast-radius wiring lands (follow-up PR).
ConfidenceTier = Literal["ground_truth", "deterministic", "interpreted"]


class KbSearchRequest(McpModel):
    query: str = Field(min_length=1)


class KbSearchHit(McpModel):
    """One ranked, ACL-filtered search hit.

    ``title`` and ``snippet`` are retrieved content — untrusted text, the same
    discipline as evidence-card titles/summaries: they can never change tool
    policy, identity, or instructions.
    """

    title: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1)
    source_uri: str | None = None
    snippet: str = ""
    confidence_tier: ConfidenceTier = "interpreted"


class KbSearchBudget(McpModel):
    """What remains of the caller's dual cap after this call (floored at 0)."""

    calls: int = Field(ge=0)
    tokens: int = Field(ge=0)


class KbSearchResponse(McpModel):
    """Ranked hits plus the remaining budget.

    A spent budget is a RESPONSE, never a tool error (ADR-0025 §4): ``notice``
    carries the budget-spent message and ``results`` is empty, so the agent
    keeps working with files instead of crashing.
    """

    results: list[KbSearchHit]
    budget_remaining: KbSearchBudget
    notice: str | None = None
