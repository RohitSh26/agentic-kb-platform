"""Deterministic panel reconciliation: merge duplicates, keep disagreements, rank.

This is code, not prompt (invariant 3's spirit: guarantees live in code). The
code_reviewer model call layers an advisory verdict and synthesis ON TOP of this
result; it can never drop or soften a panelist finding, because the posted review
is rendered from the deterministic merge, not from model output.
"""

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from review_panel.domain.findings import SEVERITY_RANK, PanelistReview, Severity

#: Token-set Jaccard similarity at or above which two findings are duplicates.
_DUPLICATE_SIMILARITY = 0.7


class MergedFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    severity: Severity
    finding: str
    evidence_ids: list[str]
    lenses: list[str]
    #: Set when panelists agreed the issue exists but disagreed on severity —
    #: surfaced explicitly rather than silently resolved (code_reviewer charter).
    disagreement: str | None = None


class ReconciledReview(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: list[MergedFinding] = Field(default_factory=list[MergedFinding])
    open_questions: list[str] = Field(default_factory=list)
    lens_verdicts: dict[str, str] = Field(default_factory=dict[str, str])


def _normalize_tokens(text: str) -> frozenset[str]:
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in text.lower())
    return frozenset(token for token in cleaned.split() if len(token) > 1)


def _is_duplicate(a: frozenset[str], b: frozenset[str]) -> bool:
    if not a or not b:
        return False
    union = len(a | b)
    return union > 0 and len(a & b) / union >= _DUPLICATE_SIMILARITY


class _Group:
    """One merged finding under construction (mutable working state)."""

    def __init__(self, lens: str, severity: Severity, finding: str, evidence: list[str]) -> None:
        self.tokens = _normalize_tokens(finding)
        self.severities: dict[str, Severity] = {lens: severity}
        self.texts: list[tuple[Severity, str]] = [(severity, finding)]
        self.evidence: set[str] = set(evidence)

    def absorb(self, lens: str, severity: Severity, finding: str, evidence: list[str]) -> None:
        self.severities[lens] = severity
        self.texts.append((severity, finding))
        self.evidence.update(evidence)
        self.tokens = self.tokens | _normalize_tokens(finding)

    def merged(self) -> MergedFinding:
        top_severity = min(self.severities.values(), key=lambda s: SEVERITY_RANK[s])
        # keep the strongest evidence's wording: the text of the most severe instance
        text = min(self.texts, key=lambda pair: SEVERITY_RANK[pair[0]])[1]
        distinct = sorted(set(self.severities.values()), key=lambda s: SEVERITY_RANK[s])
        disagreement = None
        if len(distinct) > 1:
            votes = ", ".join(
                f"{lens}={severity}" for lens, severity in sorted(self.severities.items())
            )
            disagreement = f"severity disputed ({votes}); highest kept"
        return MergedFinding(
            severity=top_severity,
            finding=text,
            evidence_ids=sorted(self.evidence),
            lenses=sorted(self.severities),
            disagreement=disagreement,
        )


def reconcile(reviews: Sequence[PanelistReview]) -> ReconciledReview:
    """Merge duplicate findings across lenses, keep disagreements explicit, rank by severity."""
    groups: list[_Group] = []
    open_questions: list[str] = []
    lens_verdicts: dict[str, str] = {}
    for panelist in reviews:
        lens_verdicts[panelist.lens] = panelist.review.verdict
        for question in panelist.review.open_questions:
            if question not in open_questions:
                open_questions.append(question)
        for finding in panelist.review.findings:
            tokens = _normalize_tokens(finding.finding)
            group = next((g for g in groups if _is_duplicate(g.tokens, tokens)), None)
            if group is None:
                groups.append(
                    _Group(panelist.lens, finding.severity, finding.finding, finding.evidence_ids)
                )
            else:
                group.absorb(panelist.lens, finding.severity, finding.finding, finding.evidence_ids)
    merged = sorted(
        (group.merged() for group in groups),
        key=lambda f: SEVERITY_RANK[f.severity],
    )
    return ReconciledReview(
        findings=merged,
        open_questions=open_questions,
        lens_verdicts=dict(sorted(lens_verdicts.items())),
    )
