"""Consolidated-runner result shapes (evals/run_all.py, docs/architecture/evaluation-system.md).

Pure, DB/subprocess-free — mirrors the harness.records/harness.run_status seam so report
rendering and exit-code logic are unit-testable without a database or shelling out. A `CheckResult`
is one concrete thing that ran (or didn't); a `TierResult` groups the checks that make up one tier
(T0-T4). Failures are never paraphrased: `detail` carries the verbatim captured output or exception
text for a failing check, while `reason` is a short, human-authored label (a skip reason, or a
one-line pointer like "exit 1") — see evaluation-system.md "Generate-and-test loops" for why this
distinction matters to anything that might act on a failure.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

Status = Literal["pass", "fail", "skip"]


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    # short, human-authored: why this was skipped, or a one-line pointer for a failure
    reason: str | None = None
    # verbatim captured output/exception text for a failing check — never a prose summary
    detail: str | None = None
    metrics: dict[str, str] = field(default_factory=dict[str, str])
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class TierResult:
    tier: str  # "T0".."T4"
    title: str
    checks: tuple[CheckResult, ...]

    @property
    def status(self) -> Status:
        if not self.checks:
            return "skip"
        if any(check.status == "fail" for check in self.checks):
            return "fail"
        if all(check.status == "skip" for check in self.checks):
            return "skip"
        return "pass"

    @property
    def duration_seconds(self) -> float:
        return sum(check.duration_seconds for check in self.checks)


def overall_status(tiers: Sequence[TierResult]) -> Status:
    if any(tier.status == "fail" for tier in tiers):
        return "fail"
    if all(tier.status == "skip" for tier in tiers):
        return "skip"
    return "pass"


def exit_code(tiers: Sequence[TierResult]) -> int:
    """0 unless some tier FAILED. A skipped tier (missing creds/DB, or T4/T0 by design) is
    honest degradation, not a runner failure — see evaluation-system.md "Degradation over
    failure"."""
    return 1 if overall_status(tiers) == "fail" else 0
