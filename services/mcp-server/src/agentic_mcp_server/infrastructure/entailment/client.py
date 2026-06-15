"""EntailmentClient Protocol + verdict DTO for the L3 verifier (PR-31).

L3 asks ONE question per deterministically-unresolved claim: *do the cited,
resolved evidence texts ENTAIL the claim?* The answer is a single bool + a terse
reason. The broker depends only on this Protocol; the concrete Ollama-first client
lives in ``ollama_client.py`` and a hermetic fake in ``fake.py``.

The client never sees ids, ledgers, or policy — only the claim text and the
evidence texts the verifier already resolved and authorised. It returns a verdict;
the verifier owns gating, caching, and signing.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EntailmentVerdict:
    """The L3 model's verdict on one claim against its cited evidence.

    ``entailed`` is the load-bearing bool; ``reason`` is a short, model-authored
    rationale stored in the cache and surfaced in ``checks.L3_entailment``. Neither
    answer nor evidence text is ever echoed back into logs by the verifier.
    """

    entailed: bool
    reason: str


class EntailmentClient(Protocol):
    """Decide whether ``evidence_texts`` (taken together) entail ``claim_text``.

    Implementations MUST be robust to small-model output (fenced/prose JSON) and
    MUST NOT raise on a non-entailment — a claim the evidence does not support is a
    ``False`` verdict, not an error. The model identity is exposed via
    ``model_version`` so the verifier can key the entailment cache on it.
    """

    @property
    def model_version(self) -> str:
        """Stable identifier of the backing model+params (part of the cache key)."""
        ...

    async def check_entailment(
        self, *, claim_text: str, evidence_texts: list[str]
    ) -> EntailmentVerdict:
        """Return the entailment verdict for one claim against its evidence."""
        ...


__all__ = ["EntailmentClient", "EntailmentVerdict"]
