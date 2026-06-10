"""Base model for all build-plane knowledge artifact schemas."""

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from agentic_kb_builder.domain.schema_versions import OUTPUT_SCHEMA_VERSION

ARTIFACT_SCHEMA_VERSION: Final = OUTPUT_SCHEMA_VERSION


class ArtifactModel(BaseModel):
    """Base for all knowledge artifact models."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0.0"] = ARTIFACT_SCHEMA_VERSION
