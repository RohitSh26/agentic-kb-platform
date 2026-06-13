"""The baseline regression gate (harness.run_status.exit_code) must fail the run.

EV-1: the harness computed a `regressed` verdict but `run.py` always exited 0, so CI could pass
a token-cost regression. These hermetic cases pin the exit-code contract without a database.
"""

import io

from harness.baseline import BaselineComparison, Delta
from harness.records import RunRecord
from harness.run_status import CASE_FAILURE, OK, REGRESSION, exit_code


def _record(case_id: str = "c1", *, succeeded: bool = True) -> RunRecord:
    return RunRecord(
        case_id=case_id,
        task_type="retrieval_recall",
        succeeded=succeeded,
        expected_items=0,
        missing_items=(),
        total_claims=0,
        unsupported_claims=0,
        events=(),
    )


def _comparison(verdict: str) -> BaselineComparison:
    return BaselineComparison(
        present=True,
        deltas={"context_tokens_per_successful_task": Delta(old=100.0, new=140.0, relative=0.4)},
        verdict=verdict,  # type: ignore[arg-type]
        biggest_mover="context_tokens_per_successful_task",
    )


def test_regressed_verdict_fails_the_run() -> None:
    stream = io.StringIO()
    code = exit_code(
        [_record()],
        _comparison("regressed"),
        fail_on_regress=True,
        update_baseline=False,
        stream=stream,
    )
    assert code == REGRESSION
    assert "regressed" in stream.getvalue()
    assert "context_tokens_per_successful_task" in stream.getvalue()


def test_improved_flat_and_no_baseline_verdicts_pass() -> None:
    for verdict in ("improved", "flat", "no_baseline"):
        assert (
            exit_code(
                [_record()],
                _comparison(verdict),
                fail_on_regress=True,
                update_baseline=False,
                stream=io.StringIO(),
            )
            == OK
        )


def test_update_baseline_skips_the_gate() -> None:
    assert (
        exit_code(
            [_record()],
            _comparison("regressed"),
            fail_on_regress=True,
            update_baseline=True,
            stream=io.StringIO(),
        )
        == OK
    )


def test_no_fail_on_regress_opts_out() -> None:
    assert (
        exit_code(
            [_record()],
            _comparison("regressed"),
            fail_on_regress=False,
            update_baseline=False,
            stream=io.StringIO(),
        )
        == OK
    )


def test_case_failure_takes_priority_over_regression() -> None:
    stream = io.StringIO()
    code = exit_code(
        [_record("bad", succeeded=False)],
        _comparison("regressed"),
        fail_on_regress=True,
        update_baseline=False,
        stream=stream,
    )
    assert code == CASE_FAILURE
    assert "bad" in stream.getvalue()
