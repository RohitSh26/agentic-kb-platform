"""Schemas for context.create_change_pack — the BUILD-lane context selector.

Given a change task, the broker resolves (deterministically, ranked) the small set of
files an implementer needs: the TARGET file(s), the TEST file(s) that teach the repo's
testing style, and the top dependency file(s) — each with a human reason, a NUMERIC
confidence, and an estimated token cost. The runtime opens only these files (no grep,
no walking), so it reads ~4 graph-selected files instead of 20 noisy ones.
"""

from pydantic import BaseModel, ConfigDict, Field

from agentic_mcp_server.mcp.tool_schemas.base import McpModel
from agentic_mcp_server.mcp.tool_schemas.context import RUN_ID_PATTERN


class FileRef(BaseModel):
    """One selected file, with WHY it was selected (reasons make bad packs debuggable)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)  # repo-relative path the runtime opens
    reason: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    est_tokens: int = Field(ge=0)


class ChangeContextRequest(McpModel):
    task: str = Field(min_length=1)
    target_hint: str | None = None
    budget_tokens: int = Field(default=25000, ge=1)
    # Correlation handle so the selection's retrieval_event joins the rest of the run in the
    # ledger / replay (same pattern + log-injection guard as create_pack). None ⇒ the
    # non-run sentinel "-" (selection is not strictly run-scoped).
    run_id: str | None = Field(default=None, pattern=RUN_ID_PATTERN)


class ChangeContextResponse(McpModel):
    # Tiered by the BUILD priority: target > test > dependency (the test file teaches the
    # repo's testing style and matters more than a dependency for writing code).
    target_files: list[FileRef]
    test_files: list[FileRef]
    dependency_files: list[FileRef]
    relevant_symbols: list[str]
    # Non-fatal observations (e.g. "test file resolved by naming convention, not the graph").
    notes: list[str]
