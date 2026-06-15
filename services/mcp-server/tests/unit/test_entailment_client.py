"""Unit tests for the EntailmentClient JSON parsing + fake (PR-31).

Hermetic: no Ollama, no network. Covers the quote/JSON robustness the L3 verifier
relies on — fenced/prose output is salvaged, an unparseable answer fails CLOSED
(non-entailed) rather than raising or fabricating support.
"""

import pytest

from agentic_mcp_server.infrastructure.entailment.fake import FakeEntailmentClient
from agentic_mcp_server.infrastructure.entailment.ollama_client import (
    ENTAILMENT_PROMPT_VERSION,
    _parse_verdict,
)


def test_parse_clean_json() -> None:
    verdict = _parse_verdict('{"entailed": true, "reason": "supported"}')
    assert verdict.entailed is True
    assert verdict.reason == "supported"


def test_parse_fenced_json() -> None:
    verdict = _parse_verdict('```json\n{"entailed": false, "reason": "nope"}\n```')
    assert verdict.entailed is False
    assert verdict.reason == "nope"


def test_parse_non_bool_fails_closed() -> None:
    # A model that returns a non-bool verdict must never be treated as entailment.
    verdict = _parse_verdict('{"entailed": "yes", "reason": "r"}')
    assert verdict.entailed is False


def test_parse_pure_prose_fails_closed() -> None:
    # No JSON at all ⇒ raises ValueError so the caller's retry loop resamples; the
    # client itself (not tested here) ultimately fails closed after exhausting retries.
    with pytest.raises(ValueError):
        _parse_verdict("I cannot determine this.")


def test_prompt_version_is_stable_string() -> None:
    assert isinstance(ENTAILMENT_PROMPT_VERSION, str) and ENTAILMENT_PROMPT_VERSION


async def test_fake_records_calls_and_defaults_to_non_entailed() -> None:
    fake = FakeEntailmentClient()
    fake.seed("entailed claim", entailed=True, reason="ok")

    yes = await fake.check_entailment(claim_text="entailed claim", evidence_texts=["e"])
    assert yes.entailed is True
    no = await fake.check_entailment(claim_text="unseeded claim", evidence_texts=["e"])
    assert no.entailed is False  # default non-entailed, never accidental pass
    assert fake.calls == ["entailed claim", "unseeded claim"]
    assert fake.model_version
