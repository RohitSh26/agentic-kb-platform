"""Relationship-judge parsing + quote-guard are robust and fabrication-proof (no network).

The judge contract (docs/contracts/relationship-judgment.md) is strict JSON. Like wikify,
small local models are sloppy, so the parser repairs/strips, and the vocabulary + quote
guards force any out-of-contract verdict to AMBIGUOUS rather than inventing an edge.
"""

import uuid

import pytest

from agentic_kb_builder.domain import JudgeCandidate, JudgeEndpoint, RelationshipJudgment
from agentic_kb_builder.domain.judge_records import guard_quote, quote_is_grounded
from agentic_kb_builder.infrastructure.azure_openai.chat_model_client import _parse_judgment


def _judgment(bucket: str, quote: str) -> RelationshipJudgment:
    return RelationshipJudgment.model_validate(
        {
            "relation_type": "documents",
            "trust_bucket": bucket,
            "supporting_quote": quote,
            "reason": "r",
        }
    )


def test_parses_plain_judgment_json() -> None:
    raw = (
        '{"relation_type": "documents", "trust_bucket": "INFERRED_HIGH",'
        ' "supporting_quote": "see payment_service.py", "reason": "doc names the file"}'
    )
    judgment = _parse_judgment(raw)
    assert judgment.relation_type == "documents"
    assert judgment.trust_bucket == "INFERRED_HIGH"
    assert judgment.supporting_quote == "see payment_service.py"


def test_strips_fences_and_repairs_truncated_json() -> None:
    raw = (
        "```json\n"
        '{"relation_type": "documents", "trust_bucket": "INFERRED_LOW",'
        ' "supporting_quote": "q", "reason": "partial'
    )
    judgment = _parse_judgment(raw)
    assert judgment.trust_bucket == "INFERRED_LOW"


def test_banned_relation_is_forced_ambiguous_never_invented() -> None:
    # `related_to` is banned; the judge must never invent it as a real edge.
    raw = (
        '{"relation_type": "related_to", "trust_bucket": "INFERRED_HIGH",'
        ' "supporting_quote": "q", "reason": "r"}'
    )
    judgment = _parse_judgment(raw)
    assert judgment.relation_type == "documents"
    assert judgment.trust_bucket == "AMBIGUOUS"


def test_extracted_bucket_from_judge_is_forced_ambiguous() -> None:
    # The LLM judge may NEVER assign EXTRACTED (reserved for deterministic producers).
    raw = (
        '{"relation_type": "documents", "trust_bucket": "EXTRACTED",'
        ' "supporting_quote": "q", "reason": "r"}'
    )
    judgment = _parse_judgment(raw)
    assert judgment.trust_bucket == "AMBIGUOUS"


def test_non_json_output_raises() -> None:
    with pytest.raises(ValueError, match="did not return usable JSON"):
        _parse_judgment("I cannot judge this.")


def test_quote_is_grounded_verbatim_substring() -> None:
    spans = ("the payment service rollout is in src/payment_service.py", "def charge(): ...")
    assert quote_is_grounded("payment service rollout", cited_spans=spans)
    assert quote_is_grounded("def charge()", cited_spans=spans)


def test_quote_is_grounded_tolerates_reflowed_whitespace() -> None:
    spans = ("the   payment\nservice  rollout",)
    assert quote_is_grounded("payment service rollout", cited_spans=spans)


def test_quote_not_grounded_for_fabricated_or_empty_quote() -> None:
    spans = ("the payment service rollout",)
    assert not quote_is_grounded("the billing subsystem", cited_spans=spans)
    assert not quote_is_grounded("", cited_spans=spans)


def test_guard_downgrades_inferred_with_non_verbatim_quote_to_ambiguous() -> None:
    spans = ("the payment service rollout",)
    guarded = guard_quote(_judgment("INFERRED_HIGH", "the billing subsystem"), cited_spans=spans)
    assert guarded.trust_bucket == "AMBIGUOUS"


def test_guard_keeps_inferred_with_verbatim_quote() -> None:
    spans = ("the payment service rollout is documented",)
    guarded = guard_quote(_judgment("INFERRED_HIGH", "payment service rollout"), cited_spans=spans)
    assert guarded.trust_bucket == "INFERRED_HIGH"


def test_candidate_cited_spans_are_both_endpoint_bodies() -> None:
    cand = JudgeCandidate(
        from_endpoint=JudgeEndpoint(uuid.uuid4(), "doc", "doc body", "h1"),
        to_endpoint=JudgeEndpoint(uuid.uuid4(), "code", "code body", "h2"),
    )
    assert cand.cited_spans == ("doc body", "code body")
