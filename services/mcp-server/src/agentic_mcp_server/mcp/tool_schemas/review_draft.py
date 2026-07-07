"""Request/response schemas for the ``get_review_draft`` tool (PR-41, ADR-0031).

Read-only, compute-never: this fetches a draft the review-panel service
already computed and stored in its own `review_panel` Postgres schema — it
never triggers computation and carries no `kb_search`-style budget charge
(fetching a stored draft is not knowledge retrieval, docs/contracts/
review-panel.md "Fetching drafts over MCP").
"""

from datetime import datetime
from typing import Any

from pydantic import Field

from agentic_mcp_server.mcp.tool_schemas.base import McpModel
from agentic_mcp_server.mcp.tool_schemas.context import RUN_ID_PATTERN

#: "owner/name" — the GitHub repo slug the review-panel's draft_key embeds
#: (review-panel.md). Charset-guarded like RUN_ID_PATTERN: the value lands
#: verbatim in structured logs and the retrieval_event ledger `details`.
REPO_PATTERN = r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$"


class GetReviewDraftRequest(McpModel):
    """``head_sha`` omitted ⇒ the newest stored draft for ``(repo, pr_number)``.

    This tool never calls GitHub to resolve a PR's *current* head SHA (unlike
    the review-panel CLI, review-panel.md "Draft retrieval") — it is Postgres
    read-only, compute-never, by construction.
    """

    repo: str = Field(pattern=REPO_PATTERN)
    pr_number: int = Field(ge=1)
    head_sha: str | None = Field(default=None, pattern=RUN_ID_PATTERN)


class ReviewDraftRecord(McpModel):
    """The stored row (review-panel.md's Draft table), returned intact.

    ``draft`` is the review-panel-owned `review_draft_v1` JSON document,
    passed through verbatim — untrusted retrieved content, the same
    discipline as every other card/snippet field in this contract. mcp-server
    does not parse, validate, or reshape it; that schema stays owned by
    review-panel and is kept in sync only through the contract, never by
    import (ADR-0008).
    """

    draft_key: str
    repo: str
    pr_number: int
    head_sha: str
    created_at: datetime
    draft: dict[str, Any]


class GetReviewDraftResponse(McpModel):
    """``found=False`` is the clean not-found envelope — never a tool error."""

    found: bool
    draft: ReviewDraftRecord | None = None
