"""ModelClient interface: the only door to Azure OpenAI for the build plane.

Builders depend on this Protocol, never on the SDK, so tests stay hermetic and
the model backend stays swappable (rule: python.md). After retired the
prior prose pipeline (document extraction now goes through Graphify's
LLM pipeline behind the ``docify`` adapter), the only build-plane LLM use behind this
door is the phase-3B relationship judge.
"""

from typing import Protocol

from agentic_kb_builder.domain import (
    JudgeCandidate,
    RelationshipJudgment,
)


class ModelClient(Protocol):
    """The build-plane model door: the phase-3B relationship judge."""

    model_name: str
    model_params_hash: str

    async def generate_relationship_judgment(
        self, *, candidate: JudgeCandidate, prompt_version: str
    ) -> RelationshipJudgment:
        """Rule on ONE bounded candidate pair (phase 3B,.

        Returns an ontology relation + an LLM-judge trust bucket + the verbatim
        supporting quote + a reason. The implementation MUST quote-guard the
        supporting_quote against the candidate's cited source spans (invariant 7),
        downgrading to AMBIGUOUS when it is not a verbatim substring, and MUST
        never emit ``EXTRACTED`` or ``related_to``.
        """
        ...
