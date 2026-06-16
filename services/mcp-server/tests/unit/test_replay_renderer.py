"""Unit tests for the replay timeline renderer.

Tests the pure formatting function ``render_timeline`` without any DB or CLI
involvement. Covers: create_pack, expand, open_evidence, graph.get_neighbors,
verify_answer, governance.checkpoint, and the empty-rows edge case.
"""

import uuid
from datetime import UTC, datetime, timedelta

from agentic_mcp_server.infrastructure.postgres.retrieval_events import ReplayEventRow
from agentic_mcp_server.replay import render_timeline


def _row(
    tool_name: str,
    status: str = "approved",
    tokens: int = 0,
    latency_ms: int | None = None,
    details: dict | None = None,  # type: ignore[type-arg]
    created_at: datetime | None = None,
    offset_seconds: float = 0.0,
) -> ReplayEventRow:
    base = datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)
    ts = created_at or base
    if offset_seconds:
        ts = base + timedelta(seconds=offset_seconds)
    return ReplayEventRow(
        retrieval_id=uuid.uuid4(),
        run_id="run-test",
        kb_version="v1",
        agent_name="impl-agent",
        tool_name=tool_name,
        status=status,
        cache_hit=False,
        tokens_returned=tokens,
        reused_evidence_ids=[],
        new_evidence_ids=[],
        details=details,
        created_at=ts,
        latency_ms=latency_ms,
    )


def test_render_timeline_empty_returns_empty_list() -> None:
    assert render_timeline([]) == []


def test_render_timeline_single_row_no_details() -> None:
    rows = [_row("context.create_pack", tokens=500, latency_ms=120)]
    lines = render_timeline(rows)
    assert len(lines) == 1
    line = lines[0]
    assert "context.create_pack" in line
    assert "[approved]" in line
    assert "500tok" in line
    assert "120ms" in line


def test_render_timeline_create_pack_details() -> None:
    details = {
        "task": "auth service",
        "candidates_considered": 3,
        "cards": [
            {
                "artifact_id": "aaa-bbb",
                "title": "Auth overview",
                "score": 0.92,
                "card_type": "doc_chunk",
            },
            {
                "artifact_id": "ccc-ddd",
                "title": "Token validator",
                "score": 0.85,
                "card_type": "code_symbol",
            },
        ],
        "budget": {"allowed": 8000, "used": 300, "remaining": 7700},
    }
    rows = [_row("context.create_pack", tokens=300, details=details)]
    lines = render_timeline(rows)
    assert len(lines) == 1
    line = lines[0]
    assert "task=" in line
    assert "auth service" in line
    assert "candidates=3" in line
    assert "cards=2" in line
    assert "budget_used=300/8000" in line
    assert "Auth overview" in line


def test_render_timeline_expand_details() -> None:
    details = {
        "seed_artifact_ids": ["id-1", "id-2"],
        "tiers": ["EXTRACTED"],
        "cards_added": 4,
        "truncated": False,
        "tokens": 1200,
    }
    rows = [_row("context.expand", tokens=1200, details=details)]
    lines = render_timeline(rows)
    line = lines[0]
    assert "seeds=2" in line
    assert "tiers=EXTRACTED" in line
    assert "cards_added=4" in line
    assert "tokens=1200" in line


def test_render_timeline_expand_truncated() -> None:
    details = {
        "seed_artifact_ids": ["id-1"],
        "tiers": ["EXTRACTED", "INFERRED"],
        "cards_added": 5,
        "truncated": True,
        "tokens": 3000,
    }
    rows = [_row("context.expand", details=details)]
    lines = render_timeline(rows)
    line = lines[0]
    assert "truncated=true" in line
    assert "tiers=EXTRACTED+INFERRED" in line


def test_render_timeline_open_evidence_details() -> None:
    details = {
        "evidence_id": "ev-123",
        "level": "L2",
        "injection_flagged": False,
        "tokens": 400,
    }
    rows = [_row("context.open_evidence", status="approved", tokens=400, details=details)]
    lines = render_timeline(rows)
    line = lines[0]
    assert "evidence_id=ev-123" in line
    assert "level=L2" in line
    assert "tokens=400" in line


def test_render_timeline_open_evidence_injection_flagged() -> None:
    details = {
        "evidence_id": "ev-999",
        "level": "L2",
        "injection_flagged": True,
        "tokens": 200,
    }
    rows = [_row("context.open_evidence", details=details)]
    lines = render_timeline(rows)
    assert "injection_flagged=true" in lines[0]


def test_render_timeline_graph_get_neighbors_details() -> None:
    details = {
        "artifact_id": "abcdef12-3456-7890-abcd-ef1234567890",
        "depth": 2,
        "trust_floor": "EXTRACTED",
        "neighbors_by_type": {"calls": 3, "defined_in": 1},
    }
    rows = [_row("graph.get_neighbors", details=details)]
    lines = render_timeline(rows)
    line = lines[0]
    assert "depth=2" in line
    assert "trust_floor=EXTRACTED" in line
    assert "calls:3" in line
    assert "defined_in:1" in line


def test_render_timeline_verify_answer_details() -> None:
    details = {
        "answer_id": "ans-001",
        "claims": [
            {
                "claim_id": "c1",
                "checks": {"L0_exists": True, "L0_in_active_version": True},
                "ok": True,
            },
            {
                "claim_id": "c2",
                "checks": {"L0_exists": False},
                "ok": False,
            },
        ],
        "overall": "partial",
    }
    rows = [_row("context.verify_answer", details=details)]
    lines = render_timeline(rows)
    line = lines[0]
    assert "answer=ans-001" in line
    assert "claims=2" in line
    assert "passed=1" in line
    assert "overall=partial" in line


def test_render_timeline_governance_checkpoint() -> None:
    details = {
        "from_agent": "orchestrator",
        "to_agent": "impl-agent",
        "plan_summary": "Implement the auth refactor",
        "decision": "approved",
        "edits": [],
    }
    rows = [_row("governance.checkpoint", status="approved", details=details)]
    lines = render_timeline(rows)
    line = lines[0]
    assert "governance.checkpoint" in line
    assert "from=orchestrator" in line
    assert "to=impl-agent" in line
    assert "decision=approved" in line
    assert "plan=" in line
    assert "Implement the auth refactor" in line


def test_render_timeline_multiple_rows_elapsed_time() -> None:
    """Elapsed time increments correctly across rows."""
    rows = [
        _row("context.create_pack", offset_seconds=0),
        _row("context.expand", offset_seconds=1.5),
        _row("governance.checkpoint", status="approved", offset_seconds=3.0),
    ]
    lines = render_timeline(rows)
    assert len(lines) == 3
    # First row: +0ms elapsed
    assert "+0ms" in lines[0]
    # Second row: ~1.5s elapsed
    assert "+1.5s" in lines[1]
    # Third row: ~3s elapsed
    assert "+3.0s" in lines[2]


def test_render_timeline_rejected_checkpoint() -> None:
    details = {
        "from_agent": "orchestrator",
        "to_agent": "deploy-agent",
        "plan_summary": "Deploy to production",
        "decision": "rejected",
        "edits": [],
    }
    rows = [_row("governance.checkpoint", status="rejected", details=details)]
    lines = render_timeline(rows)
    line = lines[0]
    assert "[rejected]" in line
    assert "decision=rejected" in line


def test_render_timeline_no_details_shows_tool_name() -> None:
    rows = [_row("ledger.list_retrievals", details=None)]
    lines = render_timeline(rows)
    assert "ledger.list_retrievals" in lines[0]
