"""Audit log format: ids and metadata only, never retrieved content."""

import logging
import uuid

import pytest

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.telemetry.audit import audit_context_access

AUDIT_LOGGER = "agentic_mcp_server.audit"


def test_audit_line_carries_ids_teams_and_suppressions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    returned = uuid.uuid4()
    suppressed = uuid.uuid4()
    flagged = uuid.uuid4()
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        audit_context_access(
            tool="context.create_pack",
            requester=Requester(subject="impl-agent", teams=frozenset({"team-b", "team-a"})),
            kb_version="kb-test",
            artifact_ids=[returned],
            suppressed_artifact_ids=[suppressed],
            injection_flagged_ids=[flagged],
        )
    [record] = caplog.records
    assert record.name == AUDIT_LOGGER
    line = record.getMessage()
    assert line.startswith("audit.context_access tool=context.create_pack ")
    assert "subject=impl-agent" in line
    assert "teams=team-a,team-b" in line  # sorted for stable grepping
    assert "kb_version=kb-test" in line
    assert f"artifact_ids={returned}" in line
    assert f"suppressed_artifact_ids={suppressed}" in line
    assert f"injection_flagged_ids={flagged}" in line


def test_empty_collections_become_dash_placeholders(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        audit_context_access(
            tool="graph.get_neighbors",
            requester=Requester(subject="agent", teams=frozenset()),
            kb_version="kb-test",
            artifact_ids=[],
        )
    line = caplog.records[0].getMessage()
    assert "teams=- " in line
    assert "artifact_ids=- " in line
    assert "suppressed_artifact_ids=- " in line
    assert line.endswith("injection_flagged_ids=-")
