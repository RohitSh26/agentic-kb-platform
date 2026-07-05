"""Pins the trace_span table name + the no-content attribute guard against
docs/contracts/tracing.md and docs/contracts/review-panel.md: renaming either must
break this test, not a runtime surprise (mirrors mcp-server's equivalent contract test).
"""

from pathlib import Path

from review_panel.infrastructure.postgres_trace_sink import TRACE_SPAN_TABLE
from review_panel.infrastructure.trace_sink import FORBIDDEN_ATTRIBUTE_KEYS

REPO_ROOT = Path(__file__).resolve().parents[4]
TRACING_CONTRACT = REPO_ROOT / "docs" / "contracts" / "tracing.md"
REVIEW_PANEL_CONTRACT = REPO_ROOT / "docs" / "contracts" / "review-panel.md"


def test_trace_span_table_name_matches_the_contracts() -> None:
    assert TRACE_SPAN_TABLE == "trace_span"
    assert TRACE_SPAN_TABLE in TRACING_CONTRACT.read_text(encoding="utf-8")
    assert TRACE_SPAN_TABLE in REVIEW_PANEL_CONTRACT.read_text(encoding="utf-8")


def test_forbidden_attribute_keys_are_documented_in_the_no_content_rule() -> None:
    """The shared baseline lives in tracing.md; this service's own additions (the
    literal PR/KB fields a node closure has in scope) are documented in
    review-panel.md's "Tracing" section — either counts."""
    contract_text = TRACING_CONTRACT.read_text(encoding="utf-8") + REVIEW_PANEL_CONTRACT.read_text(
        encoding="utf-8"
    )
    for key in FORBIDDEN_ATTRIBUTE_KEYS:
        assert key in contract_text, f"{key} missing from the tracing no-content rule docs"
