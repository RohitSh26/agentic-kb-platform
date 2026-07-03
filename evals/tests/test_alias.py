"""Alias golden-set case shape + top-1 metrics (PR-38, docs/contracts/alias-reference.md).

top1_hit / aggregate are the pure DB-free scoring seam scripts/eval_alias_resolution.py
feeds real resolver output into; these tests pin the metric and the seed set loading.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.alias import AliasCase, AliasResult, aggregate, load_alias_suite, top1_hit

EVALS_DIR = Path(__file__).resolve().parent.parent
ALIAS_GOLDEN_PATH = EVALS_DIR / "retrieval_cases" / "alias_golden_v1.yaml"


def _case(**overrides: object) -> AliasCase:
    base: dict[str, object] = {
        "id": "sample-case",
        "query": "the durable cache fix",
        "expected_targets": ["services/kb-builder/.../durable_output_cache.py"],
        "provenance": "commit abc123",
    }
    base.update(overrides)
    return AliasCase.model_validate(base)


def test_top1_hit_when_first_target_matches() -> None:
    case = _case(expected_targets=["a.py", "b.py"])
    result = AliasResult(case=case, resolved_targets=("a.py", "c.py"))
    assert top1_hit(result) is True


def test_top1_miss_when_first_target_wrong() -> None:
    case = _case(expected_targets=["a.py"])
    result = AliasResult(case=case, resolved_targets=("wrong.py", "a.py"))
    assert top1_hit(result) is False


def test_top1_miss_on_resolver_miss() -> None:
    case = _case()
    result = AliasResult(case=case, resolved_targets=())
    assert top1_hit(result) is False


def test_aggregate_computes_top1_accuracy_and_lists_misses() -> None:
    hit = AliasResult(case=_case(id="c-hit", expected_targets=["a.py"]), resolved_targets=("a.py",))
    miss = AliasResult(case=_case(id="c-miss", expected_targets=["a.py"]), resolved_targets=())
    report = aggregate([hit, miss])
    assert report.cases == 2
    assert report.hits == 1
    assert report.top1_accuracy == 0.5
    assert report.misses == ("c-miss",)


def test_aggregate_empty_is_zero_not_error() -> None:
    report = aggregate([])
    assert report.cases == 0
    assert report.top1_accuracy == 0.0
    assert report.misses == ()


def test_expected_targets_cannot_be_empty() -> None:
    with pytest.raises(ValidationError):
        _case(expected_targets=[])


def test_seed_golden_suite_loads_with_25_unique_cases() -> None:
    suite = load_alias_suite(ALIAS_GOLDEN_PATH)
    assert suite.suite == "alias_golden_v1"
    assert len(suite.cases) == 25
    ids = [c.id for c in suite.cases]
    assert len(ids) == len(set(ids))
    assert all(c.expected_targets for c in suite.cases)
