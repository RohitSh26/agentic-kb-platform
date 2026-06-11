"""Contract-level guarantees for the MCP tool schemas (mcp-tools rule)."""

import uuid
from typing import get_args

import pytest
from pydantic import ValidationError

from agentic_mcp_server.mcp.tool_registry import TOOL_SCHEMAS
from agentic_mcp_server.mcp.tool_schemas import (
    MCP_SCHEMA_VERSION,
    CreatePackRequest,
    EvidenceCard,
    ListRetrievalsRequest,
    OpenEvidenceResponse,
    RequestMoreRequest,
    RequestMoreResponse,
    RequestMoreStatus,
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


def test_ledger_rejects_the_non_run_sentinel() -> None:
    """run_id "-" aggregates all subjects' non-run activity — operator-only."""
    with pytest.raises(ValidationError):
        ListRetrievalsRequest(run_id="-")
    assert ListRetrievalsRequest(run_id="run-1").run_id == "run-1"


AUTHORIZATION = {"policy": "team_acl_v1", "decision": "allowed"}


def test_denied_status_requires_denial_reason() -> None:
    with pytest.raises(ValidationError, match="denial_reason"):
        RequestMoreResponse.model_validate(
            {
                "status": "denied",
                "reused_evidence_ids": [],
                "new_evidence_cards": [],
                "tokens_returned": 0,
                "budget_remaining_tokens": 100,
                "authorization": AUTHORIZATION,
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


def test_request_more_status_pins_the_contract_values() -> None:
    assert set(get_args(RequestMoreStatus)) == {
        "reused",
        "approved",
        "denied",
        "needs_human_approval",
    }


def test_evidence_cards_are_handles_never_raw_text() -> None:
    """Cards are L0/L1 only — expansion levels must be unrepresentable on a card."""
    for expansion_level in ("L2", "L3", "L4"):
        with pytest.raises(ValidationError):
            EvidenceCard.model_validate(
                {
                    "evidence_id": "ev-1",
                    "artifact_id": str(uuid.uuid4()),
                    "level": expansion_level,
                    "card_type": "chunk",
                    "title": "t",
                    "confidence": 0.9,
                    "authority_score": 1.0,
                    "tokens_if_expanded": 100,
                }
            )


def test_open_evidence_exposes_raw_text_only_as_untrusted_content() -> None:
    """Expanded text is retrieved content: the field name carries the security rule."""
    fields = OpenEvidenceResponse.model_fields
    assert "untrusted_content" in fields
    assert "content" not in fields
    assert "text" not in fields
    for level in ("L2", "L3"):
        response = OpenEvidenceResponse.model_validate(
            {
                "evidence_id": "ev-1",
                "level": level,
                "untrusted_content": "raw chunk text",
                "tokens_used": 10,
                "budget_remaining_tokens": 90,
                "authorization": AUTHORIZATION,
            }
        )
        assert response.level == level
    with pytest.raises(ValidationError):
        OpenEvidenceResponse.model_validate(
            {
                "evidence_id": "ev-1",
                "level": "L0",
                "untrusted_content": "raw chunk text",
                "tokens_used": 10,
                "budget_remaining_tokens": 90,
                "authorization": AUTHORIZATION,
            }
        )
