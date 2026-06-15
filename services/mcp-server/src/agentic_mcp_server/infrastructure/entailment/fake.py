"""Hermetic FakeEntailmentClient for tests + local dev (PR-31).

Records every call so a test can assert the L3 cache prevented LLM calls (a hit
=> zero calls) and that L3 ran ONLY on deterministically-unresolved claims. The
verdict is seeded per claim text; an unseeded claim defaults to non-entailment so
a test never accidentally passes on a missing seed.
"""

from dataclasses import dataclass, field

from agentic_mcp_server.infrastructure.entailment.client import EntailmentVerdict


@dataclass
class FakeEntailmentClient:
    """Deterministic, call-counting EntailmentClient for hermetic tests.

    ``seed(claim_text, entailed, reason)`` fixes the verdict for a claim; every
    ``check_entailment`` appends its claim text to ``calls`` so a test can count
    LLM invocations (cache-hit ⇒ zero). ``model_version`` is fixed so the cache
    key is stable across a test run.
    """

    model_version: str = "fake:entail-v0"
    _verdicts: dict[str, EntailmentVerdict] = field(
        default_factory=lambda: dict[str, EntailmentVerdict]()
    )
    calls: list[str] = field(default_factory=lambda: list[str]())

    def seed(self, claim_text: str, *, entailed: bool, reason: str = "") -> None:
        self._verdicts[claim_text] = EntailmentVerdict(entailed=entailed, reason=reason)

    async def check_entailment(
        self, *, claim_text: str, evidence_texts: list[str]
    ) -> EntailmentVerdict:
        # Record the call so tests can assert L3 gating + cache behaviour.
        self.calls.append(claim_text)
        return self._verdicts.get(
            claim_text,
            EntailmentVerdict(entailed=False, reason="no seeded verdict (default non-entailed)"),
        )


__all__ = ["FakeEntailmentClient"]
