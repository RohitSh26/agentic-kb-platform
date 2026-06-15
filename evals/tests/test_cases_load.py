"""Case loading: committed YAMLs parse, coverage holds, loader validators fire."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from harness.cases import BENCHMARK_TASK_TYPES, EvalCase, load_cases

EVALS_DIR = Path(__file__).resolve().parent.parent


def case_dict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "sample-case",
        "task_type": "retrieval_recall",
        "task": "session retention lookup",
        "approved_context_plan": "find the retention policy",
        "fixtures": {
            "artifacts": [{"key": "doc_a", "title": "Doc A", "body_text": "retention is 90 days"}],
            "search_seeds": [{"keyword": "retention", "hits": ["doc_a"]}],
        },
        "expected": {"docs": ["doc_a"]},
    }
    base.update(overrides)
    return base


def test_all_committed_cases_load_with_unique_ids() -> None:
    retrieval = load_cases(EVALS_DIR / "retrieval_cases")
    agent_task = load_cases(EVALS_DIR / "agent_task_cases")
    assert len(retrieval) >= 3
    assert len(agent_task) == len(BENCHMARK_TASK_TYPES)
    ids = [case.id for case in retrieval + agent_task]
    assert len(ids) == len(set(ids))


def test_agent_task_cases_cover_all_six_benchmark_types() -> None:
    covered = {case.task_type for case in load_cases(EVALS_DIR / "agent_task_cases")}
    assert covered == set(BENCHMARK_TASK_TYPES)


def test_minimal_case_validates() -> None:
    case = EvalCase.model_validate(case_dict())
    assert case.budget_tokens == 8000
    assert case.fixtures.artifacts[0].knowledge_kind == "source_backed"


def test_unknown_fixture_key_is_rejected() -> None:
    raw = case_dict(expected={"docs": ["doc_a", "ghost"]})
    with pytest.raises(ValidationError, match=r"unknown fixture keys.*ghost"):
        EvalCase.model_validate(raw)


def test_duplicate_artifact_keys_are_rejected() -> None:
    raw = case_dict()
    raw["fixtures"]["artifacts"] = raw["fixtures"]["artifacts"] * 2
    with pytest.raises(ValidationError, match="duplicate fixture artifact keys"):
        EvalCase.model_validate(raw)


def test_unreachable_search_seed_is_rejected() -> None:
    raw = case_dict()
    raw["fixtures"]["search_seeds"] = [{"keyword": "webhooks", "hits": ["doc_a"]}]
    with pytest.raises(ValidationError, match=r"never queried.*webhooks"):
        EvalCase.model_validate(raw)


def test_script_question_makes_a_seed_reachable() -> None:
    raw = case_dict()
    raw["fixtures"]["search_seeds"].append({"keyword": "purge", "hits": ["doc_a"]})
    raw["script"] = [
        {
            "tool": "context.request_more",
            "agent": "impl-agent",
            "question": "when does the purge job run",
            "why_needed": "plan depends on purge timing",
            "decision_needed": "schedule of cleanup",
        }
    ]
    EvalCase.model_validate(raw)


def test_unknown_agent_name_is_rejected() -> None:
    raw = case_dict()
    raw["script"] = [
        {
            "tool": "context.open_evidence",
            "agent": "impl_agent",  # typo: underscore instead of hyphen
            "evidence": "doc_a",
        }
    ]
    with pytest.raises(ValidationError, match="impl_agent"):
        EvalCase.model_validate(raw)


def test_executor_allowances_cover_every_scriptable_agent() -> None:
    from typing import get_args

    from harness.cases import AgentName
    from harness.executor import AGENT_ALLOWANCES

    assert set(AGENT_ALLOWANCES) == set(get_args(AgentName))


def test_unknown_evidence_handles_skip_fixture_check() -> None:
    raw = case_dict()
    raw["agent_output"] = {"claims": [{"claim": "made up", "evidence": ["unknown:fabricated"]}]}
    EvalCase.model_validate(raw)


def test_verify_answer_step_parses_and_references_fixtures() -> None:
    raw = case_dict()
    raw["script"] = [
        {
            "tool": "context.verify_answer",
            "agent": "impl-agent",
            "answer_id": "ans-1",
            "claim": "retention is 90 days",
            "evidence": ["doc_a"],
            "retrieved": ["doc_a"],
            "expect_overall": "passed",
        }
    ]
    case = EvalCase.model_validate(raw)
    assert case.script[0].tool == "context.verify_answer"  # type: ignore[union-attr]


def test_platform_trust_step_parses_with_default_status() -> None:
    raw = case_dict()
    raw["script"] = [
        {
            "tool": "context.platform_trust",
            "agent": "impl-agent",
            "verification_required": True,
            "present_receipt": False,
            "expect_status": "denied",
        }
    ]
    case = EvalCase.model_validate(raw)
    assert case.script[0].verification_required is True  # type: ignore[union-attr]


def test_verify_answer_step_evidence_must_reference_a_fixture() -> None:
    raw = case_dict()
    raw["script"] = [
        {
            "tool": "context.verify_answer",
            "agent": "impl-agent",
            "answer_id": "ans-1",
            "claim": "fabricated",
            "evidence": ["ghost"],
            "expect_overall": "failed",
        }
    ]
    with pytest.raises(ValidationError, match=r"unknown fixture keys.*ghost"):
        EvalCase.model_validate(raw)


def test_must_not_leak_must_reference_a_fixture() -> None:
    raw = case_dict(must_not_leak=["ghost"])
    with pytest.raises(ValidationError, match=r"unknown fixture keys.*ghost"):
        EvalCase.model_validate(raw)


def test_requester_teams_and_acl_teams_default_empty() -> None:
    case = EvalCase.model_validate(case_dict())
    assert case.requester_teams == []
    assert case.must_not_leak == []
    assert case.fixtures.artifacts[0].acl_teams == []


def test_acl_teams_on_artifact_parses() -> None:
    raw = case_dict()
    raw["fixtures"]["artifacts"][0]["acl_teams"] = ["security"]
    case = EvalCase.model_validate(raw)
    assert case.fixtures.artifacts[0].acl_teams == ["security"]


def test_duplicate_case_ids_across_files_are_rejected(tmp_path: Path) -> None:
    import yaml

    for name in ("a.yaml", "b.yaml"):
        (tmp_path / name).write_text(yaml.safe_dump(case_dict()), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate case id"):
        load_cases(tmp_path)
