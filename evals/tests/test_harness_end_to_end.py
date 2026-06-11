"""End-to-end: cases through the real broker against a migrated registry.

Requires TEST_DATABASE_URL (make migrate-test-db); skipped otherwise so the
unit suite stays hermetic. Local runs never require Azure.
"""

import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from harness.baseline import compare
from harness.cases import EvalCase, load_cases
from harness.executor import execute_case
from harness.fixtures import (
    RegistryNotMigratedError,
    clean_registry,
    require_registry_schema,
)
from harness.metrics import compute_metrics
from harness.records import RunRecord
from harness.report import REPORT_SCHEMA_VERSION, build_report

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
EVALS_DIR = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None,
    reason="TEST_DATABASE_URL not set (needs a migrated registry: make migrate-test-db)",
)


async def _run(cases: list[EvalCase]) -> list[RunRecord]:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            try:
                await require_registry_schema(session)
            except RegistryNotMigratedError as error:
                pytest.skip(str(error))
        try:
            return [(await execute_case(case, factory)).record for case in cases]
        finally:
            async with factory() as session:
                await clean_registry(session)
    finally:
        await engine.dispose()


async def test_committed_cases_run_green_and_metrics_hold() -> None:
    cases = load_cases(EVALS_DIR / "retrieval_cases") + load_cases(EVALS_DIR / "agent_task_cases")
    records = await _run(cases)

    failed = [record.case_id for record in records if not record.succeeded]
    assert not failed, f"cases failed: {failed}"
    assert all(not record.missing_items for record in records)

    metrics = compute_metrics(records)
    assert metrics["missing_context_rate"].value == 0.0
    assert metrics["duplicate_context_tokens"].value == 0.0
    assert metrics["unsupported_claim_rate"].value == 0.0
    # case 04 scripts an exact reuse and case 05 a semantic reuse — both must register
    evidence_reuse = metrics["evidence_reuse_rate"].value
    semantic_hits = metrics["semantic_cache_hit_rate"].value
    assert evidence_reuse is not None and evidence_reuse > 0.0
    assert semantic_hits is not None and semantic_hits > 0.0

    report = build_report(records, metrics, compare(None, metrics), git_sha=None)
    assert report["schema_version"] == REPORT_SCHEMA_VERSION
    assert isinstance(report["cases"], list) and len(report["cases"]) == len(cases)
    baseline = report["baseline"]
    assert isinstance(baseline, dict) and baseline["verdict"] == "no_baseline"


def _retention_case(**overrides: Any) -> EvalCase:
    base: dict[str, Any] = {
        "id": "synthetic-path-case",
        "task_type": "retrieval_recall",
        "task": "session retention lookup",
        "approved_context_plan": "find the retention policy",
        "fixtures": {
            "artifacts": [
                {"key": "doc_a", "title": "Retention", "body_text": "retention is 90 days"}
            ],
            "search_seeds": [{"keyword": "retention", "hits": ["doc_a"]}],
        },
        "expected": {"docs": ["doc_a"]},
    }
    base.update(overrides)
    return EvalCase.model_validate(base)


async def test_unknown_evidence_claim_counts_as_unsupported() -> None:
    """The unknown: handle must fail validate_evidence_references end to end."""
    case = _retention_case(
        agent_output={
            "claims": [
                {"claim": "retention is 90 days", "evidence": ["doc_a"]},
                {"claim": "fabricated capability", "evidence": ["unknown:made-up"]},
            ]
        },
    )
    [record] = await _run([case])
    assert record.succeeded
    assert record.total_claims == 2
    assert record.unsupported_claims == 1
    assert compute_metrics([record])["unsupported_claim_rate"].value == 0.5


async def test_budget_denial_is_contractual_not_a_case_failure() -> None:
    """delivery-agent has a 1-request allowance; the second distinct question is denied."""
    questions = [
        ("what alerts exist for the retention job", "monitoring the rollout", "alert wiring"),
        ("how do webhooks report delivery problems", "failure visibility", "escalation path"),
    ]
    case = _retention_case(
        script=[
            {
                "tool": "context.request_more",
                "agent": "delivery-agent",
                "question": question,
                "why_needed": why,
                "decision_needed": decision,
            }
            for question, why, decision in questions
        ],
    )
    [record] = await _run([case])
    assert record.succeeded  # denials are contractual broker behavior, not failures
    statuses = [e.status for e in record.events if e.tool_name == "context.request_more"]
    assert statuses == ["approved", "denied"]
