"""PR-01 acceptance: the contracts package exposes the three schema namespaces."""

import contracts
from contracts.agent_output_schemas import AGENT_OUTPUT_SCHEMA_VERSION, AgentOutputModel
from contracts.artifact_schemas import ARTIFACT_SCHEMA_VERSION, ArtifactModel
from contracts.mcp_schemas import MCP_SCHEMA_VERSION, McpModel


def test_exposes_three_schema_namespaces() -> None:
    assert contracts.mcp_schemas is not None
    assert contracts.artifact_schemas is not None
    assert contracts.agent_output_schemas is not None


def test_version_constants() -> None:
    for version in (
        contracts.OUTPUT_SCHEMA_VERSION,
        contracts.PROMPT_VERSION,
        contracts.CHUNKER_VERSION,
        contracts.GRAPHIFY_VERSION,
        MCP_SCHEMA_VERSION,
        ARTIFACT_SCHEMA_VERSION,
        AGENT_OUTPUT_SCHEMA_VERSION,
    ):
        assert isinstance(version, str)
        assert version.count(".") == 2


def test_base_models_pin_schema_version() -> None:
    assert McpModel().schema_version == MCP_SCHEMA_VERSION
    assert ArtifactModel().schema_version == ARTIFACT_SCHEMA_VERSION
    assert AgentOutputModel().schema_version == AGENT_OUTPUT_SCHEMA_VERSION
