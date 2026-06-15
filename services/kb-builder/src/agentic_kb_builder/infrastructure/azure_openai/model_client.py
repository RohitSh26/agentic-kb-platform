"""ModelClient interface: the only door to Azure OpenAI for the build plane.

Builders depend on this Protocol, never on the SDK, so tests stay hermetic and
the model backend stays swappable (rule: python.md). It covers both build-plane
LLM uses: wikify generation and the phase-3B relationship judge.
"""

from collections.abc import Sequence
from typing import Protocol

from agentic_kb_builder.domain import (
    Chunk,
    JudgeCandidate,
    RelationshipJudgment,
    WikifyGeneration,
)


class WikifyModelClient(Protocol):
    """The narrow slice the wikify generator depends on (just generation).

    Kept separate from the full ``ModelClient`` so a wikify-only test double need
    not implement the judge method, and vice versa. ``ChatModelClient`` satisfies
    both because it implements the union."""

    model_name: str
    model_params_hash: str

    async def generate_wikify(
        self, *, chunks: Sequence[Chunk], prompt_version: str
    ) -> WikifyGeneration:
        """Produce a summary, concepts, and source-backed facts for one source."""
        ...


class ModelClient(WikifyModelClient, Protocol):
    """The full build-plane model door: wikify generation + the phase-3B judge."""

    async def generate_relationship_judgment(
        self, *, candidate: JudgeCandidate, prompt_version: str
    ) -> RelationshipJudgment:
        """Rule on ONE bounded candidate pair (phase 3B, ADR-0010/0011).

        Returns an ontology relation + an LLM-judge trust bucket + the verbatim
        supporting quote + a reason. The implementation MUST quote-guard the
        supporting_quote against the candidate's cited source spans (invariant 7),
        downgrading to AMBIGUOUS when it is not a verbatim substring, and MUST
        never emit ``EXTRACTED`` or ``related_to``.
        """
        ...
