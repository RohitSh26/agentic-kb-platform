"""Manifests in agents/ stay consistent with the broker's contracts.

The runtime serves these manifests, so mcp-server's contract tests pin them:
every allowed tool is a registered broker tool (no unrestricted KB search),
every output_schema resolves in AGENT_OUTPUT_SCHEMAS, and budgets match
.claude/rules/token-budgets.md.
"""

from pathlib import Path

import pytest

from agentic_mcp_server.agent_output_schemas import AGENT_OUTPUT_SCHEMAS
from agentic_mcp_server.mcp.tool_registry import TOOL_SCHEMAS

AGENTS_DIR = Path(__file__).resolve().parents[4] / "agents"

# (max_context_calls, max_context_tokens) ceilings from .claude/rules/token-budgets.md
BUDGET_RULES: dict[str, tuple[int, int]] = {
    "orchestrator": (6, 18_000),  # full run context budget: 12k-18k
    "implementation_agent": (2, 4_000),  # 2 requests / 3k-4k
    "test_layer_agent": (1, 2_500),  # 1 request / 1.5k-2.5k
    "code_reviewer_agent": (1, 2_500),  # 1 request / 1.5k-2.5k
    "delivery_planner_agent": (1, 1_500),  # 1 request / 1k-1.5k
    "pr_planner_agent": (1, 1_500),  # 1 request / 1k-1.5k
}

pytestmark = pytest.mark.skipif(
    not AGENTS_DIR.is_dir(), reason="agents/ manifests not present in this checkout"
)


def _frontmatter(path: Path) -> dict[str, object]:
    lines = path.read_text().splitlines()
    assert lines[0] == "---", f"{path.name}: manifest must start with frontmatter"
    fields: dict[str, object] = {}
    current_list: list[str] | None = None
    for line in lines[1:]:
        if line == "---":
            return fields
        if line.startswith("  - "):
            assert current_list is not None, f"{path.name}: list item outside a list"
            current_list.append(line.removeprefix("  - ").strip())
        else:
            key, _, raw = line.partition(":")
            value = raw.strip()
            if value == "":
                current_list = []
                fields[key.strip()] = current_list
            else:
                current_list = None
                fields[key.strip()] = value
    raise AssertionError(f"{path.name}: unterminated frontmatter")


def _manifests() -> dict[str, dict[str, object]]:
    paths = sorted(AGENTS_DIR.glob("*.md"))
    manifests = {}
    for path in paths:
        if path.name == "README.md":
            continue
        fields = _frontmatter(path)
        manifests[str(fields["name"])] = fields
    return manifests


def test_all_six_roles_have_a_manifest() -> None:
    assert set(_manifests()) == set(BUDGET_RULES)


def test_allowed_tools_are_registered_broker_tools_only() -> None:
    for name, fields in _manifests().items():
        tools = fields["allowed_tools"]
        assert isinstance(tools, list) and tools, f"{name}: allowed_tools missing"
        for tool in tools:
            assert tool in TOOL_SCHEMAS, f"{name}: unknown tool {tool}"
            prefix = tool.split(".")[0]
            assert prefix in {"context", "ledger"}, f"{name}: {tool} outside context/ledger"


def test_no_manifest_grants_unrestricted_search() -> None:
    for name, fields in _manifests().items():
        tools = fields["allowed_tools"]
        assert isinstance(tools, list)
        assert not any("search" in tool for tool in tools), f"{name}: search tool granted"


def test_only_the_orchestrator_creates_packs() -> None:
    for name, fields in _manifests().items():
        tools = fields["allowed_tools"]
        assert isinstance(tools, list)
        if name != "orchestrator":
            assert "context.create_pack" not in tools, f"{name}: subagents never create packs"


def test_every_output_schema_resolves_in_the_registry() -> None:
    for name, fields in _manifests().items():
        assert fields["output_schema"] in AGENT_OUTPUT_SCHEMAS, f"{name}: unknown output_schema"


def test_every_manifest_requires_evidence_ids() -> None:
    for name, fields in _manifests().items():
        assert fields["requires_evidence_ids"] == "true", f"{name}: evidence IDs are mandatory"


def test_budgets_match_the_token_budget_rules() -> None:
    for name, fields in _manifests().items():
        max_calls, max_tokens = BUDGET_RULES[name]
        assert int(str(fields["max_context_calls"])) <= max_calls, f"{name}: too many calls"
        assert int(str(fields["max_context_tokens"])) <= max_tokens, f"{name}: budget too large"
