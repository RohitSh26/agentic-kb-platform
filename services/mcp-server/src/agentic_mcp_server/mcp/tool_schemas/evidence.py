"""Evidence card models shared by the context.* tool schemas.

Cards are the handle-first currency of the Context Broker (invariant 3):
packs carry L0/L1 cards; raw text (L2/L3) is only reachable through
context.open_evidence with an explicit token cap.
"""

import uuid
from typing import Annotated, Literal

from pydantic import Field

from agentic_mcp_server.mcp.tool_schemas.base import McpModel

EvidenceLevel = Literal["L0", "L1", "L2", "L3", "L4"]

# Free-form so adopting teams can bring their own roles — the broker never
# branches on it (identity binds to the authenticated session). Charset-guarded
# like run_id: the value lands verbatim in key=value audit logs, so spaces,
# newlines, '=' and quotes must stay unrepresentable (log-line forgery guard).
AgentRole = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")]


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
    # Human-readable citation (file:symbol) derived in CODE from artifact metadata. This is
    # the reference a user-facing answer should show, so raw evidence_id UUIDs never need to
    # appear in prose (ADR-0022, two-identifier rule). evidence_id stays the audit handle.
    display_citation: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    authority_score: float = Field(ge=0.0, le=1.0)
    tokens_if_expanded: int = Field(ge=0)
    injection_flagged: bool = False
    injection_signals: list[str] = Field(default_factory=list)
    # Temporal semantics (PR-33, ADR-0010/0011 phase 4). Deterministically derived
    # at read time from already-stored data — ranking/labelling signals only, never
    # a trust gate. source_kind = code/doc/card/pr/adr/other; temporal_state =
    # current/superseded; stale_for_intent = the card references a removed/absent
    # symbol under a structure-seeking intent (a routing hint, NOT primary
    # evidence). These NEVER affect the verifier's L0 not_stale check.
    source_kind: Literal["code", "doc", "card", "pr", "adr", "other"] = "other"
    temporal_state: Literal["current", "superseded"] = "current"
    stale_for_intent: bool = False
