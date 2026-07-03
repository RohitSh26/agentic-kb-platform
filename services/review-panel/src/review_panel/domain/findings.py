"""review_findings_v1 — the panelist/synthesizer output schema.

Deliberately duplicated small DTO (ADR-0008): the canonical shape lives in
services/mcp-server/src/agentic_mcp_server/agent_output_schemas/review_findings_v1.py
and the cross-service agreement is docs/contracts/review-panel.md +
docs/contracts/agent-output-contracts.md. Never import across services.
"""

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from review_panel.domain.errors import ReviewerOutputError

REVIEW_FINDINGS_SCHEMA_VERSION = "1.0.0"

Severity = Literal["blocker", "major", "minor", "note"]

#: Rank order for severity: lower is more severe.
SEVERITY_RANK: dict[str, int] = {"blocker": 0, "major": 1, "minor": 2, "note": 3}

#: The four specialist lenses, in canonical order. Adding a lens is adding an entry
#: here plus its manifest in agents/ — no dispatch code changes.
PANEL_LENSES: tuple[str, ...] = ("bug", "security", "quality", "test_coverage")


class ReviewFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    severity: Severity
    finding: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class ReviewFindingsV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0.0"] = REVIEW_FINDINGS_SCHEMA_VERSION
    verdict: Literal["approve", "request_changes"]
    findings: list[ReviewFinding] = Field(default_factory=list[ReviewFinding])
    open_questions: list[str] = Field(default_factory=list)


class PanelistReview(BaseModel):
    """One specialist lens's schema-validated output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lens: str
    review: ReviewFindingsV1


_CODE_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(.*)\n\s*```\s*$", re.DOTALL)


def parse_review_findings(raw: str, *, lens: str) -> ReviewFindingsV1:
    """Parse + schema-validate a model output. Anything else fails the node.

    Schema validation is the untrusted-content gate on the OUTPUT side: an
    injected instruction that makes the model reply with prose ("APPROVED!")
    instead of review_findings_v1 JSON fails here and no draft is stored.
    """
    text = raw.strip()
    fenced = _CODE_FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReviewerOutputError(f"lens={lens} output is not JSON: {exc}") from exc
    try:
        return ReviewFindingsV1.model_validate(payload)
    except ValidationError as exc:
        raise ReviewerOutputError(f"lens={lens} output failed review_findings_v1: {exc}") from exc
