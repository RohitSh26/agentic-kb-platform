"""Deterministic reconciliation: merge dupes, keep disagreements, rank by severity."""

from review_panel.domain.findings import PanelistReview, ReviewFinding, ReviewFindingsV1
from review_panel.domain.reconcile import reconcile


def _panelist(lens: str, *findings: ReviewFinding, questions: list[str] | None = None):
    return PanelistReview(
        lens=lens,
        review=ReviewFindingsV1(
            verdict="request_changes",
            findings=list(findings),
            open_questions=questions or [],
        ),
    )


def _finding(severity: str, text: str, evidence: list[str]) -> ReviewFinding:
    return ReviewFinding.model_validate(
        {"severity": severity, "finding": text, "evidence_ids": evidence}
    )


def test_duplicate_findings_merge_into_one_with_union_evidence() -> None:
    result = reconcile(
        [
            _panelist(
                "bug", _finding("major", "Race condition in cache write path", ["src/cache.py:42"])
            ),
            _panelist(
                "security",
                _finding("major", "race condition in cache write path", ["src/cache.py:40"]),
            ),
        ]
    )
    assert len(result.findings) == 1
    merged = result.findings[0]
    assert merged.lenses == ["bug", "security"]
    assert merged.evidence_ids == ["src/cache.py:40", "src/cache.py:42"]
    assert merged.disagreement is None  # same severity: agreement, not dispute


def test_severity_disagreement_is_kept_explicit_and_highest_wins() -> None:
    result = reconcile(
        [
            _panelist(
                "bug", _finding("major", "Race condition in cache write path", ["src/cache.py:42"])
            ),
            _panelist(
                "security",
                _finding("minor", "race condition in cache write path", ["src/cache.py:42"]),
            ),
        ]
    )
    merged = result.findings[0]
    assert merged.severity == "major"
    assert merged.disagreement is not None
    assert "bug=major" in merged.disagreement
    assert "security=minor" in merged.disagreement


def test_distinct_findings_stay_separate_and_rank_by_severity() -> None:
    result = reconcile(
        [
            _panelist("quality", _finding("note", "Helper name hides intent", ["src/a.py:1"])),
            _panelist("security", _finding("blocker", "SQL injection in search", ["src/b.py:2"])),
            _panelist("bug", _finding("major", "Off-by-one in pagination", ["src/c.py:3"])),
        ]
    )
    assert [f.severity for f in result.findings] == ["blocker", "major", "note"]
    assert len(result.findings) == 3


def test_merged_text_comes_from_the_most_severe_instance() -> None:
    result = reconcile(
        [
            _panelist(
                "security",
                _finding("blocker", "Race condition in cache write PATH", ["src/cache.py:42"]),
            ),
            _panelist(
                "bug", _finding("minor", "race condition in cache write path", ["src/cache.py:42"])
            ),
        ]
    )
    merged = result.findings[0]
    assert merged.severity == "blocker"
    assert merged.finding == "Race condition in cache write PATH"


def test_open_questions_deduped_and_lens_verdicts_collected() -> None:
    result = reconcile(
        [
            _panelist("bug", questions=["Is retry safe?"]),
            _panelist("test_coverage", questions=["Is retry safe?", "Any e2e coverage?"]),
        ]
    )
    assert result.open_questions == ["Is retry safe?", "Any e2e coverage?"]
    assert result.lens_verdicts == {
        "bug": "request_changes",
        "test_coverage": "request_changes",
    }


def test_no_findings_reconciles_to_empty() -> None:
    result = reconcile([_panelist("bug"), _panelist("quality")])
    assert result.findings == []
