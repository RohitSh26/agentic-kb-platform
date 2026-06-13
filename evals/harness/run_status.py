"""Translate run records + baseline comparison into a process exit code.

The harness has always *computed* a `regressed` verdict (docs/contracts/evals-report.md);
this module is the enforcement half — it makes a regression fail the run so CI cannot pass a
token-cost regression. Kept DB-free and out of `run.py` so it is unit-testable from `harness`.
"""

import sys
from collections.abc import Sequence
from typing import TextIO

from harness.baseline import BaselineComparison
from harness.records import RunRecord

OK = 0
CASE_FAILURE = 1
REGRESSION = 3


def exit_code(
    records: Sequence[RunRecord],
    comparison: BaselineComparison,
    *,
    fail_on_regress: bool,
    update_baseline: bool,
    stream: TextIO = sys.stderr,
) -> int:
    """Return the process exit code for a completed eval run.

    `0` ok · `1` a case failed · `3` the baseline regressed. Case failures take priority.
    The regression gate is skipped under `--update-baseline` (a new baseline is being accepted
    on purpose) or when `fail_on_regress` is off (report-only).
    """
    failed = [record.case_id for record in records if not record.succeeded]
    if failed:
        print(f"failed cases: {', '.join(failed)}", file=stream)
        return CASE_FAILURE

    if fail_on_regress and not update_baseline and comparison.verdict == "regressed":
        mover = comparison.biggest_mover
        delta = comparison.deltas.get(mover) if mover is not None else None
        detail = (
            f" (biggest mover: {mover} {delta.old:.4g} -> {delta.new:.4g})"
            if delta is not None
            else ""
        )
        print(
            f"baseline verdict: regressed{detail}; failing the run "
            "(pass --no-fail-on-regress to report only, or --update-baseline to accept)",
            file=stream,
        )
        return REGRESSION

    return OK
