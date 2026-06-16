"""display_citation: human-readable file:symbol references (ADR-0022, two-identifier rule)."""

import uuid

from agentic_mcp_server.context_broker.retrieval import display_citation
from agentic_mcp_server.infrastructure.postgres.artifacts import ArtifactRow


def _row(*, artifact_type: str, title: str | None, source_uri: str) -> ArtifactRow:
    return ArtifactRow(
        artifact_id=uuid.uuid4(),
        artifact_type=artifact_type,
        title=title,
        body_text="",
        knowledge_kind="source_backed",
        authority_score=1.0,
        source_uri=source_uri,
    )


def test_code_symbol_renders_path_and_symbol() -> None:
    row = _row(
        artifact_type="code_symbol",
        title="parse_agent_allowances",
        source_uri="github://owner/repo/services/mcp-server/src/agentic_mcp_server/budgets.py",
    )
    assert display_citation(row) == (
        "services/mcp-server/src/agentic_mcp_server/budgets.py:parse_agent_allowances"
    )


def test_code_file_renders_path_only() -> None:
    row = _row(
        artifact_type="code_file",
        title="budgets.py",
        source_uri="github://owner/repo/services/mcp-server/src/agentic_mcp_server/budgets.py",
    )
    assert display_citation(row) == "services/mcp-server/src/agentic_mcp_server/budgets.py"


def test_non_github_scheme_falls_back_to_readable_tail() -> None:
    row = _row(
        artifact_type="doc",
        title="Architecture",
        source_uri="azuredevops://org/project/_wiki/wikis/w/Architecture",
    )
    # no github prefix -> everything after '://'
    assert display_citation(row) == "org/project/_wiki/wikis/w/Architecture"


def test_never_emits_a_raw_uuid_when_metadata_exists() -> None:
    row = _row(
        artifact_type="code_symbol",
        title="Thing",
        source_uri="github://o/r/a/b.py",
    )
    cite = display_citation(row)
    assert str(row.artifact_id) not in cite
    assert cite == "a/b.py:Thing"
