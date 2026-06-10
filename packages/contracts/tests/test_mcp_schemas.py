"""Contract-level guarantees for the MCP tool schemas (mcp-tools rule)."""

import pytest
from pydantic import ValidationError

from contracts.mcp_schemas import (
    MCP_SCHEMA_VERSION,
    TOOL_SCHEMAS,
    CreatePackRequest,
    RequestMoreRequest,
    RequestMoreResponse,
)

EXPECTED_TOOLS = {
    "context.create_pack",
    "context.read_pack",
    "context.request_more",
    "context.open_evidence",
    "graph.get_neighbors",
    "ledger.list_retrievals",
}


def test_registry_covers_the_v1_tool_surface() -> None:
    assert set(TOOL_SCHEMAS) == EXPECTED_TOOLS


def test_bare_query_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RequestMoreRequest.model_validate({"query": "give me everything"})


def test_request_more_requires_full_justification() -> None:
    with pytest.raises(ValidationError):
        RequestMoreRequest.model_validate(
            {
                "context_pack_id": "pack-1",
                "agent_name": "impl-agent",
                "question": "what validates the payload?",
                # why_needed / decision_needed / already_checked / max_tokens missing
            }
        )


def test_run_id_rejects_log_injection_charsets() -> None:
    for bad in ("run 1", "run\nstatus=ok", "run=1", 'run"1', "x" * 129):
        with pytest.raises(ValidationError):
            CreatePackRequest(
                run_id=bad,
                task="t",
                approved_context_plan="p",
                retrieval_profile="default",
                budget_tokens=8000,
            )


def test_denied_status_requires_denial_reason() -> None:
    with pytest.raises(ValidationError, match="denial_reason"):
        RequestMoreResponse.model_validate(
            {
                "status": "denied",
                "reused_evidence_ids": [],
                "new_evidence_cards": [],
                "tokens_returned": 0,
                "budget_remaining_tokens": 100,
            }
        )


def test_schema_version_default_matches_constant() -> None:
    request = CreatePackRequest(
        run_id="run-1",
        task="t",
        approved_context_plan="p",
        retrieval_profile="default",
        budget_tokens=8000,
    )
    assert request.schema_version == MCP_SCHEMA_VERSION
