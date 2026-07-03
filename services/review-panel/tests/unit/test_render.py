"""Draft rendering: nothing dropped, ranked, disagreement surfaced, draft-labelled."""

from review_panel.domain.findings import ReviewFindingsV1
from review_panel.domain.reconcile import MergedFinding, ReconciledReview
from review_panel.domain.render import DRAFT_DISCLAIMER, suggested_comment, summary_markdown

HEAD = "abc123def456"


def _reconciled() -> ReconciledReview:
    return ReconciledReview(
        findings=[
            MergedFinding(
                severity="blocker",
                finding="SQL injection in search",
                evidence_ids=["src/b.py:2"],
                lenses=["security"],
            ),
            MergedFinding(
                severity="major",
                finding="Race condition in cache write",
                evidence_ids=["src/cache.py:42"],
                lenses=["bug", "security"],
                disagreement="severity disputed (bug=major, security=minor); highest kept",
            ),
        ],
        open_questions=["Is retry safe?"],
        lens_verdicts={"bug": "request_changes", "security": "request_changes"},
    )


def _synthesis() -> ReviewFindingsV1:
    return ReviewFindingsV1.model_validate(
        {
            "schema_version": "1.0.0",
            "verdict": "request_changes",
            "findings": [
                {
                    "severity": "blocker",
                    "finding": "Fix injection before merge",
                    "evidence_ids": ["src/b.py:2"],
                }
            ],
            "open_questions": ["Was the query builder reviewed before?"],
        }
    )


def _summary() -> str:
    return summary_markdown(
        repo="acme/platform",
        pr_number=7,
        head_sha=HEAD,
        reconciled=_reconciled(),
        synthesis=_synthesis(),
    )


def test_every_merged_finding_is_rendered_ranked_with_disagreement() -> None:
    body = _summary()
    assert body.index("SQL injection in search") < body.index("Race condition in cache write")
    assert "Disagreement: severity disputed (bug=major, security=minor); highest kept" in body
    assert "lenses: bug, security" in body


def test_open_questions_from_panel_and_synthesizer_are_merged() -> None:
    body = _summary()
    assert "Is retry safe?" in body
    assert "Was the query builder reviewed before?" in body


def test_summary_is_draft_labelled_and_verdict_advisory() -> None:
    body = _summary()
    assert DRAFT_DISCLAIMER in body
    assert "Advisory verdict (draft, not published): `request_changes`" in body


def test_no_findings_renders_explicit_empty_state() -> None:
    body = summary_markdown(
        repo="acme/platform",
        pr_number=7,
        head_sha=HEAD,
        reconciled=ReconciledReview(lens_verdicts={"bug": "approve"}),
        synthesis=ReviewFindingsV1(verdict="approve"),
    )
    assert "No findings from any lens." in body
    assert DRAFT_DISCLAIMER in body


def test_suggested_comment_carries_severity_evidence_and_disagreement() -> None:
    finding = _reconciled().findings[1]
    comment = suggested_comment(finding)
    assert comment.startswith("**[major]** Race condition in cache write")
    assert "`src/cache.py:42`" in comment
    assert "lenses: bug, security" in comment
    assert "Panel disagreement: severity disputed" in comment


def test_suggested_comment_without_disagreement_has_no_disagreement_line() -> None:
    comment = suggested_comment(_reconciled().findings[0])
    assert "disagreement" not in comment.lower()
