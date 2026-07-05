"""Pins the trace_span table name + the no-content attribute guard against
docs/contracts/tracing.md, mirroring test_registry_dependency.py's convention: renaming
either must break this test, not a runtime surprise.
"""

from pathlib import Path

from agentic_mcp_server.infrastructure.postgres.trace_spans import TRACE_SPAN_TABLE
from agentic_mcp_server.infrastructure.tracing.trace_sink import FORBIDDEN_ATTRIBUTE_KEYS

REPO_ROOT = Path(__file__).resolve().parents[4]
TRACING_CONTRACT = REPO_ROOT / "docs" / "contracts" / "tracing.md"
REGISTRY_CONTRACT = REPO_ROOT / "docs" / "contracts" / "postgres-knowledge-registry.md"


def test_trace_span_table_name_matches_the_contracts() -> None:
    assert TRACE_SPAN_TABLE == "trace_span"
    assert TRACE_SPAN_TABLE in TRACING_CONTRACT.read_text(encoding="utf-8")
    assert TRACE_SPAN_TABLE in REGISTRY_CONTRACT.read_text(encoding="utf-8")


def test_forbidden_attribute_keys_are_documented_in_the_no_content_rule() -> None:
    contract_text = TRACING_CONTRACT.read_text(encoding="utf-8")
    for key in FORBIDDEN_ATTRIBUTE_KEYS:
        assert key in contract_text, f"{key} missing from the tracing contract's no-content rule"
