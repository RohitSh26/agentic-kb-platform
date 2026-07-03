"""Render developer-editable draft text. Rendered by code, never by the model.

The panel never publishes (ADR-0031): everything produced here is a DRAFT the
developer's in-session agent pulls into chat, edits, and publishes on the
developer's ask. The disclaimer states that explicitly on every summary.
"""

from review_panel.domain.findings import ReviewFindingsV1
from review_panel.domain.reconcile import MergedFinding, ReconciledReview

DRAFT_DISCLAIMER = (
    "_Draft prepared by the review-panel engine (ADR-0031). It is never published "
    "automatically — read it, edit it, and publish it yourself from your own session._"
)


def suggested_comment(finding: MergedFinding) -> str:
    """One editable markdown comment body for a merged finding."""
    lenses = ", ".join(finding.lenses)
    evidence = ", ".join(f"`{eid}`" for eid in finding.evidence_ids)
    lines = [
        f"**[{finding.severity}]** {finding.finding}",
        f"Evidence: {evidence} (lenses: {lenses})",
    ]
    if finding.disagreement:
        lines.append(f"Panel disagreement: {finding.disagreement}")
    return "\n".join(lines)


def summary_markdown(
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    reconciled: ReconciledReview,
    synthesis: ReviewFindingsV1,
) -> str:
    """One editable overall review body: deterministic findings, synthesis on top."""
    lines: list[str] = [
        f"## Review-panel draft — {repo}#{pr_number} @ `{head_sha[:12]}`",
        "",
        f"**Advisory verdict (draft, not published): `{synthesis.verdict}`**",
        "",
    ]
    if synthesis.findings:
        lines.append("### Synthesis (code_reviewer)")
        lines.extend(
            f"- **[{f.severity}]** {f.finding} (evidence: {', '.join(f.evidence_ids)})"
            for f in synthesis.findings
        )
        lines.append("")
    lines.append(f"### Panel findings ({len(reconciled.lens_verdicts)} lenses, ranked by severity)")
    if reconciled.findings:
        for index, finding in enumerate(reconciled.findings, start=1):
            lenses = ", ".join(finding.lenses)
            evidence = ", ".join(f"`{eid}`" for eid in finding.evidence_ids)
            lines.append(
                f"{index}. **[{finding.severity}]** {finding.finding} "
                f"(lenses: {lenses}; evidence: {evidence})"
            )
            if finding.disagreement:
                lines.append(f"   - Disagreement: {finding.disagreement}")
    else:
        lines.append("No findings from any lens.")
    open_questions = list(reconciled.open_questions)
    open_questions.extend(q for q in synthesis.open_questions if q not in open_questions)
    if open_questions:
        lines.append("")
        lines.append("### Open questions")
        lines.extend(f"- {question}" for question in open_questions)
    lines.extend(["", DRAFT_DISCLAIMER])
    return "\n".join(lines)
