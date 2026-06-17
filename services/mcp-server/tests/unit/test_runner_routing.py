"""Conformance: the runner's intent router is DETERMINISTIC code (ADR-0022).

A question can never be routed into the build pipeline, and a build request always
takes the build lane. This is the guarantee the prompt-routed clients cannot make,
so it must never silently regress.
"""

import importlib.util
from pathlib import Path

import pytest

_RUNNER = Path(__file__).resolve().parents[4] / "scripts" / "agent_runner.py"


def _load():
    spec = importlib.util.spec_from_file_location("agent_runner", _RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load()


@pytest.mark.parametrize(
    "task",
    [
        "Explain how we use graphify to create code graphs",  # the prompt that started this
        "How does the Context Broker enforce a per-agent token budget?",
        "Where is parse_agent_allowances defined?",
        "Why does create_pack trim cards?",
        "Summarize the verifier ladder",
        "What depends on the GraphifyGraphifier?",
        # ambiguous asks default to read-only first (judge's rule)
        "How would we fix the 404 on private repos?",
        "Can you look into the wiki filtering issue?",
    ],
)
def test_questions_route_to_read_explain(task: str) -> None:
    assert runner.classify_intent(task) == runner.READ_EXPLAIN


@pytest.mark.parametrize(
    "task",
    [
        "Add input validation to the GitHub connector",
        "Fix the ADO wiki path bug",
        "Refactor the verifier into smaller modules",
        "Write tests for the config validator",
        "Implement a retry on transient 404s",
        "Rename display_citation to source_ref",
        "Update the orchestrator manifest",
    ],
)
def test_change_requests_route_to_build_change(task: str) -> None:
    assert runner.classify_intent(task) == runner.BUILD_CHANGE


def test_evidence_for_explain_uses_citations_not_uuids() -> None:
    cards = [
        {
            "evidence_id": "1111-uuid",
            "display_citation": "budgets.py:parse_agent_allowances",
            "summary": "parses per-agent allowances",
        },
        {
            "evidence_id": "2222-uuid",
            "title": "ReadPackRequest",
            "summary": "the read_pack request",
        },
    ]
    rendered = runner._evidence_for_explain(cards)
    assert "budgets.py:parse_agent_allowances" in rendered
    assert "1111-uuid" not in rendered  # the audit handle never appears in explain evidence
    assert "2222-uuid" not in rendered
