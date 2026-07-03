"""Contract-level guarantees for the MCP tool schemas (mcp-tools rule)."""

import uuid
from typing import get_args

import pytest
from pydantic import ValidationError

from agentic_mcp_server.mcp.tool_registry import TOOL_SCHEMAS
from agentic_mcp_server.mcp.tool_schemas import (
    MCP_SCHEMA_VERSION,
    ConfidenceTier,
    CreatePackRequest,
    EvidenceCard,
    KbSearchHit,
    KbSearchRequest,
    ListRetrievalsRequest,
    OpenEvidenceResponse,
    ReadPackRequest,
    RequestMoreRequest,
    RequestMoreResponse,
    RequestMoreStatus,
)
from agentic_mcp_server.mcp.tool_schemas.verification import ClaimInput, VerifyAnswerRequest

EXPECTED_TOOLS = {
    "context.create_pack",
    "context.read_pack",
    "context.request_more",
    "context.open_evidence",
    "context.expand",
    "graph.get_neighbors",
    "ledger.list_retrievals",
    "context.verify_answer",
    "context.platform_trust",
    "context.create_change_pack",
    "kb_search",
}


def test_registry_covers_the_v1_tool_surface() -> None:
    assert set(TOOL_SCHEMAS) == EXPECTED_TOOLS


def test_bare_query_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RequestMoreRequest.model_validate({"query": "give me everything"})


def test_kb_search_accepts_exactly_a_bare_query() -> None:
    """The ADR-0025 simple path: {"query": ...} IS the whole contract — no run/pack
    handle, no justification fields — while anything extra still dies at validation."""
    request = KbSearchRequest.model_validate({"query": "where is build_seq resolved?"})
    assert request.query == "where is build_seq resolved?"
    with pytest.raises(ValidationError):
        KbSearchRequest.model_validate({"query": ""})
    with pytest.raises(ValidationError):
        KbSearchRequest.model_validate({"query": "x", "run_id": "run-1"})


def test_kb_search_confidence_tiers_pin_the_contract_values() -> None:
    """docs/proposals/2026-07-02-tool-design-first-kb-architecture.md §3 tiering."""
    assert set(get_args(ConfidenceTier)) == {"ground_truth", "deterministic", "interpreted"}


def test_kb_search_hits_default_to_interpreted_and_reject_unknown_tiers() -> None:
    """Keyword hits are not cross-validated: `interpreted` unless a path (e.g. the
    future graph-derived one) explicitly claims a stronger tier the Literal admits."""
    hit = KbSearchHit(title="BudgetPolicy", artifact_type="code_symbol")
    assert hit.confidence_tier == "interpreted"
    deterministic = KbSearchHit(
        title="BudgetPolicy", artifact_type="code_symbol", confidence_tier="deterministic"
    )
    assert deterministic.confidence_tier == "deterministic"
    with pytest.raises(ValidationError):
        KbSearchHit.model_validate(
            {"title": "t", "artifact_type": "code_symbol", "confidence_tier": "certain"}
        )


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


def test_read_pack_role_is_open_to_team_defined_agents() -> None:
    """The framework is the product: a team's own role name must validate."""
    for role in ("security_auditor", "orchestrator", "implementation", "sre.oncall-v2", "x" * 64):
        assert ReadPackRequest(context_pack_id="pack-1", role=role).role == role


def test_read_pack_role_rejects_log_injection_charsets() -> None:
    """role lands verbatim in key=value audit logs — same guard as run_id.

    The trailing-newline case pins pydantic-core's strict end-of-text `$`:
    Python's re engine would let "x\\n" through and break the log line.
    """
    for bad in ("a b", "x\nstatus=ok", "security_auditor\n", "r=1", 'r"1', "", "x" * 65):
        with pytest.raises(ValidationError):
            ReadPackRequest(context_pack_id="pack-1", role=bad)


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


def test_verify_answer_ids_reject_control_chars() -> None:
    # answer_id + claim_id land verbatim in the verify_answer audit log; a newline/CR
    # would let an agent forge log lines (same class of guard as run_id and role).
    evidence = [str(uuid.uuid4())]
    with pytest.raises(ValidationError):
        VerifyAnswerRequest(
            answer_id="ans\nstatus=ok",
            claims=[ClaimInput(claim_id="c1", text="t", evidence_ids=evidence)],
        )
    with pytest.raises(ValidationError):
        VerifyAnswerRequest(
            answer_id="ans",
            claims=[ClaimInput(claim_id="c1\rstatus=ok", text="t", evidence_ids=evidence)],
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
