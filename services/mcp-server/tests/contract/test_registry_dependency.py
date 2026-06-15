"""Pins the registry surface mcp-server depends on without sharing ORM code.

kb-builder owns the schema; mcp-server queries it through names pinned in
docs/contracts/postgres-knowledge-registry.md. This test keeps the query
constants and the contract document in lockstep — renaming a column in the
registry must break this test, not production health checks.
"""

from pathlib import Path

from agentic_mcp_server.infrastructure.postgres.active_kb_version import (
    BUILD_SEQ_COLUMN,
    KB_BUILD_RUN_TABLE,
    KB_VERSION_COLUMN,
    STATUS_COLUMN,
)
from agentic_mcp_server.infrastructure.postgres.artifacts import (
    KNOWLEDGE_ARTIFACT_TABLE,
    SOURCE_ITEM_TABLE,
)
from agentic_mcp_server.infrastructure.postgres.edges import KNOWLEDGE_EDGE_TABLE
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    RETRIEVAL_EVENT_TABLE,
    RETRIEVAL_STATUS_COLUMN,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
REGISTRY_CONTRACT = REPO_ROOT / "docs" / "contracts" / "postgres-knowledge-registry.md"

PINNED_NAMES = (
    KB_BUILD_RUN_TABLE,
    KB_VERSION_COLUMN,
    BUILD_SEQ_COLUMN,
    STATUS_COLUMN,
    KNOWLEDGE_ARTIFACT_TABLE,
    SOURCE_ITEM_TABLE,
    KNOWLEDGE_EDGE_TABLE,
    RETRIEVAL_EVENT_TABLE,
    RETRIEVAL_STATUS_COLUMN,
)


def test_pinned_names_match_the_registry_contract() -> None:
    assert KB_BUILD_RUN_TABLE == "kb_build_run"
    assert KB_VERSION_COLUMN == "kb_version"
    assert BUILD_SEQ_COLUMN == "build_seq"
    assert STATUS_COLUMN == "status"
    assert KNOWLEDGE_ARTIFACT_TABLE == "knowledge_artifact"
    assert SOURCE_ITEM_TABLE == "source_item"
    assert KNOWLEDGE_EDGE_TABLE == "knowledge_edge"
    assert RETRIEVAL_EVENT_TABLE == "retrieval_event"
    assert RETRIEVAL_STATUS_COLUMN == "status"


def test_contract_document_records_the_pinned_names() -> None:
    contract_text = REGISTRY_CONTRACT.read_text(encoding="utf-8")
    for name in PINNED_NAMES:
        assert name in contract_text, f"{name} missing from {REGISTRY_CONTRACT}"
