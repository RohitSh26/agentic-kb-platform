"""review_draft_v1 assembly: key format, merged questions, provenance, round-trip."""

from panel_test_support import make_pr

from review_panel.domain.draft import (
    ENGINE_NAME,
    ENGINE_VERSION,
    ReviewDraft,
    build_draft,
    draft_key,
)
from review_panel.domain.findings import PANEL_LENSES, ReviewFindingsV1
from review_panel.domain.reconcile import MergedFinding, ReconciledReview
from review_panel.domain.render import DRAFT_DISCLAIMER


def _reconciled() -> ReconciledReview:
    return ReconciledReview(
        findings=[
            MergedFinding(
                severity="major",
                finding="Race condition in cache write",
                evidence_ids=["src/cache.py:42"],
                lenses=["bug", "security"],
                disagreement="severity disputed (bug=major, security=minor); highest kept",
            )
        ],
        open_questions=["Is retry safe?", "Shared question"],
        lens_verdicts={"bug": "request_changes", "security": "request_changes"},
    )


def _synthesis() -> ReviewFindingsV1:
    return ReviewFindingsV1(
        verdict="request_changes",
        open_questions=["Shared question", "Synth-only question"],
    )


def _draft() -> ReviewDraft:
    return build_draft(
        pr=make_pr(),
        reconciled=_reconciled(),
        synthesis=_synthesis(),
        model="fake:panel-test",
        kb_used=True,
    )


def test_draft_key_format_is_the_contract_key() -> None:
    assert draft_key("acme/platform", 7, "abc") == "acme/platform#7@abc"
    pr = make_pr()
    assert _draft().draft_key == f"{pr.repo}#{pr.number}@{pr.head_sha}"


def test_findings_carry_editable_suggested_comments() -> None:
    draft = _draft()
    assert len(draft.findings) == 1
    finding = draft.findings[0]
    assert finding.suggested_comment.startswith("**[major]**")
    assert finding.lenses == ["bug", "security"]
    assert finding.disagreement is not None


def test_open_questions_merge_panel_and_synthesizer_deduped() -> None:
    assert _draft().open_questions == ["Is retry safe?", "Shared question", "Synth-only question"]


def test_provenance_names_engine_model_and_lenses() -> None:
    provenance = _draft().provenance
    assert provenance.engine == ENGINE_NAME
    assert provenance.engine_version == ENGINE_VERSION
    assert provenance.model == "fake:panel-test"
    assert provenance.lenses == list(PANEL_LENSES)
    assert provenance.kb_used is True


def test_summary_is_draft_labelled_and_verdict_advisory() -> None:
    draft = _draft()
    assert draft.advisory_verdict == "request_changes"
    assert DRAFT_DISCLAIMER in draft.summary_markdown


def test_draft_round_trips_through_json() -> None:
    """What goes into the jsonb column must come back identical (contract)."""
    draft = _draft()
    assert ReviewDraft.model_validate(draft.model_dump(mode="json")) == draft
