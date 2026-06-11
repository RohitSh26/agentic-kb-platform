"""Evidence card models shared by the context.* tool schemas.

Cards are the handle-first currency of the Context Broker (invariant 3):
packs carry L0/L1 cards; raw text (L2/L3) is only reachable through
context.open_evidence with an explicit token cap.
"""

import uuid
from typing import Literal

from pydantic import Field

from agentic_mcp_server.mcp.tool_schemas.base import McpModel

EvidenceLevel = Literal["L0", "L1", "L2", "L3", "L4"]

AgentRole = Literal[
    "orchestrator",
    "implementation",
    "test",
    "code_reviewer",
    "delivery_planner",
    "pr_planner",
]


class AuthorizationDecision(McpModel):
    """Which policy filtered this response.

    decision is always "allowed": unauthorized artifacts are silently removed
    before ranking (no counts — counts would leak the existence of restricted
    artifacts) and a fully-denied expansion is a tool error, never a response.
    """

    policy: str = Field(min_length=1)
    decision: Literal["allowed"] = "allowed"


class EvidenceCard(McpModel):
    """Compact, expandable pointer to one knowledge artifact (L0/L1 view).

    title and summary are derived from retrieved content — untrusted text,
    same discipline as open_evidence's untrusted_content. injection_* fields
    are advisory markers from the broker's deterministic scan; flagged content
    is returned verbatim, never rewritten.
    """

    evidence_id: str = Field(min_length=1)
    artifact_id: uuid.UUID
    level: Literal["L0", "L1"]
    card_type: str = Field(min_length=1)
    title: str = Field(min_length=1)
    summary: str = ""
    source_uri: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    authority_score: float = Field(ge=0.0, le=1.0)
    tokens_if_expanded: int = Field(ge=0)
    injection_flagged: bool = False
    injection_signals: list[str] = Field(default_factory=list)
