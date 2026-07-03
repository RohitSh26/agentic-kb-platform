"""Manifests in agents/ stay consistent with the broker's contracts.

The runtime serves these manifests, so mcp-server's contract tests pin them to
the ADR-0025/ADR-0030 canon: the roster is the twelve roles; every role uses
the budgeted `kb_search` tool (registered in TOOL_SCHEMAS — PR-37 made the
grant real) plus native tools from the fixed ADR-0025 host-side vocabulary;
no manifest grants any OTHER search surface; budgets stay within the
.claude/rules/token-budgets.md ceilings; and every output_schema resolves in
AGENT_OUTPUT_SCHEMAS (the one known gap, adr_draft_v1, is pinned as a strict
xfail until its tracked ADR-0030 follow-up registers it).
"""

from pathlib import Path

import pytest

from agentic_mcp_server.agent_output_schemas import AGENT_OUTPUT_SCHEMAS
from agentic_mcp_server.mcp.tool_registry import TOOL_SCHEMAS

AGENTS_DIR = Path(__file__).resolve().parents[4] / "agents"

# (max_context_calls, max_context_tokens) ceilings from .claude/rules/token-budgets.md,
# mapped onto the ADR-0030 roster: orchestrator = the full-run band; adr_writer and
# infra_code sit in the implementation band (2 requests / 3k-4k); the four specialist
# reviewers sit in the reviewer band (1 request / 1.5k-2.5k) like the synthesizer.
BUDGET_RULES: dict[str, tuple[int, int]] = {
    "orchestrator": (6, 18_000),  # full run context budget: 12k-18k
    "implementation_agent": (2, 4_000),  # 2 requests / 3k-4k
    "test_layer_agent": (1, 2_500),  # 1 request / 1.5k-2.5k
    "code_reviewer_agent": (1, 2_500),  # 1 request / 1.5k-2.5k
    "delivery_planner_agent": (1, 1_500),  # 1 request / 1k-1.5k
    "pr_planner_agent": (1, 1_500),  # 1 request / 1k-1.5k
    "adr_writer_agent": (2, 4_000),  # implementation band (ADR-0030)
    "infra_code_agent": (2, 4_000),  # implementation band (ADR-0030)
    "bug_reviewer_agent": (1, 2_500),  # reviewer band (ADR-0030 panel)
    "security_reviewer_agent": (1, 2_500),  # reviewer band (ADR-0030 panel)
    "quality_reviewer_agent": (1, 2_500),  # reviewer band (ADR-0030 panel)
    "test_coverage_reviewer_agent": (1, 2_500),  # reviewer band (ADR-0030 panel)
}

# ADR-0025 §2: agents keep native tools in a scoped workspace. These are HOST-side
# tools (the runtime's own read/grep/edit surface), deliberately NOT broker tools,
# so they never appear in TOOL_SCHEMAS. A manifest may grant only these + registered
# broker tools — an unknown name is a manifest typo the runtime could not serve.
NATIVE_TOOLS = {"read_file", "read_full", "grep", "edit_file", "list_files"}

# Output schemas a manifest references that are known-unregistered, each pinned by
# its own strict-xfail test below so the gap flips loudly when the follow-up lands.
KNOWN_UNREGISTERED_OUTPUT_SCHEMAS = {"adr_draft_v1"}

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


def test_all_twelve_roles_have_a_manifest() -> None:
    assert set(_manifests()) == set(BUDGET_RULES)


def test_allowed_tools_are_registered_broker_tools_or_native_tools() -> None:
    for name, fields in _manifests().items():
        tools = fields["allowed_tools"]
        assert isinstance(tools, list) and tools, f"{name}: allowed_tools missing"
        for tool in tools:
            assert tool in TOOL_SCHEMAS or tool in NATIVE_TOOLS, f"{name}: unknown tool {tool}"


def test_every_role_grants_the_budgeted_kb_search_and_no_other_search() -> None:
    """ADR-0030: every role uses the ADR-0025 pattern — the ONE search surface is the
    registered, server-side-budgeted kb_search; any other search grant is unrestricted
    search by the back door and must fail this contract."""
    assert "kb_search" in TOOL_SCHEMAS, "kb_search must be a registered broker tool (PR-37)"
    for name, fields in _manifests().items():
        tools = fields["allowed_tools"]
        assert isinstance(tools, list)
        assert "kb_search" in tools, f"{name}: every ADR-0030 role is KB-first"
        assert [tool for tool in tools if "search" in tool] == ["kb_search"], (
            f"{name}: kb_search is the only permitted search surface"
        )


def test_no_manifest_grants_the_retired_mandatory_broker_flow() -> None:
    """ADR-0025 keeps context.* available as OPTIONAL server capabilities, but the
    twelve manifests express KB-first/file-fallback — none re-introduces the retired
    mandatory pack flow as an agent grant."""
    for name, fields in _manifests().items():
        tools = fields["allowed_tools"]
        assert isinstance(tools, list)
        assert not any(tool.startswith("context.") for tool in tools), (
            f"{name}: manifests express the ADR-0025 pattern, not the retired pack flow"
        )


def test_every_output_schema_resolves_in_the_registry() -> None:
    for name, fields in _manifests().items():
        schema = fields["output_schema"]
        if schema in KNOWN_UNREGISTERED_OUTPUT_SCHEMAS:
            continue  # pinned by its own strict-xfail test below
        assert schema in AGENT_OUTPUT_SCHEMAS, f"{name}: unknown output_schema"


@pytest.mark.xfail(
    reason="adr_draft_v1 registration is a tracked ADR-0030 follow-up, out of PR-37 scope",
    strict=True,
)
def test_adr_writer_output_schema_is_registered() -> None:
    """Strict: the moment the follow-up registers adr_draft_v1 this XPASSes and fails
    the suite, forcing removal of the marker AND of the KNOWN_UNREGISTERED entry."""
    assert "adr_draft_v1" in AGENT_OUTPUT_SCHEMAS


def test_every_manifest_requires_evidence_ids() -> None:
    for name, fields in _manifests().items():
        assert fields["requires_evidence_ids"] == "true", f"{name}: evidence IDs are mandatory"


def test_budgets_match_the_token_budget_rules() -> None:
    for name, fields in _manifests().items():
        max_calls, max_tokens = BUDGET_RULES[name]
        assert int(str(fields["max_context_calls"])) <= max_calls, f"{name}: too many calls"
        assert int(str(fields["max_context_tokens"])) <= max_tokens, f"{name}: budget too large"
