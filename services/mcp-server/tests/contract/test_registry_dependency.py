"""Pins the registry surface mcp-server depends on without sharing ORM code.

kb-builder owns the schema; mcp-server queries it through names pinned in
docs/contracts/postgres-knowledge-registry.md. This test keeps the query
constants and the contract document in lockstep — renaming a column in the
registry must break this test, not production health checks.
"""

from pathlib import Path

from agentic_mcp_server.infrastructure.postgres.active_kb_version import (
    KB_BUILD_RUN_TABLE,
    KB_VERSION_COLUMN,
    STATUS_COLUMN,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
REGISTRY_CONTRACT = REPO_ROOT / "docs" / "contracts" / "postgres-knowledge-registry.md"


def test_pinned_names_match_the_registry_contract() -> None:
    assert KB_BUILD_RUN_TABLE == "kb_build_run"
    assert KB_VERSION_COLUMN == "kb_version"
    assert STATUS_COLUMN == "status"


def test_contract_document_records_the_pinned_names() -> None:
    contract_text = REGISTRY_CONTRACT.read_text(encoding="utf-8")
    for name in (KB_BUILD_RUN_TABLE, KB_VERSION_COLUMN, STATUS_COLUMN):
        assert name in contract_text, f"{name} missing from {REGISTRY_CONTRACT}"
