"""review_draft_v1 — the persisted draft document (docs/contracts/review-panel.md).

The draft is what the developer's in-session agent pulls into chat, edits with
the developer, and publishes only on the developer's ask (ADR-0031). It is a
value object assembled deterministically from the reconciled panel output; the
model contributes findings and the advisory verdict, never the document shape.
"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from review_panel.domain.findings import PANEL_LENSES, ReviewFindingsV1, Severity
from review_panel.domain.pr import PRContext
from review_panel.domain.reconcile import ReconciledReview
from review_panel.domain.render import suggested_comment, summary_markdown

REVIEW_DRAFT_SCHEMA_VERSION = "1.0.0"
ENGINE_NAME = "review-panel"
ENGINE_VERSION = "0.1.0"


def draft_key(repo: str, pr_number: int, head_sha: str) -> str:
    """Draft identity AND checkpoint thread id (contract): <repo>#<pr>@<head_sha>."""
    return f"{repo}#{pr_number}@{head_sha}"


class DraftFinding(BaseModel):
    """One reconciled finding plus its developer-editable suggested comment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    severity: Severity
    finding: str
    evidence_ids: list[str]
    lenses: list[str]
    disagreement: str | None = None
    suggested_comment: str


class DraftProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    engine: str = ENGINE_NAME
    engine_version: str = ENGINE_VERSION
    model: str
    lenses: list[str] = Field(default_factory=lambda: list(PANEL_LENSES))
    kb_used: bool = False


class ReviewDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0.0"] = REVIEW_DRAFT_SCHEMA_VERSION
    draft_key: str
    repo: str
    pr_number: int
    head_sha: str
    generated_at: datetime
    advisory_verdict: Literal["approve", "request_changes"]
    lens_verdicts: dict[str, str]
    findings: list[DraftFinding]
    open_questions: list[str]
    synthesis: ReviewFindingsV1
    summary_markdown: str
    provenance: DraftProvenance


def build_draft(
    *,
    pr: PRContext,
    reconciled: ReconciledReview,
    synthesis: ReviewFindingsV1,
    model: str,
    kb_used: bool,
) -> ReviewDraft:
    """Assemble the draft document. Deterministic given its inputs (bar the timestamp)."""
    open_questions = list(reconciled.open_questions)
    open_questions.extend(q for q in synthesis.open_questions if q not in open_questions)
    return ReviewDraft(
        draft_key=draft_key(pr.repo, pr.number, pr.head_sha),
        repo=pr.repo,
        pr_number=pr.number,
        head_sha=pr.head_sha,
        generated_at=datetime.now(UTC),
        advisory_verdict=synthesis.verdict,
        lens_verdicts=dict(reconciled.lens_verdicts),
        findings=[
            DraftFinding(
                severity=finding.severity,
                finding=finding.finding,
                evidence_ids=list(finding.evidence_ids),
                lenses=list(finding.lenses),
                disagreement=finding.disagreement,
                suggested_comment=suggested_comment(finding),
            )
            for finding in reconciled.findings
        ],
        open_questions=open_questions,
        synthesis=synthesis,
        summary_markdown=summary_markdown(
            repo=pr.repo,
            pr_number=pr.number,
            head_sha=pr.head_sha,
            reconciled=reconciled,
            synthesis=synthesis,
        ),
        provenance=DraftProvenance(model=model, kb_used=kb_used),
    )
