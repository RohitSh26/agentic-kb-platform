"""Base models and evidence-ID validation for agent outputs.

Every claim cites evidence IDs from the run's Evidence Pack (invariant 7):
claims carry a non-empty evidence_ids list at the schema level, so an
unevidenced claim cannot be constructed — it must be downgraded to an open
question instead. validate_evidence_references rejects outputs that cite
handles the pack never returned.

Bump AGENT_OUTPUT_SCHEMA_VERSION on any breaking change and update
docs/contracts/agent-output-contracts.md in the same PR.
"""

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

AGENT_OUTPUT_SCHEMA_VERSION = "1.0.0"


class AgentOutputComponent(BaseModel):
    """Base for nested parts of an agent output (no schema_version of their own)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class AgentOutputModel(AgentOutputComponent):
    """Base for all top-level agent output schemas."""

    schema_version: Literal["1.0.0"] = AGENT_OUTPUT_SCHEMA_VERSION


class EvidencedClaim(AgentOutputComponent):
    """One statement of fact or recommendation, always backed by evidence."""

    claim: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class AgentOutputValidationError(ValueError):
    pass


def referenced_evidence_ids(output: AgentOutputModel) -> set[str]:
    """All evidence IDs cited anywhere in the output, found by model walk."""
    found: set[str] = set()
    _walk(output, found)
    return found


def _walk(model: BaseModel, found: set[str]) -> None:
    # walks submodels and lists only — a schema nesting models in dict values
    # would escape this; keep V1 schemas to lists of components
    for name in type(model).model_fields:
        value: object = getattr(model, name)
        if isinstance(value, BaseModel):
            _walk(value, found)
        elif isinstance(value, list):
            items = cast(list[object], value)
            if name == "evidence_ids":
                found.update(item for item in items if isinstance(item, str))
            else:
                for item in items:
                    if isinstance(item, BaseModel):
                        _walk(item, found)


def validate_evidence_references(output: AgentOutputModel, known_evidence_ids: set[str]) -> None:
    """Reject outputs citing evidence handles the run's pack never returned."""
    unknown = referenced_evidence_ids(output) - known_evidence_ids
    if unknown:
        raise AgentOutputValidationError(
            "output cites evidence_ids unknown to the evidence pack: " + ", ".join(sorted(unknown))
        )
