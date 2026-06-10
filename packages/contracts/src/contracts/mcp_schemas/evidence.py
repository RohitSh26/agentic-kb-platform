"""Evidence card models shared by the context.* tool schemas.

Cards are the handle-first currency of the Context Broker (invariant 3):
packs carry L0/L1 cards; raw text (L2/L3) is only reachable through
context.open_evidence with an explicit token cap.
"""

import uuid
from typing import Literal

from pydantic import Field

from contracts.mcp_schemas.base import McpModel

EvidenceLevel = Literal["L0", "L1", "L2", "L3", "L4"]

AgentRole = Literal[
    "orchestrator",
    "implementation",
    "test",
    "code_reviewer",
    "delivery_planner",
    "pr_planner",
]


class EvidenceCard(McpModel):
    """Compact, expandable pointer to one knowledge artifact (L0/L1 view)."""

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
